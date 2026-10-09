from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from corporate_kb.dev_debug import interactive

HELP = """GigaCode CLI
  -i, --prompt-interactive Execute a prompt and continue in interactive mode [string]
      --approval-mode Set the mode [choices: "default", "plan", "auto-edit", "yolo"]
      --exclude-tools Tools to exclude [array]
      --max-session-turns Maximum turns [number]
      --core-tools Core tool paths [array]
  -e, --extensions A list of extensions to use [array]
      --allowed-mcp-server-names Allowed MCP server names [array]
      --version Show version
      --help Show help
"""


def _fake_probe(monkeypatch: pytest.MonkeyPatch, help_text: str = HELP) -> list[str]:
    calls = []
    monkeypatch.setattr(interactive.shutil, "which", lambda _command: "/opt/gigacode")

    def capture(executable: str, flag: str) -> str:
        assert executable == "/opt/gigacode"
        calls.append(flag)
        return help_text if flag == "--help" else "GigaCode 0.20.0\n"

    monkeypatch.setattr(interactive, "_capture", capture)
    return calls


def test_probe_requires_advertised_interactive_and_approval_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_probe(monkeypatch)
    status = interactive.probe()
    assert status["available"] is True
    assert status["interactive_flag"] == "--prompt-interactive"
    assert status["version"] == "GigaCode 0.20.0"
    assert calls == ["--help", "--version"]
    for omitted in (
        "--prompt-interactive",
        "--approval-mode",
        "--exclude-tools",
        "--max-session-turns",
    ):
        text = "\n".join(line for line in HELP.splitlines() if omitted not in line)
        _fake_probe(monkeypatch, text)
        assert interactive.probe()["available"] is False
    _fake_probe(monkeypatch, HELP.replace('"default", ', ""))
    assert interactive.probe()["available"] is False


def test_probe_accepts_documented_short_interactive_alias_and_fails_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_probe(monkeypatch, HELP.replace("-i, --prompt-interactive", "-i"))
    assert interactive.probe()["interactive_flag"] == "-i"
    monkeypatch.setattr(interactive.shutil, "which", lambda _command: None)
    assert interactive.probe("gigacode --yolo")["available"] is False
    assert interactive.probe("gigacode\n--yolo")["available"] is False
    _fake_probe(monkeypatch)

    def broken(_executable: str, _flag: str) -> str:
        raise OSError("password=very-secret")

    monkeypatch.setattr(interactive, "_capture", broken)
    status = interactive.probe()
    assert status["available"] is False
    assert "very-secret" not in str(status)


def test_bounded_probe_captures_help_and_rejects_flood_or_hang(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake-cli"
    script.write_text(f"#!{sys.executable}\nprint('native help')\n", encoding="utf-8")
    script.chmod(0o700)
    assert "native help" in interactive._capture(str(script), "--help")
    monkeypatch.setattr(interactive, "_MAX_PROBE_BYTES", 64)
    script.write_text(f"#!{sys.executable}\nprint('x' * 1000)\n", encoding="utf-8")
    with pytest.raises(interactive.InteractiveLaunchError, match="excessive"):
        interactive._capture(str(script), "--help")
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(10)\n", encoding="utf-8")
    monkeypatch.setattr(interactive, "_PROBE_TIMEOUT", 0.1)
    with pytest.raises(interactive.InteractiveLaunchError, match="timeout"):
        interactive._capture(str(script), "--help")


def test_non_tty_is_refused_before_any_probe_or_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(interactive.sys, "stdin", io.StringIO("yes\n"))
    monkeypatch.setattr(interactive, "probe", lambda _command: pytest.fail("must not probe"))
    with pytest.raises(interactive.InteractiveLaunchError, match="real stdin terminal"):
        interactive.run_interactive(tmp_path, "fix the failure")


@pytest.mark.parametrize("read_only", [False, True])
def test_native_child_inherits_terminal_without_automatic_answers_or_headless_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    read_only: bool,
) -> None:
    _fake_probe(monkeypatch)
    monkeypatch.setattr(interactive, "_require_tty", lambda: None)
    monkeypatch.setattr(interactive, "_settings_paths", lambda _root: [])
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 7)

    monkeypatch.setattr(interactive.subprocess, "run", run)
    result = interactive.run_interactive(tmp_path, "Inspect $(do-not-execute)", read_only=read_only)
    assert result == 7
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs == {
        "cwd": tmp_path.resolve(),
        "stdin": None,
        "stdout": None,
        "stderr": None,
        "shell": False,
        "check": False,
    }
    assert args[args.index("--approval-mode") + 1] == ("plan" if read_only else "default")
    assert not {"--yolo", "--allowed-tools", "--prompt", "-p", "--output-format"} & set(args)
    assert args[args.index("--max-session-turns") + 1] == "30"
    denied = args[args.index("--exclude-tools") + 1].split(",")
    assert {"shell", "run_shell_command", "agent", "web_fetch", "web_search"} <= set(denied)
    assert ("edit" in denied) is read_only
    assert args[args.index("--extensions") + 1] == "none"
    assert args[args.index("--allowed-mcp-server-names") + 1] == interactive._NO_MCP
    prompt = args[args.index("--prompt-interactive") + 1]
    assert "Inspect $(do-not-execute)" in prompt
    assert "every source edit" in prompt
    assert "untrusted evidence" in prompt


@pytest.mark.parametrize(
    "settings",
    [
        {"permissions": {"allow": ["Edit"]}},
        {"permissions": {"allow": ["*"]}},
        {"tools": {"allowed": ["write_file"]}},
        {"allowedTools": ["edit"]},
        {"hooks": {"BeforeTool": [{"command": "something"}]}},
        {"tools": {"discoveryCommand": "custom-executable"}},
        {"mcp": {"serverCommand": "custom-server"}},
        {"output": {"format": "stream-json"}},
    ],
)
def test_existing_auto_approval_and_execution_settings_block_launch_without_rewriting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    settings: dict[str, Any],
) -> None:
    settings["apiKey"] = "do-not-include-in-errors"
    path = tmp_path / "settings.json"
    original = json.dumps(settings).encode()
    path.write_bytes(original)
    monkeypatch.setattr(interactive, "_settings_paths", lambda _root: [path])
    with pytest.raises(interactive.InteractiveLaunchError) as caught:
        interactive._preflight(tmp_path, set())
    assert "do-not-include-in-errors" not in str(caught.value)
    assert path.read_bytes() == original


def test_preflight_reads_jsonc_read_rules_and_requires_mcp_filter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        """{
        // URL comment must not corrupt a quoted URL.
        "apiUrl": "https://example.test/*path*/",
        "permissions": {"allow": ["Read(./src/**)", "Grep"]},
        "mcpServers": {"local": {"command": "node", "trust": true}}
    }""",
        encoding="utf-8",
    )
    monkeypatch.setattr(interactive, "_settings_paths", lambda _root: [path])
    with pytest.raises(interactive.InteractiveLaunchError, match="mcpServers"):
        interactive._preflight(tmp_path, set())
    interactive._preflight(tmp_path, {"--allowed-mcp-server-names", "--extensions"})
    path.write_text('{"tools": {}, "tools": {"allowed": ["edit"]}}', encoding="utf-8")
    with pytest.raises(interactive.InteractiveLaunchError, match="Cannot safely inspect"):
        interactive._preflight(tmp_path, set())
