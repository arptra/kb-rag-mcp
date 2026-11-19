"""Small host-independent contracts for knowledge and model providers."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


@dataclass(frozen=True)
class Principal:
    owner_id: str
    shared: bool = False


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=8000)
    mode: Literal["quick", "investigate"] = "quick"
    effort: Literal["low", "medium", "high"] = "medium"
    index_ids: list[str] = Field(default_factory=list, max_length=16)
    parent_run_id: str | None = Field(default=None, max_length=80)

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
    title: str
    text: str
    index_id: str
    url: str | None = None
    revision: str | None = None


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
