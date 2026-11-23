"""Private notes and automatically collected, explicitly unverified run experience."""

from __future__ import annotations

import builtins
import hashlib
import json
import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_harness.store import RunStore, timestamp

MAX_PER_OWNER = 200
MAX_TOTAL = 10_000
MAX_FAILURES = 12
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
    kind: Literal["note", "correction", "lesson", "case"] = "note"
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


class MemoryFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    rating: Literal["helpful", "unhelpful"]
    comment: str = Field(default="", max_length=1000)

    @field_validator("comment")
    @classmethod
    def trim_comment(cls, value: str) -> str:
        return value.strip()


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
            db.execute(
                "CREATE TABLE IF NOT EXISTS memory_usages "
                "(memory_id TEXT NOT NULL, run_id TEXT NOT NULL, owner TEXT NOT NULL, "
                "PRIMARY KEY(memory_id, run_id))"
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
        for key in (
            "origin",
            "outcome",
            "feedback",
            "failures",
            "sources",
            "strategy",
            "usage_count",
            "last_used_at",
            "_last_used_run_id",
        ):
            if existing and key in existing:
                item[key] = existing[key]
        item.setdefault("origin", "manual")
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
            db.execute(
                "DELETE FROM memory_usages WHERE memory_id=? AND owner=?", (memory_id, owner)
            )

    def retrieve(
        self,
        owner: str,
        question: str,
        allowed: set[str],
        selected: set[str],
        *,
        exclude_run_id: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        if not self.enabled(owner):
            return []
        query = _terms(question)
        if not query:
            return []
        ranked: list[tuple[int, dict[str, Any]]] = []
        for item in self.list(owner, allowed & selected):
            if exclude_run_id is not None and item.get("source_run_id") == exclude_run_id:
                continue
            if item["status"] not in {"active", "experience"}:
                continue
            words = _terms(f"{item['title']} {item['text']} {item.get('_match_question', '')}")
            score = len(query & words)
            if score:
                # Explicit feedback must not disappear behind repeated unreviewed runs.
                rating = (item.get("feedback") or {}).get("rating")
                priority = (
                    3
                    if item["status"] == "active"
                    else 2
                    if rating == "unhelpful"
                    else 1
                    if rating == "helpful"
                    else 0
                )
                ranked.append((score * 4 + priority, item))
        ranked.sort(key=lambda pair: (pair[0], pair[1]["updated_at"]), reverse=True)
        remaining = 2400
        result = []
        for _, item in ranked[:3]:
            prefix = ""
            if item["status"] == "experience":
                prefix = "НЕПОДТВЕРЖДЁННЫЙ ОПЫТ. Это подсказка для проверки, не источник фактов. "
                if (item.get("feedback") or {}).get("rating") == "unhelpful":
                    prefix += (
                        "Пользователь отметил прошлый ответ как неудачный. "
                        "Повторять этот ответ нельзя. "
                    )
            text = (prefix + item["text"])[: min(1000, remaining)]
            if not text:
                break
            result.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "text": text,
                    "kind": item["kind"],
                    "index_ids": item["index_ids"],
                    "status": item["status"],
                    "origin": item.get("origin", "manual"),
                    "feedback": item.get("feedback"),
                    "usage_count": item.get("usage_count", 0),
                    "last_used_at": item.get("last_used_at"),
                }
            )
            remaining -= len(text)
        return result

    def record_usage(self, owner: str, memory_ids: builtins.list[str], run_id: str) -> None:
        """Count at most once per consuming run, without refreshing retention order."""
        now = timestamp()
        with self.runs._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM runs WHERE id=? AND owner=?", (run_id, owner)
            ).fetchone():
                return
            for memory_id in set(memory_ids):
                row = db.execute(
                    "SELECT data FROM memories WHERE id=? AND owner=?", (memory_id, owner)
                ).fetchone()
                if row is None:
                    continue
                item = json.loads(row[0])
                inserted = db.execute(
                    "INSERT OR IGNORE INTO memory_usages VALUES (?,?,?)",
                    (memory_id, run_id, owner),
                )
                if not inserted.rowcount:
                    continue
                item.update(
                    usage_count=item.get("usage_count", 0) + 1,
                    last_used_at=now,
                )
                db.execute(
                    "UPDATE memories SET data=? WHERE id=? AND owner=?",
                    (json.dumps(item, ensure_ascii=False), memory_id, owner),
                )

    @staticmethod
    def _automatic_id(owner: str, run_id: str) -> str:
        return "auto_" + hashlib.sha256(f"{owner}\0{run_id}".encode()).hexdigest()[:48]

    def _automatic(self, owner: str, run_id: str) -> dict[str, Any] | None:
        try:
            return self.get(owner, self._automatic_id(owner, run_id))
        except KeyError:
            return None

    def record_outcome(
        self, owner: str, run: dict[str, Any], *, index_ids: builtins.list[str]
    ) -> dict[str, Any] | None:
        """Save one bounded case per finished run; successful completion is not verification."""
        if run.get("status") not in {"completed", "partial", "failed", "timed_out", "interrupted"}:
            return None
        if not self.enabled(owner) or not index_ids:
            return None
        existing = self._automatic(owner, run["id"])
        if existing and existing["status"] == "active":
            return self.public(existing)
        return self._save_experience(owner, run, index_ids=index_ids, existing=existing)

    def record_feedback(
        self,
        owner: str,
        run: dict[str, Any],
        *,
        rating: Literal["helpful", "unhelpful"],
        comment: str = "",
        index_ids: builtins.list[str],
    ) -> dict[str, Any] | None:
        feedback = MemoryFeedback(rating=rating, comment=comment)
        existing = self._automatic(owner, run["id"])
        if not index_ids or (not self.enabled(owner) and existing is None):
            return None
        if run.get("status") in {"queued", "running", "cancelling"}:
            raise ValueError("Сначала дождитесь завершения исследования.")
        # Feedback corrects previously collected experience even while reuse is disabled.
        updated = {**run, "feedback": {**feedback.model_dump(), "updated_at": timestamp()}}
        return self._save_experience(owner, updated, index_ids=index_ids, existing=existing)

    def _save_experience(
        self,
        owner: str,
        run: dict[str, Any],
        *,
        index_ids: builtins.list[str],
        existing: dict[str, Any] | None,
        failures: builtins.list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        now = timestamp()
        failures = failures if failures is not None else (existing or {}).get("failures", [])
        feedback = run.get("feedback") or (existing or {}).get("feedback")
        bad = (feedback or {}).get("rating") == "unhelpful"
        outcome = run.get("status", "failed")
        incomplete = outcome != "completed"
        question = str(run.get("question", ""))[:2000]
        sources = [
            {
                "id": str(source.get("id", ""))[:200],
                "index_id": str(source.get("index_id", ""))[:200],
                "title": str(source.get("title", ""))[:160],
            }
            for source in run.get("evidence", [])[:8]
        ]
        strategy = {
            "mode": run.get("mode", ""),
            "effort": run.get("effort", ""),
            **{
                key: int(run.get("metrics", {}).get(key, 0))
                for key in ("source_calls", "tool_calls", "model_calls")
            },
            "tools": list(
                dict.fromkeys(
                    str(step.get("name", ""))[:100]
                    for step in run.get("research_trace", [])
                    if isinstance(step, dict) and step.get("name")
                )
            )[:20],
            "coverage": {
                str(index_id)[:200]: {
                    "status": value.get("status", "pending"),
                    "count": int(value.get("count", 0)),
                }
                for index_id, value in list(run.get("coverage", {}).items())[:100]
                if isinstance(value, dict)
            },
        }
        text = f"Предыдущий вопрос: {question[:500]}\n"
        if bad:
            text += (
                "Пользователь указал, что ответ не помог. "
                "Найдите новые подтверждения и проверьте вывод заново.\n"
            )
        elif incomplete:
            text += "Прошлый запуск не дал полного результата; вывод нельзя считать решением.\n"
        else:
            text += (
                "Прошлый запуск завершился; правильность вывода "
                "нужно проверить по текущим источникам.\n"
            )
        if feedback and feedback.get("comment"):
            text += (
                "Обратная связь пользователя (также требует проверки): "
                f"{feedback['comment'][:1000]}\n"
            )
        if strategy["tools"]:
            text += (
                "Ранее использованные инструменты: " + ", ".join(strategy["tools"])[:250] + ".\n"
            )
        recommendations = {
            "search": "Перепроверьте доступ к индексу и повторите поиск другим запросом.",
            "read": "Повторно найдите документ и проверьте актуальный идентификатор и доступ.",
            "external": (
                "Перепроверьте доступность и настройку внешнего инструмента, затем повторите вызов."
            ),
            "invalid": (
                "Сверьте имя инструмента, схему аргументов и выбранный индекс перед вызовом."
            ),
        }
        for failure in failures[:MAX_FAILURES]:
            indexes = ", ".join(failure["index_ids"][:3])[:240]
            text += f"{failure['operation']} [{indexes}]: {recommendations[failure['operation']]}\n"
        if sources:
            text += (
                "Ранее найденные документы (проверить заново): "
                + "; ".join(
                    f"{source['index_id']}/{source['id']} — {source['title']}"
                    for source in sources[:4]
                )[:500]
                + "\n"
            )
        if not bad and run.get("answer"):
            text += "Предыдущий вывод (не подтверждённое знание): " + str(run["answer"])[:1000]
        item = {
            "id": self._automatic_id(owner, run["id"]),
            "title": ("Урок: " if bad or incomplete or failures else "Опыт: ") + question[:150],
            "text": text[:2400],
            "kind": "lesson" if bad or incomplete or failures else "case",
            "index_ids": sorted(
                set(index_ids)
                | set(run.get("index_ids", []))
                | set(run.get("context_index_ids", []))
                | {source["index_id"] for source in run.get("evidence", [])}
                | set((existing or {}).get("index_ids", []))
            ),
            "source_run_id": run["id"],
            "status": "experience",
            "origin": "automatic",
            "outcome": outcome,
            "feedback": feedback,
            "failures": failures[:MAX_FAILURES],
            "sources": sources,
            "strategy": strategy,
            "created_at": (existing or {}).get("created_at", now),
            "updated_at": now,
            "usage_count": (existing or {}).get("usage_count", 0),
            "last_used_at": (existing or {}).get("last_used_at"),
            "_match_question": question,
        }
        with self.runs._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM runs WHERE id=? AND owner=?", (run["id"], owner)
            ).fetchone():
                return None
            if existing is None:
                count = db.execute(
                    "SELECT COUNT(*) FROM memories WHERE owner=?", (owner,)
                ).fetchone()[0]
                total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                if count >= MAX_PER_OWNER or total >= MAX_TOTAL:
                    # Rotate old automatic experience only; never discard a person's notes.
                    rows = db.execute(
                        "SELECT id,owner,data FROM memories WHERE status='experience' "
                        "ORDER BY updated ASC"
                    ).fetchall()
                    candidate = next(
                        (
                            (key, item_owner)
                            for key, item_owner, data in rows
                            if (count < MAX_PER_OWNER or item_owner == owner)
                            and json.loads(data).get("origin") == "automatic"
                        ),
                        None,
                    )
                    if candidate is None:
                        return None
                    db.execute("DELETE FROM memories WHERE id=? AND owner=?", candidate)
                    db.execute("DELETE FROM memory_usages WHERE memory_id=? AND owner=?", candidate)
            db.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status,updated=excluded.updated,data=excluded.data "
                "WHERE memories.owner=excluded.owner",
                (
                    item["id"],
                    owner,
                    item["status"],
                    run["id"],
                    now,
                    json.dumps(item, ensure_ascii=False),
                ),
            )
        return self.public(item)

    def record_failure(
        self, owner: str, run: dict[str, Any], *, operation: str, index_ids: builtins.list[str]
    ) -> None:
        """Keep operation/scope and next checks, never exception text or tool arguments."""
        if (
            operation not in {"search", "read", "external", "invalid"}
            or not index_ids
            or not self.enabled(owner)
        ):
            return
        existing = self._automatic(owner, run["id"])
        if existing and existing["status"] == "active":
            return
        failures = (existing or {}).get("failures", [])
        failure = {"operation": operation, "index_ids": sorted(set(index_ids))}
        if failure in failures or len(failures) >= MAX_FAILURES:
            return
        scope = (
            set(index_ids)
            | set(run.get("index_ids", []))
            | set(run.get("context_index_ids", []))
            | {source["index_id"] for source in run.get("evidence", [])}
        )
        self._save_experience(
            owner, run, index_ids=sorted(scope), existing=existing, failures=[*failures, failure]
        )

    @staticmethod
    def public(item: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in item.items() if not key.startswith("_")}
