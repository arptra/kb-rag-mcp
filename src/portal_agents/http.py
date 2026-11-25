"""Authenticated chat transport for a native Deep Agents graph."""

# ruff: noqa: RUF001 -- Russian interface text.
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
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

from corporate_kb.agents.activation import GigaCodeAgentController
from corporate_kb.catalog import RagCatalog
from corporate_kb.config import Settings
from corporate_kb.mcp.servers import McpServerRegistry
from portal_agents.artifacts import build_artifact_tools
from portal_agents.connections import Connections, connection_routes
from portal_agents.engine import run_turn
from portal_agents.model import create_model
from portal_agents.model_errors import ModelGatewayError, check_gateway_reply, gateway_exception
from portal_agents.model_settings import ModelProfile, ModelSettingsStore, ModelSettingsUpdate
from portal_agents.tools import build_tools
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
    connection_revisions: dict[str, str] = Field(default_factory=dict)

    def includes(self, previous: _Grants) -> bool:
        if not previous.connection_revisions.items() <= self.connection_revisions.items():
            return False
        if not previous.indexes <= self.indexes or (previous.graph and not self.graph):
            return False
        if previous.workspace and previous.workspace != self.workspace:
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
    reference = uuid4().hex[:16]
    _LOG.warning(
        "Model call failed ref=%s operation=%s provider=%s adapter=%s category=%s "
        "status=%s attempts=%d upstream_request_id=%s",
        reference, operation, profile.provider,
        profile.adapter if profile.provider == "api" else "-", error.category,
        error.status, error.attempts, error.request_id or "-",
    )
    return reference


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

    @asynccontextmanager
    async def connected(
        profile: ModelProfile, *, probe: bool = False,
    ) -> AsyncIterator[BaseChatModel]:
        with profile.model_kwargs() as kwargs:
            if probe:
                kwargs["max_retries"] = 0
                if profile.provider == "api":
                    kwargs["output_tokens"] = 128
            async with create_model(settings, controller=controller, **kwargs) as model:
                yield model

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
            servers=sorted((s["id"], s["url"], sorted(s["tools"])) for s in servers),
            connection_revisions=revisions,
        )

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

    async def model_settings(request: Request) -> Response:
        if not await manager_authorized(request):
            raise HTTPException(403, "Войдите в портал как администратор.")
        if request.method == "GET":
            profile = await asyncio.to_thread(profiles.load)
            return JSONResponse(profile.public(), headers=_HEADERS)
        _mutation(request)
        try:
            update = ModelSettingsUpdate.model_validate_json(await _body(request, 512 * 1024))
            if not await manager_authorized(request):
                raise HTTPException(403, "Права администратора изменились.")
            operation = profiles.save if request.method == "PUT" else profiles.preview
            profile = await asyncio.to_thread(operation, update)
        except ValidationError:
            raise HTTPException(422, "Проверьте поля подключения и размер файлов.") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if request.method == "PUT":
            return JSONResponse(profile.public(), headers=_HEADERS)

        if profile.provider == "gigacode" and not (controller and controller.status()["available"]):
            raise HTTPException(409, "Сначала включите GigaCode и завершите вход.")
        try:
            # A single completed assistant message verifies this exact draft. No chat
            # history, tools, traces or provider response bodies enter the probe.
            with tracing_context(enabled=False):
                async with connected(profile, probe=True) as model:
                    reply = await model.ainvoke("Reply with OK.")
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
        except ValidationError:
            raise HTTPException(422, "Укажите чат и вопрос длиной от 1 до 16 000 знаков.") from None
        profile = await asyncio.to_thread(profiles.load)
        if profile.provider == "api" and profile.warning:
            raise HTTPException(503, "Администратору нужно проверить настройки корпоративного API.")
        if profile.provider == "gigacode" and (
            controller is None or not controller.status()["available"]
        ):
            raise HTTPException(503, "GigaCode ожидает включения или входа администратора.")
        grants = await scope(request)
        owner_chat = _owner_chat(owner, turn.chat)

        async def authorized() -> bool:
            return await identity(request) == owner and (await scope(request)).includes(grants)

        async def graph_access() -> bool:
            return bool(grants.graph and authorize_graph and await authorize_graph(request))

        async def stream() -> AsyncIterator[bytes]:
            events: asyncio.Queue[tuple[str, str, str | None]] = asyncio.Queue(maxsize=64)

            async def report(kind: str, detail: str, *, reference: str | None = None) -> None:
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
                        mcp_read_tools={server: names for server, _, names in grants.servers},
                        mcp_credential=connections.token,
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
                except PermissionError:
                    await report("error", "Доступ к запросу ограничен.")
                except ModelGatewayError as exc:
                    reference = _log_model_failure(profile, exc, "chat")
                    if exc.status == 429:
                        detail = "Достигнут лимит запросов к модели. Попробуйте позже."
                    elif exc.retryable or (exc.status is not None and exc.status >= 500):
                        detail = "Не удалось получить ответ от модели. Попробуйте позже."
                    else:
                        detail = str(exc)
                    await report("error", detail, reference=reference)
                except Exception as exc:
                    # Provider errors may contain prompts, API credentials or private addresses.
                    _LOG.warning("Agent turn stopped (%s)", type(exc).__name__)
                    await report(
                        "error",
                        "Запрос прерван. Проверьте подключение модели и выбранной среды.",
                    )
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
                    if kind in {"answer", "error"}:
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
            Route("/api/model", model_status, methods=["GET", "POST"]),
            Route("/api/model/settings", model_settings, methods=["GET", "PUT"]),
            Route("/api/model/test", model_settings, methods=["POST"]),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/api/chats/{chat:uuid}", history, methods=["GET", "DELETE"]),
            Mount("/assets", app=StaticFiles(directory=_ASSETS)),
        ],
    )
