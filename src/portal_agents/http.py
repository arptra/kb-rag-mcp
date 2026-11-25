"""Authenticated chat transport for a native Deep Agents graph."""

# ruff: noqa: RUF001 -- Russian interface text.
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import aiosqlite
from filelock import FileLock
from filelock import Timeout as LockTimeout
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langsmith import tracing_context
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from corporate_kb.agents.activation import (
    GigaCodeAgentController,
    GigaCodeDecisionError,
    accepts_decisions,
)
from corporate_kb.catalog import RagCatalog
from corporate_kb.config import Settings
from corporate_kb.gigacode_runner import GigaCodeProcessCrashed, GigaCodeTimedOut
from corporate_kb.mcp.servers import McpServerRegistry
from portal_agents.artifacts import build_artifact_tools
from portal_agents.checkpoint_cleanup import CheckpointBoundary
from portal_agents.connections import Connections, connection_routes
from portal_agents.engine import run_turn
from portal_agents.experience import ExperienceStore, StrategyPreferences, profile_key
from portal_agents.model import create_model
from portal_agents.model_diagnostics import log_model_422, model_request_scope
from portal_agents.model_errors import (
    ModelGatewayError,
    check_gateway_reply,
    gateway_exception,
    model_policy_refusal,
)
from portal_agents.model_settings import ModelProfile, ModelSettingsStore, ModelSettingsUpdate
from portal_agents.research_probe import research_events
from portal_agents.tools import Authorize, build_tools, connection_inventory
from portal_agents.workspace import OfflineWorkspace

_ASSETS = Path(__file__).parent / "static"
_LOG = logging.getLogger(__name__)
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
Access = Callable[[Request], Awaitable[bool]]


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    chat: UUID
    question: str = Field(min_length=1, max_length=16000)


class _Grants(BaseModel):
    indexes: frozenset[str]
    graph: bool
    servers: list[tuple[str, str, list[str]]]
    workspace: str | None
    workspace_name: str = Field(default="", exclude=True)
    connection_revisions: dict[str, str] = Field(default_factory=dict)

    @property
    def workspace_info(self) -> tuple[str, str] | None:
        return (self.workspace_name, self.workspace) if self.workspace else None

    @property
    def mcp_read_tools(self) -> dict[str, list[str]]:
        return {server: names for server, _, names in self.servers}

    def includes(self, previous: _Grants) -> bool:
        if not previous.connection_revisions.items() <= self.connection_revisions.items():
            return False
        if not previous.indexes <= self.indexes or (previous.graph and not self.graph):
            return False
        if previous.workspace and previous.workspace != self.workspace:
            return False
        if previous.workspace_name and previous.workspace_name != self.workspace_name:
            return False
        tools = {(server, url, tool) for server, url, names in self.servers for tool in names}
        return all(
            (server, url, tool) in tools
            for server, url, names in previous.servers
            for tool in names
        )

    def permits_row(self, grants: str) -> bool:
        try:
            return self.includes(_Grants.model_validate_json(grants))
        except ValidationError:
            return False


async def _published(
    db: aiosqlite.Connection,
    owner_chat: str,
    scope: _Grants,
) -> tuple[int, str, str] | None:
    async with db.execute(
        "SELECT id,thread,checkpoint,grants FROM replies WHERE owner_chat=? ORDER BY id DESC",
        (owner_chat,),
    ) as cursor:
        async for row_id, thread, checkpoint, previous in cursor:
            if scope.permits_row(previous):
                return row_id, thread, checkpoint
    return None


def _mutation(request: Request) -> None:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "Требуется application/json.")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin)[:2] != urlsplit(str(request.url))[:2]:
        raise HTTPException(403, "Запрос с другого сайта отклонён.")


async def _body(request: Request, limit: int) -> bytearray:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise HTTPException(413, "Слишком большой запрос.")
        data.extend(chunk)
    return data


def _line(event: str, text: str, *, reference: str | None = None) -> bytes:
    payload = {"event": event, "text": text}
    if event == "answer":
        payload["html"] = _MARKDOWN.render(text)
    if reference:
        payload["reference"] = reference
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode()


