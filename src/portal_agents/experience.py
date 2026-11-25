"""Shared transport experience, partitioned by model connection and free of conversation data."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import TypedDict
from urllib.parse import urlsplit

from portal_agents.model_settings import ModelProfile

_CONFIRMED = {"native_to_read_success", "read_to_native_success", "long_read_success"}
_OUTCOMES = _CONFIRMED | {
    "read_success", "native_success", "read_format", "native_schema",
    "read_length", "native_incomplete", "policy",
}
_KEY = re.compile(r"[a-f0-9]{64}")
_TTL = 30 * 24 * 60 * 60
# Each allowance applies separately to confirmed recoveries and other outcomes.
_PROFILE_LIMIT = 64
_GLOBAL_LIMIT = 2048
_REQUIRED_TURNS = 2


def profile_key(profile: ModelProfile, *, config_identity: str = "") -> str:
    """Identify the active transport. Never read credential values or credential files."""
    identity: list[object] = [profile.provider, config_identity]
    if profile.provider == "api":
        address: tuple[str, str | None, int | None, str]
        try:
            endpoint = urlsplit(profile.base_url.strip())
            port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
            path = endpoint.path.rstrip("/").removesuffix("/chat/completions")
            address = (endpoint.scheme, endpoint.hostname, port, path)
        except ValueError:
            # A malformed saved endpoint can still be inspected in the admin settings.
            address = ("invalid", None, None, profile.base_url)
        identity.extend((profile.adapter, address, profile.model_name, profile.disable_thinking,
                         profile.output_tokens, profile.timeout_seconds))
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class StrategyPreferences:
    prefer_chat_reads: bool | None = None
    larger_read_answer: bool = False


class ExperienceSummary(TypedDict):
    turns: int
    outcomes: dict[str, int]
    prefer_chat_reads: bool | None
    larger_read_answer: bool


class ExperienceStore:
    """Record one row per reported turn, with no chat identifier or user-supplied strings."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._initialization = Lock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            with self._initialization:
                if not self._ready:
                    self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
                    with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                        # Serialize schema creation across portal workers on the first request.
                        db.execute("BEGIN IMMEDIATE")
                        db.execute(
                            """CREATE TABLE IF NOT EXISTS strategy_turns (
                                id INTEGER PRIMARY KEY, profile TEXT NOT NULL,
                                recorded_at INTEGER NOT NULL, confirmed INTEGER NOT NULL,
                                outcomes TEXT NOT NULL)"""
                        )
                        db.execute("CREATE INDEX IF NOT EXISTS strategy_profile "
                                   "ON strategy_turns(profile, confirmed, id)")
                        db.execute("CREATE INDEX IF NOT EXISTS strategy_recent "
                                   "ON strategy_turns(confirmed, id)")
                        db.execute("CREATE INDEX IF NOT EXISTS strategy_expiry "
                                   "ON strategy_turns(recorded_at)")
                    self._ready = True
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("Expected an opaque model profile fingerprint")

    def record(self, profile_key: str, outcome: str) -> None:
        """Record a whole turn that has just one outcome."""
        self.record_turn(profile_key, (outcome,))

    def record_turn(self, profile_key: str, outcomes: Iterable[str]) -> None:
        """Atomically record all outcomes of one turn. Duplicate observations count once."""
        self._validate_key(profile_key)
        if isinstance(outcomes, (str, bytes)):
            raise ValueError("Expected a collection of model outcome codes")
        seen = set()
        for code in outcomes:
            if not isinstance(code, str) or code not in _OUTCOMES:
                raise ValueError("Unknown model outcome code")
            seen.add(code)
        if not seen:
            return
        if "policy" in seen:
            seen.difference_update(_CONFIRMED)
        confirmed = int(bool(seen & _CONFIRMED))
        now = int(time.time())
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM strategy_turns WHERE recorded_at <= ?", (now - _TTL,))
            db.execute(
                "INSERT INTO strategy_turns(profile, recorded_at, confirmed, outcomes) "
                "VALUES (?, ?, ?, ?)", (profile_key, now, confirmed, json.dumps(sorted(seen))),
            )
            # Failed/ordinary calls cannot evict confirmed recoveries through journal noise.
            db.execute(
                """DELETE FROM strategy_turns WHERE profile=? AND confirmed=? AND id <= (
                    SELECT id FROM strategy_turns WHERE profile=? AND confirmed=?
                    ORDER BY id DESC LIMIT 1 OFFSET ?)""",
                (profile_key, confirmed, profile_key, confirmed, _PROFILE_LIMIT),
            )
            db.execute(
                """DELETE FROM strategy_turns WHERE confirmed=? AND id <= (
                    SELECT id FROM strategy_turns WHERE confirmed=? ORDER BY id DESC
                    LIMIT 1 OFFSET ?)""", (confirmed, confirmed, _GLOBAL_LIMIT),
            )

    def _snapshot(self, profile_key: str) -> tuple[int, dict[str, int], StrategyPreferences]:
        self._validate_key(profile_key)
        since = int(time.time()) - _TTL
        with closing(self._connect()) as db:
            rows = db.execute("SELECT outcomes FROM strategy_turns "
                              "WHERE profile=? AND recorded_at>?", (profile_key, since))
            counts = dict.fromkeys(sorted(_OUTCOMES), 0)
            turns = 0
            for (raw,) in rows:
                try:
                    codes = set(json.loads(raw))
                except (ValueError, TypeError, RecursionError):
                    raise sqlite3.DatabaseError("Invalid model experience record") from None
                turns += 1
                for code in codes & _OUTCOMES:
                    counts[code] += 1
        read, native = counts["native_to_read_success"], counts["read_to_native_success"]
        preference = None
        if read >= _REQUIRED_TURNS and not native:
            preference = True
        elif native >= _REQUIRED_TURNS and not read:
            preference = False
        return turns, counts, StrategyPreferences(
            preference, counts["long_read_success"] >= _REQUIRED_TURNS,
        )

    def preferences(self, profile_key: str) -> StrategyPreferences:
        """Use recent completed recoveries. Conflicting transport directions stay neutral."""
        return self._snapshot(profile_key)[2]

    def summary(self, profile_key: str) -> ExperienceSummary:
        """Return only retained turn counts, fixed outcome counts and computed preferences."""
        turns, counts, prefs = self._snapshot(profile_key)
        return {"turns": turns, "outcomes": counts, "prefer_chat_reads": prefs.prefer_chat_reads,
                "larger_read_answer": prefs.larger_read_answer}
