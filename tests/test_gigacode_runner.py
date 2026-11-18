from __future__ import annotations

import json
from pathlib import Path

import pytest

from corporate_kb.gigacode_runner import GigaCodeRunner, browser_authentication_url


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

    debug_directory = tmp_path / "debug"
    result = GigaCodeRunner(
        settings_factory(gigacode_command=str(executable))
    ).run_json(
        checkout=checkout,
        prompt="Verify dependency evidence.",
        schema=schema,
        debug_directory=debug_directory,
    )

    assert result.session_id == "json-session"
    assert result.analyzed_files == ("Client.kt",)
    assert result.payload["edge_updates"][0]["candidate_id"] == "dep:one"
    assert "Verify dependency evidence" in (debug_directory / "prompt.txt").read_text()
    assert json.loads((debug_directory / "invocation.json").read_text())["read_only"] is True
    assert (debug_directory / "stdout.jsonl").is_file()
    assert json.loads((debug_directory / "result.json").read_text())["payload"] == result.payload


def test_gigacode_runner_workspace_edit_enables_edit_tools(
    settings_factory,
    tmp_path,
) -> None:
    executable = tmp_path / "gigacode-edit"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import sys

if "--version" in sys.argv:
    print("0.99.0-edit-test")
    raise SystemExit(0)

excluded = sys.argv[sys.argv.index("--exclude-tools") + 1]
if "write" in excluded or "edit" in excluded:
    raise SystemExit(9)
if sys.argv[sys.argv.index("--approval-mode") + 1] != "auto-edit":
    raise SystemExit(11)
prompt = sys.stdin.read()
if '"changed_files"' not in prompt or "read-only tool calls" in prompt:
    raise SystemExit(10)
payload = {
    "status": "completed",
    "message": "Button updated",
    "changed_files": ["src/Button.tsx"],
    "verification": "Reviewed the edited source",
}
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": json.dumps(payload),
}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    checkout = tmp_path / "repository"
    checkout.mkdir()
    debug_directory = tmp_path / "debug-edit"

    result = GigaCodeRunner(
        settings_factory(gigacode_command=str(executable))
    ).run_workspace_edit(
        checkout=checkout,
        prompt="Implement one UI annotation.",
        debug_directory=debug_directory,
    )

    assert result.payload["status"] == "completed"
    assert result.payload["changed_files"] == ["src/Button.tsx"]
    invocation = json.loads((debug_directory / "invocation.json").read_text())
    assert invocation["read_only"] is False
    assert invocation["approval_mode"] == "auto-edit"


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


@pytest.mark.parametrize(
    "url",
    [
        "https://auth.example/device?state=a%2Fb&code=abc==&redirect_uri=https%3A%2F%2Flocalhost",
        "http://localhost:8080/login?code=a.b;:)",
        "https://[::1]:8080/login",
        "HTTPS://auth.example/login?state=A%2B%2Fa%3D",
    ],
)
def test_browser_authentication_url_preserves_complete_url(url: str) -> None:
    assert browser_authentication_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "//auth.example/login",
        "https:///login",
        "https://",
        "https://user:secret@auth.example/login",
        "https://user@auth.example/login",
        "https://auth.example/login\n",
        "https://auth.example/\x1blogin",
        "https://auth.example/\x7flogin",
        "https://auth.example/a b",
        "https://auth.example\\@other.example/login",
        "https://auth.example:invalid/login",
        "https://auth.example:99999/login",
        "https://[broken/login",
        "https://%65xample.com/login",
    ],
)
def test_browser_authentication_url_rejects_invalid_browser_links(url: str) -> None:
    assert browser_authentication_url(url) is None


@pytest.mark.parametrize("kind", ["assistant", "result", "stream_event", "user", "tool"])
def test_gigacode_authentication_ignores_model_and_tool_urls(kind: str) -> None:
    line = json.dumps(
        {
            "type": kind,
            "auth_url": "https://auth.example/device?code=example",
            "message": {
                "content": "Open browser to authenticate: https://auth.example/device?code=example"
            },
        }
    )

    assert GigaCodeRunner._authentication_url(line) is None


