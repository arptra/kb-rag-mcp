"""Shared notes and experience respect source access; conversations remain private."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.memory import MemoryFeedback, MemoryInput
from agent_harness.runtime import HarnessRuntime

ALICE, BOB = Principal("alice"), Principal("bob")


class Source:
    def __init__(self) -> None:
        self.allowed = ["a", "b"]
        self.fail = False

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": item, "name": item} for item in self.allowed]

    async def search(self, **kwargs: Any) -> list[Evidence]:
        if self.fail:
            raise RuntimeError("secret-api-token must never enter draft lessons")
        return [
            Evidence(
                id="doc", title="Current", text="Fresh verified state", index_id=kwargs["index_id"]
            )
        ]

    async def read(self, **kwargs: Any) -> Evidence:
        return (await self.search(**kwargs))[0]


class Model:
    def __init__(self) -> None:
        self.contexts: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        self.contexts.append(json.loads(kwargs["messages"][-1]["content"]))
        return {"content": "Fresh answer [doc]", "tool_calls": []}


def runtime(tmp_path: Path) -> HarnessRuntime:
    return HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None), Source(), Model()
    )


async def finish(
    app: HarnessRuntime, question: str = "payment timeout", *,
    principal: Principal = ALICE, **kwargs: Any,
) -> dict[str, Any]:
    run = await app.submit(principal, RunRequest(question=question, **kwargs))
    async with asyncio.timeout(3):
        while True:
            result = await app.get(principal, run["id"])
            if result["status"] not in {"queued", "running", "cancelling"}:
                return result
            await asyncio.sleep(0.01)


def note(**kwargs: Any) -> MemoryInput:
    return MemoryInput(
        title="Payment timeout",
        text="Check payment retry settings in current code",
        index_ids=["a"],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_confirmed_memory_is_shared_but_drafts_are_private(tmp_path: Path) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        saved = await app.save_memory(ALICE, note())
        shared = await app.save_memory(BOB, note(confirmed=True))
        first = await finish(app, index_ids=["a"])
        assert shared["id"] in {item["id"] for item in first["memory_used"]}
        assert saved["id"] not in {item["id"] for item in first["memory_used"]}
        assert saved["id"] not in {
            item["id"] for item in (await app.list_memory(BOB))["memories"]
        }
        await app.save_memory(ALICE, note(confirmed=True), saved["id"])
        result = await finish(app, index_ids=["a"])
        assert saved["id"] in {item["id"] for item in result["memory_used"]}
        assert isinstance(app.model, Model)
        context = app.model.contexts[-1]
        assert context["shared_memory_not_evidence"][0]["text"].startswith("Check payment")
        assert all(item["id"] != saved["id"] for item in context["evidence"])
        assert not (await finish(app, "inventory billing", index_ids=["a"]))["memory_used"]
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_selected_scope_revocation_and_disable_stop_reuse(tmp_path: Path) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        await app.save_memory(ALICE, note(confirmed=True))
        assert not (await finish(app, index_ids=["b"]))["memory_used"]
        assert isinstance(app.source, Source)
        app.source.allowed = ["b"]
        visible = (await app.list_memory(ALICE))["memories"]
        assert all(item["index_ids"] == ["b"] for item in visible)
        app.source.allowed = ["a", "b"]
        await app.set_memory_enabled(ALICE, False)
        assert not (await finish(app, index_ids=["a"]))["memory_used"]
        assert len((await app.list_memory(ALICE))["memories"]) == 2
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_correction_replaces_advice_without_changing_evidence(tmp_path: Path) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        saved = await app.save_memory(ALICE, note(confirmed=True))
        corrected = note(confirmed=True).model_copy(
            update={"text": "Payment retries belong to ledger", "kind": "correction"}
        )
        await app.save_memory(ALICE, corrected, saved["id"])
        await finish(app, index_ids=["a"])
        assert isinstance(app.model, Model)
        context = app.model.contexts[-1]
        assert context["shared_memory_not_evidence"][0]["text"] == corrected.text
        assert context["evidence"][0]["text"] == "Fresh verified state"
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_source_link_preserves_scope_and_ownership_on_edit(tmp_path: Path) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        run = await finish(app, index_ids=["a", "b"])
        saved = await app.save_memory(ALICE, note(confirmed=True, source_run_id=run["id"]))
        assert saved["index_ids"] == ["a", "b"]
        with pytest.raises(KeyError):
            await app.save_memory(BOB, note(source_run_id=run["id"]))
        assert isinstance(app.source, Source)
        app.source.allowed = ["a"]
        with pytest.raises(ValueError, match="источникам"):
            await app.save_memory(ALICE, note(confirmed=True), saved["id"])
        with pytest.raises(KeyError):
            await app.delete_memory(BOB, saved["id"])
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_failure_experience_is_automatically_used_as_safe_advice(tmp_path: Path) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        assert isinstance(app.source, Source)
        app.source.fail = True
        failed = await finish(app, index_ids=["a"])
        notes = (await app.list_memory(ALICE))["memories"]
        assert len(notes) == 1 and notes[0]["status"] == "experience"
        assert notes[0]["source_run_id"] == failed["id"]
        assert "secret-api-token" not in json.dumps(notes)
        assert "RuntimeError" not in json.dumps(notes)
        app.source.fail = False
        repeated = await finish(app, index_ids=["a"])
        assert notes[0]["id"] in {item["id"] for item in repeated["memory_used"]}
        assert isinstance(app.model, Model)
        context = app.model.contexts[-1]
        assert context["shared_memory_not_evidence"][0]["text"].startswith("НЕПОДТВЕРЖДЁННЫЙ ОПЫТ")
        assert all(item["id"] != notes[0]["id"] for item in context["evidence"])
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_parent_answer_is_bounded_rechecked_advisory_context(tmp_path: Path) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        first = await finish(app, index_ids=["a"])
        await finish(app, "More detail", parent_run_id=first["id"], index_ids=["a"])
        assert isinstance(app.model, Model)
        assert app.model.contexts[-1]["history_answer_not_evidence"] == "Fresh answer [doc]"
        await finish(app, "More detail", parent_run_id=first["id"], index_ids=["b"])
        assert app.model.contexts[-1]["history_answer_not_evidence"] is None
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_deletion_removes_private_chat_but_preserves_shared_memory(
    tmp_path: Path,
) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        first = await finish(app, index_ids=["a"])
        second = await finish(app, parent_run_id=first["id"], index_ids=["a"])
        saved = await app.save_memory(ALICE, note(confirmed=True, source_run_id=second["id"]))
        draft = await app.save_memory(ALICE, note(source_run_id=second["id"]))
        with pytest.raises(KeyError):
            await app.delete_run(BOB, first["id"])
        deleted = await app.delete_run(ALICE, first["id"])
        assert set(deleted["deleted_ids"]) == {first["id"], second["id"]}
        memories = (await app.list_memory(ALICE))["memories"]
        assert len(memories) == 3
        assert all(item["source_run_id"] is None for item in memories)
        assert saved["id"] in {item["id"] for item in memories}
        assert draft["id"] not in {item["id"] for item in memories}
        assert len((await app.list_memory(BOB))["memories"]) == 3
        app.store.save(ALICE.owner_id, first)
        with pytest.raises(KeyError):
            await app.get(ALICE, first["id"])
        assert (await finish(app, index_ids=["a"]))["status"] == "completed"
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_active_deletion_is_blocked_cancelled_queued_deletion_is_safe(tmp_path: Path) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        run = await app.submit(ALICE, RunRequest(question="payment", index_ids=["a"]))
        with pytest.raises(ValueError, match="остановите"):
            await app.delete_run(ALICE, run["id"])
        await app.cancel(ALICE, run["id"])
        await app.delete_run(ALICE, run["id"])
        await asyncio.sleep(0.03)
        assert all(not worker.done() for worker in app._workers)
    finally:
        await app.stop()


def test_memory_http_shares_reads_but_restricts_mutations_and_validates_origin(
    tmp_path: Path,
) -> None:
    from starlette.requests import Request
    from starlette.testclient import TestClient

    from agent_harness.http import create_app

    engine = runtime(tmp_path)

    async def authenticate(request: Request) -> Principal | None:
        owner = request.headers.get("x-user")
        return Principal(owner) if owner else None

    with TestClient(create_app(engine, authenticate)) as client:
        assert client.get("/api/memory").status_code == 401
        headers = {"x-user": "alice"}
        saved = client.post("/api/memory", json=note(confirmed=True).model_dump(), headers=headers)
        assert saved.status_code == 201
        item = saved.json()
        assert item["status"] == "active" and "_match_question" not in item
        shared = client.get("/api/memory", headers={"x-user": "bob"}).json()
        assert shared["scope"] == "system"
        assert shared["limits"]["max_total"] == 10_000
        assert len(shared["memories"]) == 1
        visible = shared["memories"][0]
        assert visible["id"] == item["id"] and visible["visibility"] == "shared"
        assert not visible["can_edit"] and not visible["can_delete"]
        assert item["can_edit"] and item["can_delete"]
        assert (
            client.delete(f"/api/memory/{item['id']}", headers={"x-user": "bob"}).status_code == 404
        )
        assert (
            client.put(
                "/api/memory/preferences",
                json={"enabled": False},
                headers={**headers, "origin": "https://other.invalid"},
            ).status_code
            == 403
        )
        assert client.put(
            "/api/memory/preferences", json={"enabled": False}, headers=headers
        ).json() == {"enabled": False}
        assert (
            client.post(
                "/api/memory", json={**note().model_dump(), "text": "x" * 2401}, headers=headers
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/memory", json={**note().model_dump(), "index_ids": []}, headers=headers
            ).status_code
            == 400
        )
        assert client.delete(f"/api/memory/{item['id']}", headers=headers).status_code == 200
        assert client.get("/api/memory", headers=headers).json()["memories"] == []


def test_shared_memory_http_pages_keep_old_records_manageable_without_exposing_drafts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.requests import Request
    from starlette.testclient import TestClient

    import agent_harness.memory as memory_module
    import agent_harness.runtime as runtime_module
    from agent_harness.http import create_app

    monkeypatch.setattr(memory_module, "LIST_LIMIT", 2)
    monkeypatch.setattr(runtime_module, "LIST_LIMIT", 2)
    engine = runtime(tmp_path)

    async def authenticate(request: Request) -> Principal | None:
        owner = request.headers.get("x-user")
        return Principal(owner) if owner else None

    alice, bob = {"x-user": "alice"}, {"x-user": "bob"}
    with TestClient(create_app(engine, authenticate)) as client:
        old = client.post(
            "/api/memory", json=note(confirmed=True).model_dump(), headers=alice
        ).json()
        for _ in range(2):
            client.post("/api/memory", json=note(confirmed=True).model_dump(), headers=alice)
        draft = client.post("/api/memory", json=note().model_dump(), headers=alice).json()
        first = client.get("/api/memory", headers=bob).json()
        assert len(first["memories"]) == 2
        assert first["pagination"] == {"offset": 0, "next_offset": 2, "total": 3}
        assert old["id"] not in {item["id"] for item in first["memories"]}
        second = client.get("/api/memory?offset=2", headers=bob).json()
        assert second["pagination"] == {"offset": 2, "next_offset": None, "total": 3}
        assert [item["id"] for item in second["memories"]] == [old["id"]]
        assert not second["memories"][0]["can_edit"]
        assert draft["id"] not in {item["id"] for item in first["memories"] + second["memories"]}
        own_page = client.get("/api/memory?offset=2", headers=alice).json()
        editable = next(item for item in own_page["memories"] if item["id"] == old["id"])
        assert editable["can_delete"]
        assert client.delete(f"/api/memory/{old['id']}", headers=alice).status_code == 200
        assert client.get("/api/memory?offset=10000", headers=bob).json()["memories"] == []
        for invalid in ("-1", "10001", "text", "0.5", "99999999999"):
            assert client.get(f"/api/memory?offset={invalid}", headers=bob).status_code == 400


@pytest.mark.asyncio
async def test_finished_case_feedback_and_reuse_are_visible_without_manual_save(
    tmp_path: Path,
) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        first = await finish(app, index_ids=["a"])
        memories = (await app.list_memory(ALICE))["memories"]
        assert len(memories) == 1 and memories[0]["source_run_id"] == first["id"]
        item = memories[0]
        feedback = await app.set_run_feedback(
            ALICE, first["id"], MemoryFeedback(rating="unhelpful", comment="Check ledger retries")
        )
        assert feedback["memory"]["id"] == item["id"]
        assert feedback["memory"]["kind"] == "lesson"
        second = await finish(app, index_ids=["a"])
        assert item["id"] in {used["id"] for used in second["memory_used"]}
        assert isinstance(app.model, Model)
        used = app.model.contexts[-1]["shared_memory_not_evidence"]
        assert used[0]["status"] == "experience"
        assert "Check ledger retries" in used[0]["text"]
        assert "Fresh answer" not in used[0]["text"]
        current = {entry["id"]: entry for entry in (await app.list_memory(ALICE))["memories"]}
        assert current[item["id"]]["usage_count"] == 1
    finally:
        await app.stop()
    app = runtime(tmp_path)
    await app.start()
    try:
        saved = (await app.get(ALICE, first["id"]))["feedback"]
        assert saved == {"rating": "unhelpful", "comment": "Check ledger retries"}
        assert len((await app.list_memory(ALICE))["memories"]) == 2
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_feedback_checks_owner_and_current_full_scope(tmp_path: Path) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        first = await finish(app, index_ids=["a", "b"])
        request = MemoryFeedback(rating="helpful")
        with pytest.raises(KeyError):
            await app.set_run_feedback(BOB, first["id"], request)
        assert isinstance(app.source, Source)
        app.source.allowed = ["a"]
        with pytest.raises(ValueError, match="источникам"):
            await app.set_run_feedback(ALICE, first["id"], request)
        assert not app.store.get(ALICE.owner_id, first["id"]).get("feedback")
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_experience_and_feedback_cross_users_without_sharing_conversations(
    tmp_path: Path,
) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        first = await finish(app, index_ids=["a"])
        shared = (await app.list_memory(BOB))["memories"]
        assert len(shared) == 1
        case = shared[0]
        assert case["visibility"] == "shared"
        assert "source_run_id" not in case and not case["can_edit"]
        assert not any(key.startswith("_") for key in case)
        with pytest.raises(KeyError):
            await app.get(BOB, first["id"])
        second = await finish(app, principal=BOB, index_ids=["a"])
        assert case["id"] in {item["id"] for item in second["memory_used"]}
        assert app.memory.get(ALICE.owner_id, case["id"])["usage_count"] == 1
        await app.set_run_feedback(
            ALICE, first["id"],
            MemoryFeedback(rating="unhelpful", comment="Inspect ledger retry worker"),
        )
        assert isinstance(app.model, Model)
        third = await finish(app, principal=BOB, index_ids=["a"])
        context = app.model.contexts[-1]
        used = next(
            item for item in context["shared_memory_not_evidence"] if item["id"] == case["id"]
        )
        assert used["kind"] == "lesson" and used["feedback"]["rating"] == "unhelpful"
        assert "Inspect ledger retry worker" in used["text"]
        assert "Fresh answer" not in used["text"]
        assert case["id"] in {item["id"] for item in third["memory_used"]}
        assert app.memory.get(ALICE.owner_id, case["id"])["usage_count"] == 2
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_shared_memory_respects_each_readers_full_source_access(tmp_path: Path) -> None:
    class ScopedSource(Source):
        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            indexes = self.allowed if principal == ALICE else ["a"]
            return [{"id": item, "name": item} for item in indexes]

    app = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None), ScopedSource(), Model()
    )
    await app.start()
    try:
        first = await finish(app, index_ids=["a", "b"])
        case = (await app.list_memory(ALICE))["memories"][0]
        assert case["source_run_id"] == first["id"]
        assert (await app.list_memory(BOB))["memories"] == []
        second = await finish(app, principal=BOB, index_ids=["a"])
        assert not second["memory_used"]
        assert isinstance(app.model, Model)
        assert not app.model.contexts[-1]["shared_memory_not_evidence"]
        with pytest.raises(KeyError):
            await app.save_memory(BOB, note(confirmed=True), case["id"])
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_personal_opt_out_and_shared_deletion_affect_the_intended_users(
    tmp_path: Path,
) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        note_item = await app.save_memory(ALICE, note(confirmed=True))
        await app.set_memory_enabled(ALICE, False)
        alice = await finish(app, index_ids=["a"])
        assert not alice["memory_used"]
        assert len((await app.list_memory(ALICE))["memories"]) == 1
        bob = await finish(app, principal=BOB, index_ids=["a"])
        assert note_item["id"] in {item["id"] for item in bob["memory_used"]}
        assert (await app.list_memory(BOB))["enabled"]
        await app.delete_memory(ALICE, note_item["id"])
        next_bob = await finish(app, principal=BOB, index_ids=["a"])
        assert note_item["id"] not in {item["id"] for item in next_bob["memory_used"]}
    finally:
        await app.stop()


def test_feedback_http_checks_origin_owner_payload_and_persists_experience(tmp_path: Path) -> None:
    from starlette.requests import Request
    from starlette.testclient import TestClient

    from agent_harness.http import create_app

    engine = runtime(tmp_path)

    async def authenticate(request: Request) -> Principal | None:
        owner = request.headers.get("x-user")
        return Principal(owner) if owner else None

    with TestClient(create_app(engine, authenticate)) as client:
        headers = {"x-user": "alice"}
        created = client.post(
            "/api/runs", headers=headers, json={"question": "payment timeout", "index_ids": ["a"]}
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = client.get(f"/api/runs/{run_id}", headers=headers).json()
            if result["status"] not in {"queued", "running", "cancelling"}:
                break
            time.sleep(0.01)
        assert result["status"] == "completed"
        endpoint = f"/api/runs/{run_id}/feedback"
        payload = {"rating": "helpful"}
        assert client.put(endpoint, json=payload).status_code == 401
        assert client.put(endpoint, json=payload, headers={"x-user": "bob"}).status_code == 404
        assert (
            client.put(
                endpoint, json=payload, headers={**headers, "origin": "https://other.invalid"}
            ).status_code
            == 403
        )
        assert client.put(endpoint, json={"rating": "unknown"}, headers=headers).status_code == 400
        assert (
            client.put(
                endpoint, json={**payload, "comment": "x" * 1001}, headers=headers
            ).status_code
            == 400
        )
        accepted = client.put(endpoint, json=payload, headers=headers)
        assert accepted.status_code == 200
        assert accepted.json()["memory"]["status"] == "experience"
        assert accepted.json()["memory"]["feedback"]["rating"] == "helpful"
        assert len(client.get("/api/memory", headers=headers).json()["memories"]) == 1
