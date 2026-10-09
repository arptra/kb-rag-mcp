"""The local web console queues work and explicit approvals; it never owns native stdin."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from dev_console import server

TOKEN = "private-console-token-" + "x" * 32
ORIGIN = "http://127.0.0.1:8788"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN}


class FakeController:
    command = "gigacode"

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.pending = True

    def overview(self) -> dict[str, Any]:
        self.calls.append(("overview",))
        return {"sessions": [{"id": "session-1"}], "repair": None}

    def session_detail(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("detail", session_id))
        if session_id == "missing":
            raise FileNotFoundError
        return {"session": {"id": session_id}, "events": [], "pending_approval": None}

    def start_recording(self, session_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start", session_id, kwargs))
        return {"id": session_id or "session-1", "recording": True}

    def stop_recording(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("stop", session_id))
        return {"recording": False}

    def add_note(self, session_id: str, text: str) -> dict[str, Any]:
        self.calls.append(("note", session_id, text))
        return {"saved": True}

    def prepare_bundle(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("bundle", session_id))
        return {"path": "bundle-1.json"}

    def queue_repair(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("queue", session_id, kwargs))
        return {"status": "queued", "session_id": session_id}

    def cancel_repair(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("cancel", session_id))
        self.pending = False
        return {"cancellation_requested": True}

    def approval(self, session_id: str, request_id: str, approved: bool) -> dict[str, Any]:
        if session_id != "session-1" or request_id != "request-1" or not self.pending:
            raise RuntimeError("No matching pending approval")
        self.calls.append(("approval", session_id, request_id, approved))
        self.pending = False
        return {"accepted": True, "approved": approved}

    def read_artifact(self, session_id: str, path: str) -> dict[str, Any]:
        self.calls.append(("artifact", session_id, path))
        return {"path": path, "content": "sanitized diagnostic"}


def _client(controller: FakeController, **kwargs: Any) -> httpx.AsyncClient:
    app = server.create_app(controller, token=TOKEN)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, **kwargs)


@pytest.mark.asyncio
async def test_every_api_read_and_mutation_requires_bearer_without_query_bypass() -> None:
    controller = FakeController()
    async with _client(controller) as client:
        for path in (
            "/api/state", "/api/doctor", "/api/sessions/session-1",
            "/api/sessions/session-1/artifacts/bundle.json",
        ):
            response = await client.get(path, params={"token": TOKEN})
            assert response.status_code == 401
        for path in (
            "/api/sessions", "/api/sessions/session-1/recording/start",
            "/api/sessions/session-1/recording/stop", "/api/sessions/session-1/note",
            "/api/sessions/session-1/bundle", "/api/sessions/session-1/fix",
            "/api/sessions/session-1/cancel", "/api/approvals/request-1",
        ):
            response = await client.post(path, json={}, headers={"Origin": ORIGIN})
            assert response.status_code == 401
        response = await client.get("/api/state", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
    assert controller.calls == []


@pytest.mark.asyncio
async def test_exact_host_origin_and_mutation_origin_are_required() -> None:
    controller = FakeController()
    async with _client(controller, headers=HEADERS) as client:
        assert (await client.get("/api/state")).status_code == 200
        for host in ("evil.example:8788", "127.0.0.1:9999", "127.0.0.1.evil.example:8788"):
            response = await client.get("/api/state", headers={"Host": host})
            assert response.status_code == 403
        for origin in ("https://evil.example", "null", "http://localhost:8788", "https://127.0.0.1:8788"):
            response = await client.post("/api/sessions", json={}, headers={"Origin": origin})
            assert response.status_code == 403
        client.headers.pop("Origin")
        response = await client.post("/api/sessions", json={})
        assert response.status_code == 403
        response = await client.get(
            "/api/state", headers={"Host": "localhost:8788", "Origin": "http://localhost:8788"}
        )
        assert response.status_code == 200
    assert controller.calls == [("overview",), ("overview",)]


@pytest.mark.asyncio
async def test_ui_assets_contain_no_token_and_have_security_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>Dev console</title>")
    (static / "app.js").write_text("window.consoleReady = true;")
    (static / "style.css").write_text("body { color: black }")
    monkeypatch.setattr(server, "_STATIC", static)
    async with _client(FakeController()) as client:
        for path in ("/", "/app.js", "/style.css"):
            response = await client.get(path)
            assert response.status_code == 200
            assert TOKEN not in response.text
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        script = await client.get("/app.js")
        assert script.text == "window.consoleReady = true;"
        assert "javascript" in script.headers["Content-Type"]
        assert (await client.get("/not-a-static-file")).status_code == 404
        assert (await client.get("/", headers={"Host": "evil.example:8788"})).status_code == 403


@pytest.mark.asyncio
async def test_actions_are_bounded_typed_and_cannot_supply_a_shell_command() -> None:
    controller = FakeController()
    async with _client(controller, headers=HEADERS) as client:
        for payload in ({"command": "touch unexpected"}, {"max_rounds": True}, {"max_rounds": 11}):
            response = await client.post("/api/sessions/session-1/fix", json=payload)
            assert response.status_code == 400
        for payload in ({"log_paths": "any.log"}, {"log_paths": ["\0"]}, {"unexpected": True}):
            assert (await client.post("/api/sessions", json=payload)).status_code == 400
        response = await client.post(
            "/api/sessions/session-1/note",
            content=b"x" * (32 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code in {400, 413}
        response = await client.post("/api/sessions", content="{}")
        assert response.status_code in {400, 415}
        assert (await client.post("/api/sessions/session-1/unknown", json={})).status_code == 404
    assert controller.calls == []


@pytest.mark.asyncio
async def test_queue_and_recording_requests_never_execute_a_native_workflow() -> None:
    controller = FakeController()
    async with _client(controller, headers=HEADERS) as client:
        created = await client.post("/api/sessions", json={"label": "Reproduce clone failure"})
        assert created.json()["id"] == "session-1"
        queued = await client.post(
            "/api/sessions/session-1/fix", json={"goal": "Explain clone failure", "max_rounds": 2}
        )
        assert queued.status_code == 200
        assert queued.json()["status"] == "queued"
        note = await client.post("/api/sessions/session-1/note", json={"text": "Failed again"})
        assert note.status_code == 200
        assert (await client.post("/api/sessions/session-1/bundle", json={})).status_code == 200
        stopped = await client.post("/api/sessions/session-1/recording/stop", json={})
        assert stopped.status_code == 200
        started = await client.post("/api/sessions/session-1/recording/start", json={})
        assert started.status_code == 200
        assert (await client.get("/api/sessions/session-1")).status_code == 200
        assert (await client.get("/api/sessions/missing")).status_code == 404
        artifact = await client.get("/api/sessions/session-1/artifacts/bundle.json")
        assert artifact.json()["content"] == "sanitized diagnostic"
    assert (
        "queue", "session-1", {"goal": "Explain clone failure", "max_rounds": 2}
    ) in controller.calls
    assert all(
        call[0] in {"start", "queue", "note", "bundle", "stop", "detail", "artifact"}
        for call in controller.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_approval_boolean_is_exact_and_duplicate_or_late_answers_conflict(
    approved: bool,
) -> None:
    controller = FakeController()
    async with _client(controller, headers=HEADERS) as client:
        for invalid in ("true", "false", 0, 1, None):
            response = await client.post(
                "/api/approvals/request-1", json={"session_id": "session-1", "approved": invalid}
            )
            assert response.status_code == 400
        wrong = await client.post(
            "/api/approvals/request-1", json={"session_id": "other", "approved": approved}
        )
        assert wrong.status_code == 409
        payload = {"session_id": "session-1", "approved": approved}
        accepted = await client.post("/api/approvals/request-1", json=payload)
        assert accepted.status_code == 200
        assert accepted.json()["approved"] is approved
        assert (await client.post("/api/approvals/request-1", json=payload)).status_code == 409
    assert controller.calls == [("approval", "session-1", "request-1", approved)]


@pytest.mark.asyncio
async def test_cancel_does_not_answer_pending_native_approval() -> None:
    controller = FakeController()
    async with _client(controller, headers=HEADERS) as client:
        cancelled = await client.post("/api/sessions/session-1/cancel", json={})
        assert cancelled.json()["cancellation_requested"] is True
        late = await client.post(
            "/api/approvals/request-1", json={"session_id": "session-1", "approved": True}
        )
        assert late.status_code == 409
    assert controller.calls == [("cancel", "session-1")]


@pytest.mark.asyncio
async def test_doctor_only_probes_cli_when_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def probe(command: str) -> dict[str, Any]:
        called.append(command)
        return {"available": True, "command": command}

    monkeypatch.setattr(server, "probe", probe)
    async with _client(FakeController(), headers=HEADERS) as client:
        assert (await client.get("/api/doctor")).json()["available"] is True
    assert called == ["gigacode"]
