from __future__ import annotations

from pathlib import Path

import pytest

from corporate_kb.gigacode_runner import GigaCodeRunner


def _fake_gigacode(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

if "--version" in sys.argv:
    print("0.99.0-test")
    raise SystemExit(0)

prompt = sys.stdin.read()
if "evidence-backed" not in prompt:
    print("missing prompt", file=sys.stderr)
    raise SystemExit(4)
if "--output-format" not in sys.argv or "stream-json" not in sys.argv:
    raise SystemExit(5)
unsupported = {"--json-schema", "--safe-mode", "--max-tool-calls", "--max-wall-time"}
if unsupported.intersection(sys.argv):
    raise SystemExit(6)
if "--exclude-tools" not in sys.argv or "--max-session-turns" not in sys.argv:
    raise SystemExit(7)
if "GIGACODE OUTPUT CONTRACT" not in prompt or '"markdown"' not in prompt:
    raise SystemExit(8)

print("Open browser to authenticate: https://auth.example/device?code=test-code", file=sys.stderr)
print(json.dumps({
    "type": "system",
    "subtype": "session_start",
    "session_id": "fake-session",
    "model": "fake-gigacode",
}))
print(json.dumps({
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "read_file"}]},
}))
result_payload = {
    "markdown": (
        "# Fake service SSOT\\n\\nThis evidence-backed document was produced after "
        "reading README.md and records observed behavior without inventing missing APIs.\\n"
    ),
    "analyzed_files": ["README.md"],
    "blocking_unknowns": ["Runtime behavior is not present in source."],
}
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "fake-session",
    "model": "fake-gigacode",
    "is_error": False,
    "duration_ms": 17,
    "usage": {"total_tokens": 42},
    "result": json.dumps(result_payload),
}))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_gigacode_runner_probes_and_parses_structured_stream(
    settings_factory,
    tmp_path,
) -> None:
    executable = _fake_gigacode(tmp_path / "gigacode")
    checkout = tmp_path / "repository"
    checkout.mkdir()
    (checkout / "README.md").write_text("# Fake repository\n", encoding="utf-8")
    settings = settings_factory(gigacode_command=str(executable))
    runner = GigaCodeRunner(settings)

    status = runner.status(refresh=True)
    assert status["available"] is True
    assert status["version"] == "0.99.0-test"
    assert status["read_only"] is True

    progress: list[str] = []
    authentication_urls: list[str] = []
    authentication_completed: list[bool] = []
    result = runner.run(
        checkout=checkout,
        prompt="Create an evidence-backed service SSOT.",
        progress=progress.append,
        authentication_url=authentication_urls.append,
        authentication_complete=lambda: authentication_completed.append(True),
    )

    assert result.session_id == "fake-session"
    assert result.model == "fake-gigacode"
    assert result.analyzed_files == ("README.md",)
    assert result.usage == {"total_tokens": 42}
    assert "# Fake service SSOT" in result.markdown
    assert any("GigaCode session ready" in line for line in progress)
    assert any("tools=read_file" in line for line in progress)
    assert any("GigaCode result" in line for line in progress)
    assert authentication_urls == ["https://auth.example/device?code=test-code"]
    assert authentication_completed == [True]
    assert any("waits for browser authentication" in line for line in progress)
    assert any("authentication completed" in line for line in progress)


def test_gigacode_runner_reports_missing_executable(settings_factory) -> None:
    runner = GigaCodeRunner(
        settings_factory(gigacode_command="definitely-missing-gigacode-code-command")
    )

    status = runner.status(refresh=True)

    assert status["available"] is False
    assert "was not found" in status["error"]


def test_gigacode_runner_preserves_structured_failure_reason(
    settings_factory,
    tmp_path,
) -> None:
    executable = tmp_path / "gigacode-error"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import sys

if "--version" in sys.argv:
    print("0.99.0-test")
    raise SystemExit(0)

print("safe mode warning", file=sys.stderr)
print(json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "error": {"message": "No auth type is selected"},
}))
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    checkout = tmp_path / "repository"
    checkout.mkdir()
    progress: list[str] = []

    with pytest.raises(RuntimeError, match="No auth type is selected"):
        GigaCodeRunner(settings_factory(gigacode_command=str(executable))).run(
            checkout=checkout,
            prompt="Create an evidence-backed service SSOT.",
            progress=progress.append,
        )

    assert any("No auth type is selected" in line for line in progress)
