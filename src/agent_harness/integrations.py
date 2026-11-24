"""Settings-backed, permission-checked bridge to remote tools and isolated repositories."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from threading import Event
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_harness.contracts import (
    ANALYSIS_FILENAME,
    MAX_ANALYSIS_SOURCE_BYTES,
    Evidence,
    Principal,
    ToolExecutionFailure,
    sandbox_api_index,
    tool_failure_reason,
)
from agent_harness.credentials import CredentialStore
from agent_harness.integrations_config import IntegrationSettings, IntegrationStore, SandboxConfig
from agent_harness.tls_credentials import TLSCredentialStore


class SandboxCheckArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class WriteAnalysisScriptArgs(RepositoryArgs):
    filename: str = Field(pattern=ANALYSIS_FILENAME, description="Simple .py or .java filename")
    source: str = Field(
        min_length=1, max_length=MAX_ANALYSIS_SOURCE_BYTES,
        description="Complete UTF-8 source, up to 24 KiB",
    )


class MCPListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connection_id: str = Field(min_length=1, max_length=64)
    query: str = Field(default="", max_length=200)
    refresh: bool = Field(default=False, description="Fetch live definitions again in this run.")


class MCPToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connection_id: str = Field(min_length=1, max_length=64)
    tool: str = Field(min_length=1, max_length=200)


class MCPSchemaArgs(MCPToolArgs):
    refresh: bool = Field(default=False, description="Fetch the live input contract again.")


class MCPArgs(MCPToolArgs):
    arguments: dict[str, Any] = Field(default_factory=dict)
    schema_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class A2AArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connection_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=8000)


ARGUMENTS: dict[str, type[BaseModel]] = {
    "check_sandbox_api": SandboxCheckArgs,
    "clone_repository": RepositoryArgs,
    "search_repository": SearchRepositoryArgs,
    "read_repository": ReadRepositoryArgs,
    "write_analysis_script": WriteAnalysisScriptArgs,
    "run_in_sandbox": RunSandboxArgs,
    "mcp_list_tools": MCPListArgs,
    "mcp_call": MCPArgs,
    "mcp_tool_schema": MCPSchemaArgs,
    "a2a_ask": A2AArgs,
}
DESCRIPTIONS = {
    "check_sandbox_api": "Start the selected sandbox image and check its configured HTTP "
    "endpoint. Returns a fresh status from the temporary container. Use to verify readiness.",
    "clone_repository": "Clone a configured repository into this run's sandbox. Harness "
    "automatically applies the user's saved SSH access for the selected repository.",
    "search_repository": "Search literal text in a repository; prepares its sandbox if needed.",
    "read_repository": "Read lines from a repository file inside this run's sandbox.",
    "write_analysis_script": "Prepare a small Python or Java analysis program to answer a "
    "specific question about the selected repository. Java requires a profile with a JDK. "
    "The file is local to this run. "
    "Use run_in_sandbox with the command in the receipt to obtain the actual output. "
    "Write the same filename again to correct a failed check.",
    "run_in_sandbox": "Run a command in an isolated repository container without network access. "
    "Only for analysis, build and tests requested by the user. No host filesystem access. "
    "For Gradle use gradle or ./gradlew directly. The user's saved Maven registry is attached "
    "automatically for this repository; never request raw credentials.",
    "mcp_list_tools": "Discover the current allowed tools and their descriptions on a selected "
    "MCP connection. Use this to choose a relevant tool, then read mcp_tool_schema and call it. "
    "Use query to filter a large catalog. Set refresh=true if an earlier list does not show "
    "a needed action or the connection's capabilities may have changed. "
    "The catalog is metadata, not evidence about the corporate system.",
    "mcp_tool_schema": "Read an allowed MCP tool's input schema before calling it. "
    "Set refresh=true to recheck a previously inspected contract. "
    "This is tool metadata, not evidence of the corporate system.",
    "mcp_call": "Call an administrator-allowed MCP tool. Read mcp_tool_schema first. "
    "Do not mutate external systems unless "
    "the user's request explicitly authorizes that action.",
    "a2a_ask": "Ask an allowed remote agent to investigate a question. Send only necessary data. "
    "Remote conclusions are claims requiring source verification, not established facts.",
}


class IntegrationHub:
    supports_read_only_mcp = True

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

            self._sandbox = SandboxBackend(
                credentials=self.credentials,
                tls_credentials=TLSCredentialStore(self.store.path.parent),
            )
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
        return profile.apply_to(settings.sandbox)

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        await self._check(principal)
        settings = self.store.load()
        return [
            *([{"id": sandbox_api_index(""), "name": "API · Базовая среда", "kind": "sandbox"}]
              if settings.sandbox.enabled and settings.sandbox.service_port else []),
            *[
                {"id": sandbox_api_index(item.id), "name": "API · " + item.name, "kind": "sandbox"}
                for item in settings.sandbox_profiles
                if settings.sandbox.enabled and item.service_port
                and item.allows(principal.owner_id)
            ],
            *[
                {"id": "repo:" + item.id, "name": "Код · " + item.name}
                for item in settings.repositories
                if settings.sandbox.enabled and item.allows(principal.owner_id)
            ],
            *[
                {"id": "connection:" + item.id, "name": item.kind.upper() + " · " + item.name,
                 "kind": item.kind}
                for item in settings.connections
                if item.allows(principal.owner_id) and (item.kind == "a2a" or item.has_tools)
            ],
        ]

    async def allowed_mcp_tools(
        self, principal: Principal, index_ids: list[str],
    ) -> dict[str, set[str]]:
        """Local allowlists for choosing a remembered route; call() rechecks at execution."""
        await self._check(principal)
        selected = set(index_ids)
        return {
            "connection:" + item.id: set(item.allowed_tools)
            for item in self.store.load().connections
            if item.kind == "mcp" and item.has_tools and "connection:" + item.id in selected
            and item.allows(principal.owner_id)
        }

    async def describe(
        self,
        principal: Principal,
        index_ids: list[str],
        *, read_only_mcp: bool = False,
    ) -> list[dict[str, Any]]:
        available = {item["id"] for item in await self.indexes(principal)} & set(index_ids)
        settings = self.store.load()
        names: list[str] = []
        if not read_only_mcp and any(item.startswith("sandbox-api:") for item in available):
            names.append("check_sandbox_api")
        repository_ids = [
            item.id for item in settings.repositories if "repo:" + item.id in available
        ]
        if repository_ids:
            names.extend(
                ("clone_repository", "search_repository", "read_repository",
                 "write_analysis_script", "run_in_sandbox")
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
        mcp_ids = [item.id for item in connections if item.kind == "mcp"]
        a2a_ids = [item.id for item in connections if item.kind == "a2a"]

        def parameters(name: str) -> dict[str, Any]:
            schema = ARGUMENTS[name].model_json_schema()
            if "repository_id" in schema["properties"]:
                schema["properties"]["repository_id"]["enum"] = repository_ids
            elif "connection_id" in schema["properties"]:
                schema["properties"]["connection_id"]["enum"] = (
                    a2a_ids if name == "a2a_ask" else mcp_ids
                )
            return schema

        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": DESCRIPTIONS[name]
                    + (
                        " Allowed connection IDs: " + ", ".join(mcp_ids)
                        + (". Discover read-only operations with mcp_list_tools." if read_only_mcp
                           else ". Tool name hints: " + allowed)
                        if name in {"mcp_list_tools", "mcp_call", "mcp_tool_schema"}
                        else ""
                    ),
                    "parameters": parameters(name),
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
        read_only_mcp: bool = False,
    ) -> list[Evidence]:
        await self._check(principal, cancel)
        if name not in ARGUMENTS:
            raise ValueError("Tool is not allowed")
        if read_only_mcp and name not in {"mcp_list_tools", "mcp_tool_schema", "mcp_call"}:
            raise ValueError("Only MCP read operations are allowed in this run")
        validated = ARGUMENTS[name].model_validate(args).model_dump()
        # Refresh controls only the graph's metadata cache. The remote reader
        # always performs a live MCP list and uses a stable evidence ID.
        validated.pop("refresh", None)
        if validated.get("schema_revision") is None:
            validated.pop("schema_revision", None)
        settings = self.store.load()
        mcp_failure = None
        if name == "check_sandbox_api":
            resource_id = sandbox_api_index(sandbox_profile_id)
            if resource_id not in index_ids:
                raise PermissionError("Sandbox API is outside this run's scope")
            check = await self.check_profile(principal, sandbox_profile_id, cancel, http_only=True)
            result = [Evidence(id="sandbox-http-" + run_id, title="Проверка HTTP API в песочнице",
                               index_id=resource_id, text=check["message"],
                               tool_outcome=None if check.get("http_status") else "failed")]
            credential_env = ""
        elif "repository_id" in validated:
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
                    not connection.has_tools or (
                        name != "mcp_list_tools"
                        and validated["tool"] not in connection.allowed_tools
                        and not connection.include_read_only_tools
                    )
                ))
            ):
                raise PermissionError("Connection is outside the permitted scope")
            credential_env = connection.credential_env
            try:
                result = await self.remote.call(
                    name, validated, connection=connection, principal=principal,
                    run_id=run_id, cancel=cancel,
                    **({"require_read_only": True} if read_only_mcp else {}),
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
            if current_sandbox != sandbox_config:
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
                or current.credential_env != connection.credential_env
                or (current.kind == "mcp" and (
                    (connection.include_read_only_tools and not current.include_read_only_tools)
                    or
                    (name == "mcp_list_tools" and not set(connection.allowed_tools).issubset(
                        current.allowed_tools
                    ))
                    or (name != "mcp_list_tools" and validated["tool"] not in current.allowed_tools
                        and (validated["tool"] in connection.allowed_tools
                             or not current.include_read_only_tools))
                ))
            ):
                raise PermissionError("Connection permission changed during the call")
        secret = os.environ.get(credential_env, "") if credential_env else ""
        remote_tool = validated["tool"] if name == "mcp_call" else None
        if remote_tool and secret and secret in remote_tool:
            remote_tool = None
        if mcp_failure is not None:
            # A rejected dynamic name has not necessarily been observed on the peer.
            if connection is not None and remote_tool in connection.allowed_tools:
                mcp_failure.remote_tool = remote_tool
            raise mcp_failure
        accepted = []
        for item in result[:10]:
            payload = Evidence.model_validate(item).model_dump()
            if payload["index_id"] != resource_id:
                raise ValueError("Tool returned evidence outside its scope")
            # Only the portal graph source may attest to a complete service inventory.
            payload.pop("service_inventory", None)
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

    async def _probe_config(self, config: SandboxConfig, cancel: Event) -> dict[str, Any]:
        if config.service_port is not None:
            from agent_harness.sandbox_service import probe_service

            return await probe_service(self.sandbox, config, cancel)
        result = dict(await self.sandbox.probe(config))
        result["message"] = result.get("message") or result.get("reason", "")
        return result

    async def check_profile(self, principal: Principal, profile_id: str, cancel: Event,
                            *, http_only: bool = False) -> dict[str, Any]:
        await self._check(principal, cancel)
        config = self._sandbox_config(self.store.load(), principal, profile_id)
        if http_only and config.service_port is None:
            raise ValueError("Порт приложения для песочницы не настроен.")
        result = await self._probe_config(config, cancel)
        await self._check(principal, cancel)
        if config != self._sandbox_config(self.store.load(), principal, profile_id):
            raise PermissionError("Sandbox profile changed during the check")
        return result

    async def probe(self, kind: str, resource_id: str = "") -> dict[str, Any]:
        settings = self.store.load()
        try:
            if kind == "sandbox":
                return await self._probe_config(settings.sandbox, Event())
            if kind == "sandbox_profile":
                profile = next(
                    (item for item in settings.sandbox_profiles if item.id == resource_id), None
                )
                if profile is None:
                    raise ValueError("Sandbox profile not found")
                return await self._probe_config(profile.apply_to(settings.sandbox), Event())
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
