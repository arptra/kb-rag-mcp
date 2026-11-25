"""Administrator-published workspace and service connections shared by portal agents."""
# ruff: noqa: RUF001 -- Russian interface text.
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from corporate_kb.config import Settings
from corporate_kb.mcp.servers import McpServerDefinition, McpServerRegistry
from portal_agents.artifacts import validate_artifact_source
from portal_agents.tools import discover_read_tools
from portal_agents.workspace import OfflineWorkspace, _docker

_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NAME = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[\w.:-]+$")]
_IMAGE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9./:@_-]{0,254}$")


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Workspace(_Input):
    name: str = Field(max_length=100)
    image: str = Field(max_length=255)


class Server(_Input):
    name: str = Field(min_length=2, max_length=120)
    url: str = Field(max_length=2048)


class Token(_Input):
    token: str | None = Field(default=None, max_length=16384, repr=False)


class Publication(Token):
    tools: list[_NAME] = Field(max_length=500)


class Source(_Input):
    kind: Literal["git", "maven"]
    url: str = Field(max_length=2048)
    username: str = Field(default="", max_length=200)
    secret: str | None = Field(default=None, max_length=65536, repr=False)
    known_hosts: str | None = Field(default=None, max_length=32768, repr=False)


def _endpoint(url: str, schemes: set[str], *, ssh: bool = False) -> None:
    try:
        parts = urlsplit(url)
        valid = (len(url) <= 2048 and parts.scheme in schemes and parts.hostname and
                 (ssh or not parts.username) and parts.password is None and
                 not parts.query and not parts.fragment and parts.port != 0 and
                 not any(c.isspace() or ord(c) < 32 or c == "\\" for c in url))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Укажите полный адрес сервиса без пароля и параметров.")


class Connections:
    def __init__(self, settings: Settings, registry: McpServerRegistry | None) -> None:
        self.settings = settings
        self.registry = registry
        self.path = settings.agents_state_dir / "connections.sqlite3"

    def initialize(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS connections"
                       "(id TEXT PRIMARY KEY, data TEXT NOT NULL, revision TEXT NOT NULL)")

    def rows(self) -> dict[str, dict[str, Any]]:
        with closing(sqlite3.connect(self.path)) as db:
            return {key: {**json.loads(data), "revision": rev}
                    for key, data, rev in db.execute("SELECT id,data,revision FROM connections")}

    def change(self, key: str, edit: Callable[[dict[str, Any]], dict[str, Any] | None]) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM connections WHERE id=?", (key,)).fetchone()
            old = json.loads(row[0]) if row else {}
            updated = edit(old)
            if updated == old:
                return
            if updated is None:
                db.execute("DELETE FROM connections WHERE id=?", (key,))
            else:
                db.execute("INSERT OR REPLACE INTO connections VALUES(?,?,?)",
                           (key, json.dumps(updated), uuid4().hex))

    def workspace(self, rows: dict[str, Any]) -> dict[str, str]:
        config = rows.get("workspace", {"name": "", "image": self.settings.agents_sandbox_image})
        return {"name": config["name"] or config["image"] or "", "image": config["image"] or ""}

    def servers(self, rows: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for server in self.registry.list() if self.registry else []:
            entry = rows.get("mcp:" + server.id)
            if entry is None:
                tools = self.settings.agents_mcp_read_tools.get(server.id, [])
            else:
                tools = entry["tools"] if entry["url"] == server.url else []
            result.append({"id": server.id, "name": server.name, "url": server.url,
                           "tools": tools, "has_token": bool(entry and entry["url"] == server.url
                                                            and entry.get("token"))})
        return result

    def snapshot(self) -> tuple[dict[str, str], list[dict[str, Any]], dict[str, str]]:
        rows = self.rows()
        workspace = self.workspace(rows)
        servers = [s for s in self.servers(rows) if s["tools"]]
        revisions = {key: rows[key]["revision"] for s in servers
                     if (key := "mcp:" + s["id"]) in rows}
        if workspace["image"]:
            revisions.update({key: row["revision"] for key, row in rows.items()
                              if key.startswith("source:")})
        return workspace, servers, revisions

    def token(self, server_id: str, url: str) -> str:
        row = self.rows().get("mcp:" + server_id, {})
        return str(row.get("token", "")) if row.get("url") == url else ""

    def sources(self) -> dict[str, dict[str, Any]]:
        return {key.removeprefix("source:"): row for key, row in self.rows().items()
                if key.startswith("source:")}

    def save_source(self, key: str, draft: Source) -> None:
        def update(old: dict[str, Any]) -> dict[str, Any]:
            reuse = all(old.get(k) == getattr(draft, k) for k in ("kind", "url", "username"))
            previous_secret = old.get("secret", "") if reuse else ""
            secret = draft.secret if draft.secret is not None else previous_secret
            hosts = (draft.known_hosts if draft.known_hosts is not None else
                     old.get("known_hosts", "") if reuse else "")
            if draft.kind == "maven" and bool(draft.username) != bool(secret):
                raise ValueError("Для закрытого репозитория нужны логин и пароль.")
            source = {"kind": draft.kind, "url": draft.url, "username": draft.username,
                      "secret": secret or "", "known_hosts": hosts or ""}
            validate_artifact_source(source)
            return source
        self.change("source:" + key, update)


async def _images() -> list[str]:
    try:
        code, names = await _docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}",
                                    timeout=8)
    except (OSError, TimeoutError, ValueError):
        code, names = 1, ""
    if code:
        raise HTTPException(503, "Docker недоступен процессу портала. Проверьте службу и доступ.")
    return sorted({name for name in names.splitlines() if _IMAGE.fullmatch(name)})


