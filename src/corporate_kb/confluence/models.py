"""Public, credential-free configuration and normalized Confluence records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["manual", "interval", "daily", "weekly"] = "interval"
    interval_minutes: int = Field(default=60, ge=1, le=525600)
    time: str = "03:00"
    weekday: int = Field(default=0, ge=0, le=6)
    timezone: str = "Europe/Moscow"

    @field_validator("time")
    @classmethod
    def validate_time(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError("time must be HH:MM") from exc
        return parsed.strftime("%H:%M")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value

    def next_at(self, now: datetime | None = None, *, failures: int = 0) -> str | None:
        now = now or datetime.now(UTC)
        if self.kind == "manual":
            return None
        if failures:
            return (now + timedelta(minutes=min(60, 2 ** min(failures, 6)))).isoformat()
        if self.kind == "interval":
            return (now + timedelta(minutes=self.interval_minutes)).isoformat()
        local = now.astimezone(ZoneInfo(self.timezone))
        hour, minute = map(int, self.time.split(":"))
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(days=1)
        if self.kind == "weekly":
            candidate += timedelta(days=(self.weekday - candidate.weekday()) % 7)
        # A round-trip gives a deterministic valid wall time during DST gaps.
        return candidate.astimezone(UTC).isoformat()


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    index_id: str = Field(min_length=1, max_length=100)
    base_url: str = Field(max_length=2048)
    root_page_url: str = Field(min_length=1, max_length=2048)
    deployment: Literal["auto", "cloud", "data_center"] = "auto"
    username: str | None = Field(default=None, max_length=320)
    enabled: bool = True
    include_history: bool = False
    analyze_images: bool = True
    schedule: Schedule = Field(default_factory=Schedule)
    max_pages: int = Field(default=50000, ge=1, le=500000)
    max_attachment_bytes: int = Field(default=25_000_000, ge=1, le=100_000_000)

    @property
    def effective_cloud(self) -> bool:
        return self.deployment == "cloud" or (
            self.deployment == "auto"
            and (urlsplit(self.base_url).hostname or "").lower().endswith(".atlassian.net")
        )

    @field_validator("base_url")
    @classmethod
    def validate_base(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Confluence URL must be an HTTP(S) URL without credentials or query")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_root(self) -> SourceConfig:
        if self.root_page_url.isdigit():
            return self
        root, base = urlsplit(self.root_page_url), urlsplit(self.base_url)
        if (root.scheme, root.netloc) != (base.scheme, base.netloc):
            raise ValueError("Root page must belong to the configured Confluence origin")
        if root.username or root.password:
            raise ValueError("Root URL must not contain credentials")
        return self

    @model_validator(mode="after")
    def validate_authentication(self) -> SourceConfig:
        if self.effective_cloud and not self.username:
            raise ValueError(
                "Confluence Cloud requires an Atlassian account email (username) with the API token"
            )
        return self


class Page(BaseModel):
    id: str
    title: str
    version: int
    updated_at: str | None = None
    parent_id: str | None = None
    url: str
    body: str = ""


class Attachment(BaseModel):
    id: str
    title: str
    version: int = 1
    media_type: str = "application/octet-stream"
    download_url: str
    size: int | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ConfluenceError(RuntimeError):
    """A safe error that does not include headers, response bodies or credentials."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class SyncCancelled(RuntimeError):
    pass
