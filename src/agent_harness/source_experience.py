"""A search-route index of shared experience. A miss applies only to a similar question."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from typing import Any


def observed_sources(
    run: dict[str, Any], choices: list[dict[str, Any]], failures: list[dict[str, Any]],
) -> dict[str, str]:
    """Snippets alone, an unvisited source and a failed transport teach no preference."""
    if (run.get("status") not in {"completed", "partial"}
            or run.get("stop_reason") in {"model_policy_stop", "model_refusal_text",
                                          "model_unavailable", "authentication_required"}):
        return {}
    covered = run.get("coverage", {})
    latest_hits = run.get("_search_hit_ids", {})
    evidence = {item["index_id"] for item in run.get("evidence", []) if item.get("text")}
    failed = {index for failure in failures for index in failure.get("index_ids", [])}
    results = {index: "empty" for index, entry in covered.items()
               if entry.get("status") == "empty" and not entry.get("count")
               and not latest_hits.get(index) and index not in evidence and index not in failed}
    for index, verdict in run.get("_source_assessments", {}).items():
        if (verdict == "irrelevant" and index in evidence and index not in failed
                and covered.get(index, {}).get("status") == "searched"):
            results[index] = verdict
    if ((run.get("status") == "completed" or run.get("_accepted_with_warnings"))
            and not run.get("_final_quality_issue")
            and (run.get("feedback") or {}).get("rating") != "unhelpful"):
        results.update({item["index_id"]: "useful" for item in choices})
    return results


class SourceExperience:
    """One opaque feature vector per automatic memory, written in its original transaction."""

    def __init__(self, db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE IF NOT EXISTS source_route_config "
                   "(id INTEGER PRIMARY KEY CHECK(id=1), salt BLOB NOT NULL)")
        fresh = db.execute("INSERT OR IGNORE INTO source_route_config VALUES(1,randomblob(32))")
        self.salt = db.execute("SELECT salt FROM source_route_config WHERE id=1").fetchone()[0]
        db.execute("CREATE TABLE IF NOT EXISTS source_routes "
                   "(rowid INTEGER PRIMARY KEY, memory_id TEXT UNIQUE NOT NULL, "
                   "observed REAL NOT NULL, outcomes TEXT NOT NULL)")
        # SQLite builds without FTS5 still rank bounded, recent, permitted observations.
        try:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS source_route_terms USING fts5(features)")
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" not in str(exc):
                raise
            db.execute("CREATE TABLE IF NOT EXISTS source_route_terms(features TEXT NOT NULL)")
        self.fts = "VIRTUAL TABLE" in db.execute(
            "SELECT sql FROM sqlite_master WHERE name='source_route_terms'",
        ).fetchone()[0].upper()
        db.execute("""CREATE TRIGGER IF NOT EXISTS delete_source_route AFTER DELETE ON memories
                      BEGIN DELETE FROM source_routes WHERE memory_id=OLD.id; END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS delete_source_route_terms
                      AFTER DELETE ON source_routes
                      BEGIN DELETE FROM source_route_terms WHERE rowid=OLD.rowid; END""")
        self.needs_backfill = fresh.rowcount == 1

    def features(self, terms: set[str]) -> set[str]:
        # An exact token and a short character prefix also match common inflections.
        # Hash in an instance-specific namespace. The index contains no question text.
        words = sorted(terms, key=lambda s: (-len(s), s))[:48]
        tokens = {"w" + word for word in words}
        tokens.update("p" + word[:5] for word in words if len(word) >= 5)
        return {hashlib.blake2b(token.encode(), key=self.salt, digest_size=12).hexdigest()
                for token in tokens}

    def update(self, db: sqlite3.Connection, item: dict[str, Any], terms: set[str],
               observations: dict[str, str], revisions: dict[str, str],
               observed_at: str | None = None) -> None:
        db.execute("DELETE FROM source_routes WHERE memory_id=?", (item["id"],))
        features = self.features(terms)
        if not observations or not features:
            return
        scope = set(item["index_ids"])
        data = {index: [outcome, str(revisions.get(index, ""))[:200]]
                for index, outcome in observations.items() if index in scope}
        if not data:
            return
        # Using a memory later must not refresh the date of the underlying source check.
        try:
            observed = datetime.fromisoformat(observed_at or item["created_at"]).timestamp()
        except (TypeError, ValueError, OverflowError):
            # Unusable historical timestamps cannot establish the freshness of an observation.
            return
        entry = db.execute(
            "INSERT INTO source_routes(memory_id,observed,outcomes) VALUES(?,?,?)",
            (item["id"], observed, json.dumps(data)),
        )
        db.execute("INSERT INTO source_route_terms(rowid,features) VALUES(?,?)",
                   (entry.lastrowid, " ".join(sorted(features))))

    def scores(self, db: sqlite3.Connection, terms: set[str], allowed: set[str],
               selected: set[str], *, exclude: str, revisions: dict[str, str],
               retention_days: int) -> dict[str, float]:
        features = self.features(terms)
        selected = selected & allowed
        if not features or not selected:
            return {}
        now = time.time()
        lifetime = retention_days * 86400
        sql = """
            SELECT f.features,r.outcomes,r.observed
            FROM source_route_terms AS f JOIN source_routes AS r ON f.rowid=r.rowid
            JOIN memories AS m ON m.id=r.memory_id
            WHERE m.status IN ('active','experience') AND m.id!=? AND r.observed>=?
              AND NOT EXISTS (SELECT value FROM json_each(m.data,'$.index_ids')
                              EXCEPT SELECT value FROM json_each(?))
              AND EXISTS (SELECT key FROM json_each(r.outcomes)
                          INTERSECT SELECT value FROM json_each(?))
        """
        params: list[Any] = [exclude, now - lifetime, json.dumps(sorted(allowed)),
                             json.dumps(sorted(selected))]
        if self.fts:
            sql += " AND f.features MATCH ? ORDER BY rank,r.observed DESC LIMIT 64"
            params.append(" OR ".join(sorted(features)))
        else:
            sql += " ORDER BY r.observed DESC LIMIT 256"
        totals: dict[str, float] = defaultdict(float)
        weights: dict[str, float] = defaultdict(float)
        for raw, outcomes, observed in db.execute(sql, params):
            previous = set(raw.split())
            similarity = 2 * len(features & previous) / (len(previous) + len(features))
            if similarity < 0.3:
                continue
            age = max(0.0, now - observed)
            weight = similarity * 0.5 ** (age / max(86400, lifetime / 3))
            for index, (signal, revision) in json.loads(outcomes).items():
                if index not in selected:
                    continue
                if signal != "useful" and revisions.get(index) and revisions[index] != revision:
                    continue
                value = {"useful": 1.0, "empty": -0.45, "irrelevant": -0.3}[signal]
                totals[index] += weight * value
                weights[index] += weight
        # Shrink sparse observations toward neutral; a negative never removes a source.
        return {index: total / (1 + weights[index]) for index, total in totals.items()}
