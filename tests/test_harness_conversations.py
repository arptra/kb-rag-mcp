"""Persisted conversations retain ordered owner-only turns and live source redaction."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.http import create_app
from agent_harness.runtime import HarnessRuntime
from agent_harness.store import RunStore, timestamp

ALICE, BOB = Principal("alice"), Principal("bob")


class Source:
    def __init__(self) -> None:
        self.allowed = {"alice": ["a", "b"], "bob": ["a"]}

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": key, "name": key} for key in self.allowed[principal.owner_id]]

    async def search(self, **kwargs: Any) -> list[Evidence]:
        return [
            Evidence(
                id="doc", title="Ledger", text="Current ledger source", index_id=kwargs["index_id"]
            )
        ]

    async def read(self, **kwargs: Any) -> Evidence:
        return (await self.search(**kwargs))[0]


class Model:
    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        return {"content": "Full ledger answer [doc] " * 80, "tool_calls": []}


def runtime(tmp_path: Path, **kwargs: Any) -> HarnessRuntime:
    return HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None, **kwargs),
        Source(),
        Model(),
    )


async def finish(
    app: HarnessRuntime, run: dict[str, Any], owner: Principal = ALICE
) -> dict[str, Any]:
    async with asyncio.timeout(4):
        while True:
            result = await app.get(owner, run["id"])
            if result["status"] not in {"queued", "running", "cancelling"}:
                return result
            await asyncio.sleep(0.01)


async def turn(
    app: HarnessRuntime,
    question: str,
    parent: str | None = None,
    owner: Principal = ALICE,
    **kwargs: Any,
) -> dict[str, Any]:
    return await finish(
        app,
        await app.submit(
            owner, RunRequest(question=question, parent_run_id=parent, index_ids=["a"], **kwargs)
        ),
        owner,
    )


async def authenticate(request: Request) -> Principal | None:
    owner = request.headers.get("x-test-owner")
    return Principal(owner) if owner else None


@pytest.mark.asyncio
async def test_conversation_contains_all_full_turns_and_survives_restart(tmp_path: Path) -> None:
    app = runtime(tmp_path)
    await app.start()
    first = await turn(app, "Explain ledger")
    second = await turn(app, "Which queue?", first["id"])
    third = await turn(app, "Explain the retry", second["id"])
    await turn(app, "Bob private", owner=BOB)
    try:
        assert {item["conversation_id"] for item in (first, second, third)} == {first["id"]}
        listed = await app.list_conversations(ALICE)
        assert listed["total"] == 1 and listed["has_more"] is False
        summary = listed["conversations"][0]
        assert summary["title"] == "Explain ledger"
        assert summary["run_count"] == summary["turn_count"] == 3
        assert summary["last_run_id"] == third["id"]
        detail = await app.get_conversation(ALICE, first["id"])
        assert [item["id"] for item in detail["runs"]] == [first["id"], second["id"], third["id"]]
        assert all(len(item["answer"]) > 1000 and item["evidence"] for item in detail["runs"])
        assert detail["total_runs"] == 3 and detail["history_truncated"] is False
        assert len(await app.list_runs(ALICE)) == 3  # Legacy API remains run-oriented.
    finally:
        await app.stop()
    app = runtime(tmp_path)
    await app.start()
    try:
        detail = await app.get_conversation(ALICE, first["id"])
        assert len(detail["runs"]) == 3
        fourth = await turn(app, "Continue after restart", third["id"])
        assert fourth["conversation_id"] == first["id"]
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_conversation_http_pagination_owner_checks_and_acl_redaction(tmp_path: Path) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        first = await turn(app, "Ledger")
        second = await turn(app, "Ledger next", first["id"])
        third = await turn(app, "Ledger last", second["id"])
        await turn(app, "Separate conversation")
        server = create_app(app, authenticate, manage_lifespan=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server), base_url="http://testserver"
        ) as client:
            headers = {"x-test-owner": "alice"}
            url = f"/api/conversations/{first['id']}"
            assert (await client.get("/api/conversations")).status_code == 401
            assert (await client.get(url, headers={"x-test-owner": "bob"})).status_code == 404
            assert (await client.delete(url, headers={"x-test-owner": "bob"})).status_code == 404
            listed = (await client.get("/api/conversations?limit=1", headers=headers)).json()
            assert listed["total"] == 2 and listed["has_more"] and listed["next_offset"] == 1
            page = (await client.get(url + "?limit=2", headers=headers)).json()
            assert [run["id"] for run in page["runs"]] == [first["id"], second["id"]]
            assert page["has_more"] and page["next_offset"] == 2
            last = (await client.get(url + "?limit=2&offset=2", headers=headers)).json()
            assert [run["id"] for run in last["runs"]] == [third["id"]]
            assert not last["has_more"] and last["next_offset"] is None
            for query in ("limit=201", "offset=-1", "limit=oops", "unexpected=1"):
                assert (await client.get(url + "?" + query, headers=headers)).status_code == 400
            assert (
                await client.delete(url, headers={**headers, "origin": "https://other.test"})
            ).status_code == 403
            assert isinstance(app.source, Source)
            app.source.allowed["alice"] = ["b"]
            hidden = (await client.get(url, headers=headers)).json()
            assert all(not run["answer"] and not run["evidence"] for run in hidden["runs"])
            assert all(not run["memory_used"] for run in hidden["runs"])
            app.source.allowed["alice"] = ["a", "b"]
            deleted = (await client.delete(url, headers=headers)).json()
            assert set(deleted["deleted_ids"]) == {first["id"], second["id"], third["id"]}
            assert deleted["conversation_id"] == first["id"]
            assert (await client.get(url, headers=headers)).status_code == 404
            # Shared experience outlives its originating private conversation.
            assert (await app.list_memory(BOB))["memories"]
    finally:
        await app.stop()


def legacy(runs: RunStore, owner: str, key: str, parent: str | None = None) -> None:
    run = {
        "id": key,
        "status": "completed",
        "created_at": timestamp(),
        "updated_at": timestamp(),
        "question": key,
        "answer": "answer",
        "parent_run_id": parent,
        "evidence": [],
        "warnings": [],
        "index_ids": [],
        "context_index_ids": [],
    }
    with runs._connect() as db:
        db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?)",
            (key, owner, run["status"], run["created_at"], json.dumps(run)),
        )


def test_legacy_parent_chains_migrate_with_owner_boundary_and_cycle_protection(
    tmp_path: Path,
) -> None:
    settings = HarnessSettings(state_dir=tmp_path, _env_file=None)
    runs = RunStore(settings)
    legacy(runs, "alice", "root")
    legacy(runs, "alice", "child", "root")
    legacy(runs, "alice", "grandchild", "child")
    legacy(runs, "bob", "foreign")
    legacy(runs, "alice", "unsafe-parent", "foreign")
    legacy(runs, "alice", "cycle-a", "cycle-b")
    legacy(runs, "alice", "cycle-b", "cycle-a")
    restarted = RunStore(settings)
    assert restarted.get("alice", "grandchild")["conversation_id"] == "root"
    assert restarted.get("alice", "unsafe-parent")["conversation_id"] == "unsafe-parent"
    assert restarted.get("bob", "foreign")["conversation_id"] == "foreign"
    assert restarted.get("alice", "cycle-b")["conversation_id"] == "cycle-a"
    with pytest.raises(KeyError):
        restarted.delete_conversation("alice", "foreign")


@pytest.mark.asyncio
async def test_pruned_root_has_stable_chat_id_explicit_history_limit_and_can_be_deleted(
    tmp_path: Path,
) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        first = await turn(app, "Original")
        child = await turn(app, "Retained turn", first["id"])
        with app.store._connect() as db:
            db.execute("UPDATE runs SET created='2000-01-01' WHERE id=?", (first["id"],))
        app.store.prune()
        detail = await app.get_conversation(ALICE, first["id"])
        assert [run["id"] for run in detail["runs"]] == [child["id"]]
        assert detail["history_truncated"] is True
        following = await turn(app, "Continue retained conversation", child["id"])
        assert following["conversation_id"] == first["id"]
        deleted = await app.delete_conversation(ALICE, first["id"])
        assert set(deleted["deleted_ids"]) == {child["id"], following["id"]}
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_deleting_chat_with_active_descendant_is_atomic(tmp_path: Path) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        first = await turn(app, "Original")
        # Seed a queued descendant without scheduling it, to keep the active state deterministic.
        child = {
            **first,
            "id": "active-child",
            "parent_run_id": first["id"],
            "status": "queued",
            "created_at": timestamp(),
            "completed_at": None,
        }
        app.store.save("alice", child)
        with pytest.raises(ValueError):
            await app.delete_conversation(ALICE, first["id"])
        assert {first["id"], child["id"]} <= app.store.run_ids()
        child["status"] = "cancelled"
        app.store.save("alice", child)
        assert len((await app.delete_conversation(ALICE, first["id"]))["deleted_ids"]) == 2
    finally:
        await app.stop()
