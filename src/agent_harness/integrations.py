"""Settings-backed, permission-checked bridge to remote tools and isolated repositories."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from threading import Event
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_harness.contracts import Evidence, Principal, ToolExecutionFailure, tool_failure_reason
from agent_harness.credentials import CredentialStore
from agent_harness.integrations_config import IntegrationSettings, IntegrationStore, SandboxConfig


class RepositoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repository_id: str = Field(min_length=1, max_length=64)


class SearchRepositoryArgs(RepositoryArgs):
    query: str = Field(min_length=1, max_length=1000)


class ReadRepositoryArgs(RepositoryArgs):
    path: str = Field(min_length=1, max_length=500)
    start_line: int = Field(default=1, ge=1, le=1_000_000)
    end_line: int = Field(default=160, ge=1, le=1_000_000)


class RunSandboxArgs(RepositoryArgs):
    command: list[str] = Field(min_length=1, max_length=64)


class MCPListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connection_id: str = Field(min_length=1, max_length=64)
    query: str = Field(default="", max_length=200)


class MCPSchemaArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connection_id: str = Field(min_length=1, max_length=64)
    tool: str = Field(min_length=1, max_length=200)


class MCPArgs(MCPSchemaArgs):
    arguments: dict[str, Any] = Field(default_factory=dict)


class A2AArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connection_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=8000)


ARGUMENTS: dict[str, type[BaseModel]] = {
    "clone_repository": RepositoryArgs,
    "search_repository": SearchRepositoryArgs,
    "read_repository": ReadRepositoryArgs,
    "run_in_sandbox": RunSandboxArgs,
    "mcp_list_tools": MCPListArgs,
    "mcp_call": MCPArgs,
    "mcp_tool_schema": MCPSchemaArgs,
    "a2a_ask": A2AArgs,
}
DESCRIPTIONS = {
    "clone_repository": "Clone a configured repository into this run's sandbox.",
    "search_repository": "Search literal text in a repository; prepares its sandbox if needed.",
    "read_repository": "Read lines from a repository file inside this run's sandbox.",
    "run_in_sandbox": "Run a command in an isolated repository container without network access. "
    "Only for analysis, build and tests requested by the user. No host filesystem access.",
    "mcp_list_tools": "Discover the current allowed tools and their descriptions on a selected "
    "MCP connection. Use this to choose a relevant tool, then read mcp_tool_schema and call it. "
    "Use query to filter names/descriptions if the catalog is large or truncated. "
    "The catalog is metadata, not evidence about the corporate system.",
    "mcp_tool_schema": "Read an allowed MCP tool's input schema before calling it. "
    "This is tool metadata, not evidence of the corporate system.",
    "mcp_call": "Call an administrator-allowed MCP tool. Read mcp_tool_schema first. "
    "Do not mutate external systems unless "
    "the user's request explicitly authorizes that action.",
    "a2a_ask": "Ask an allowed remote agent to investigate a question. Send only necessary data. "
    "Remote conclusions are claims requiring source verification, not established facts.",
}


class IntegrationHub:
    def __init__(
        self,
        store: IntegrationStore,
        authorize: Callable[[Principal], Awaitable[bool]],
    ) -> None:
        self.store, self.authorize = store, authorize
        self.credentials = CredentialStore(store.path.parent)
        # Lazy imports keep optional connectors out of an unused portable installation.
        self._sandbox: Any = None
        self._remote: Any = None

    @property
    def sandbox(self) -> Any:
        if self._sandbox is None:
            from agent_harness.sandbox import SandboxBackend

            self._sandbox = SandboxBackend(credentials=self.credentials)
        return self._sandbox

    @property
    def remote(self) -> Any:
        if self._remote is None:
            from agent_harness.remote_tools import RemoteTools

            self._remote = RemoteTools()
        return self._remote

    async def _check(self, principal: Principal, cancel: Event | None = None) -> None:
        if cancel is not None and cancel.is_set():
            raise asyncio.CancelledError
        if not await self.authorize(principal):
            raise PermissionError("Доступ отозван. Войдите в Harness заново.")

    async def profiles(self, principal: Principal) -> list[dict[str, str]]:
        await self._check(principal)
        settings = self.store.load()
        if not settings.sandbox.enabled:
            return []
        return [
            {"id": "", "name": "Базовая среда"},
            *[
                {"id": profile.id, "name": profile.name}
                for profile in settings.sandbox_profiles
                if profile.allows(principal.owner_id)
            ],
        ]

    @staticmethod
    def _sandbox_config(
        settings: IntegrationSettings, principal: Principal, profile_id: str
    ) -> SandboxConfig:
        if not settings.sandbox.enabled:
            raise PermissionError("Repository sandbox is disabled")
        if not profile_id:
            return settings.sandbox
        profile = next((item for item in settings.sandbox_profiles if item.id == profile_id), None)
        if profile is None or not profile.allows(principal.owner_id):
            raise PermissionError("Sandbox profile is outside the permitted scope")
        # All resource/security limits remain administrator-owned global settings.
        return settings.sandbox.model_copy(update={"image": profile.image})

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        await self._check(principal)
        settings = self.store.load()
        return [
            *[
                {"id": "repo:" + item.id, "name": "Код · " + item.name}
                for item in settings.repositories
                if settings.sandbox.enabled and item.allows(principal.owner_id)
            ],
            *[
                {"id": "connection:" + item.id, "name": item.kind.upper() + " · " + item.name}
                for item in settings.connections
                if item.allows(principal.owner_id) and (item.kind == "a2a" or item.allowed_tools)
            ],
        ]

    async def describe(
        self,
        principal: Principal,
        index_ids: list[str],
    ) -> list[dict[str, Any]]:
        available = {item["id"] for item in await self.indexes(principal)} & set(index_ids)
        settings = self.store.load()
        names: list[str] = []
        if any(value.startswith("repo:") for value in available):
            names.extend(
                ("clone_repository", "search_repository", "read_repository", "run_in_sandbox")
            )
        connections = [
            item for item in settings.connections if "connection:" + item.id in available
        ]
        if any(item.kind == "mcp" for item in connections):
            names.extend(("mcp_list_tools", "mcp_tool_schema", "mcp_call"))
        if any(item.kind == "a2a" for item in connections):
            names.append("a2a_ask")
        allowed = "; ".join(
            item.id + ": " + ", ".join(item.allowed_tools)
            for item in connections
            if item.kind == "mcp"
        )[:2000]
        connection_ids = ", ".join(item.id for item in connections if item.kind == "mcp")
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": DESCRIPTIONS[name]
                    + (
                        " Allowed connection IDs: " + connection_ids
                        + ". Tool name hints: " + allowed
                        if name in {"mcp_list_tools", "mcp_call", "mcp_tool_schema"}
                        else ""
                    ),
                    "parameters": ARGUMENTS[name].model_json_schema(),
                },
            }
            for name in names
        ]

    async def call(
        self,
        name: str,
        args: Any,
        *,
        principal: Principal,
        run_id: str,
        index_ids: list[str],
        cancel: Event,
        sandbox_profile_id: str = "",
    ) -> list[Evidence]:
        await self._check(principal, cancel)
        if name not in ARGUMENTS:
            raise ValueError("Tool is not allowed")
        validated = ARGUMENTS[name].model_validate(args).model_dump()
        settings = self.store.load()
        mcp_failure = None
        if "repository_id" in validated:
            resource_id = "repo:" + validated["repository_id"]
            repository = next(
                (item for item in settings.repositories if item.id == validated["repository_id"]),
                None,
            )
            if (
                resource_id not in index_ids
                or repository is None
                or not repository.allows(principal.owner_id)
                or not settings.sandbox.enabled
            ):
                raise PermissionError("Repository is outside the permitted scope")
            sandbox_config = self._sandbox_config(settings, principal, sandbox_profile_id)
            credential_env = repository.credential_env
            result = await self.sandbox.call(
                name,
                validated,
                repository=repository,
                config=sandbox_config,
                principal=principal,
                run_id=run_id,
                cancel=cancel,
            )
        else:
            resource_id = "connection:" + validated["connection_id"]
            connection = next(
                (item for item in settings.connections if item.id == validated["connection_id"]),
                None,
            )
            if (
                resource_id not in index_ids
                or connection is None
                or not connection.allows(principal.owner_id)
                or connection.kind != (
                    "mcp" if name in {"mcp_list_tools", "mcp_call", "mcp_tool_schema"} else "a2a"
                )
                or (connection.kind == "mcp" and (
                    not connection.allowed_tools or (
                        name != "mcp_list_tools"
                        and validated["tool"] not in connection.allowed_tools
                    )
                ))
            ):
                raise PermissionError("Connection is outside the permitted scope")
            credential_env = connection.credential_env
            try:
                result = await self.remote.call(
                    name, validated, connection=connection, principal=principal,
                    run_id=run_id, cancel=cancel,
                )
            except Exception as exc:
                if name != "mcp_call":
                    raise
                # Recheck the allowlist below before returning the identity of the failed tool.
                mcp_failure = ToolExecutionFailure(
                    "MCP call failed", reason=tool_failure_reason(exc), index_id=resource_id,
                )
                result = []
        await self._check(principal, cancel)
        if resource_id not in {item["id"] for item in await self.indexes(principal)}:
            raise PermissionError("Resource access was revoked")
        if "repository_id" in validated:
            current_sandbox = self._sandbox_config(self.store.load(), principal, sandbox_profile_id)
            if current_sandbox.image != sandbox_config.image:
                raise PermissionError("Sandbox profile changed during the call")
        if name in {"mcp_list_tools", "mcp_call", "mcp_tool_schema", "a2a_ask"}:
            assert connection is not None
            current = next(
                (
                    item
                    for item in self.store.load().connections
                    if item.id == validated["connection_id"]
                ),
                None,
            )
            if (
                current is None
                or not current.allows(principal.owner_id)
                or current.kind != connection.kind
                or current.url != connection.url
                or (current.kind == "mcp" and (
                    (name == "mcp_list_tools" and not set(connection.allowed_tools).issubset(
                        current.allowed_tools
                    ))
                    or (name != "mcp_list_tools" and validated["tool"] not in current.allowed_tools)
                ))
            ):
                raise PermissionError("Connection permission changed during the call")
        secret = os.environ.get(credential_env, "") if credential_env else ""
        remote_tool = validated["tool"] if name == "mcp_call" else None
        if remote_tool and secret and secret in remote_tool:
            remote_tool = None
        if mcp_failure is not None:
            mcp_failure.remote_tool = remote_tool
            raise mcp_failure
        accepted = []
        for item in result[:10]:
            payload = Evidence.model_validate(item).model_dump()
            if payload["index_id"] != resource_id:
                raise ValueError("Tool returned evidence outside its scope")
            payload["remote_tool"] = remote_tool
            if secret:
                payload = {
                    key: value.replace(secret, "[redacted]") if isinstance(value, str) else value
                    for key, value in payload.items()
                }
            accepted.append(Evidence.model_validate(payload))
        return accepted

    async def close_run(self, run_id: str) -> None:
        if self._sandbox is not None:
            await self._sandbox.close_run(run_id)

    async def probe(self, kind: str, resource_id: str = "") -> dict[str, Any]:
        settings = self.store.load()
        try:
            if kind == "sandbox":
                result = dict(await self.sandbox.probe(settings.sandbox))
                result["message"] = result.get("message") or result.get("reason", "")
                return result
            if kind == "sandbox_profile":
                profile = next(
                    (item for item in settings.sandbox_profiles if item.id == resource_id), None
                )
                if profile is None:
                    raise ValueError("Sandbox profile not found")
                config = settings.sandbox.model_copy(update={"image": profile.image})
                result = dict(await self.sandbox.probe(config))
                result["message"] = result.get("message") or result.get("reason", "")
                return result
            if kind == "repository":
                repository = next((r for r in settings.repositories if r.id == resource_id), None)
                if repository is None:
                    raise ValueError("Repository not found")
                result = dict(await self.sandbox.probe(settings.sandbox))
                result["message"] = (
                    "Песочница готова. Адрес сохранён; доступ к Git проверяется при клонировании."
                    if result.get("available")
                    else result.get("message") or result.get("reason", "Песочница недоступна.")
                )
                return result
            if kind == "connection":
                connection = next((c for c in settings.connections if c.id == resource_id), None)
                if connection is None:
                    raise ValueError("Connection not found")
                return dict(await self.remote.probe(connection))
            raise ValueError("Unknown connection type")
        except Exception as exc:
            return {"available": False, "message": f"Проверка не пройдена ({type(exc).__name__})."}
