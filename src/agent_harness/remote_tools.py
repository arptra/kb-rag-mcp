"""Bounded, explicitly authorized outgoing MCP and A2A calls.

Connection addresses and credential names come from administrator configuration;
model arguments cannot change the destination or expand the MCP tool allowlist.
MCP cancellation closes the local session; this SDK does not cancel remote work.
A2A cancellation additionally requests tasks/cancel when a task ID is known.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import suppress
from datetime import timedelta
from threading import Event
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, METHOD_NOT_FOUND
from referencing import Registry

from .contracts import Evidence, Principal, ToolExecutionFailure, tool_failure_reason
from .integrations_config import ConnectionConfig

_MAX_CHARS = 12000
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_ARGUMENT_BYTES = 32 * 1024
_MAX_TOOLS = 128
_T = TypeVar("_T")


class RemoteToolError(ToolExecutionFailure):
    """A deliberately sanitized error safe for a model or a browser to display."""


class _PeerFailure(ValueError):
    """The remote invocation explicitly reported a failed result."""


def _revision(definition: dict[str, Any], secret: str) -> str:
    payload = json.dumps(_redact_tree(definition, secret), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class _LimitedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        received = 0
        async for chunk in self.stream:
            received += len(chunk)
            if received > _MAX_RESPONSE_BYTES:
                raise ValueError("Remote response exceeds the byte limit")
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


async def _guard_response(response: httpx.Response) -> None:
    # Never send credentials to a redirect target, including another path/host.
    if response.is_redirect:
        raise ValueError("Remote redirects are disabled")
    if int(response.headers.get("content-length", "0")) > _MAX_RESPONSE_BYTES:
        raise ValueError("Remote response exceeds the byte limit")
    response.stream = _LimitedStream(response.stream)  # type: ignore[arg-type]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _redact(value: str, secret: str) -> str:
    return value.replace(secret, "[redacted]") if secret else value


def _redact_tree(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return _redact(value, secret)
    if isinstance(value, dict):
        return {
            _redact(str(key), secret): _redact_tree(item, secret) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_tree(item, secret) for item in value]
    return value


def _error_cause(error: BaseException) -> BaseException:
    # The MCP SDK wraps even local validation errors in AnyIO task groups.
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


def _error_kind(error: BaseException) -> str:
    cause = _error_cause(error)
    if isinstance(cause, ToolExecutionFailure) and cause.reason == "mcp_input_mismatch":
        return "ValidationError"
    return "ValueError" if isinstance(cause, _PeerFailure) else type(cause).__name__


def _text_parts(parts: Any) -> list[str]:
    if not isinstance(parts, list):
        return []
    result = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            result.append(part["text"])
        elif isinstance(part.get("data"), (dict, list)):
            result.append(_json(part["data"]))
    return result


def _validate_schema(schema: Any, arguments: dict[str, Any]) -> None:
    if not isinstance(schema, dict):
        raise ValueError("Invalid tool schema")

    def local_references(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(item, str) or not item.startswith("#")
                ):
                    raise ValueError("External schema references are disabled")
                local_references(item)
        elif isinstance(value, list):
            for item in value:
                local_references(item)

    local_references(schema)
    validator = validator_for(schema)
    validator.check_schema(schema)

    # An explicit empty registry disables jsonschema's compatibility fallback
    # that can retrieve remote references (including those rebased by $id).
    validator(schema, registry=Registry()).validate(arguments)


class RemoteTools:
    def __init__(self, *, timeout_seconds: float = 60, poll_interval: float = 0.25) -> None:
        if not 0 < timeout_seconds <= 60 or not 0 < poll_interval <= 5:
            raise ValueError("Invalid remote tool timeout")
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval
        self._cleanup_seconds = min(2.0, timeout_seconds / 4)

    @staticmethod
    def _credential(connection: ConnectionConfig) -> str:
        if not connection.credential_env:
            return ""
        secret = os.environ.get(connection.credential_env, "")
        if not secret or any(ord(char) < 32 or ord(char) == 127 for char in secret):
            raise ValueError("Connection credential is unavailable")
        return secret

    def _client(self, secret: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"Authorization": f"Bearer {secret}"} if secret else {},
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            event_hooks={"response": [_guard_response]},
        )

    async def _bounded(
        self,
        work: Callable[[], Coroutine[Any, Any, _T]],
        cancel: Event,
    ) -> _T:
        if cancel.is_set():
            raise asyncio.CancelledError
        task = asyncio.create_task(work())
        deadline = asyncio.get_running_loop().time() + (
            self.timeout_seconds - self._cleanup_seconds
        )
        try:
            while not task.done():
                if cancel.is_set():
                    raise asyncio.CancelledError
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                await asyncio.wait({task}, timeout=min(0.025, remaining))
            return await task
        finally:
            if not task.done():
                task.cancel()
                # Keep the MCP task-group's enter/exit in its own task. A2A also
                # uses this grace period to cancel a known remote task.
                with suppress(BaseException):
                    await asyncio.wait_for(task, timeout=self._cleanup_seconds)

    async def probe(self, connection: ConnectionConfig) -> dict[str, Any]:
        """Administrator-only discovery; callers decide who may inspect it."""
        try:
            if not connection.enabled:
                return {
                    "available": False,
                    "status": "disabled",
                    "message": "Подключение выключено.",
                }
            secret = self._credential(connection)
            operation = self._probe_mcp if connection.kind == "mcp" else self._probe_a2a
            result: dict[str, Any] = await self._bounded(
                lambda: operation(connection, secret),
                Event(),
            )
            # Even a malicious peer echoing its Authorization header must not
            # expose the server-side credential in the settings UI.
            result = _redact_tree(result, secret)
            if connection.kind == "mcp":
                items = result["tools"]
            else:
                card = result["agent_card"]
                card["name"] = card["name"][:300]
                card["description"] = card["description"][:2000]
                items = card["skills"]
            while len(_json(result)) > _MAX_CHARS and items:
                items.pop()
                result["truncated"] = True
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {
                "available": False,
                "status": "offline",
                "error": _error_kind(exc),
                "message": f"Подключиться не удалось ({_error_kind(exc)}).",
            }

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        *,
        connection: ConnectionConfig,
        principal: Principal,
        run_id: str,
        cancel: Event,
        require_read_only: bool = False,
    ) -> list[Evidence]:
        observed_catalog: list[str] = []
        try:
            if not connection.allows(principal.owner_id):
                raise PermissionError("Connection is unavailable to this user")
            if not isinstance(args, dict) or args.get("connection_id") != connection.id:
                raise ValueError("Invalid connection arguments")
            if name in {"mcp_list_tools", "mcp_tool_schema"} and "refresh" in args:
                if type(args["refresh"]) is not bool:
                    raise ValueError("Invalid MCP refresh flag")
                args = {key: value for key, value in args.items() if key != "refresh"}
            if len(_json(args).encode()) > _MAX_ARGUMENT_BYTES:
                raise ValueError("Arguments exceed the byte limit")
            if name == "mcp_list_tools" and connection.kind == "mcp":
                query = args.get("query", "")
                if (set(args) - {"connection_id", "query"}
                        or not isinstance(query, str) or len(query) > 200):
                    raise ValueError("Invalid MCP discovery arguments")
                if not connection.has_tools:
                    raise PermissionError("No tools are allowed on this connection")
                secret = self._credential(connection)
                text = await self._bounded(
                    lambda: self._call_mcp(connection, None, None, secret, query=query,
                                           require_read_only=require_read_only,
                                           observed_catalog=observed_catalog), cancel,
                )
                title = f"{connection.name}: доступные инструменты"
            elif name in {"mcp_call", "mcp_tool_schema"} and connection.kind == "mcp":
                tool = args.get("tool")
                arguments = args.get("arguments", {}) if name == "mcp_call" else None
                fields = {"connection_id", "tool"}
                if name == "mcp_call":
                    fields.update({"arguments", "schema_revision"})
                if (
                    set(args) - fields
                    or not isinstance(tool, str)
                    or (name == "mcp_call" and not isinstance(arguments, dict))
                ):
                    raise ValueError("Invalid MCP arguments")
                if tool not in connection.allowed_tools and not connection.include_read_only_tools:
                    raise PermissionError("Tool is not in the explicit allowlist")
                secret = self._credential(connection)
                text = await self._bounded(
                    lambda: self._call_mcp(connection, tool, arguments, secret,
                                           require_read_only=require_read_only,
                                           expected_revision=args.get("schema_revision"),
                                           observed_catalog=observed_catalog),
                    cancel,
                )
                title = f"{connection.name}: {tool}"
            elif name == "a2a_ask" and connection.kind == "a2a":
                question = args.get("question")
                if (
                    set(args) != {"connection_id", "question"}
                    or not isinstance(question, str)
                    or not question.strip()
                    or len(question) > 8000
                ):
                    raise ValueError("Invalid A2A question")
                secret = self._credential(connection)
                text = await self._bounded(
                    lambda: self._call_a2a(connection, question, secret),
                    cancel,
                )
                title = f"{connection.name}: ответ удалённого агента"
            else:
                raise ValueError("Tool does not match the connection")
            digest = hashlib.sha256(_json([run_id, connection.id, name, args]).encode()).hexdigest()
            return [
                Evidence(
                    id=f"connection-{digest[:32]}",
                    index_id=f"connection:{connection.id}",
                    title=_redact(title, secret)[:300],
                    text=_redact(text, secret)[:_MAX_CHARS],
                    url=connection.url,
                    mcp_catalog_revision=observed_catalog[0] if observed_catalog else None,
                    tool_outcome="empty" if name == "mcp_call" and not text.strip() else None,
                    kind="tool_metadata"
                    if name in {"mcp_list_tools", "mcp_tool_schema"} else "source",
                )
            ]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            cause = _error_cause(exc)
            reason = tool_failure_reason(cause)
            if isinstance(cause, _PeerFailure):
                reason = "remote_error"
            elif isinstance(cause, httpx.TimeoutException):
                reason = "timeout"
            elif (isinstance(cause, httpx.HTTPStatusError)
                  and cause.response.status_code in {401, 403}):
                reason = "permission"
            raise RemoteToolError(
                f"Remote operation failed ({_error_kind(exc)})", reason=reason,
            ) from None

    async def _tools(
        self, session: ClientSession, allowed_names: set[str] | None = None,
        *, include_read_only: bool = False, require_read_only: bool = False,
        name: str | None = None, query: str = "",
    ) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        seen = set()
        terms = query.casefold().split()
        for _ in range(8):
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                if name is not None and tool.name != name:
                    continue
                annotations = tool.annotations
                read_only = (annotations is not None and annotations.readOnlyHint is True
                             and annotations.destructiveHint is not True)
                if require_read_only and not read_only:
                    continue
                if (allowed_names is not None and tool.name not in allowed_names
                        and not (include_read_only and read_only)):
                    continue
                if terms and not all(term in (tool.name + " " + (tool.description or "")).casefold()
                                     for term in terms):
                    continue
                tools.append(
                    {
                        "name": tool.name,
                        "description": (tool.description or "")[:1000],
                        "inputSchema": tool.inputSchema,
                        **({"read_only": True} if read_only else {}),
                    }
                )
                if len(tools) >= _MAX_TOOLS:
                    return tools
            cursor = page.nextCursor
            if not cursor:
                return tools
            if cursor in seen:
                raise ValueError("Repeated MCP pagination cursor")
            seen.add(cursor)
        raise ValueError("MCP discovery exceeds the page limit")

    async def _probe_mcp(self, connection: ConnectionConfig, secret: str) -> dict[str, Any]:
        async with (
            self._client(secret) as client,
            streamable_http_client(connection.url, http_client=client) as streams,
            ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
            ) as session,
        ):
            await session.initialize()
            tools = await self._tools(session)
        result: dict[str, Any] = {
            "available": True,
            "status": "online",
            "tools": [],
            "truncated": len(tools) >= _MAX_TOOLS,
            "message": "Подключение доступно; список инструментов получен.",
        }
        for tool in tools:
            result["tools"].append(tool)
            if len(_json(result)) > _MAX_CHARS:
                result["tools"].pop()
                result["truncated"] = True
                break
        return result

    async def _call_mcp(
        self,
        connection: ConnectionConfig,
        tool: str | None,
        arguments: dict[str, Any] | None,
        secret: str,
        *,
        query: str = "",
        expected_revision: str | None = None,
        require_read_only: bool = False,
        observed_catalog: list[str] | None = None,
    ) -> str:
        async with (
            self._client(secret) as client,
            streamable_http_client(connection.url, http_client=client) as streams,
            ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
            ) as session,
        ):
            await session.initialize()
            definitions = await self._tools(
                session, set(connection.allowed_tools),
                include_read_only=connection.include_read_only_tools,
                require_read_only=require_read_only,
            )
            for item in definitions:
                item["revision"] = _revision(item, secret)
            if observed_catalog is not None:
                revisions = sorted(item["revision"] for item in definitions)
                observed_catalog.append(hashlib.sha256(_json(revisions).encode()).hexdigest())
            if len(definitions) >= _MAX_TOOLS and (tool or query):
                # A capped manifest cannot establish that a particular authorized
                # tool disappeared. Filter on the paginated SDK results before
                # applying the cap to this specific lookup.
                definitions = await self._tools(
                    session, set(connection.allowed_tools), name=tool, query=query,
                    include_read_only=connection.include_read_only_tools,
                    require_read_only=require_read_only,
                )
                for item in definitions:
                    item["revision"] = _revision(item, secret)
            if tool is None:
                terms = query.casefold().split()
                matches = [item for item in definitions if all(
                    term in (item["name"] + " " + item["description"]).casefold()
                    for term in terms
                )]
                catalog: dict[str, Any] = {
                    "kind": "tool_catalog_not_system_evidence", "tools": [],
                    "matched_count": len(matches), "truncated": len(definitions) >= _MAX_TOOLS,
                    "next_action": "Read mcp_tool_schema for a selected tool before calling it. "
                    "If truncated, repeat mcp_list_tools with a more specific query.",
                }
                for item in matches:
                    catalog["tools"].append(_redact_tree({
                        "name": item["name"], "description": item["description"][:500],
                        "revision": item["revision"],
                        **({"read_only": True} if item.get("read_only") else {}),
                    }, secret))
                    if len(_json(catalog)) > _MAX_CHARS:
                        catalog["tools"].pop()
                        catalog["truncated"] = True
                        break
                return _json(catalog)
            matches = [item for item in definitions if item["name"] == tool]
            if not matches:
                raise ToolExecutionFailure("Tool not listed", reason="mcp_tool_missing")
            if len(matches) != 1:
                raise ToolExecutionFailure("Duplicate tool name", reason="mcp_tool_ambiguous")
            revision = matches[0]["revision"]
            if arguments is None:
                schema_result = _redact_tree(
                    {
                        "kind": "tool_schema_not_system_evidence",
                        **matches[0],
                        "truncated": False,
                        "revision": revision,
                    },
                    secret,
                )
                if len(_json(schema_result)) > _MAX_CHARS:
                    schema_result.update(
                        {
                            "inputSchema": None,
                            "truncated": True,
                            "name": schema_result["name"][:200],
                            "description": schema_result["description"][:1000],
                            "message": "Схема превышает лимит контекста и не передана целиком.",
                        }
                    )
                return _json(schema_result)
            if expected_revision and expected_revision != revision:
                raise ToolExecutionFailure("Contract changed", reason="mcp_schema_changed")
            try:
                _validate_schema(matches[0]["inputSchema"], arguments)
            except ValidationError:
                raise ToolExecutionFailure(
                    "Invalid arguments", reason="mcp_input_mismatch",
                ) from None
            try:
                result = await session.call_tool(tool, arguments)
            except McpError as exc:
                conflict = {INVALID_PARAMS: "mcp_dispatch_invalid_args",
                            METHOD_NOT_FOUND: "mcp_dispatch_unknown_tool"}.get(exc.error.code)
                if conflict is None:
                    raise
                # This response arrived after dispatch; even a schema-looking
                # JSON-RPC error is not proof that the action had no effects.
                raise ToolExecutionFailure("Contract conflict after dispatch",
                                           reason=conflict) from None
            if result.isError:
                raise _PeerFailure("Remote tool reported a failure")
            parts = [part.model_dump(mode="json") for part in result.content]
            texts = _text_parts(parts)
            if result.structuredContent is not None:
                texts.append(_json(result.structuredContent))
            if not texts and parts:
                raise ValueError("Remote tool returned no text or structured content")
            nonempty = (text for text in texts if text.strip() not in {"", "[]", "{}", "null"})
            return "\n\n".join(nonempty)

    @staticmethod
    async def _response_json(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError("Remote response exceeds the byte limit")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("Invalid JSON response")
        return result

    async def _probe_a2a(self, connection: ConnectionConfig, secret: str) -> dict[str, Any]:
        parsed = urlsplit(connection.url)
        card_url = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                "/.well-known/agent-card.json",
                "",
                "",
            )
        )
        async with (
            self._client(secret) as client,
            client.stream("GET", card_url, headers={"A2A-Version": "0.3"}) as response,
        ):
            card = await self._response_json(response)
        # Only display descriptive metadata. The card's endpoints, extensions,
        # auth schemes and embedded URLs never alter the configured destination.
        skills = []
        for skill in card.get("skills", [])[:64]:
            if isinstance(skill, dict):
                skills.append(
                    {key: str(skill.get(key, ""))[:1000] for key in ("id", "name", "description")}
                )
        result: dict[str, Any] = {
            "available": True,
            "status": "online",
            "message": "Агент доступен; описание получено.",
            "agent_card": {
                "name": str(card.get("name", ""))[:300],
                "description": str(card.get("description", ""))[:2000],
                "protocol_version": str(card.get("protocolVersion", ""))[:30],
                "skills": skills,
            },
            "truncated": len(card.get("skills", [])) > len(skills),
        }
        while len(_json(result)) > _MAX_CHARS and skills:
            skills.pop()
            result["truncated"] = True
        return result

    async def _rpc(
        self,
        client: httpx.AsyncClient,
        url: str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = uuid4().hex
        async with client.stream(
            "POST",
            url,
            headers={"A2A-Version": "0.3", "Accept": "application/json"},
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        ) as response:
            payload = await self._response_json(response)
        if (
            payload.get("jsonrpc") != "2.0"
            or payload.get("id") != request_id
            or "error" in payload
            or not isinstance(payload.get("result"), dict)
        ):
            raise ValueError("Invalid A2A response")
        return payload["result"]  # type: ignore[no-any-return]

    async def _call_a2a(self, connection: ConnectionConfig, question: str, secret: str) -> str:
        task_id: str | None = None
        async with self._client(secret) as client:
            try:
                result = await self._rpc(
                    client,
                    connection.url,
                    "message/send",
                    {
                        "message": {
                            "kind": "message",
                            "role": "user",
                            "messageId": uuid4().hex,
                            "parts": [{"kind": "text", "text": question}],
                        },
                        "configuration": {
                            "blocking": False,
                            "historyLength": 0,
                            "acceptedOutputModes": ["text/plain"],
                        },
                    },
                )
                while result.get("kind") != "message":
                    if not isinstance(result.get("id"), str) or not result["id"]:
                        raise ValueError("A2A task identifier is missing")
                    if task_id is not None and result["id"] != task_id:
                        raise ValueError("A2A task identifier changed")
                    task_id = result["id"]
                    state = result.get("status", {}).get("state")
                    if state == "completed":
                        break
                    if state not in {"submitted", "working"}:
                        raise ValueError("A2A task did not complete")
                    await asyncio.sleep(self.poll_interval)
                    result = await self._rpc(
                        client,
                        connection.url,
                        "tasks/get",
                        {
                            "id": task_id,
                            "historyLength": 0,
                        },
                    )
                texts = _text_parts(result.get("parts"))
                for artifact in result.get("artifacts", []):
                    if isinstance(artifact, dict):
                        texts.extend(_text_parts(artifact.get("parts")))
                status_message = result.get("status", {}).get("message", {})
                if isinstance(status_message, dict):
                    texts.extend(_text_parts(status_message.get("parts")))
                if not texts:
                    raise ValueError("A2A returned no text or structured content")
                return "\n\n".join(texts)
            except asyncio.CancelledError:
                if task_id is not None:
                    with suppress(Exception):
                        await asyncio.wait_for(
                            self._rpc(
                                client,
                                connection.url,
                                "tasks/cancel",
                                {"id": task_id},
                            ),
                            timeout=self._cleanup_seconds / 2,
                        )
                raise
