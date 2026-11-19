"""Portable administrator-owned integration settings; credentials stay on the server."""

from __future__ import annotations

import os
from pathlib import Path
from threading import RLock
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=2000)
    enabled: bool = True
    credential_env: str = Field(default="", pattern=r"^([A-Z][A-Z0-9_]{0,127})?$")
    allowed_owners: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and not any(c.isspace() or ord(c) < 32 for c in value)
            )
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use an HTTP(S) address without credentials, query or fragment")
        return value

    @field_validator("allowed_owners")
    @classmethod
    def owners(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("Invalid user ID")
        return list(dict.fromkeys(values))

    def allows(self, owner_id: str) -> bool:
        return (
            bool(owner_id)
            and self.enabled
            and (not self.allowed_owners or owner_id in self.allowed_owners)
        )


class RepositoryConfig(ResourceConfig):
    ref: str = Field(default="HEAD", min_length=1, max_length=200, pattern=r"^[^\s~^:?*\[\\]+$")

    @model_validator(mode="after")
    def safe_git_source(self) -> Self:
        if urlsplit(self.url).scheme != "https" or self.ref.startswith("-"):
            raise ValueError("Repositories require HTTPS and a branch, tag or commit")
        return self


class ConnectionConfig(ResourceConfig):
    kind: Literal["mcp", "a2a"]
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("allowed_tools")
    @classmethod
    def tool_names(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("Invalid tool name")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def credentials_need_tls(self) -> Self:
        if self.credential_env and urlsplit(self.url).scheme != "https":
            raise ValueError("Connections with credentials require HTTPS")
        return self


class SandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    image: str = Field(
        default="harness-sandbox:local", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,250}$"
    )
    cpus: float = Field(default=1, ge=0.25, le=8)
    memory_mb: int = Field(default=1024, ge=128, le=16384)
    workspace_mb: int = Field(default=512, ge=64, le=8192)
    pids_limit: int = Field(default=128, ge=16, le=1024)
    timeout_seconds: int = Field(default=90, ge=5, le=600)
    max_output_chars: int = Field(default=12000, ge=1000, le=48000)
    max_parallel: int = Field(default=2, ge=1, le=8)


class IntegrationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repositories: list[RepositoryConfig] = Field(default_factory=list, max_length=100)
    connections: list[ConnectionConfig] = Field(default_factory=list, max_length=100)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        for items in (self.repositories, self.connections):
            if len({item.id for item in items}) != len(items):
                raise ValueError("Resource IDs must be unique")
        return self


class IntegrationStore:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "integrations.json"
        self._lock = RLock()

    def load(self) -> IntegrationSettings:
        with self._lock:
            if not self.path.exists():
                return IntegrationSettings()
            return IntegrationSettings.model_validate_json(self.path.read_bytes())

    def save(self, settings: IntegrationSettings) -> None:
        data = settings.model_dump_json(indent=2).encode()
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".integrations-{uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    os.chmod(temporary, 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
