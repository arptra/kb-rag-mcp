from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from corporate_kb.config import Settings


@pytest.fixture
def settings_factory(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "knowledge_dir": tmp_path / "knowledge",
            "cache_dir": tmp_path / ".cache" / "kb",
            "embedding_provider": "hash",
            "embedding_dimension": 64,
            "chunk_size_tokens": 40,
            "chunk_hard_max_tokens": 60,
            "chunk_overlap_tokens": 8,
            "benchmark_questions_path": tmp_path / "evaluation" / "questions.json",
            "managed_tools_path": tmp_path / ".cache" / "kb" / "managed_tools.json",
            "mcp_servers_path": tmp_path / ".cache" / "kb" / "mcp_servers.json",
            "index_catalog_path": tmp_path / ".cache" / "kb" / "index_catalog.json",
            "managed_indexes_dir": tmp_path / ".cache" / "kb" / "indexes",
            "repository_cache_dir": tmp_path / ".cache" / "kb" / "repositories",
            "graph_store_path": tmp_path / ".cache" / "kb" / "system_graph.json",
            "service_map_path": tmp_path / ".cache" / "kb" / "service_map.json",
            "analysis_archive_dir": tmp_path / ".cache" / "kb" / "analysis",
            "job_logs_dir": tmp_path / ".cache" / "kb" / "job-logs",
            "ssot_skill_path": tmp_path / "skills" / "build-service-ssot",
            "repository_analysis_timeout_seconds": 30,
            "index_build_timeout_seconds": 30,
            "auto_index": False,
        }
        values.update(overrides)
        return Settings(**values).resolved()

    return factory