def _log_model_failure(profile: ModelProfile, error: ModelGatewayError, operation: str) -> str:
    if error.status == 422:
        return log_model_422(error)
    reference = uuid4().hex[:16]
    _LOG.warning(
        "Model call failed ref=%s operation=%s provider=%s adapter=%s category=%s "
        "status=%s attempts=%d stage=%s tools=%d upstream_request_id=%s validation_field=%s",
        reference, operation, profile.provider,
        profile.adapter if profile.provider == "api" else "-", error.category,
        error.status, error.attempts, error.stage, error.tool_count, error.request_id or "-",
        error.validation_field or "-",
    )
    return reference


def _input_422(operation: str, detail: str, error: ValueError) -> HTTPException:
    ref = uuid4().hex[:16]
    fields = Turn.model_fields if operation == "chat" else ModelSettingsUpdate.model_fields
    issues = []
    if isinstance(error, ValidationError):
        for item in error.errors(include_url=False, include_context=False, include_input=False)[:8]:
            loc = item["loc"]
            field = loc[0] if loc and loc[0] in fields else "-"
            issues.append({"field": field, "type": item["type"] if item["type"] in {
                "uuid_parsing", "uuid_type", "missing", "string_too_short", "string_too_long",
                "string_type", "literal_error", "extra_forbidden", "json_invalid", "model_type",
            } else "invalid"})
    _LOG.warning("portal_422 ref=%s operation=%s provider_called=false issues=%s",
                 ref, operation, json.dumps(issues, separators=(",", ":")))
    return HTTPException(422, f"{detail} Номер ошибки {ref}.",
                         headers={"X-Portal-Diagnostic-ID": ref})


def _owner_chat(owner: str, chat: UUID) -> str:
    return hashlib.sha256(json.dumps([owner, chat.hex]).encode()).hexdigest()


