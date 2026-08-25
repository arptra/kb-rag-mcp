from __future__ import annotations

import json
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


def test_gigacode_runner_accepts_a_strict_non_ssot_json_contract(
    settings_factory,
    tmp_path,
) -> None:
    executable = tmp_path / "gigacode-json"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import sys

if "--version" in sys.argv:
    print("0.99.0-json-test")
    raise SystemExit(0)

prompt = sys.stdin.read()
if '"edge_updates"' not in prompt:
    raise SystemExit(8)
payload = {
    "edge_updates": [{"candidate_id": "dep:one", "decision": "confirm"}],
    "analyzed_files": ["Client.kt"],
    "warnings": [],
}
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "json-session",
    "result": json.dumps(payload),
}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    checkout = tmp_path / "repository"
    checkout.mkdir()
    schema = {
        "type": "object",
        "properties": {
            "edge_updates": {"type": "array"},
            "analyzed_files": {"type": "array"},
            "warnings": {"type": "array"},
        },
        "required": ["edge_updates", "analyzed_files", "warnings"],
    }

    result = GigaCodeRunner(
        settings_factory(gigacode_command=str(executable))
    ).run_json(
        checkout=checkout,
        prompt="Verify dependency evidence.",
        schema=schema,
    )

    assert result.session_id == "json-session"
    assert result.analyzed_files == ("Client.kt",)
    assert result.payload["edge_updates"][0]["candidate_id"] == "dep:one"


def test_gigacode_runner_accepts_plain_markdown_result(
    settings_factory,
    tmp_path,
) -> None:
    executable = tmp_path / "gigacode-markdown"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import sys

if "--version" in sys.argv:
    print("0.99.0-markdown-test")
    raise SystemExit(0)

sys.stdin.read()
markdown = (
    "# Plain Markdown service SSOT\\n\\n"
    "This repository exposes an observed HTTP status endpoint and delegates persistence "
    "through a repository interface. Runtime deployment details are not present in source.\\n"
)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": markdown,
    "session_id": "plain-markdown-session",
}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    checkout = tmp_path / "repository"
    checkout.mkdir()
    progress: list[str] = []

    result = GigaCodeRunner(
        settings_factory(gigacode_command=str(executable))
    ).run(
        checkout=checkout,
        prompt="Create an evidence-backed service SSOT.",
        progress=progress.append,
    )

    assert result.session_id == "plain-markdown-session"
    assert result.analyzed_files == ()
    assert "# Plain Markdown service SSOT" in result.markdown
    assert result.blocking_unknowns == (
        "GigaCode returned plain Markdown without structured file metadata.",
    )
    assert any("mode=result:markdown-fallback" in line for line in progress)


def test_gigacode_runner_extracts_embedded_json_and_camel_case_fields() -> None:
    markdown = (
        "# Embedded JSON SSOT\\n\\n"
        "This evidence-backed service description is deliberately long enough to pass "
        "the SSOT validation contract without inventing any unsupported runtime facts."
    )
    raw_result = (
        "Here is the requested object:\n```json\n"
        + '{"markdown": '
        + json.dumps(markdown)
        + ', "analyzedFiles": ["README.md"], "blockingUnknowns": []}'
        + "\n```\nDone."
    )

    structured, mode = GigaCodeRunner._result_contract(
        {"type": "result", "result": raw_result},
        [],
        [],
    )

    assert mode == "result:json"
    assert structured is not None
    assert structured["analyzed_files"] == ["README.md"]
    assert structured["blocking_unknowns"] == []
    assert structured["markdown"] == markdown


def test_gigacode_runner_uses_final_assistant_text_when_result_is_empty() -> None:
    assistant_markdown = (
        "# Assistant event SSOT\\n\\n"
        "The terminal result omitted its text, but the final assistant event retained this "
        "complete evidence-backed service description for safe recovery by the runner."
    )

    structured, mode = GigaCodeRunner._result_contract(
        {"type": "result", "subtype": "success", "is_error": False},
        [assistant_markdown],
        [],
    )

    assert mode == "assistant_message:markdown-fallback"
    assert structured is not None
    assert structured["markdown"] == assistant_markdown


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
