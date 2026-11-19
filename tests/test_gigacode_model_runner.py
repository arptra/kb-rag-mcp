from __future__ import annotations

import json
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from corporate_kb.gigacode_runner import (
    GigaCodeCancelled,
    GigaCodeModelProfile,
    GigaCodeRunner,
)

_VERSION = "verified-test-launcher 1.0"
_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


def _profile(arguments: tuple[str, ...] = ()) -> GigaCodeModelProfile:
    return GigaCodeModelProfile(
        name="deployment-verified-test-wrapper",
        cli_version=_VERSION,
        arguments=arguments,
        isolation_notes="Test wrapper emits JSON only; it has no tools or inherited configuration.",
    )


def _launcher(tmp_path: Path, *, native_event: dict | None = None) -> tuple[Path, Path]:
    capture = tmp_path / "calls.jsonl"
    script = tmp_path / "verified-launcher"
    version = tmp_path / "version.txt"
    version.write_text(_VERSION, encoding="utf-8")
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, signal, sys, time\n"
        "from pathlib import Path\n"
        f"capture = Path({str(capture)!r})\n"
        f"version = Path({str(version)!r})\n"
        "if '--version' in sys.argv:\n"
        "    print(version.read_text())\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        "with capture.open('a') as handle:\n"
        "    handle.write(json.dumps({'cwd': os.getcwd(), 'files': os.listdir('.'), "
        "'args': sys.argv[1:], 'prompt': prompt}) + '\\n')\n"
        f"native_event = json.loads({json.dumps(native_event)!r})\n"
        "if native_event is not None:\n"
        "    def stopped(*args):\n"
        "        capture.with_suffix('.stopped').write_text('stopped')\n"
        "        raise SystemExit(0)\n"
        "    signal.signal(signal.SIGINT, stopped)\n"
        "    signal.signal(signal.SIGTERM, stopped)\n"
        "    print(json.dumps(native_event), flush=True)\n"
        "    time.sleep(15)\n"
        "payload = {'answer': 'decision', 'tool_calls': [{'name': 'kb_search', "
        "'args': {'query': 'service'}}]}\n"
        "print(json.dumps({'type': 'assistant', 'message': {'content': ["
        "{'type': 'text', 'text': json.dumps(payload)}]}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'is_error': False, 'result': payload, "
        "'session_id': 'model-step', 'model': 'test-model', 'usage': {'total_tokens': 12}}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, capture


def test_model_json_requires_an_explicit_profile_before_probing(settings_factory, monkeypatch):
    runner = GigaCodeRunner(settings_factory())

    def unexpected_probe(*args, **kwargs):
        pytest.fail("A missing profile must not launch the CLI")

    monkeypatch.setattr(runner, "status", unexpected_probe)
    with pytest.raises(ValueError, match=r"verified.*profile is required"):
        runner.run_model_json(prompt="Answer", schema=_SCHEMA)


def test_model_json_uses_profile_fresh_cwd_and_decision_contract(settings_factory, tmp_path):
    executable, capture = _launcher(tmp_path)
    profile = _profile(("--verified-policy", "no-native-tools"))
    runner = GigaCodeRunner(settings_factory(gigacode_command=str(executable)))
    progress: list[str] = []
    result = runner.run_model_json(
        prompt="Use the supplied evidence.", schema=_SCHEMA, profile=profile,
        progress=progress.append,
    )
    runner.run_model_json(prompt="Next step", schema=_SCHEMA, profile=profile)

    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    assert len(calls) == 2
    assert calls[0]["cwd"] != calls[1]["cwd"]
    for call in calls:
        assert call["files"] == []
        assert not Path(call["cwd"]).exists()
        assert call["args"] == [
            "--verified-policy", "no-native-tools", "--output-format", "stream-json",
            "--max-session-turns", "2",
        ]
    assert "MODEL DECISION OUTPUT CONTRACT" in calls[0]["prompt"]
    assert "Do not call native CLI tools" in calls[0]["prompt"]
    assert "Do not pretend to read a repository" in calls[0]["prompt"]
    assert "Use the supplied evidence." in calls[0]["prompt"]
    assert result.payload["tool_calls"][0]["name"] == "kb_search"
    assert result.session_id == "model-step"
    assert result.usage == {"total_tokens": 12}
    assert any(profile.name in item for item in progress)


