"""Small explicit private memory, with advisory retrieval rather than model training."""

from __future__ import annotations

import builtins
import json
import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_harness.store import RunStore, timestamp

MAX_PER_OWNER = 200
MAX_TOTAL = 10_000
MAX_DRAFTS = 50
_STOP = frozenset(
    [
        "what",
        "how",
        "does",
        "this",
        "that",
        "with",
        "from",
        "into",
        "the",
        "and",
        "for",
        "are",
        "was",
        "were",
        "can",
        "not",
        "как",
        "что",
        "это",
        "где",
        "когда",
        "почему",
        "работает",
        "работаеть",
        "система",
        "системы",
        "вопрос",
        "ошибка",
        "для",
        "или",
        "при",
        "про",
        "все",
        "уже",
        "она",
        "они",
        "нам",
        "нас",
        "так",
        "есть",
        "нужно",
        "надо",
        "будет",
        "сейчас",
    ]
)


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=2400)
    kind: Literal["note", "correction", "lesson"] = "note"
    index_ids: list[str] = Field(min_length=1, max_length=1000)
    source_run_id: str | None = Field(default=None, max_length=80)
    confirmed: bool = False

    @field_validator("title", "text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Memory must not be blank")
        return value

    @field_validator("index_ids")
    @classmethod
    def valid_scope(cls, value: list[str]) -> builtins.list[str]:
        if any(not item.strip() or len(item) > 200 for item in value):
            raise ValueError("Invalid memory scope")
        return sorted(set(value))


class MemoryPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


def _terms(value: str) -> set[str]:
    return {word for word in re.findall(r"[\w-]{3,}", value.casefold()) if word not in _STOP}


class MemoryStore:
    """Uses the run database; every read/write includes the authenticated owner."""

    def __init__(self, runs: RunStore) -> None:
        self.runs = runs
        with runs._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                "status TEXT NOT NULL, source_run_id TEXT, updated TEXT NOT NULL, "
                "data TEXT NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS memories_owner ON memories(owner, updated)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS memory_preferences "
                "(owner TEXT PRIMARY KEY, enabled INTEGER NOT NULL)"
            )

    def enabled(self, owner: str) -> bool:
        with self.runs._connect() as db:
            row = db.execute(
                "SELECT enabled FROM memory_preferences WHERE owner=?", (owner,)
            ).fetchone()
        return bool(row[0]) if row else True

    def set_enabled(self, owner: str, enabled: bool) -> None:
        with self.runs._connect() as db:
            db.execute(
                "INSERT INTO memory_preferences VALUES (?,?) "
                "ON CONFLICT(owner) DO UPDATE SET enabled=excluded.enabled",
                (owner, int(enabled)),
            )

    def list(self, owner: str, allowed: set[str]) -> builtins.list[dict[str, Any]]:
        with self.runs._connect() as db:
            rows = db.execute(
                "SELECT data FROM memories WHERE owner=? ORDER BY updated DESC LIMIT ?",
                (owner, MAX_PER_OWNER),
            ).fetchall()
        return [item for row in rows if set((item := json.loads(row[0]))["index_ids"]) <= allowed]

    def get(self, owner: str, memory_id: str) -> dict[str, Any]:
        with self.runs._connect() as db:
            row = db.execute(
                "SELECT data FROM memories WHERE id=? AND owner=?", (memory_id, owner)
            ).fetchone()
        if row is None:
            raise KeyError("Memory not found")
        return dict(json.loads(row[0]))

    def save(
        self,
        owner: str,
        request: MemoryInput,
        *,
        memory_id: str | None = None,
        match_question: str = "",
    ) -> dict[str, Any]:
        existing = self.get(owner, memory_id) if memory_id else None
        now = timestamp()
        item = {
            "id": memory_id or uuid4().hex,
            **request.model_dump(exclude={"confirmed"}),
            "status": "active" if request.confirmed else "draft",
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
            # Matching text is private and never sent to another owner or to the UI.
            "_match_question": match_question[:2000] or (existing or {}).get("_match_question", ""),
        }
        with self.runs._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                request.source_run_id is not None
                and not db.execute(
                    "SELECT 1 FROM runs WHERE id=? AND owner=?", (request.source_run_id, owner)
                ).fetchone()
            ):
                raise KeyError("Run not found")
            if existing is None:
                count = db.execute(
                    "SELECT COUNT(*) FROM memories WHERE owner=?", (owner,)
                ).fetchone()[0]
                total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                if count >= MAX_PER_OWNER or total >= MAX_TOTAL:
                    raise ValueError("Память заполнена. Удалите ненужные записи перед сохранением.")
            db.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status,source_run_id=excluded.source_run_id,updated=excluded.updated,"
                "data=excluded.data WHERE memories.owner=excluded.owner",
                (
                    item["id"],
                    owner,
                    item["status"],
                    item["source_run_id"],
                    now,
                    json.dumps(item, ensure_ascii=False),
                ),
            )
        return self.public(item)

    def delete(self, owner: str, memory_id: str) -> None:
        with self.runs._connect() as db:
            deleted = db.execute("DELETE FROM memories WHERE id=? AND owner=?", (memory_id, owner))
            if not deleted.rowcount:
                raise KeyError("Memory not found")

    def retrieve(
        self, owner: str, question: str, allowed: set[str], selected: set[str]
    ) -> builtins.list[dict[str, Any]]:
        if not self.enabled(owner):
            return []
        query = _terms(question)
        if not query:
            return []
        ranked: list[tuple[int, dict[str, Any]]] = []
        for item in self.list(owner, allowed & selected):
            if item["status"] != "active":
                continue
            words = _terms(f"{item['title']} {item['text']} {item['_match_question']}")
            score = len(query & words)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (pair[0], pair[1]["updated_at"]), reverse=True)
        remaining = 2400
        result = []
        for _, item in ranked[:3]:
            text = item["text"][: min(1000, remaining)]
            if not text:
                break
            result.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "text": text,
                    "kind": item["kind"],
                    "index_ids": item["index_ids"],
                }
            )
            remaining -= len(text)
        return result

    def record_failure(
        self, owner: str, run: dict[str, Any], *, operation: str, index_ids: builtins.list[str]
    ) -> None:
        """Only fixed labels; never persist exception text, tool args or credentials."""
        labels = {
            "search": "Поиск источника",
            "read": "Чтение документа",
            "external": "Внешний инструмент",
            "invalid": "Проверка вызова инструмента",
        }
        if operation not in labels or not index_ids or not self.enabled(owner):
            return
        title = f"{labels[operation]}: требуется проверка"
        with self.runs._connect() as db:
            rows = db.execute(
                "SELECT data FROM memories WHERE owner=? AND status='draft'", (owner,)
            ).fetchall()
        drafts = [json.loads(row[0]) for row in rows]
        if len(drafts) >= MAX_DRAFTS or any(
            item["source_run_id"] == run["id"] and item["title"] == title for item in drafts
        ):
            return
        try:
            self.save(
                owner,
                MemoryInput(
                    title=title,
                    text=(
                        "Операция в этом исследовании не завершилась. Причина не подтверждена. "
                        "Проверьте доступ и параметры, затем впишите проверенное решение. "
                        "До вашего подтверждения эта запись не используется в ответах."
                    ),
                    kind="lesson",
                    index_ids=index_ids,
                    source_run_id=run["id"],
                    confirmed=False,
                ),
                match_question=run["question"],
            )
        except (ValueError, KeyError):
            # Full memory or a concurrently removed run never interrupts research.
            return

    @staticmethod
    def public(item: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in item.items() if not key.startswith("_")}
