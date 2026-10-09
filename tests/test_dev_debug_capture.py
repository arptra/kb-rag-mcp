"""Development capture retains useful failure details without changing public behavior."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP
from starlette.requests import Request

from corporate_kb.dev_debug import capture, recording
from skill_registry import SkillsRegistry
from skill_registry.http import register_skills_routes


@pytest.mark.parametrize("flag", [None, "0", "true"])
def test_capture_disabled_is_noop(
    flag: str | None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    if flag is None:
        monkeypatch.delenv("KB_DEV_DEBUG_CAPTURE", raising=False)
    else:
        monkeypatch.setenv("KB_DEV_DEBUG_CAPTURE", flag)

    def forbidden(_text: str) -> str:
        pytest.fail("Disabled capture must not inspect or redact the exception")

    monkeypatch.setattr(recording, "redact_text", forbidden)
    error = RuntimeError("private diagnostic")
    assert capture.emit_failure("skills", "git", error) is False
    assert not caplog.records
    assert not hasattr(error, "_kb_dev_debug_failure_captured")


def test_capture_keeps_diagnostic_and_traceback_but_redacts_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("KB_DEV_DEBUG_CAPTURE", "1")
    caplog.set_level(logging.ERROR, logger=capture.__name__)
    failure = "\n".join(
        [
            "Git operation failed: fatal: couldn't find remote ref missing-branch",
            "https://person:password-value@git.example/repo?token=query-value",
            "Authenticate at https://auth.example/oauth/authorize?code=auth-value",
            "Authorization: Bearer header-value",
            "-----BEGIN PRIVATE KEY-----",
            "private-key-value",
            "-----END PRIVATE KEY-----",
        ]
    )
    try:
        raise RuntimeError(failure)
    except RuntimeError as exc:
        assert capture.emit_failure("skills", "git", exc, job_id="job-123", token="field-value")
        assert capture.emit_failure("skills", "http", exc) is False

    assert len(caplog.records) == 1
    output = caplog.records[0].getMessage()
    assert "couldn't find remote ref missing-branch" in output
    assert "Traceback (most recent call last)" in output
    assert '"phase": "git"' in output
    assert '"job_id": "job-123"' in output
    assert "https://git.example/repo" in output
    for secret in (
        "password-value", "query-value", "header-value", "private-key-value", "field-value",
        "auth-value", "https://auth.example",
    ):
        assert secret not in output
    assert caplog.records[0].exc_info is None


@pytest.mark.parametrize("broken_part", ["redactor", "logger"])
def test_capture_failure_cannot_mask_original_error(
    broken_part: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KB_DEV_DEBUG_CAPTURE", "1")

    def broken(*_args: Any, **_kwargs: Any) -> str:
        raise OSError("collector unavailable")

    if broken_part == "redactor":
        monkeypatch.setattr(recording, "redact_text", broken)
    else:
        monkeypatch.setattr(capture.logger, "error", broken)
    assert capture.emit_failure("skills", "git", RuntimeError("original failure")) is False


def _source() -> dict[str, Any]:
    return {
        "name": "Example",
        "git_url": "https://git.example/repo.git",
        "skills_path": "skills",
        "interval_minutes": 0,
    }


@pytest.mark.parametrize("phase", ["git", "scan", "publish"])
def test_registry_capture_reports_failure_phase_and_preserves_public_error(
    phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("KB_DEV_DEBUG_CAPTURE", "1")
    registry = SkillsRegistry(tmp_path / "registry")
    source = registry.save_source(_source())
    diagnostic = "Git operation failed: fatal: couldn't find remote ref missing-branch"

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(diagnostic)

    manager = SimpleNamespace(
        materialize=lambda *_args, **_kwargs: ([tmp_path], [SimpleNamespace(commit="abc")])
    )
    monkeypatch.setattr(registry, "_manager", lambda _path: manager)
    monkeypatch.setattr("skill_registry.registry.scan_skills", lambda *_args: [])
    if phase == "git":
        manager.materialize = fail
    elif phase == "scan":
        monkeypatch.setattr("skill_registry.registry.scan_skills", fail)
    else:
        monkeypatch.setattr(registry, "_record_success", fail)

    job = registry.sync_now(source["id"])
    assert job["status"] == "failed"
    assert job["error"] == "Git ref was not found; check the configured branch, tag or commit"
    assert len(caplog.records) == 1
    output = caplog.records[0].getMessage()
    assert diagnostic in output
    assert f'"phase": "{phase}"' in output
    assert job["id"] in output
    assert source["id"] in output


@pytest.mark.asyncio
async def test_http_preview_captures_git_failure_once_without_leaking_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("KB_DEV_DEBUG_CAPTURE", "1")
    registry = SkillsRegistry(tmp_path / "registry")
    diagnostic = "Git operation failed: fatal: couldn't find remote ref preview-branch"

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(diagnostic)

    monkeypatch.setattr(registry, "_manager", lambda _path: SimpleNamespace(materialize=fail))

    async def authorized(_request: Request) -> bool:
        return True

    server = FastMCP("capture-test")
    register_skills_routes(
        server, registry, reader_authorized=authorized, manager_authorized=authorized
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.http_app()), base_url="https://example.test"
    ) as client:
        response = await client.post("/admin/api/skills/sources/validate", json=_source())
    assert response.status_code == 409
    assert response.json() == {"error": "Skills operation is currently unavailable"}
    assert len(caplog.records) == 1
    assert diagnostic in caplog.records[0].getMessage()
    assert '"operation": "preview"' in caplog.records[0].getMessage()
