"""Small host-independent contracts for knowledge and model providers."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelDecisionError(ValueError):
    """A model reply can be corrected without restarting the model connection."""


class ConversationUnavailable(ModelDecisionError):
    """A failed compaction must not silently drop the user's earlier constraints."""


@dataclass(frozen=True)
class Principal:
    owner_id: str
    shared: bool = False


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=8000)
    mode: Literal["quick", "investigate", "autonomous"] = "quick"
    effort: Literal["low", "medium", "high"] = "medium"
    index_ids: list[str] = Field(default_factory=list, max_length=1000)
    parent_run_id: str | None = Field(default=None, max_length=80)
    sandbox_profile_id: str = Field(default="", pattern=r"^([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})?$")

    @field_validator("question")
    @classmethod
    def nonempty_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Question must not be blank")
        return value


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    document_id: str | None = None
    title: str
    text: str
    index_id: str
    url: str | None = None
    revision: str | None = None
    kind: Literal["source", "tool_metadata"] = "source"


class SourcePort(Protocol):
    """Providers enforce source permissions on each call, including background calls."""

    async def indexes(self, principal: Principal) -> list[dict[str, str]]: ...

    async def search(
        self,
        *,
        query: str,
        index_id: str,
        limit: int,
        principal: Principal,
        cancel: Event,
    ) -> list[Evidence]: ...

    async def read(
        self,
        *,
        document_id: str,
        index_id: str,
        principal: Principal,
        cancel: Event,
    ) -> Evidence: ...


class ModelPort(Protocol):
    def status(self) -> dict[str, Any]: ...

    async def decide(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        cancel: Event,
        max_input_chars: int,
    ) -> dict[str, Any]: ...


class ToolPort(Protocol):
    """Optional tools own execution isolation and recheck access on every call."""

    async def indexes(self, principal: Principal) -> list[dict[str, str]]: ...

    async def describe(
        self, principal: Principal, index_ids: list[str]
    ) -> list[dict[str, Any]]: ...

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
    ) -> list[Evidence]: ...

    async def close_run(self, run_id: str) -> None: ...
