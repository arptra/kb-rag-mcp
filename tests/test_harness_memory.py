"""Private confirmed notes and automatic experience remain scoped advisory context."""

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
    app: HarnessRuntime, question: str = "payment timeout", **kwargs: Any
) -> dict[str, Any]:
    run = await app.submit(ALICE, RunRequest(question=question, **kwargs))
    async with asyncio.timeout(3):
        while True:
            result = await app.get(ALICE, run["id"])
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
async def test_only_confirmed_matching_private_memory_is_used(tmp_path: Path) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        saved = await app.save_memory(ALICE, note())
        await app.save_memory(BOB, note(confirmed=True))
        assert not (await finish(app, index_ids=["a"]))["memory_used"]
        assert (await app.list_memory(BOB))["memories"][0]["id"] != saved["id"]
        await app.save_memory(ALICE, note(confirmed=True), saved["id"])
        result = await finish(app, index_ids=["a"])
        assert saved["id"] in {item["id"] for item in result["memory_used"]}
        assert isinstance(app.model, Model)
        context = app.model.contexts[-1]
        assert context["private_notes_not_evidence"][0]["text"].startswith("Check payment")
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
        assert context["private_notes_not_evidence"][0]["text"] == corrected.text
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
        assert context["private_notes_not_evidence"][0]["text"].startswith("НЕПОДТВЕРЖДЁННЫЙ ОПЫТ")
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
async def test_deletion_removes_descendants_memory_and_prevents_resurrection(
    tmp_path: Path,
) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        first = await finish(app, index_ids=["a"])
        second = await finish(app, parent_run_id=first["id"], index_ids=["a"])
        await app.save_memory(ALICE, note(confirmed=True, source_run_id=second["id"]))
        with pytest.raises(KeyError):
            await app.delete_run(BOB, first["id"])
        deleted = await app.delete_run(ALICE, first["id"])
        assert set(deleted["deleted_ids"]) == {first["id"], second["id"]}
        assert (await app.list_memory(ALICE))["memories"] == []
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


def test_memory_http_is_owner_scoped_validated_and_origin_protected(tmp_path: Path) -> None:
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
        assert client.get("/api/memory", headers={"x-user": "bob"}).json()["memories"] == []
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
        used = app.model.contexts[-1]["private_notes_not_evidence"]
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
