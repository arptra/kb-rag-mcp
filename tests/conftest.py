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
            "auto_index": False,
        }
        values.update(overrides)
        return Settings(**values).resolved()

    return factory