def create_app(
    settings: Settings,
    catalog: RagCatalog,
    *,
    identity: Callable[[Request], Awaitable[str | None]],
    manager_authorized: Access,
    authorize_graph: Access | None = None,
    mcp_servers: McpServerRegistry | None = None,
    controller: GigaCodeAgentController | None = None,
) -> Starlette:
    locks = settings.agents_state_dir / "locks"
    profiles = ModelSettingsStore(settings)
    connections = Connections(settings, mcp_servers)
    experience = ExperienceStore(settings.agents_state_dir / "experience.sqlite3")

    @asynccontextmanager
    async def connected(
        profile: ModelProfile, *, probe: bool = False, operation: str = "chat",
    ) -> AsyncIterator[BaseChatModel]:
        with (
            model_request_scope(profile, "probe" if probe else operation),
            profile.model_kwargs() as kwargs,
        ):
            if probe:
                kwargs["max_retries"] = 0
                if profile.provider == "api":
                    kwargs["output_tokens"] = 128
            async with create_model(settings, controller=controller, **kwargs) as model:
                try:
                    yield model
                except Exception as exc:
                    failure = gateway_exception(exc)
                    if failure is not None and failure.status == 422:
                        log_model_422(failure)
                        raise failure from None
                    raise

    def acquire(owner_chat: str) -> FileLock:
        lock = FileLock(locks / owner_chat, timeout=0, thread_local=False)
        lock.acquire()
        return lock

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        settings.agents_state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        locks.mkdir(mode=0o700, exist_ok=True)
        await asyncio.to_thread(connections.initialize)
        path = settings.agents_state_dir / "checkpoints.sqlite3"
        async with (
            AsyncSqliteSaver.from_conn_string(str(path)) as saver,
            aiosqlite.connect(settings.agents_state_dir / "chats.sqlite3") as transcript,
        ):
            await saver.setup()
            await transcript.execute("BEGIN IMMEDIATE")
            await transcript.execute("""CREATE TABLE IF NOT EXISTS replies (
                id INTEGER PRIMARY KEY, owner_chat TEXT NOT NULL, thread TEXT NOT NULL,
                checkpoint TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
                grants TEXT NOT NULL, parent INTEGER)""")
            await transcript.execute(
                "CREATE INDEX IF NOT EXISTS replies_chat ON replies(owner_chat, thread)"
            )
            await transcript.execute("""CREATE TABLE IF NOT EXISTS chat_threads (
                thread TEXT PRIMARY KEY, owner_chat TEXT NOT NULL)""")
            await transcript.execute(
                "CREATE INDEX IF NOT EXISTS threads_owner_chat ON chat_threads(owner_chat)"
            )
            await transcript.commit()
            app.state.checkpoints = saver
            app.state.transcript = transcript
            yield

    async def account(request: Request) -> str:
        owner = await identity(request)
        if not owner:
            raise HTTPException(401, "Войдите в портал под учётной записью с доступом к базе.")
        return owner

    async def scope(request: Request) -> _Grants:
        graph = bool(authorize_graph and await authorize_graph(request))
        # These are local directory entries. Discovery and searches are SDK tools,
        # invoked only when the model asks. Bind private checkpoints to current grants.
        indexes = frozenset(item["id"] for item in catalog.index_directory())
        workspace, servers, revisions = await asyncio.to_thread(connections.snapshot)
        return _Grants(
            indexes=indexes, graph=graph, workspace=workspace["image"] or None,
            workspace_name=workspace["name"],
            servers=sorted((s["id"], s["url"], sorted(s["tools"])) for s in servers),
            connection_revisions=revisions,
        )

    def access_check(request: Request, owner: str, grants: _Grants) -> Authorize:
        async def authorized() -> bool:
            return await identity(request) == owner and (await scope(request)).includes(grants)
        return authorized

    async def resources(request: Request) -> Response:
        owner = await account(request)
        grants = await scope(request)
        try:
            inventory = await connection_inventory(
                authorize=access_check(request, owner, grants), workspace=grants.workspace_info,
                mcp_servers=mcp_servers, mcp_read_tools=grants.mcp_read_tools,
            )
        except PermissionError:
            raise HTTPException(403, "Доступ к ресурсам изменился.") from None
        return JSONResponse(inventory, headers=_HEADERS)

    async def index(request: Request) -> Response:
        return FileResponse(
            _ASSETS / "index.html",
            headers={
                **_HEADERS,
                "Content-Security-Policy": "default-src 'self'; object-src 'none'; base-uri 'none'",
                "Referrer-Policy": "same-origin",
            },
        )

    async def model_status(request: Request) -> Response:
        manager = await manager_authorized(request)
        if not manager:
            await account(request)
        profile = await asyncio.to_thread(profiles.load)
        if request.method == "POST":
            _mutation(request)
            if not manager:
                raise HTTPException(403, "Только администратор может включить GigaCode.")
            if profile.provider == "api" or controller is None:
                raise HTTPException(409, "Для этого подключения запуск GigaCode не требуется.")
            controller.enable()
        result: dict[str, Any] = {
            "manager": manager, "provider": profile.provider,
            "model": "GigaCode",
        }
        if profile.provider == "api":
            result["model"] = profile.model_name or (
                "API GigaChat" if profile.adapter == "gpt2giga" else "API"
            )
            result["adapter"] = profile.adapter
            result["state"] = "error" if profile.warning else "configured"
        else:
            snapshot = controller.status() if controller else {}
            result["state"] = snapshot.get("state", "disabled")
            if manager:
                result["authentication_url"] = snapshot.get("authentication_url")
        return JSONResponse(result, headers=_HEADERS)

    async def model_settings(request: Request, *, research: bool = False) -> Response:
        if not await manager_authorized(request):
            raise HTTPException(403, "Войдите в портал как администратор.")
        if request.method == "GET":
            profile = await asyncio.to_thread(profiles.load)
            public = profile.public()
            try:
                public["experience"] = await asyncio.to_thread(
                    experience.summary, profile_key(profile),
                )
            except (OSError, sqlite3.Error):
                _LOG.warning("Agent experience could not be read")
            return JSONResponse(public, headers=_HEADERS)
        _mutation(request)
        try:
            update = ModelSettingsUpdate.model_validate_json(await _body(request, 512 * 1024))
            if not await manager_authorized(request):
                raise HTTPException(403, "Права администратора изменились.")
            operation = profiles.save if request.method == "PUT" else profiles.preview
            profile = await asyncio.to_thread(operation, update)
        except ValidationError as exc:
            raise _input_422("model-settings", "Проверьте поля подключения и размер файлов.",
                             exc) from None
        except ValueError as exc:
            raise _input_422("model-settings", str(exc), exc) from None
        if request.method == "PUT":
            return JSONResponse(profile.public(), headers=_HEADERS)

        if profile.provider == "gigacode" and not (
            controller and accepts_decisions(controller.status())
        ):
            raise HTTPException(409, "Сначала включите GigaCode и завершите вход.")
        if research:
            async def stream() -> AsyncIterator[bytes]:
                check = research_events(
                    connected(profile, operation="research-probe"),
                    lambda: manager_authorized(request),
                    lambda failure: _log_model_failure(profile, failure, "research-probe"),
                )
                async with aclosing(check):
                    async for kind, text in check:
                        yield _line(kind, text)
            return StreamingResponse(
                stream(), media_type="application/x-ndjson",
                headers={**_HEADERS, "X-Accel-Buffering": "no"},
            )
        try:
            # A single completed assistant message verifies this exact draft. No chat
            # history, tools, traces or provider response bodies enter the probe.
            with tracing_context(enabled=False):
                async with connected(profile, probe=True) as model:
                    probe_model = model.with_retry(
                        retry_if_exception_type=(GigaCodeProcessCrashed, GigaCodeTimedOut,
                                                 GigaCodeDecisionError),
                        stop_after_attempt=2, wait_exponential_jitter=False,
                    ) if profile.provider == "gigacode" else model
                    reply = await probe_model.ainvoke("Reply with OK.")
                    check_gateway_reply(reply.text)
        except Exception as exc:
            failure = gateway_exception(exc)
            if failure is not None:
                reference = _log_model_failure(profile, failure, "probe")
                detail = f"{failure} Номер ошибки {reference}."
            else:
                detail = "Нет ответа от модели. Проверьте доступность адреса и сертификаты TLS."
            raise HTTPException(502, detail) from None
        if (not isinstance(reply, AIMessage) or not reply.text.strip() or reply.tool_calls
                or reply.additional_kwargs.get("refusal")
                or reply.response_metadata.get("finish_reason") in {"content_filter", "length"}):
            raise HTTPException(502, "Модель не вернула завершённый ответ на проверочный запрос.")
        return JSONResponse({"ok": True}, headers=_HEADERS)

    async def test_research(request: Request) -> Response:
        return await model_settings(request, research=True)

    async def history(request: Request) -> Response:
        owner = await account(request)
        chat_id = request.path_params["chat"]
        owner_chat = _owner_chat(owner, chat_id)
        saver: AsyncSqliteSaver = request.app.state.checkpoints
        db: aiosqlite.Connection = request.app.state.transcript
        if request.method == "DELETE":
            _mutation(request)
            try:
                lock = acquire(owner_chat)
            except LockTimeout:
                raise HTTPException(409, "Сначала остановите ответ.") from None
            try:
                rows = await db.execute_fetchall(
                    "SELECT thread FROM chat_threads WHERE owner_chat=?", (owner_chat,)
                )
                for (thread,) in rows:
                    await saver.adelete_thread(thread)
                await db.execute("DELETE FROM replies WHERE owner_chat=?", (owner_chat,))
                await db.execute("DELETE FROM chat_threads WHERE owner_chat=?", (owner_chat,))
                await db.commit()
                return Response(status_code=204, headers=_HEADERS)
            finally:
                lock.release()
        grants = await scope(request)
        last = await _published(db, owner_chat, grants)
        messages = []
        if last is not None:
            rows = await db.execute_fetchall(
                """WITH RECURSIVE lineage(id, parent) AS (
                SELECT id,parent FROM replies WHERE id=? AND owner_chat=?
                UNION ALL SELECT r.id,r.parent FROM replies r JOIN lineage l ON r.id=l.parent
                WHERE r.owner_chat=? AND r.id<l.id)
                SELECT question,answer,grants FROM replies JOIN lineage USING(id)
                ORDER BY id""",
                (last[0], owner_chat, owner_chat),
            )
            latest = await scope(request)
            if (
                await identity(request) != owner
                or not latest.includes(grants)
                or not all(latest.permits_row(r[2]) for r in rows)
            ):
                raise HTTPException(403, "Доступ к истории изменился.")
            for question, answer, _ in rows:
                messages.append({"role": "user", "text": question})
                messages.append(
                    {"role": "assistant", "text": answer, "html": _MARKDOWN.render(answer)}
                )
        return JSONResponse({"messages": messages}, headers=_HEADERS)

    async def chat(request: Request) -> Response:
        _mutation(request)
        owner = await account(request)
        try:
            turn = Turn.model_validate_json(await _body(request, 100000))
        except ValidationError as exc:
            raise _input_422("chat", "Укажите чат и вопрос длиной от 1 до 16 000 знаков.",
                             exc) from None
        profile = await asyncio.to_thread(profiles.load)
        if profile.provider == "api" and profile.warning:
            raise HTTPException(503, "Администратору нужно проверить настройки корпоративного API.")
        runtime = controller.status() if profile.provider == "gigacode" and controller else {}
        if profile.provider == "gigacode" and not accepts_decisions(runtime):
            raise HTTPException(503, "GigaCode ожидает включения или входа администратора.")
        grants = await scope(request)
        owner_chat = _owner_chat(owner, turn.chat)

        authorized = access_check(request, owner, grants)

        async def graph_access() -> bool:
            return bool(grants.graph and authorize_graph and await authorize_graph(request))

        async def stream() -> AsyncIterator[bytes]:
            events: asyncio.Queue[tuple[str, str, str | None]] = asyncio.Queue(maxsize=64)
            model_key = profile_key(profile)
            outcomes: set[str] = set()

            async def report(kind: str, detail: str, *, reference: str | None = None) -> None:
                if kind in {"answer", "error", "restricted"}:
                    if kind == "restricted":
                        outcomes.add("policy")
                    observed = outcomes if kind == "answer" else {
                        item for item in outcomes if not item.endswith("_success")
                    }
                    if observed:
                        try:
                            await asyncio.to_thread(experience.record_turn, model_key, observed)
                        except (OSError, sqlite3.Error):
                            _LOG.warning("Agent experience could not be saved")
                await events.put((kind, detail, reference))

            async def execute() -> None:
                workspace = (
                    OfflineWorkspace(grants.workspace, authorized) if grants.workspace else None
                )
                try:
                    db: aiosqlite.Connection = request.app.state.transcript
                    saver: AsyncSqliteSaver = request.app.state.checkpoints
                    # Follow only successful SDK checkpoints. A cancelled tool batch
                    # must not be replayed as the next question in the conversation.
                    last = await _published(db, owner_chat, grants)
                    key = last[1] if last else uuid4().hex
                    boundary = await CheckpointBoundary.capture(
                        saver, key, last[2] if last else None,
                    )
                    await db.execute(
                        "INSERT OR IGNORE INTO chat_threads(thread,owner_chat) VALUES(?,?)",
                        (key, owner_chat),
                    )
                    await db.commit()
                    tools = build_tools(
                        catalog,
                        authorize=authorized,
                        authorize_graph=graph_access if grants.graph else None,
                        index_ids=grants.indexes,
                        mcp_servers=mcp_servers,
                        mcp_read_tools=grants.mcp_read_tools,
                        mcp_credential=connections.token,
                        workspace=grants.workspace_info,
                    )
                    if workspace:
                        tools.extend((workspace.tool(), workspace.gradle_tool()))
                        sources = await asyncio.to_thread(connections.sources)
                        tools.extend(build_artifact_tools(
                            {key: source for key, source in sources.items()
                             if grants.connection_revisions.get("source:" + key)
                             == source["revision"]},
                            workspace, authorized,
                        ))
                    try:
                        preferences = await asyncio.to_thread(experience.preferences, model_key)
                    except (OSError, sqlite3.Error):
                        _LOG.warning("Agent experience could not be read")
                        preferences = StrategyPreferences()
                    async with connected(profile) as model:
                        answer = await run_turn(
                            model,
                            turn.question,
                            tools,
                            on_event=report,
                            authorize=authorized,
                            thread_id=key,
                            checkpointer=saver,
                            checkpoint_id=last[2] if last else None,
                            prefer_chat_reads=preferences.prefer_chat_reads,
                            larger_read_answer=preferences.larger_read_answer,
                            experience=outcomes,
                        )
                    saved = await saver.aget_tuple({"configurable": {"thread_id": key}})
                    if saved is None or not await authorized():
                        raise PermissionError("Доступ к результату изменился.")
                    await db.execute(
                        """INSERT INTO replies(owner_chat,thread,checkpoint,question,answer,
                           grants,parent) VALUES(?,?,?,?,?,?,?)""",
                        (
                            owner_chat,
                            key,
                            saved.checkpoint["id"],
                            turn.question,
                            answer,
                            grants.model_dump_json(),
                            last[0] if last else None,
                        ),
                    )
                    await db.commit()
                    await report("answer", answer)
                except Exception as exc:
                    if model_policy_refusal(exc):
                        cleanup = asyncio.create_task(boundary.rollback(saver, key))
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            await cleanup
                            raise
                        reference = (exc.diagnostic_reference if isinstance(exc, ModelGatewayError)
                                     else None)
                        await report(
                            "restricted", "Корпоративная модель ограничила ответ. "
                            "Уточните задачу или задайте другой вопрос.", reference=reference,
                        )
                    elif isinstance(exc, PermissionError):
                        await report("error", "Доступ к запросу ограничен.")
                    elif isinstance(exc, ModelGatewayError):
                        reference = _log_model_failure(profile, exc, "chat")
                        if exc.status == 429 or exc.category == "rate-limit":
                            detail = "Достигнут лимит запросов к модели. Попробуйте позже."
                        elif exc.category == "process-crash":
                            detail = ("GigaCode повторно завершился во время ответа. "
                                      "Портал автоматически проверяет его готовность.")
                        elif exc.retryable or (exc.status is not None and exc.status >= 500):
                            detail = "Не удалось получить ответ от модели. Попробуйте позже."
                        else:
                            detail = str(exc)
                        await report("error", detail, reference=reference)
                    else:
                        # Provider errors may contain prompts, credentials or private addresses.
                        _LOG.warning("Agent turn stopped (%s)", type(exc).__name__)
                        await report("error", "Запрос прерван. Проверьте подключение модели "
                                     "и выбранной среды.")
                finally:
                    if workspace:
                        await workspace.close()

            try:
                lock = acquire(owner_chat)
            except LockTimeout:
                yield _line("error", "В этом чате уже выполняется запрос.")
                return
            task = asyncio.create_task(execute())
            try:
                yield _line("start", "")
                if runtime.get("state") == "recovering":
                    yield _line("model_recovering", "Ожидаем восстановления GigaCode.")
                while True:
                    try:
                        kind, detail, reference = await asyncio.wait_for(events.get(), timeout=10)
                    except TimeoutError:
                        if task.done():
                            await task
                            break
                        yield _line("waiting", "")
                        continue
                    yield _line(kind, detail, reference=reference)
                    if kind in {"answer", "error", "restricted"}:
                        await task
                        break
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                lock.release()

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={**_HEADERS, "X-Accel-Buffering": "no"},
        )

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/", index),
            *connection_routes(connections, manager_authorized, _mutation, _body),
            Route("/api/resources", resources, methods=["GET"]),
            Route("/api/model", model_status, methods=["GET", "POST"]),
            Route("/api/model/settings", model_settings, methods=["GET", "PUT"]),
            Route("/api/model/test", model_settings, methods=["POST"]),
            Route("/api/model/test-research", test_research, methods=["POST"]),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/api/chats/{chat:uuid}", history, methods=["GET", "DELETE"]),
            Mount("/assets", app=StaticFiles(directory=_ASSETS)),
        ],
    )
