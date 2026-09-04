"""Atomic, append-friendly artifact writer for reproducible graph experiments."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class LabArtifacts:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.events_path = self.directory / "events.jsonl"

    @staticmethod
    def new_run_id(case_id: str) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        token = os.urandom(4).hex()
        return f"{timestamp}-{case_id}-{token}"

    def json(self, name: str, value: Any) -> Path:
        return self._atomic(
            name,
            json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        )

    def text(self, name: str, value: str) -> Path:
        return self._atomic(name, value)

    def event(self, stage: str, message: str, **details: Any) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "stage": stage,
            "message": message,
            "details": details,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _atomic(self, name: str, value: str) -> Path:
        destination = self.directory / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return destination
