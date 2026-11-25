"""Exercise recovery in the mounted portal with a real child process."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from textwrap import dedent
from threading import Event
from uuid import uuid4

import httpx
import pytest
from test_a2a import indexed, message
from test_a2a_research import Model, terminal
from test_gigacode_runner_lifecycle import launcher
from test_portal_agents_connections import ADMIN, READER, browser

from corporate_kb.mcp.http_server import create_http_app


def gigacode_portal(settings_factory, executable):
    return indexed(
        settings_factory, agents_enabled=True, a2a_enabled=False, gigacode_enabled=True,
        gigacode_command=str(executable), admin_password=ADMIN["X-KB-Admin-Password"],
        mcp_http_bearer_token=READER["Authorization"].removeprefix("Bearer "),
    )


async def enable_gigacode(client):
    enabled = await client.post("/agents/api/model", headers=ADMIN, json={})
    enabled.raise_for_status()
    async with asyncio.timeout(10):
        while (await client.get("/agents/api/model", headers=READER)).json()["state"] != "ready":
            await asyncio.sleep(0.02)


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX signal and an executable script")
@pytest.mark.parametrize("failure", ["signal", "runtime-exit", "runtime-result", "unexpected-exit"])
async def test_chat_survives_a_cli_crash_after_tool_execution_and_accepts_other_chats(
    settings_factory, tmp_path: Path, failure, caplog,
):
    (tmp_path / "failure").write_text(failure)
    executable = launcher(tmp_path, dedent(r"""
        root = Path(__file__).parent
        calls = root / "calls"
        crashed = root / "crashed"
        prompt = sys.stdin.read()
        marker = "GIGACODE_AGENT_REQUEST\n"
        if marker not in prompt:
            step = "probe"
            if crashed.exists():
                (root / "recovering").touch()
                while not (root / "resume").exists():
                    time.sleep(0.01)
            payload = {"ready": True}
        else:
            request, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])
            message = request["messages"][-1]
            if message["role"] == "tool":
                if not crashed.exists():
                    crashed.touch()
                    failure = (root / "failure").read_text()
                    if failure == "signal":
                        os.kill(os.getpid(), signal.SIGKILL)
                    if failure == "unexpected-exit":
                        raise SystemExit(2)
                    if failure == "runtime-result":
                        print(json.dumps({
                            "type": "result", "is_error": True,
                            "errors": ["RuntimeError: PRIVATE_CHILD_TRACE"],
                        }), flush=True)
                        raise SystemExit(1)
                    print("[ERROR] RuntimeError: PRIVATE_CHILD_TRACE", file=sys.stderr, flush=True)
                    raise SystemExit(2)
                indexes = json.loads(message["content"])
                step = "answer"
                payload = {"content": "Available index " + indexes[0]["id"], "tool_calls": []}
            elif message["content"] == "Hello":
                step = "hello"
                payload = {"content": "Welcome", "tool_calls": []}
            else:
                step = "lookup"
                payload = {"content": "", "tool_calls": [{"name": "list_indexes", "args": {}}]}
        with calls.open("a") as stream:
            stream.write(step + "\n")
        print(json.dumps({"type": "result", "is_error": False, "result": payload}), flush=True)
    """))
    app = create_http_app(*gigacode_portal(settings_factory, executable))
    second_accepted = asyncio.Event()

    async def observed_app(scope, receive, send):
        async def observe_start(message):
            if message["type"] == "http.response.start":
                second_accepted.set()
            await send(message)

        waiting = (b"x-test-waiting-chat", b"1") in scope.get("headers", ())
        await app(scope, receive, observe_start if waiting else send)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=observed_app),
                          base_url="https://portal.test") as client,
        asyncio.timeout(20),
    ):
        await enable_gigacode(client)
        chat = str(uuid4())
        question = "Which indexes are available to me?"
        async with asyncio.TaskGroup() as tasks:
            first = tasks.create_task(client.post("/agents/api/chat", headers=READER,
                                                  json={"chat": chat, "question": question}))
            try:
                while not (tmp_path / "recovering").exists():
                    if first.done():
                        pytest.fail("Chat ended before the recovery probe started")
                    await asyncio.sleep(0.02)
                status = (await client.get("/agents/api/model", headers=READER)).json()
                assert status["state"] == "recovering"
                second = tasks.create_task(client.post(
                    "/agents/api/chat", headers={**READER, "x-test-waiting-chat": "1"},
                    json={"chat": str(uuid4()), "question": "Hello"},
                ))
                await second_accepted.wait()
            finally:
                (tmp_path / "resume").touch()
        turns = []
        for task in (first, second):
            response = task.result()
            response.raise_for_status()
            assert "PRIVATE_CHILD_TRACE" not in response.text
            turns.append([json.loads(line) for line in response.text.splitlines()])
        assert turns[0][-1]["event"] == "answer" and "default" in turns[0][-1]["text"]
        assert any(item["event"] == "model_retry" and "GigaCode" in item["text"]
                   for item in turns[0])
        assert [(item["event"], item["text"]) for item in turns[0]
                if item["event"].startswith("tool_")] == [
                    ("tool_start", "list_indexes"), ("tool_end", "list_indexes"),
                ]
        assert any(item["event"] == "model_recovering" for item in turns[1])
        assert turns[1][-1]["event"] == "answer" and turns[1][-1]["text"] == "Welcome"
        history = (await client.get(f"/agents/api/chats/{chat}", headers=READER)).json()["messages"]
        assert [m["text"] for m in history] == [question, turns[0][-1]["text"]]
        assert (await client.get("/agents/api/model", headers=READER)).json()["state"] == "ready"
        code = {"signal": -9, "runtime-exit": 2, "runtime-result": 1, "unexpected-exit": 2}[failure]
        cause = "unexpected-exit" if failure == "unexpected-exit" else "process-crash"
        assert f"cause={cause} exit_code={code}" in caplog.text
        assert "PRIVATE_CHILD_TRACE" not in caplog.text
        assert sorted((tmp_path / "calls").read_text().splitlines()) == [
            "answer", "hello", "lookup", "probe", "probe",
        ]


@pytest.mark.skipif(os.name == "nt", reason="requires an executable script")
@pytest.mark.parametrize("native_tool", ["read_file", "run_shell_command"])
async def test_native_cli_tool_does_not_disable_gigacode(
    settings_factory, tmp_path: Path, native_tool, caplog,
):
    (tmp_path / "native-tool").write_text(native_tool)
    executable = launcher(tmp_path, dedent(r"""
        root = Path(__file__).parent
        native_tool = (root / "native-tool").read_text()
        prompt = sys.stdin.read()
        marker = "GIGACODE_AGENT_REQUEST\n"
        if marker not in prompt:
            payload = {"ready": True}
        else:
            request, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])
            messages = request["messages"]
            last = messages[-1]
            if last["role"] == "tool":
                count = root / "answer-calls"
                attempts = int(count.read_text()) if count.exists() else 0
                count.write_text(str(attempts + 1))
                if attempts == 0:
                    print(json.dumps({"type": "assistant", "message": {"content": [
                        {"type": "thinking", "thinking": "PRIVATE_NATIVE_ARGUMENT"},
                        {"type": "tool_use", "name": native_tool,
                         "input": {"detail": "PRIVATE_NATIVE_ARGUMENT"}}
                    ]}}), flush=True)
                    raise SystemExit(0)
                assert native_tool == "read_file"
                assert "Do not call a built-in CLI tool" in messages[0]["content"]
                indexes = json.loads(last["content"])
                payload = {"content": "Index " + indexes[0]["id"], "tool_calls": []}
            elif last["content"] == "Hello":
                payload = {"content": "Welcome", "tool_calls": []}
            elif not request["tools"]:
                frame = json.loads(last["content"])
                observed = frame["observations"][-1]
                assert native_tool == "run_shell_command"
                assert observed["tool"] == "list_indexes"
                indexes = json.loads(observed["result"])
                payload = "Index " + indexes[0]["id"]
            else:
                payload = {"content": "", "tool_calls": [{"name": "list_indexes", "args": {}}]}
        print(json.dumps({"type": "result", "is_error": False, "result": payload}), flush=True)
    """))
    async with browser(gigacode_portal(settings_factory, executable)) as client:
        await enable_gigacode(client)
        chat = str(uuid4())
        response = await client.post(
            "/agents/api/chat", headers=READER,
            json={"chat": chat, "question": "Which indexes are available?"},
        )
        response.raise_for_status()
        assert "PRIVATE_NATIVE_ARGUMENT" not in response.text + caplog.text
        events = [json.loads(line) for line in response.text.splitlines()]
        assert [event["text"] for event in events if event["event"] == "tool_start"] == [
            "list_indexes",
        ]
        assert events[-1]["event"] == "answer" and events[-1]["text"] == "Index default"
        assert any(event["event"] == "model_retry" for event in events)
        attempts = "2" if native_tool == "read_file" else "1"
        assert (tmp_path / "answer-calls").read_text() == attempts
        assert (await client.get("/agents/api/model", headers=READER)).json()["state"] == "ready"
        following = await client.post("/agents/api/chat", headers=READER,
                                      json={"chat": chat, "question": "Hello"})
        following.raise_for_status()
        last = json.loads(following.text.splitlines()[-1])
        assert last["event"] == "answer" and last["text"] == "Welcome"


@pytest.mark.skipif(os.name == "nt", reason="requires an executable script")
async def test_cli_that_always_attempts_native_tools_completes_portal_research(
    settings_factory, tmp_path: Path,
):
    executable = launcher(tmp_path, dedent(r"""
        root = Path(__file__).parent
        prompt = sys.stdin.read()
        marker = "GIGACODE_AGENT_REQUEST\n"
        if marker not in prompt:
            payload = {"ready": True}
        else:
            request, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])
            if request["tools"]:
                with (root / "bound-calls").open("a") as calls:
                    calls.write("bound\n")
                print(json.dumps({
                    "type": "assistant", "message": {"content": [
                        {"type": "tool_use", "name": "task", "input": {}}
                    ]}
                }), flush=True)
                raise SystemExit(0)
            assert "MODEL DECISION OUTPUT CONTRACT" not in prompt
            frame = json.loads(request["messages"][-1]["content"])
            observations = frame["observations"]
            if not observations:
                payload = {"tool": "list_indexes", "args": {}}
            elif observations[-1]["tool"] == "list_indexes":
                indexes = json.loads(observations[-1]["result"])
                payload = {"tool": "search_index", "args": {
                    "index_id": indexes[0]["id"], "query": frame["question"]
                }}
            elif observations[-1]["tool"] == "search_index":
                result = json.loads(observations[-1]["result"])
                document = result["results"][0]
                payload = {"tool": "read_document", "args": {
                    "index_id": result["index_id"], "document_id": document["document_id"]
                }}
            else:
                assert observations[-1]["tool"] == "read_document"
                doc = json.loads(observations[-1]["result"])
                payload = doc["content"].strip().splitlines()[-1] + " Source: " + doc["source_path"]
                print(json.dumps({
                    "type": "assistant", "message": {"content": [{"type": "text", "text":
                        json.dumps({"content": "DRAFT_NOT_THE_FINAL_RESULT", "tool_calls": []})
                    }]}
                }), flush=True)
                time.sleep(0.05)
        print(json.dumps({"type": "result", "is_error": False, "result": payload}), flush=True)
    """))
    async with browser(gigacode_portal(settings_factory, executable)) as client:
        await enable_gigacode(client)
        chat = str(uuid4())
        response = await client.post(
            "/agents/api/chat", headers=READER,
            json={"chat": chat, "question": "What is the daily limit?"},
        )
        response.raise_for_status()
        events = [json.loads(line) for line in response.text.splitlines()]
        assert [item["text"] for item in events if item["event"] == "tool_start"] == [
            "list_indexes", "search_index", "read_document",
        ]
        assert events[-1]["event"] == "answer", events
        assert events[-1]["text"] == "The daily limit is 500. Source: limits.md"
        assert "DRAFT_NOT_THE_FINAL_RESULT" not in response.text
        assert (tmp_path / "bound-calls").read_text() == "bound\n"
        saved = (await client.get(f"/agents/api/chats/{chat}", headers=READER)).json()["messages"]
        assert saved[-1]["text"] == events[-1]["text"]


@pytest.fixture
def truncated_cli(tmp_path):
    return launcher(tmp_path, dedent(r"""
        root = Path(__file__).parent
        prompt = sys.stdin.read()
        marker = "GIGACODE_AGENT_REQUEST\n"
        event = {"type": "result", "is_error": False}
        if marker not in prompt:
            step, payload = "activation", {"ready": True}
        else:
            request, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])
            messages = request["messages"]
            latest = messages[-1]
            if latest["role"] == "tool":
                assert any(msg["content"] == "Hello" for msg in messages)
                indexes = json.loads(latest["content"])
                step, payload = "answer", {"content": "Index " + indexes[0]["id"], "tool_calls": []}
            elif latest["content"] == "Hello":
                step, payload = "greeting", {"content": "Welcome", "tool_calls": []}
            elif (root / "probe-test").exists():
                step, payload = "probe", {"content": "OK", "tool_calls": []}
            else:
                step = "lookup"
                payload = {"content": "", "tool_calls": [{"name": "list_indexes", "args": {}}]}
            if step in {"probe", "answer"}:
                attempts = root / (step + "-attempts")
                seen = int(attempts.read_text()) if attempts.exists() else 0
                attempts.write_text(str(seen + 1))
                if seen < (1 if step == "probe" else 2):
                    event["stop_reason"] = "max_tokens"
                    payload = {"content": "UNFINISHED_PRIVATE_TEXT", "tool_calls": []}
        with (root / "calls").open("a") as stream:
            stream.write(step + "\n")
        print(json.dumps({**event, "result": payload}), flush=True)
    """))


@pytest.mark.skipif(os.name == "nt", reason="requires an executable script")
async def test_chat_completes_truncated_followup_with_existing_tool_result(
    settings_factory, tmp_path, truncated_cli,
):
    async with browser(gigacode_portal(settings_factory, truncated_cli)) as client:
        await enable_gigacode(client)
        chat = str(uuid4())
        question = "List my available indexes"
        for text in ("Hello", question):
            response = await client.post("/agents/api/chat", headers=READER,
                                         json={"chat": chat, "question": text})
            response.raise_for_status()
            assert "UNFINISHED_PRIVATE_TEXT" not in response.text
            events = [json.loads(line) for line in response.text.splitlines()]
            assert events[-1]["event"] == "answer", events[-1]
        assert events[-1]["text"] == "Index default"
        assert sum(item["event"] == "model_retry" for item in events) >= 2
        tools_used = [item["text"] for item in events if item["event"] == "tool_start"]
        assert tools_used == ["list_indexes"]
        history = (await client.get(f"/agents/api/chats/{chat}", headers=READER)).json()["messages"]
        assert [m["text"] for m in history] == ["Hello", "Welcome", question, "Index default"]
        assert (await client.get("/agents/api/model", headers=READER)).json()["state"] == "ready"
        assert (tmp_path / "calls").read_text().splitlines() == [
            "activation", "greeting", "lookup", "answer", "answer", "answer",
        ]


@pytest.mark.skipif(os.name == "nt", reason="requires an executable script")
async def test_model_settings_probe_retries_a_truncated_gigacode_reply(
    settings_factory, tmp_path, truncated_cli,
):
    (tmp_path / "probe-test").touch()
    async with browser(gigacode_portal(settings_factory, truncated_cli)) as client:
        await enable_gigacode(client)
        result = await client.post("/agents/api/model/test", headers=ADMIN,
                                   json={"provider": "gigacode"})
        assert result.status_code == 200, result.text
        assert result.json() == {"ok": True}
        assert (tmp_path / "calls").read_text().splitlines() == ["activation", "probe", "probe"]


async def test_a2a_research_accepts_an_enabled_controller_while_it_is_recovering(
    settings_factory, monkeypatch,
):
    entered, release = Event(), Event()

    class RecoveringModel(Model):
        def status(self):
            snapshot = super().status()
            if self.enabled and not release.is_set():
                snapshot.update(state="recovering", available=False)
            return snapshot

        async def decide(self, **kwargs):
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            return {"content": "Ready after restart", "tool_calls": []}

    monkeypatch.setattr("corporate_kb.agents.activation.GigaCodeAgentController", RecoveringModel)
    app = create_http_app(*indexed(settings_factory))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                          base_url="https://portal") as client,
    ):
        await client.post("/admin/api/a2a/runtime/enable", json={})
        issued = await client.post("/admin/api/a2a/agents",
                                   json={"name": "Research", "index_ids": ["default"]})
        headers = {"A2A-Version": "1.0", "Authorization": "Bearer " + issued.json()["token"]}
        task = await client.post("/a2a", headers=headers, json=message())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
        finally:
            release.set()
        outcome = await terminal(client, headers, task.json()["result"]["task"]["id"])
        assert outcome["status"]["state"] == "TASK_STATE_COMPLETED"
        assert outcome["artifacts"][0]["parts"][0]["data"]["answer"] == "Ready after restart"
