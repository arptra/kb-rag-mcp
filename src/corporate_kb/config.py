"""Environment-backed application configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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
    ssot_enabled: bool = False
    ssot_knowledge_dir: Path = Path("ssot")
    ssot_cache_dir: Path = Path(".cache/ssot")
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
    default_top_k: int = Field(default=3, ge=1, le=20)
    search_candidate_k: int = Field(default=12, ge=1, le=20)
    search_excerpt_tokens: int = Field(default=260, ge=40, le=900)
    search_context_tokens: int = Field(default=1000, ge=100, le=4000)
    search_max_chunks_per_document: int = Field(default=1, ge=1, le=5)
    document_context_tokens: int = Field(default=800, ge=100, le=4000)
    ssot_document_type: str = "ssot"
    ssot_candidate_k: int = Field(default=20, ge=3, le=20)
    ssot_max_services: int = Field(default=6, ge=1, le=12)
    ssot_facts_per_service: int = Field(default=3, ge=1, le=6)
    ssot_fact_tokens: int = Field(default=100, ge=40, le=300)
    ssot_context_tokens: int = Field(default=1000, ge=300, le=4000)
    ssot_llm_base_url: str | None = None
    ssot_llm_api_key: SecretStr | None = None
    ssot_llm_model: str | None = None
    ssot_llm_timeout_seconds: int = Field(default=120, ge=10, le=1800)
    ssot_llm_max_tokens: int = Field(default=1800, ge=256, le=8000)
    ssot_llm_temperature: float = Field(default=0.1, ge=0, le=2)
    ssot_generation_workers: int = Field(default=2, ge=1, le=8)
    ssot_generation_max_source_files: int = Field(default=12, ge=1, le=100)
    ssot_generation_source_chars: int = Field(default=48_000, ge=2_000, le=500_000)
    benchmark_questions_path: Path = Path("evaluation/questions.json")
    benchmark_password: SecretStr | None = None
    benchmark_max_questions: int = Field(default=100, ge=1, le=1000)
    admin_password: SecretStr | None = None
    admin_max_upload_bytes: int = Field(default=10_000_000, ge=1, le=100_000_000)
    managed_tools_path: Path = Path(".cache/kb/managed_tools.json")
    builtin_tool_overrides_path: Path = Path(".cache/kb/builtin_tool_overrides.json")
    mcp_servers_path: Path = Path(".cache/kb/mcp_servers.json")
    index_catalog_path: Path = Path(".cache/kb/index_catalog.json")
    managed_indexes_dir: Path = Path(".cache/kb/indexes")
    repository_cache_dir: Path = Path(".cache/kb/repositories")
    graph_store_path: Path = Path(".cache/kb/system_graph.json")
    service_map_path: Path = Path(".cache/kb/service_map.json")
    analysis_archive_dir: Path = Path(".cache/kb/analysis")
    job_logs_dir: Path = Path(".cache/kb/job-logs")
    ssot_skill_path: Path = Path("skills/build-service-ssot")
    repository_max_files: int = Field(default=10_000, ge=1, le=100_000)
    repository_git_timeout_seconds: int = Field(default=60, ge=10, le=1800)
    repository_analysis_timeout_seconds: int = Field(default=600, ge=5, le=3600)
    index_build_timeout_seconds: int = Field(default=600, ge=10, le=7200)
    auto_index: bool = False
    log_level: str = "INFO"
    mcp_http_host: str = "127.0.0.1"
    mcp_http_port: int = Field(default=8000, ge=1, le=65535)
    mcp_http_path: str = "/mcp"
    mcp_http_bearer_token: SecretStr | None = None

    @field_validator("mcp_http_path")
    @classmethod
    def validate_http_path(cls, value: str) -> str:
        """Keep the MCP route unambiguous for ASGI routing and auth checks."""
        if not value.startswith("/") or value == "/" or value.endswith("/"):
            raise ValueError("KB_MCP_HTTP_PATH must look like /mcp without a trailing slash")
        if "?" in value or "#" in value or "//" in value:
            raise ValueError("KB_MCP_HTTP_PATH must be a plain absolute URL path")
        return value

    @field_validator("ssot_llm_base_url")
    @classmethod
    def validate_ssot_llm_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("KB_SSOT_LLM_BASE_URL must be an absolute http:// or https:// URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "KB_SSOT_LLM_BASE_URL must not contain credentials, query, or fragment"
            )
        return normalized

    @field_validator("ssot_llm_model")
    @classmethod
    def normalize_ssot_llm_model(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()

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
                "ssot_knowledge_dir": resolve(self.ssot_knowledge_dir),
                "ssot_cache_dir": resolve(self.ssot_cache_dir),
                "benchmark_questions_path": resolve(self.benchmark_questions_path),
                "managed_tools_path": resolve(self.managed_tools_path),
                "builtin_tool_overrides_path": resolve(self.builtin_tool_overrides_path),
                "mcp_servers_path": resolve(self.mcp_servers_path),
                "index_catalog_path": resolve(self.index_catalog_path),
                "managed_indexes_dir": resolve(self.managed_indexes_dir),
                "repository_cache_dir": resolve(self.repository_cache_dir),
                "graph_store_path": resolve(self.graph_store_path),
                "service_map_path": resolve(self.service_map_path),
                "analysis_archive_dir": resolve(self.analysis_archive_dir),
                "job_logs_dir": resolve(self.job_logs_dir),
                "ssot_skill_path": resolve(self.ssot_skill_path),
            }
        )
