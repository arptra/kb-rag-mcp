"""Storage boundary for versioned graph snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from gigacode_graph.models import GraphSnapshot


class GraphStore(Protocol):
    def load(self) -> GraphSnapshot: ...

    def save(self, snapshot: GraphSnapshot) -> None: ...

    def revision(self) -> int: ...


class JsonGraphStore:
    """Atomic, inspectable filesystem store used by the first implementation."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> GraphSnapshot:
        if not self.path.is_file():
            raise RuntimeError(
                f"Graph index is missing: {self.path}. Run: gigacode-graph index ..."
            )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return GraphSnapshot.model_validate(payload)

    def save(self, snapshot: GraphSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def revision(self) -> int:
        return self.path.stat().st_mtime_ns