@pytest.mark.parametrize("kind", ["system", "auth", "authentication_required"])
def test_gigacode_authentication_decodes_structured_instructions(kind: str) -> None:
    url = "https://auth.example/device?code=a%2Fb==&state=abc."
    line = json.dumps(
        {"type": kind, "message": "Open browser to authenticate", "url": url}
    ).replace("/", "\\/").replace("&", "\\u0026")

    assert GigaCodeRunner._authentication_url(line) == url


@pytest.mark.parametrize(
    "field",
    [
        "auth_url", "authentication_url", "authorization_url",
        "verification_uri", "verification_uri_complete", "login_url",
    ],
)
def test_gigacode_authentication_accepts_explicit_url_without_event_type(field: str) -> None:
    url = "https://auth.example/device?code=a%2Fb==&state=abc."
    line = json.dumps({field: url}).replace("/", "\\/").replace("&", "\\u0026")

    assert GigaCodeRunner._authentication_url(line) == url


def test_gigacode_authentication_rejects_arbitrary_untyped_payload() -> None:
    assert GigaCodeRunner._authentication_url(
        json.dumps({"message": "Open browser: https://auth.example/device"})
    ) is None
    assert GigaCodeRunner._authentication_url(
        json.dumps({"payload": {"auth_url": "https://auth.example/device"}})
    ) is None
    assert GigaCodeRunner._authentication_url(
        json.dumps({"auth_url": "https://user:password@auth.example/device"})
    ) is None


def test_gigacode_authentication_does_not_match_hints_inside_url() -> None:
    assert GigaCodeRunner._authentication_url(
        "OpenAPI endpoint: https://auth.example/openapi"
    ) is None
    assert GigaCodeRunner._authentication_url(
        json.dumps({"type": "system", "endpoint": "https://auth.example/openapi"})
    ) is None


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("is_error", [False, True])
def test_gigacode_authentication_callbacks_follow_cli_result(
    settings_factory, tmp_path: Path, stream: str, is_error: bool
) -> None:
    executable = tmp_path / "gigacode-auth"
    acknowledgement = tmp_path / "auth-url-delivered"
    url = "https://auth.example/device?code=a%2Fb==&state=abc."
    executable.write_text(
        """#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import time

if "--version" in sys.argv:
    print("0.99.0-auth-test")
    raise SystemExit(0)

sys.stdin.read()
"""
        + f"auth_stream = sys.{stream}\n"
        + f"auth_url = {url!r}\n"
        + f"acknowledgement = Path({str(acknowledgement)!r})\n"
        + f"is_error = {is_error!r}\n"
        + """
print("Open browser to authenticate: " + auth_url, file=auth_stream, flush=True)
deadline = time.monotonic() + 5
while not acknowledgement.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(9)
    time.sleep(0.01)
# The same URL may be printed repeatedly while the CLI waits for the browser.
print("Open browser to authenticate: " + auth_url, file=auth_stream, flush=True)
print(json.dumps({
    "type": "result",
    "is_error": is_error,
    "error": "Browser authentication failed" if is_error else None,
    "result": json.dumps({"ok": True}),
}), flush=True)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    checkout = tmp_path / "repository"
    checkout.mkdir()
    authentication_urls: list[str] = []
    authentication_completed: list[bool] = []
    progress: list[str] = []

    def on_authentication_url(value: str) -> None:
        authentication_urls.append(value)
        acknowledgement.write_text("received", encoding="utf-8")

    runner = GigaCodeRunner(settings_factory(gigacode_command=str(executable)))

    def run() -> None:
        runner.run_json(
            checkout=checkout,
            prompt="Describe image.",
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
            authentication_url=on_authentication_url,
            authentication_complete=lambda: authentication_completed.append(True),
            progress=progress.append,
        )

    if is_error:
        with pytest.raises(RuntimeError, match="Browser authentication failed"):
            run()
    else:
        run()

    assert authentication_urls == [url]
    assert authentication_completed == ([] if is_error else [True])
    assert any("waits for browser authentication" in line for line in progress)
    assert any("authentication completed" in line for line in progress) is not is_error
