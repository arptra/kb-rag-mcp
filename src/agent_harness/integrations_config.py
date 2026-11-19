"""Portable administrator-owned integration settings; credentials stay on the server."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Literal, Self
from urllib.parse import urlsplit

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
    ssh_key_id: str = Field(default="", pattern=r"^([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})?$")

    @field_validator("url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        # Override the general HTTP endpoint validator. SCP syntax is canonicalized.
        if any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("Repository address contains invalid characters")
        match = re.fullmatch(
            r"([a-zA-Z0-9_][a-zA-Z0-9_.-]*)@([a-zA-Z0-9][a-zA-Z0-9.-]*):(.+)", value
        )
        if match:
            original = value
            value = f"ssh://{match[1]}@{match[2]}/{match[3]}"
        if value.startswith("https://"):
            return super().endpoint(value)
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "ssh"
                and parsed.hostname
                and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]*", parsed.hostname)
                and parsed.username
                and re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", parsed.username)
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and re.fullmatch(r"/[a-zA-Z0-9_][a-zA-Z0-9_./-]*", parsed.path)
                and all(part not in {".", ".."} for part in parsed.path.split("/"))
            )
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use HTTPS or ssh://user@host/path without credentials or options")
        return original if match else value

    @model_validator(mode="after")
    def safe_git_source(self) -> Self:
        if self.ref.startswith("-"):
            raise ValueError("Repositories require a branch, tag or commit")
        if not self.url.startswith("https://"):
            if self.credential_env:
                raise ValueError("SSH repositories use an SSH key, not an HTTP token")
        elif self.ssh_key_id:
            raise ValueError("SSH keys can only be used with SSH repository addresses")
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


def normalize_repository_url(value: str) -> str:
    """Validate a repository address without requiring an assigned SSH key yet."""
    return RepositoryConfig.endpoint(value)


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


class SandboxProfileConfig(BaseModel):
    """An administrator-built image; requesters can choose only an allowed profile ID."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=160)
    image: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,250}$")
    enabled: bool = True
    allowed_owners: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("allowed_owners")
    @classmethod
    def owners(cls, values: list[str]) -> list[str]:
        return ResourceConfig.owners(values)

    def allows(self, owner_id: str) -> bool:
        return (
            bool(owner_id)
            and self.enabled
            and (not self.allowed_owners or owner_id in self.allowed_owners)
        )


def _default_sandbox_profiles() -> list[SandboxProfileConfig]:
    return [SandboxProfileConfig(id="java21", name="Java 21", image="harness-sandbox-java21:local")]


class IntegrationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repositories: list[RepositoryConfig] = Field(default_factory=list, max_length=100)
    connections: list[ConnectionConfig] = Field(default_factory=list, max_length=100)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    sandbox_profiles: list[SandboxProfileConfig] = Field(
        default_factory=_default_sandbox_profiles, max_length=20
    )

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        for items in (self.repositories, self.connections, self.sandbox_profiles):
            if len({item.id for item in items}) != len(items):
                raise ValueError("Resource IDs must be unique")
        return self


class IntegrationStore:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "configuration.sqlite3"
        self.legacy_path = state_dir / "integrations.json"
        self._lock = RLock()
        state_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS integrations (id INTEGER PRIMARY KEY, data TEXT)"
                )
                if (
                    db.execute("SELECT 1 FROM integrations WHERE id=1").fetchone() is None
                    and self.legacy_path.exists()
                ):
                    legacy = IntegrationSettings.model_validate_json(self.legacy_path.read_bytes())
                    db.execute("INSERT INTO integrations VALUES(1,?)", (legacy.model_dump_json(),))
        finally:
            db.close()
        self.path.chmod(0o600)

    def load(self) -> IntegrationSettings:
        with self._lock:
            db = sqlite3.connect(self.path, timeout=5)
            try:
                row = db.execute("SELECT data FROM integrations WHERE id=1").fetchone()
                return (
                    IntegrationSettings.model_validate_json(row[0])
                    if row
                    else IntegrationSettings()
                )
            finally:
                db.close()

    def save(self, settings: IntegrationSettings) -> None:
        with self._lock:
            db = sqlite3.connect(self.path, timeout=5)
            try:
                with db:
                    db.execute(
                        "INSERT INTO integrations VALUES(1,?) "
                        "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                        (settings.model_dump_json(),),
                    )
            finally:
                db.close()
