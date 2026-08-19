"""Atomic JSON persistence for service map snapshots."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

from service_map.models import ServiceMapSnapshot


class ServiceMapStore(Protocol):
    def load(self) -> ServiceMapSnapshot: ...

    def save(self, snapshot: ServiceMapSnapshot) -> None: ...

    def revision(self) -> int: ...


class JsonServiceMapStore:
    """Keep the latest service map in a human-readable, atomically replaced file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ServiceMapSnapshot:
        if not self.path.is_file():
            return ServiceMapSnapshot()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return ServiceMapSnapshot.model_validate(payload)

    def save(self, snapshot: ServiceMapSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = snapshot.model_dump_json(indent=2).encode("utf-8")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def revision(self) -> int:
        return self.path.stat().st_mtime_ns if self.path.is_file() else 0
