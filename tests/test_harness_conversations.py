"""Persisted conversations retain ordered owner-only turns and live source redaction."""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.requests import Request

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.discovery import DiscoveryStore
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
@pytest.mark.parametrize("mode", ["quick", "investigate", "autonomous"])
async def test_greeting_http_lifecycle_never_contacts_research_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    app = runtime(tmp_path)
    server = create_app(app, authenticate, manage_lifespan=False)
    await app.start()
    catalogs, model, scan = AsyncMock(), AsyncMock(), Mock()
    monkeypatch.setattr(app, "_indexes", catalogs)
    monkeypatch.setattr(app.model, "decide", model)
    monkeypatch.setattr(DiscoveryStore, "scan", scan)
    for queue in app._queues.values():
        monkeypatch.setattr(queue, "full", lambda: True)
    try:
        selected = ["a", "connection:jira"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server), base_url="http://testserver",
            headers={"x-test-owner": "alice"},
        ) as client:
            post = await client.post("/api/runs", json={
                "question": "Привет, как дела?", "mode": mode, "effort": "high",
                "index_ids": selected, "sandbox_profile_id": "java",
            })
            assert post.status_code == 202
            run = post.json()
            assert (run["answer_kind"], run["status"]) == ("conversation", "completed")
            assert run["answer"] and run["started_at"] and run["completed_at"]
            assert run["requested_index_ids"] == selected
            assert run["index_ids"] == run["context_index_ids"] == run["evidence"] == []
            assert not any(run["metrics"].values())
            url = f"/api/runs/{run['id']}"
            assert (await client.get(url)).json()["discoveries"] == []
            timeline = (await client.get(f"/api/conversations/{run['id']}")).json()
            assert timeline["runs"][0]["answer"] == run["answer"]
            assert (await client.get("/api/conversations")).json()["total"] == 1
            assert (await client.get(url, headers={"x-test-owner": "bob"})).status_code == 404
            discoveries = await client.get("/api/discoveries", params={"run_id": run["id"]})
            assert discoveries.json()["discoveries"] == []
            feedback = (await client.put(url + "/feedback", json={"rating": "helpful"})).json()
            assert feedback["feedback"]["rating"] == "helpful" and feedback["memory"] is None
            assert app.store.get("alice", run["id"])["feedback"] == feedback["feedback"]
            assert app.memory.list("alice", set(selected)) == []
        catalogs.assert_not_awaited()
        model.assert_not_awaited()
        scan.assert_not_called()
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_source_bound_answer_cannot_opt_out_of_live_acl_with_chat_marker(
    tmp_path: Path,
) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        run = await app.submit(ALICE, RunRequest(question="Привет"))
        persisted = app.store.get("alice", run["id"])
        persisted.update(answer="private source answer", context_index_ids=["revoked"])
        app.store.save("alice", persisted)
        visible = await app.get(ALICE, run["id"])
        assert not visible["answer"] and visible["warnings"]
    finally:
        await app.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["chat", "blocked", "research"])
async def test_classification_defers_sandbox_lookup_and_keeps_refusal_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    app = runtime(tmp_path)
    tools = Mock(profiles=AsyncMock(return_value=[]), close_run=AsyncMock())
    monkeypatch.setattr(app, "tools", tools)
    catalogs = AsyncMock(return_value=[])
    monkeypatch.setattr(app, "_indexes", catalogs)
    server = create_app(app, authenticate, manage_lifespan=False)
    decision = ({"finish_reason": "content_filter", "content": "", "tool_calls": []}
                if route == "blocked" else
                {"tool_calls": [{"name": "RouteTurn", "args": {
                    "route": route, "answer": "Слово." if route == "chat" else "",
                }}]})
    model = AsyncMock(return_value=decision)
    monkeypatch.setattr(app.model, "decide", model)
    await app.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server), base_url="http://testserver",
            headers={"x-test-owner": "alice"},
        ) as client:
            created = await client.post("/api/runs", json={
                "question": "Объясни подробнее", "mode": "investigate", "routing": "auto",
                "index_ids": ["a"], "sandbox_profile_id": "retired",
            })
            assert created.status_code == 202
            result = await finish(app, created.json())
            response = (await client.get(f"/api/runs/{result['id']}")).json()
            assert response["answer"] and response["discoveries"] == []
            assert result["requested_index_ids"] == ["a"] and result["index_ids"] == []
            assert result["source_free"] and not result["resumable"]
            if route == "research":
                assert result["stop_reason"] == "sandbox_unavailable"
                tools.profiles.assert_awaited_once_with(ALICE)
            else:
                tools.profiles.assert_not_awaited()
                expected = "model_policy_stop" if route == "blocked" else "completed"
                assert result["stop_reason"] == expected
            model.assert_awaited_once()
            tools.close_run.assert_not_awaited()
            catalogs.assert_not_awaited()
            assert not app.memory.list("alice", {"a"})
    finally:
        await app.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["routing", "waiting_for_gateway"])
async def test_pending_route_exposes_picker_state_and_cancels_without_source_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    app = runtime(tmp_path)
    server = create_app(app, authenticate, manage_lifespan=False)
    await app.start()
    catalogs = AsyncMock(return_value=[])
    monkeypatch.setattr(app, "_indexes", catalogs)
    # Keep the admitted request in its initial phase, without racing a real worker.
    monkeypatch.setattr(app._queues["investigate"], "put_nowait", lambda _: None)
    try:
        accepted = await app.submit(ALICE, RunRequest(
            question="Объясни подробнее", mode="investigate", index_ids=["a"], routing="auto",
        ))
        if phase == "waiting_for_gateway":
            pending = app.store.get("alice", accepted["id"])
            pending.update(phase=phase, resume_count=2, current_action={"name": "model_retry"})
            app.store.save("alice", pending)
        url = f"/api/runs/{accepted['id']}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server), base_url="http://testserver",
            headers={"x-test-owner": "alice"},
        ) as client:
            for snapshot in (
                (await client.get(url)).json(),
                (await client.get(f"/api/conversations/{accepted['id']}")).json()["runs"][0],
            ):
                assert snapshot["phase"] == phase and snapshot["source_free"]
                assert snapshot["requested_index_ids"] == ["a"] and snapshot["index_ids"] == []
            discovered = (await client.get("/api/discoveries", params={"run_id": accepted["id"]}))
            assert discovered.json()["discoveries"] == []
            # A poll must never erase what a resumed investigation should validate.
            assert app.store.get("alice", accepted["id"])["index_ids"] == ["a"]
            cancelled = (await client.post(url + "/cancel")).json()
            assert cancelled["status"] == "cancelled" and cancelled["source_free"]
            assert (await client.get(url)).json()["requested_index_ids"] == ["a"]
            assert not app.store.get("alice", accepted["id"])["index_ids"]
            catalogs.assert_not_awaited()
    finally:
        await app.stop()


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
