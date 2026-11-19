from __future__ import annotations

import json
from pathlib import Path

import pytest

from corporate_kb.gigacode_runner import GigaCodeRunner


def analysis_cli(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "if '--version' in sys.argv:\n"
        " print('corporate-analysis-cli 2.0'); raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        "if 'emit-native-tool' in prompt:\n"
        " print(json.dumps({'type':'assistant','message':{'content':["
        "{'type':'tool_use','name':'read_file'}]}}), flush=True)\n"
        "payload = {'arguments':sys.argv[1:], 'home':os.environ['HOME'],\n"
        " 'decision_prompt':'MODEL DECISION OUTPUT CONTRACT' in prompt}\n"
        "print(json.dumps({'type':'result','is_error':False,'result':payload}), flush=True)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_agent_decision_reuses_analysis_command_and_account(settings_factory, tmp_path):
    executable = analysis_cli(tmp_path / "gigacode")
    runner = GigaCodeRunner(settings_factory(
        gigacode_command=str(executable), gigacode_max_session_turns=11,
    ))
    kwargs = {"checkout": tmp_path, "prompt": "Answer from supplied evidence", "schema": {}}
    analysis = runner.run_json(**kwargs)
    decision = runner.run_json(**kwargs, decision_only=True)
    assert analysis.payload["arguments"] == decision.payload["arguments"] == [
        "--output-format", "stream-json", "--exclude-tools",
        "shell,write,edit,agent,web_fetch,web_search", "--max-session-turns", "11",
    ]
    assert decision.payload["home"] == analysis.payload["home"]
    assert decision.payload["decision_prompt"] is True
    assert analysis.payload["decision_prompt"] is False


def test_native_tools_still_work_for_analysis_but_not_agent_decisions(
    settings_factory, tmp_path,
):
    executable = analysis_cli(tmp_path / "gigacode")
    runner = GigaCodeRunner(settings_factory(gigacode_command=str(executable)))
    kwargs = {"checkout": tmp_path, "prompt": "emit-native-tool", "schema": {}}
    assert runner.run_json(**kwargs).payload["decision_prompt"] is False
    with pytest.raises(RuntimeError, match="native tool event"):
        runner.run_json(**kwargs, decision_only=True)


def test_json_tool_choices_are_not_mistaken_for_native_cli_tools(settings_factory, tmp_path):
    decision = {
        "content": "",
        "tool_calls": [{"name": "read_document", "args": {"document_id": "page"}}],
    }
    executable = tmp_path / "gigacode"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        " print('corporate-analysis-cli 2.0'); raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        f"decision = {decision!r}\n"
        "print(json.dumps({'type':'assistant', 'message':{'content':"
        "[{'type':'text','text':json.dumps(decision)}]}}))\n"
        "print(json.dumps({'type':'result','is_error':False,'result':decision}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    runner = GigaCodeRunner(settings_factory(gigacode_command=str(executable)))
    result = runner.run_json(
        checkout=tmp_path, prompt=json.dumps(decision), schema={}, decision_only=True,
    )
    assert result.payload == decision


def test_agent_decisions_cannot_enable_workspace_edits(settings_factory, tmp_path):
    runner = GigaCodeRunner(settings_factory(gigacode_command="not-installed"))
    with pytest.raises(ValueError, match="cannot enable workspace edits"):
        runner.run_json(
            checkout=tmp_path, prompt="Answer", schema={}, allow_edits=True, decision_only=True,
        )