def test_model_json_accepts_verified_wrapper_without_extra_flags(settings_factory, tmp_path):
    executable, capture = _launcher(tmp_path)
    result = GigaCodeRunner(settings_factory(gigacode_command=str(executable))).run_model_json(
        prompt="Answer", schema=_SCHEMA, profile=_profile(),
    )
    assert result.payload["answer"] == "decision"
    assert json.loads(capture.read_text())["args"] == [
        "--output-format", "stream-json", "--max-session-turns", "2",
    ]


def test_model_json_rechecks_version_even_after_a_successful_cached_probe(
    settings_factory, tmp_path,
):
    executable, capture = _launcher(tmp_path)
    runner = GigaCodeRunner(settings_factory(gigacode_command=str(executable)))
    assert runner.status()["version"] == _VERSION
    (tmp_path / "version.txt").write_text("unverified-upgrade 2.0", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"requires CLI version.*unverified-upgrade"):
        runner.run_model_json(prompt="Answer", schema=_SCHEMA, profile=_profile())
    assert not capture.exists()


@pytest.mark.parametrize("arguments", [
    ("--output-format=json",), ("--max-session-turns", "50"), ("--maxSessionTurns=5",),
    ("-otext",), ("--resume", "old"), ("-rold",), ("--continue",),
    ("--session-id=old",), ("--fork-session",), ("--cwd", "/repo"),
    ("--include-directories", "/repo"), ("--includeDirectories=/repo",),
    ("--prompt=override",), ("--input-format=stream-json",), ("--",),
])
def test_model_profile_rejects_runner_and_session_overrides(arguments):
    with pytest.raises(ValueError, match="cannot override runner argument"):
        _profile(arguments)


@pytest.mark.parametrize("field", ["name", "cli_version", "isolation_notes"])
def test_model_profile_requires_deployment_metadata(field):
    values = {"name": "verified", "cli_version": _VERSION, "arguments": (),
              "isolation_notes": "Verified wrapper without native tools or inherited MCP."}
    values[field] = " "
    with pytest.raises(ValueError, match="must be explicitly provided"):
        GigaCodeModelProfile(**values)


def test_model_profile_is_immutable():
    profile = _profile()
    with pytest.raises(FrozenInstanceError):
        profile.cli_version = "different"  # type: ignore[misc]
    with pytest.raises(ValueError, match="immutable tuple"):
        GigaCodeModelProfile("test", _VERSION, [], "Verified wrapper")  # type: ignore[arg-type]


@pytest.mark.parametrize("event", [
    {"type": "tool_use", "name": "read_file"},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "shell"}]}},
    {"type": "stream_event", "event": {"type": "content_block_start",
                                         "content_block": {"type": "tool_use"}}},
    {"type": "assistant", "message": {"tool_calls": [{"function": {"name": "mcp_write"}}]}},
])
def test_model_json_aborts_native_tool_events_and_cleans_subprocess(
    settings_factory, tmp_path, event,
):
    executable, capture = _launcher(tmp_path, native_event=event)
    runner = GigaCodeRunner(settings_factory(gigacode_command=str(executable)))

    with pytest.raises(RuntimeError, match=r"native tool event.*may already have executed"):
        runner.run_model_json(prompt="Answer", schema=_SCHEMA, profile=_profile())

    assert capture.with_suffix(".stopped").read_text() == "stopped"
    assert not Path(json.loads(capture.read_text())["cwd"]).exists()


def test_model_json_honors_cancellation_before_model_execution(settings_factory, tmp_path):
    executable, capture = _launcher(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(GigaCodeCancelled):
        GigaCodeRunner(settings_factory(gigacode_command=str(executable))).run_model_json(
            prompt="Answer", schema=_SCHEMA, profile=_profile(), cancel=cancel,
        )
    assert not capture.exists()


def test_model_json_callback_failure_cleans_running_process(settings_factory, tmp_path):
    executable, capture = _launcher(tmp_path, native_event={"type": "system", "subtype": "init"})

    def broken_callback(message: str) -> None:
        if "starting:" not in message:
            raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        GigaCodeRunner(settings_factory(gigacode_command=str(executable))).run_model_json(
            prompt="Answer", schema=_SCHEMA, profile=_profile(), progress=broken_callback,
        )
    assert capture.with_suffix(".stopped").read_text() == "stopped"
    assert not Path(json.loads(capture.read_text())["cwd"]).exists()
