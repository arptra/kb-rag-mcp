"""Shared system knowledge and unverified experience, filtered by source permissions."""

from __future__ import annotations

import builtins
import hashlib
import json
import re
import sqlite3
from collections import Counter
from typing import Any, Literal, TypedDict
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_harness.answer_quality import answer_for_history, cited_evidence_ids, is_answer_source
from agent_harness.control.settings import ControlStore
from agent_harness.store import RunStore, timestamp

LIST_LIMIT = 200
MAX_TOTAL = 10_000
MAX_FAILURES = 12
_SOURCE_TOOLS = frozenset({
    "search_knowledge", "read_document", "search_repository", "read_repository",
    "run_in_sandbox", "mcp_call", "a2a_ask",
})
_ATTEMPT_TOOLS = _SOURCE_TOOLS | {
    "clone_repository", "write_analysis_script", "mcp_list_tools", "mcp_tool_schema",
}
_PREVIOUS_CONCLUSION = "Предыдущий вывод (не подтверждённое знание): "
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


class MemoryPage(TypedDict):
    items: list[dict[str, Any]]
    total: int
    next_offset: int | None


def _terms(value: str) -> set[str]:
    return {word for word in re.findall(r"[\w-]{3,}", value.casefold()) if word not in _STOP}


def _source_choices(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Attribute only retained source material to the tool that actually produced it."""
    evidence = run.get("evidence", [])
    choices: dict[tuple[str, str, str], dict[str, Any]] = {}
    candidates = []
    for item in evidence:
        if (item.get("tool_outcome") in {"failed", "empty"} or not item.get("id")
                or not is_answer_source(item)):
            continue
        origin = item.get("tool_origin")
        if origin is None and not item["index_id"].startswith(("connection:", "repo:")):
            # These two fields were stamped by the RAG runtime before tool origins existed.
            # Catalog receipts, trace text or answer prose cannot establish provenance.
            tool = "read_document" if item.get("read_document") else item.get("checked_with")
            if tool in {"read_document", "search_knowledge"}:
                origin = {"tool": tool, "index_id": item["index_id"]}
        if isinstance(origin, dict):
            candidates.append((item, origin))
    if not candidates:
        return []
    if run["status"] == "completed" or run.get("_accepted_with_warnings"):
        cited = cited_evidence_ids(
            run.get("answer", ""), evidence, question=run.get("question", ""),
        )
        counts = Counter(item.get("id") for item in evidence)
        candidates = [(item, origin) for item, origin in candidates
                      if item["id"] in cited and counts[item["id"]] == 1]
    for item, origin in candidates:
        tool, index = origin.get("tool"), origin.get("index_id")
        if tool not in _SOURCE_TOOLS or index != item["index_id"]:
            continue
        remote = origin.get("remote_tool", "") if tool == "mcp_call" else ""
        if tool == "mcp_call" and not (isinstance(remote, str) and 0 < len(remote) <= 200):
            continue
        key = (tool, index, remote)
        if key not in choices:
            if len(choices) >= 8:
                continue
            choices[key] = {"tool": tool, "index_id": index, "evidence_ids": []}
            if remote:
                choices[key]["remote_tool"] = remote
        ids = choices[key]["evidence_ids"]
        if len(ids) < 3 and item["id"] not in ids:
            ids.append(item["id"])
            if (tool == "read_document" and item.get("document_id")
                    and item["document_id"] != item["id"]):
                choices[key].setdefault("documents", {})[item["id"]] = item["document_id"]
    return list(choices.values())


def _route_keys(choices: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    keys = set()
    for choice in choices:
        name, index = choice.get("tool", ""), choice.get("index_id", "")
        # A Jira lookup can resolve two different issues. For document reads the
        # document must match too; two unrelated pages in one index prove nothing.
        if name == "mcp_call" and choice.get("remote_tool"):
            keys.add((name, index, choice["remote_tool"]))
        else:
            for ref in choice.get("evidence_ids", []):
                keys.add((name, index, choice.get("documents", {}).get(ref, ref)))
    return keys


class MemoryStore:
    """Shared reads use source ACLs; the author alone can edit or delete a memory."""

    def __init__(self, runs: RunStore) -> None:
        self.runs = runs
        self.controls = ControlStore(runs.settings)
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
            db.execute(
                "CREATE TABLE IF NOT EXISTS memory_outcomes "
                "(memory_id TEXT NOT NULL, run_id TEXT NOT NULL, owner TEXT NOT NULL, "
                "status TEXT NOT NULL, rating TEXT NOT NULL, PRIMARY KEY(memory_id,run_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS memory_route_reuses (memory_id TEXT NOT NULL, "
                "run_id TEXT NOT NULL, owner TEXT NOT NULL, PRIMARY KEY(memory_id,run_id))"
            )
            # Older installations recorded a partial case for every gateway block. A
            # run's typed terminal reason lets us keep these out of future planning.
            db.execute(
                "UPDATE memories AS m SET data=json_set(m.data,'$.stop_reason',"
                "json_extract(r.data,'$.stop_reason')) FROM runs AS r "
                "WHERE m.owner=r.owner AND m.source_run_id=r.id AND m.status='experience' "
                "AND r.status IN ('partial','failed') "
                "AND json_valid(m.data) AND json_valid(r.data) "
                "AND json_extract(m.data,'$.origin')='automatic' "
                "AND json_type(m.data,'$.stop_reason') IS NULL "
                "AND json_extract(r.data,'$.stop_reason') IN "
                "('model_policy_stop','model_refusal_text')"
            )
        self._backfill_tool_choices()

    def _backfill_tool_choices(self) -> None:
        """Recover provable choices for existing cases while their private producer still exists."""
        cursor = ""
        while True:
            with self.runs._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute(
                    "SELECT m.id FROM memories m JOIN runs r "
                    "ON r.owner=m.owner AND r.id=m.source_run_id "
                    "WHERE m.id>? AND m.status='experience' AND r.status='completed' "
                    "AND json_valid(m.data) "
                    "AND json_extract(m.data,'$.origin')='automatic' "
                    "AND json_type(m.data,'$.strategy.successful_tools') IS NULL "
                    "ORDER BY m.id LIMIT 16", (cursor,),
                ).fetchall()
                if not rows:
                    return
                for (memory_id,) in rows:
                    card_data, run_data = db.execute(
                        "SELECT m.data,r.data FROM memories m JOIN runs r "
                        "ON r.owner=m.owner AND r.id=m.source_run_id WHERE m.id=?", (memory_id,),
                    ).fetchone()
                    try:
                        card, producer = json.loads(card_data), json.loads(run_data)
                        scope = (set(producer.get("index_ids", []))
                                 | set(producer.get("context_index_ids", []))
                                 | {e["index_id"] for e in producer.get("evidence", [])})
                        choices = []
                        if (card.get("outcome") == "completed"
                                and card.get("source_run_id") == producer.get("id")
                                and scope <= set(card["index_ids"])
                                and not producer.get("_final_quality_issue")
                                and all((value.get("feedback") or {}).get("rating") != "unhelpful"
                                        for value in (producer, card))):
                            choices = _source_choices(producer)
                        card.setdefault("strategy", {})["successful_tools"] = choices
                    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
                        # An incompatible historical record is not evidence of a tool choice.
                        continue
                    db.execute("UPDATE memories SET data=? WHERE id=?",
                               (json.dumps(card, ensure_ascii=False), memory_id))
                cursor = rows[-1][0]

    def enabled(self, owner: str) -> bool:
        with self.runs._connect() as db:
            row = db.execute(
                "SELECT enabled FROM memory_preferences WHERE owner=?", (owner,)
            ).fetchone()
        return bool(row[0]) if row else True

    def maximum(self) -> int:
        policy = self.controls.load()
        return policy.runtime.memory_max_records if policy.revision else MAX_TOTAL

    def set_enabled(self, owner: str, enabled: bool) -> None:
        with self.runs._connect() as db:
            db.execute(
                "INSERT INTO memory_preferences VALUES (?,?) "
                "ON CONFLICT(owner) DO UPDATE SET enabled=excluded.enabled",
                (owner, int(enabled)),
            )

    def list(
        self, owner: str, allowed: set[str], *, offset: int = 0
    ) -> builtins.list[dict[str, Any]]:
        return self.page(owner, allowed, offset=offset)["items"]

    def page(self, owner: str, allowed: set[str], *, offset: int = 0) -> MemoryPage:
        """Paginate permitted records so restricted entries never affect counts or offsets."""
        if offset < 0:
            raise ValueError("Memory offset must be nonnegative")
        visible = self._visible(owner, allowed, include_drafts=True)
        next_offset = offset + LIST_LIMIT
        return {
            "items": visible[offset:next_offset],
            "total": len(visible),
            "next_offset": next_offset if next_offset < len(visible) else None,
        }

    def _visible(
        self, owner: str, allowed: set[str], *, include_drafts: bool = False
    ) -> builtins.list[dict[str, Any]]:
        """Read the shared pool before filtering/ranking, not just the UI's latest page."""
        with self.runs._connect() as db:
            rows = db.execute(
                "SELECT owner,data FROM memories WHERE status IN ('active','experience') "
                "OR (status='draft' AND owner=? AND ?) ORDER BY updated DESC,id DESC",
                (owner, include_drafts),
            ).fetchall()
        return [
            {**item, "_author": author, "_reader": owner}
            for author, data in rows
            if set((item := json.loads(data))["index_ids"]) <= allowed
        ]

    def get(self, owner: str, memory_id: str) -> dict[str, Any]:
        with self.runs._connect() as db:
            row = db.execute(
                "SELECT data FROM memories WHERE id=? AND owner=?", (memory_id, owner)
            ).fetchone()
        if row is None:
            raise KeyError("Memory not found")
        return {**json.loads(row[0]), "_author": owner, "_reader": owner}

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
            # Matching text stays internal; source permissions also cover its use in ranking.
            "_match_question": match_question[:2000] or (existing or {}).get("_match_question", ""),
        }
        for key in (
            "origin",
            "outcome",
            "feedback",
            "failures",
            "sources",
            "strategy",
            "research_plan",
            "quality",
            "usage_count",
            "route_reuses",
            "stop_reason",
            "last_used_at",
            "_last_used_run_id",
        ):
            if existing and key in existing:
                item[key] = existing[key]
        item.setdefault("origin", "manual")
        if existing and item["origin"] == "automatic":
            item["index_ids"] = sorted(set(item["index_ids"]) | set(existing["index_ids"]))
        with self.runs._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                request.source_run_id is not None
                and not db.execute(
                    "SELECT 1 FROM runs WHERE id=? AND owner=?", (request.source_run_id, owner)
                ).fetchone()
            ):
                raise KeyError("Run not found")
            if existing is None and not self._make_room(db):
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
        return self.public({**item, "_author": owner, "_reader": owner})

    def delete(self, owner: str, memory_id: str) -> None:
        with self.runs._connect() as db:
            deleted = db.execute("DELETE FROM memories WHERE id=? AND owner=?", (memory_id, owner))
            if not deleted.rowcount:
                raise KeyError("Memory not found")
            db.execute("DELETE FROM memory_usages WHERE memory_id=?", (memory_id,))
            db.execute("DELETE FROM memory_outcomes WHERE memory_id=?", (memory_id,))
            db.execute("DELETE FROM memory_route_reuses WHERE memory_id=?", (memory_id,))

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
        for item in self._visible(owner, allowed & selected):
            if exclude_run_id is not None and item.get("source_run_id") == exclude_run_id:
                continue
            if item["status"] not in {"active", "experience"}:
                continue
            if (item["status"] == "experience"
                    and item.get("stop_reason") in {"model_policy_stop", "model_refusal_text"}
                    and not item.get("feedback")):
                # Keep the incident visible in SQLite/UI. Repeated gateway stops do not
                # teach a search strategy and must not crowd verified routes out of retrieval.
                continue
            if item.get("origin") == "automatic" and item["status"] == "experience":
                preceding, marker, previous = item["text"].rpartition("\n" + _PREVIOUS_CONCLUSION)
                if marker and previous and not answer_for_history(previous):
                    # Legacy cases retain their feedback and research history. Only
                    # exclude the old model notice from retrieval and term matching.
                    item = {**item, "text": preceding}
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
                quality = self._quality(item)
                # Relevance remains primary. Ratings break ties; completion alone is not truth.
                rating_bonus = max(-3, min(3, quality["helpful"] - quality["unhelpful"]))
                ranked.append((score * 16 + priority * 2 + rating_bonus, item))
        ranked.sort(key=lambda pair: (
            pair[0], min(5, pair[1].get("route_reuses", 0)),
            bool(pair[1].get("strategy", {}).get("successful_tools")),
            pair[1]["updated_at"],
        ), reverse=True)
        policy = self.controls.load()
        remaining = policy.runtime.memory_context_tokens
        result: builtins.list[dict[str, Any]] = []
        for _, item in ranked:
            if len(result) >= policy.runtime.memory_retrieval_count or remaining <= 0:
                break
            quality = self._quality(item)
            prefix = ""
            if item["status"] == "experience":
                prefix = "НЕПОДТВЕРЖДЁННЫЙ ОПЫТ. Это подсказка для проверки, не источник фактов. "
                if (item.get("feedback") or {}).get("rating") == "unhelpful":
                    prefix += (
                        "Пользователь отметил прошлый ответ как неудачный. "
                        "Повторять этот ответ нельзя. "
                    )
            if quality["unhelpful"]:
                prefix += (
                    "Этот опыт использовался в запусках, получивших отрицательную оценку. "
                    "Это не доказывает ошибку самого опыта: перепроверьте применимость и выводы. "
                )
            elif quality["incomplete"]:
                prefix += (
                    "Этот опыт использовался при незавершённых запусках; "
                    "это не доказывает ошибочность опыта. "
                    "Проверьте, каких подтверждений не хватает. "
                )
            text = prefix + item["text"]
            stored_strategy = item.get("strategy", {})
            strategy = {
                "tools": [tool for tool in stored_strategy.get("tools", [])
                          if tool in _ATTEMPT_TOOLS],
                "successful_tools": stored_strategy.get("successful_tools", []),
                "observed_tools": stored_strategy.get("observed_tools", []),
            }
            failures = item.get("failures", [])
            tool_context = json.dumps({"strategy": strategy, "failures": failures},
                                      ensure_ascii=False)
            tokens = count_tokens_approximately(
                [HumanMessage(content=text + "\n" + tool_context)],
                chars_per_token=policy.context.chars_per_token,
            )
            if tokens > remaining or not text:
                continue
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
                    "route_reuses": item.get("route_reuses", 0),
                    "last_used_at": item.get("last_used_at"),
                    "quality": quality,
                    "learning_state": self._learning_state(item),
                    "research_plan": item.get("research_plan", {}),
                    "strategy": strategy, "failures": failures,
                }
            )
            remaining -= tokens
        return result

    def record_usage(self, owner: str, memory_ids: builtins.list[str], run_id: str) -> None:
        """Count at most once per consuming run, without refreshing retention order."""
        if not self.enabled(owner):
            return
        now = timestamp()
        with self.runs._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM runs WHERE id=? AND owner=?", (run_id, owner)
            ).fetchone():
                return
            for memory_id in set(memory_ids):
                row = db.execute(
                    "SELECT data FROM memories WHERE id=? AND status IN ('active','experience')",
                    (memory_id,),
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
                item["quality"] = self._quality(item)
                db.execute(
                    "UPDATE memories SET data=? WHERE id=?",
                    (json.dumps(item, ensure_ascii=False), memory_id),
                )

    @staticmethod
    def _quality(item: dict[str, Any]) -> dict[str, int]:
        saved = item.get("quality") or {}
        return {
            "uses": int(item.get("usage_count", 0)),
            **{
                key: max(0, int(saved.get(key, 0)))
                for key in (
                    "helpful",
                    "unhelpful",
                    "completed",
                    "incomplete",
                )
            },
        }

    @staticmethod
    def _learning_state(item: dict[str, Any]) -> str:
        quality = MemoryStore._quality(item)
        own_rating = (item.get("feedback") or {}).get("rating")
        helpful = quality["helpful"] > 0 or own_rating == "helpful"
        unhelpful = quality["unhelpful"] > 0 or own_rating == "unhelpful"
        if helpful and unhelpful:
            return "mixed"
        if unhelpful:
            return "needs_review"
        if helpful:
            return "helpful"
        return "observed" if quality["uses"] else "unreviewed"

    def _record_learning(self, owner: str, run: dict[str, Any]) -> None:
        """Remember numeric outcomes of proven reuse, without copying consumer content."""
        status = str(run.get("status", ""))
        if status not in {"completed", "partial", "failed", "timed_out", "interrupted"}:
            return
        rating = (run.get("feedback") or {}).get("rating", "")
        rating = rating if rating in {"helpful", "unhelpful"} else ""
        accepted = status == "completed" or (
            status == "partial" and run.get("_accepted_with_warnings")
        )
        cited_routes = (_route_keys(_source_choices(run))
                        if accepted and not run.get("_final_quality_issue") else set())
        with self.runs._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            stored = db.execute(
                "SELECT data FROM runs WHERE id=? AND owner=?", (run["id"], owner)
            ).fetchone()
            if stored is None or int(json.loads(stored[0]).get("resume_count", 0)) != int(
                run.get("resume_count", 0)
            ):
                return
            rows = db.execute(
                "SELECT memories.id,memories.data FROM memories JOIN memory_usages "
                "ON memory_usages.memory_id=memories.id "
                "WHERE memory_usages.run_id=? AND memory_usages.owner=? "
                "AND memories.status IN ('active','experience')",
                (run["id"], owner),
            ).fetchall()
            for memory_id, data in rows:
                item = json.loads(data)
                previous = db.execute(
                    "SELECT status,rating FROM memory_outcomes WHERE memory_id=? AND run_id=?",
                    (memory_id, run["id"]),
                ).fetchone()
                current_rating = rating or (previous[1] if previous is not None else "")
                routes = _route_keys(item.get("strategy", {}).get("successful_tools", []))
                if current_rating != "unhelpful" and routes & cited_routes:
                    change = db.execute(
                        "INSERT OR IGNORE INTO memory_route_reuses VALUES (?,?,?)",
                        (memory_id, run["id"], owner),
                    ).rowcount
                else:
                    change = -db.execute(
                        "DELETE FROM memory_route_reuses "
                        "WHERE memory_id=? AND run_id=? AND owner=?",
                        (memory_id, run["id"], owner),
                    ).rowcount
                if (not change and previous is not None
                        and tuple(previous) == (status, current_rating)):
                    continue
                item["route_reuses"] = max(0, item.get("route_reuses", 0) + change)
                quality = self._quality(item)
                if previous is not None:
                    old_status, old_rating = previous
                    category = "completed" if old_status == "completed" else "incomplete"
                    quality[category] = max(0, quality[category] - 1)
                    if old_rating in {"helpful", "unhelpful"}:
                        quality[old_rating] = max(0, quality[old_rating] - 1)
                quality["completed" if status == "completed" else "incomplete"] += 1
                if current_rating:
                    quality[current_rating] += 1
                db.execute(
                    "INSERT INTO memory_outcomes VALUES (?,?,?,?,?) ON CONFLICT(memory_id,run_id) "
                    "DO UPDATE SET status=excluded.status,rating=excluded.rating "
                    "WHERE memory_outcomes.owner=excluded.owner",
                    (memory_id, run["id"], owner, status, current_rating),
                )
                item["quality"] = quality
                db.execute(
                    "UPDATE memories SET data=? WHERE id=?",
                    (json.dumps(item, ensure_ascii=False), memory_id),
                )

    def _make_room(self, db: sqlite3.Connection) -> bool:
        """Rotate the oldest automatic cases globally; keep manual/confirmed knowledge."""
        total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        maximum = self.maximum()
        if total < maximum:
            return True
        rows = db.execute(
            "SELECT id,data FROM memories WHERE status='experience' ORDER BY updated ASC"
        ).fetchall()
        candidates = [key for key, data in rows if json.loads(data).get("origin") == "automatic"]
        needed = total - maximum + 1
        if len(candidates) < needed:
            return False
        for key in candidates[:needed]:
            db.execute("DELETE FROM memories WHERE id=?", (key,))
            db.execute("DELETE FROM memory_usages WHERE memory_id=?", (key,))
            db.execute("DELETE FROM memory_outcomes WHERE memory_id=?", (key,))
            db.execute("DELETE FROM memory_route_reuses WHERE memory_id=?", (key,))
        return True

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
        if not index_ids:
            return None
        if not self.enabled(owner):
            run["memory_contribution"] = {"updated": False, "reason": "disabled"}
            return None
        self._record_learning(owner, run)
        return self._save_experience(owner, run, index_ids=index_ids)

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
        self._record_learning(owner, updated)
        return self._save_experience(owner, updated, index_ids=index_ids, feedback_update=True)

    @staticmethod
    def _research_plan(run: dict[str, Any]) -> dict[str, Any]:
        raw = run.get("research_plan")
        if not isinstance(raw, dict):
            return {}
        result: dict[str, Any] = {
            key: value[:500] if isinstance(value := raw.get(key), str) else ""
            for key in ("goal", "next_action")
        }
        for key in ("questions", "completed", "gaps"):
            values = raw.get(key)
            result[key] = (
                [value[:250] for value in values[:8] if isinstance(value, str)]
                if isinstance(values, list)
                else []
            )
        round_number = raw.get("round", 0)
        result["round"] = max(0, min(1000, round_number)) if isinstance(round_number, int) else 0
        return result

    def _experience_data(
        self,
        owner: str,
        run: dict[str, Any],
        *,
        index_ids: builtins.list[str],
        existing: dict[str, Any] | None,
        failures: builtins.list[dict[str, Any]],
        choices: builtins.list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = timestamp()
        feedback = run.get("feedback") or (existing or {}).get("feedback")
        bad = (feedback or {}).get("rating") == "unhelpful"
        outcome = run.get("status", "failed")
        incomplete = outcome != "completed"
        # One source can fail while an independent, current tool result supports
        # the answer. Remember only the cited route and keep the failure as a lesson.
        proven = not incomplete or (outcome == "partial" and run.get("_accepted_with_warnings"))
        research_plan = self._research_plan(run)
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
                    step["name"]
                    for step in run.get("research_trace", [])
                    if isinstance(step, dict) and step.get("name") in _ATTEMPT_TOOLS
                )
            )[:20],
            "successful_tools": choices if proven and not bad else [],
            "observed_tools": choices if not proven and not bad else [],
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
        if research_plan:
            if research_plan.get("completed"):
                text += (
                    "Ранее выполненные проверки: "
                    + "; ".join(research_plan["completed"])[:300]
                    + ".\n"
                )
            if research_plan.get("gaps"):
                text += "Непроверенные вопросы: " + "; ".join(research_plan["gaps"])[:300] + ".\n"
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
            label = failure.get("remote_tool") or failure.get("tool") or failure["operation"]
            text += f"{label} [{indexes}]: {recommendations[failure['operation']]}\n"
        if sources:
            text += (
                "Ранее найденные документы (проверить заново): "
                + "; ".join(
                    f"{source['index_id']}/{source['id']} — {source['title']}"
                    for source in sources[:4]
                )[:500]
                + "\n"
            )
        if (not bad and run.get("answer") and run.get("answer_kind") not in {
                "incomplete", "clarification", "search_results"}
                and run.get("stop_reason") not in {"model_policy_stop", "model_refusal_text"}
                and not run.get("_final_quality_issue")):
            text += _PREVIOUS_CONCLUSION + str(run["answer"])[:1000]
        return {
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
            "stop_reason": run.get("stop_reason"),
            "feedback": feedback,
            "failures": failures[:MAX_FAILURES],
            "sources": sources,
            "strategy": strategy,
            "research_plan": research_plan,
            "created_at": (existing or {}).get("created_at", now),
            "updated_at": now,
            "usage_count": (existing or {}).get("usage_count", 0),
            "route_reuses": (existing or {}).get("route_reuses", 0),
            "last_used_at": (existing or {}).get("last_used_at"),
            "quality": self._quality(existing or {}),
            "_match_question": question,
        }

    def _save_experience(
        self, owner: str, run: dict[str, Any], *, index_ids: builtins.list[str],
        failure: dict[str, Any] | None = None, feedback_update: bool = False,
    ) -> dict[str, Any] | None:
        # Parse potentially long answers before acquiring SQLite's writer lock.
        rejected_final = run.get("status") == "completed" and run.get("_final_quality_issue")
        choices = _source_choices(run) if failure is None and not rejected_final else []
        memory_id = self._automatic_id(owner, run["id"])
        with self.runs._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            stored = db.execute(
                "SELECT status,data FROM runs WHERE id=? AND owner=?", (run["id"], owner)
            ).fetchone()
            if stored is None or int(json.loads(stored[1]).get("resume_count", 0)) != int(
                run.get("resume_count", 0)
            ):
                return None
            # A detached operation can finish after cancellation or a new resume.
            # Check the producer inside the write transaction, not before reading
            # the existing case. Final outcome/feedback writes remain permitted.
            if failure and (stored[0] != "running" or run.get("status") != "running"):
                return None
            # Read current counters, scope and recorded failures under the same
            # transaction as the write. Parallel updates must not discard each other.
            row = db.execute(
                "SELECT data FROM memories WHERE id=? AND owner=?", (memory_id, owner)
            ).fetchone()
            existing = json.loads(row[0]) if row else None
            if existing and existing["status"] == "active" and not feedback_update:
                return self.public({**existing, "_author": owner, "_reader": owner})
            failures = list((existing or {}).get("failures", []))
            if failure:
                if failure in failures or len(failures) >= MAX_FAILURES:
                    return None
                failures.append(failure)
            if existing is None and not self._make_room(db):
                if not failure and not feedback_update:
                    run["memory_contribution"] = {"updated": False, "reason": "full"}
                return None
            if existing and existing.get("feedback") and not feedback_update:
                run = {**run, "feedback": existing["feedback"]}
            item = self._experience_data(
                owner, run, index_ids=index_ids, existing=existing, failures=failures,
                choices=choices,
            )
            if existing and existing["status"] == "active":
                # The author has confirmed/edited this card. Updating the vote on the
                # originating answer must not replace their text with a generated lesson.
                item = {**existing, "feedback": item["feedback"], "strategy": item["strategy"],
                        "updated_at": item["updated_at"]}
            db.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status,updated=excluded.updated,data=excluded.data "
                "WHERE memories.owner=excluded.owner",
                (
                    item["id"],
                    owner,
                    item["status"],
                    run["id"],
                    item["updated_at"],
                    json.dumps(item, ensure_ascii=False),
                ),
            )
        return self.public({**item, "_author": owner, "_reader": owner})

    def record_failure(
        self, owner: str, run: dict[str, Any], *, operation: str, index_ids: builtins.list[str],
        tool: str | None = None, remote_tool: str | None = None, reason: str | None = None,
    ) -> None:
        """Keep operation/scope and next checks, never exception text or tool arguments."""
        if (
            operation not in {"search", "read", "external", "invalid"}
            or not index_ids
            or not self.enabled(owner)
        ):
            return
        failure: dict[str, Any] = {"operation": operation, "index_ids": sorted(set(index_ids))}
        if reason in {"timeout", "permission", "remote_error", "nonzero_exit"}:
            failure["reason"] = reason
        if tool in _ATTEMPT_TOOLS:
            failure["tool"] = tool
            if tool in {"mcp_call", "mcp_tool_schema"} and remote_tool and len(remote_tool) <= 200:
                failure["remote_tool"] = remote_tool
        self._save_experience(owner, run, index_ids=index_ids, failure=failure)

    @staticmethod
    def public(item: dict[str, Any]) -> dict[str, Any]:
        editable = item.get("_reader") is not None and item.get("_reader") == item.get("_author")
        result = {key: value for key, value in item.items() if not key.startswith("_")}
        result.update(
            visibility="private" if item["status"] == "draft" else "shared",
            can_edit=editable,
            can_delete=editable,
            quality=MemoryStore._quality(item),
            learning_state=MemoryStore._learning_state(item),
        )
        if not editable:
            result.pop("source_run_id", None)
        return result
