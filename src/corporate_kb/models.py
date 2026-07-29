"""Typed domain models shared by indexing, storage, CLI, and MCP layers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    """Strict base model with JSON-friendly values."""

    model_config = ConfigDict(extra="forbid")


class Document(DomainModel):
    document_id: str
    title: str
    source_path: str
    source_type: str
    source_id: str
    source_url: str | None = None
    content: str
    content_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    loaded_at: datetime


class Chunk(DomainModel):
    chunk_id: str
    document_id: str
    chunk_index: int
    title: str
    heading_path: str
    text: str
    embedding_text: str
    token_count: int
    source_path: str
    source_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResult(DomainModel):
    rank: int
    score: float
    chunk_id: str
    document_id: str
    title: str
    heading_path: str
    text: str
    source_path: str
    source_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IndexStats(DomainModel):
    document_count: int
    chunk_count: int
    embedding_dimension: int
    embedding_provider: str
    embedding_model: str
    loaded_from_cache: bool
    indexed_at: datetime
    knowledge_hash: str
    cache_schema_version: int


class SearchFilters(DomainModel):
    service: str | None = None
    domain: str | None = None
    document_type: str | None = None
    status: str | None = None
    authority: str | None = None
    source_type: str | None = None

    def active(self) -> dict[str, str]:
        return {key: value for key, value in self.model_dump().items() if value is not None}