async def _check_workspace(image: str) -> None:
    async def allowed() -> bool:
        return True

    room = OfflineWorkspace(image, allowed)
    try:
        result = await room.tool().ainvoke({"script": "printf ready"})
        if result != "exit_code=0\nready":
            raise HTTPException(422, "Контейнер не прошёл проверку. Для запуска нужны sh и sleep.")
    except (OSError, ValueError, TimeoutError):
        raise HTTPException(503, "Не удалось проверить контейнер в Docker.") from None
    finally:
        await room.close()


def connection_routes(
    store: Connections, manager: Callable[[Request], Awaitable[bool]],
    mutation: Callable[[Request], None], body: Callable[[Request, int], Awaitable[bytearray]],
) -> list[Route]:
    async def page(_: Request) -> Response:
        return FileResponse(Path(__file__).parent / "static" / "connections.html", headers={
            **_HEADERS, "Content-Security-Policy": "default-src 'self'; object-src 'none'; "
                                                   "base-uri 'none'; form-action 'self'",
            "Referrer-Policy": "same-origin",
        })

    async def check(request: Request) -> None:
        if not await manager(request):
            raise HTTPException(403, "Управление подключениями доступно администратору.")

    async def overview(_: Request) -> dict[str, Any]:
        rows = await asyncio.to_thread(store.rows)
        sources = [{"id": key.removeprefix("source:"), "kind": row["kind"], "url": row["url"],
                    "username": row["username"], "has_secret": bool(row["secret"])}
                   for key, row in rows.items() if key.startswith("source:")]
        return {"workspace": store.workspace(rows), "servers": store.servers(rows),
                "sources": sources}

    async def images(_: Request) -> dict[str, Any]:
        return {"images": await _images()}

    async def workspace(request: Request) -> dict[str, Any]:
        config = Workspace.model_validate_json(request.state.payload)
        if config.image:
            if config.image not in await _images():
                raise ValueError("Выберите загруженный образ из списка Docker на сервере.")
            await _check_workspace(config.image)
        await check(request)
        await asyncio.to_thread(store.change, "workspace", lambda _: config.model_dump())
        return {"ok": True}

    async def source(request: Request) -> dict[str, Any]:
        key = request.path_params["key"]
        if request.method == "DELETE":
            await asyncio.to_thread(store.change, "source:" + key, lambda _: None)
        else:
            config = Source.model_validate_json(request.state.payload)
            await asyncio.to_thread(store.save_source, key, config)
        return {"ok": True}

    def registry() -> McpServerRegistry:
        if store.registry is None:
            raise HTTPException(503, "Реестр MCP недоступен.")
        return store.registry

    async def add_server(request: Request) -> dict[str, Any]:
        draft = Server.model_validate_json(request.state.payload)
        _endpoint(draft.url, {"http", "https"})
        definition = McpServerDefinition(**draft.model_dump())
        added = await asyncio.to_thread(registry().add, definition)
        return {"id": added.id}

    def validate_token(url: str, token: str) -> None:
        if token:
            _endpoint(url, {"https"})
            if not token.isascii() or any(ord(ch) < 33 or ord(ch) >= 127 for ch in token):
                raise ValueError("Проверьте формат токена MCP.")

    async def discover(request: Request) -> dict[str, Any]:
        server = registry().get(request.path_params["key"])
        config = Token.model_validate_json(request.state.payload)
        token = (await asyncio.to_thread(store.token, server.id, server.url)
                 if config.token is None else config.token)
        validate_token(server.url, token)
        try:
            tools = await discover_read_tools(server.url, token)
        except Exception:
            raise HTTPException(502, "MCP не вернул список инструментов. "
                                     "Проверьте адрес и учётные данные.") from None
        return {"tools": tools}

    async def publish(request: Request) -> dict[str, Any]:
        server = registry().get(request.path_params["key"])
        config = Publication.model_validate_json(request.state.payload)

        def update(old: dict[str, Any]) -> dict[str, Any]:
            saved = old.get("token", "") if old.get("url") == server.url else ""
            token = saved if config.token is None else config.token
            validate_token(server.url, token)
            return {"tools": sorted(set(config.tools)), "url": server.url, "token": token}
        await asyncio.to_thread(store.change, "mcp:" + server.id, update)
        return {"ok": True}

    def route(path: str, handler: Callable[[Request], Awaitable[dict[str, Any]]],
              *methods: str) -> Route:
        async def guarded(request: Request) -> Response:
            await check(request)
            if request.method not in {"GET", "HEAD"}:
                mutation(request)
                request.state.payload = await body(request, 128 * 1024)
                await check(request)
            if "key" in request.path_params and not _ID.fullmatch(request.path_params["key"]):
                raise HTTPException(422, "Укажите ID из латинских букв, цифр и дефисов.")
            try:
                return JSONResponse(await handler(request), headers=_HEADERS)
            except ValidationError:
                raise HTTPException(422, "Проверьте поля подключения и размер файлов.") from None
            except KeyError:
                raise HTTPException(404, "Подключение отсутствует в реестре.") from None
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        return Route("/api/connections" + path, guarded, methods=list(methods))

    return [
        Route("/connections", page), route("", overview, "GET"), route("/images", images, "GET"),
        route("/workspace", workspace, "PUT"), route("/sources/{key}", source, "PUT", "DELETE"),
        route("/servers", add_server, "POST"), route("/servers/{key}/discover", discover, "POST"),
        route("/servers/{key}", publish, "PUT"),
    ]
