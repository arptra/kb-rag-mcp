"""Persistent, safe description overrides for code-backed MCP tools."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import Lock

from pydantic import BaseModel, ConfigDict, Field, field_validator

BUILTIN_TOOL_NAMES = frozenset(
    {
        "ssot_context",
        "kb_feature_context",
        "kb_system_graph",
        "kb_search_index",
        "kb_generate_system_ssot",
        "kb_search",
        "kb_get_document",
        "kb_get_chunk",
        "kb_run_context_benchmark",
        "kb_list_documents",
        "kb_stats",
    }
)


class BuiltinToolOverride(BaseModel):
    """Only model-facing text is editable; executable code and schema stay code-backed."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = Field(min_length=10, max_length=4000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value not in BUILTIN_TOOL_NAMES:
            raise ValueError(f"Unknown built-in MCP tool: {value}")
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 10:
            raise ValueError("Tool description must contain at least 10 characters")
        return value


class BuiltinToolOverrideRegistry:
    """Persist description overrides atomically and expose immutable copies."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._lock = Lock()
        self._definitions = self._load()

    def list(self) -> list[BuiltinToolOverride]:
        with self._lock:
            return [
                self._definitions[name].model_copy(deep=True)
                for name in sorted(self._definitions)
            ]

    def description_for(self, name: str, default: str) -> str:
        with self._lock:
            definition = self._definitions.get(name)
            return definition.description if definition is not None else default

    def upsert(self, definition: BuiltinToolOverride) -> BuiltinToolOverride:
        with self._lock:
            self._definitions[definition.name] = definition
            self._save_locked()
        return definition.model_copy(deep=True)

    def _load(self) -> dict[str, BuiltinToolOverride]:
        if not self._path.is_file():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                return {}
            definitions = [BuiltinToolOverride.model_validate(item) for item in payload]
            return {item.name: item for item in definitions}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [
                self._definitions[name].model_dump(mode="json")
                for name in sorted(self._definitions)
            ],
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self._path.parent, delete=False) as handle:
                temporary_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
