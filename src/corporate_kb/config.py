"""Environment-backed application configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_QUERY_INSTRUCTION = (
    "Given a technical question about a software system, retrieve relevant corporate "
    "knowledge, architecture, requirements, business rules, APIs, events, ADRs and runbooks."
)


class Settings(BaseSettings):
    """Settings loaded from ``KB_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="KB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    knowledge_dir: Path = Path("knowledge")
    cache_dir: Path = Path(".cache/kb")
    embedding_provider: Literal["sentence_transformers", "hash"] = "hash"
    embedding_model: str = "./models/Qwen3-Embedding-0.6B"
    embedding_local_files_only: bool = True
    embedding_device: Literal["auto", "cpu", "mps", "cuda"] = "auto"
    embedding_batch_size: int = Field(default=8, ge=1)
    embedding_max_seq_length: int = Field(default=2048, ge=1)
    embedding_dimension: int = Field(default=1024, ge=1)
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION
    chunk_size_tokens: int = Field(default=700, ge=1)
    chunk_hard_max_tokens: int = Field(default=900, ge=1)
    chunk_overlap_tokens: int = Field(default=80, ge=0)
    default_top_k: int = Field(default=5, ge=1, le=20)
    auto_index: bool = False
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_chunking(self) -> Settings:
        """Reject configurations that cannot produce valid chunks."""
        if self.chunk_size_tokens > self.chunk_hard_max_tokens:
            raise ValueError("KB_CHUNK_SIZE_TOKENS must not exceed KB_CHUNK_HARD_MAX_TOKENS")
        if self.chunk_overlap_tokens >= self.chunk_size_tokens:
            raise ValueError("KB_CHUNK_OVERLAP_TOKENS must be smaller than KB_CHUNK_SIZE_TOKENS")
        return self

    def resolved(self, cwd: Path | None = None) -> Settings:
        """Return a copy with paths made absolute relative to the process working directory."""
        base = (cwd or Path.cwd()).resolve()

        def resolve(path: Path) -> Path:
            return path.resolve() if path.is_absolute() else (base / path).resolve()

        return self.model_copy(
            update={
                "knowledge_dir": resolve(self.knowledge_dir),
                "cache_dir": resolve(self.cache_dir),
            }
        )
