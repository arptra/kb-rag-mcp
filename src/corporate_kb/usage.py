"""Thread-safe, process-local usage counters for the admin dashboard."""

from __future__ import annotations

import time
from collections import Counter, deque
from datetime import UTC, datetime
from threading import Lock
from typing import Any


class UsageTracker:
    """Collect aggregate MCP usage without retaining queries or document text."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._started_at = datetime.now(UTC)
        self._last_used_at: datetime | None = None
        self._calls: Counter[str] = Counter()
        self._recent_calls: deque[float] = deque()
        self._total_context_tokens = 0
        self._total_results = 0

    def record(
        self,
        tool_name: str,
        *,
        context_tokens: int = 0,
        result_count: int = 0,
    ) -> None:
        with self._lock:
            self._calls[tool_name] += 1
            self._recent_calls.append(time.monotonic())
            self._total_context_tokens += max(0, context_tokens)
            self._total_results += max(0, result_count)
            self._last_used_at = datetime.now(UTC)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            recent_cutoff = time.monotonic() - 60
            while self._recent_calls and self._recent_calls[0] < recent_cutoff:
                self._recent_calls.popleft()
            search_count = self._calls.get("kb_search", 0)
            return {
                "started_at": self._started_at.isoformat(),
                "last_used_at": (
                    self._last_used_at.isoformat() if self._last_used_at is not None else None
                ),
                "total_calls": sum(self._calls.values()),
                "calls_last_minute": len(self._recent_calls),
                "calls_by_tool": dict(sorted(self._calls.items())),
                "search_count": search_count,
                "total_context_tokens": self._total_context_tokens,
                "average_context_tokens": (
                    round(self._total_context_tokens / search_count, 2) if search_count else 0.0
                ),
                "average_results": (
                    round(self._total_results / search_count, 2) if search_count else 0.0
                ),
            }
