"""Environment-backed settings for the isolated GigaCode graph module."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GraphSettings(BaseSettings):
    """Configuration loaded from ``GIGACODE_GRAPH_*`` variables."""

    model_config = SettingsConfigDict(
        env_prefix="GIGACODE_GRAPH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    store_path: Path = Path(".cache/gigacode-graph/graph.json")
    repository_cache_path: Path = Path(".cache/gigacode-graph/repositories")
    module_cache_path: Path = Path(".cache/gigacode-graph/module-analysis")
    ingestion_path: Path = Path(".cache/gigacode-graph/ingestion.json")
    max_java_file_bytes: int = Field(default=2_000_000, ge=10_000, le=20_000_000)
    call_depth: int = Field(default=6, ge=1, le=20)
    max_service_seed_methods: int = Field(default=5_000, ge=1, le=100_000)
    max_traced_methods_per_service: int = Field(default=20_000, ge=1, le=500_000)
    max_call_edges_per_service: int = Field(default=100_000, ge=1, le=2_000_000)
    max_weak_outbound_per_service: int = Field(default=5_000, ge=0, le=100_000)
    gigacode_max_candidates_per_repository: int = Field(default=250, ge=1, le=10_000)
    git_timeout_seconds: int = Field(default=180, ge=10, le=1800)
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8077, ge=1, le=65535)
    mcp_path: str = "/mcp"
    bearer_token: SecretStr | None = None
    log_level: str = "INFO"

    @field_validator("mcp_path")
    @classmethod
    def validate_mcp_path(cls, value: str) -> str:
        if not value.startswith("/") or value == "/" or value.endswith("/"):
            raise ValueError("GIGACODE_GRAPH_MCP_PATH must look like /mcp")
        return value

    def resolved(self, cwd: Path | None = None) -> GraphSettings:
        base = (cwd or Path.cwd()).resolve()
        paths = {
            "store_path": self.store_path,
            "repository_cache_path": self.repository_cache_path,
            "module_cache_path": self.module_cache_path,
            "ingestion_path": self.ingestion_path,
        }
        return self.model_copy(
            update={
                name: path.resolve() if path.is_absolute() else (base / path).resolve()
                for name, path in paths.items()
            }
        )

    def validate_http_security(self) -> str | None:
        token = self.bearer_token.get_secret_value() if self.bearer_token else None
        if token is not None and len(token) < 32:
            raise ValueError("GIGACODE_GRAPH_BEARER_TOKEN must contain at least 32 characters")
        if self.http_host not in {"127.0.0.1", "localhost", "::1"} and token is None:
            raise ValueError(
                "GIGACODE_GRAPH_BEARER_TOKEN with at least 32 characters is required "
                "when the HTTP server listens outside loopback"
            )
        return token
