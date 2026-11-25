"""Only successful published turns should teach a model profile how to call tools."""

import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from langchain_openai import ChatOpenAI
from test_a2a import indexed
from test_access_token_migration import _certificate
from test_portal_agents_connections import browser

from corporate_kb.access.store import AccessStore
from portal_agents import http


@pytest.mark.parametrize("second_outcome", ["complete", "denied", "truncated"])
async def test_users_share_a_proven_read_strategy_without_sharing_messages(
    settings_factory, tmp_path, monkeypatch, second_outcome,
):
    model_name = "corporate-model"
    portal = indexed(
        settings_factory, agents_enabled=True, a2a_enabled=False,
        access_enabled=True, access_db_path=tmp_path / "access.sqlite3",
        access_bootstrap_admin_password="test-only-experience-admin",
        agents_model_adapter="openai", agents_model_name=model_name,
        agents_model_base_url="https://model.test/v1",
    )
    access = AccessStore(portal[1].access_db_path)
    tokens = [access.enroll(_certificate(name)).token for name in ("alice", "bob", "carol")]
    question_markers = [uuid4().hex for _ in tokens]
    questions = [f"What is our daily limit? My note is {marker}." for marker in question_markers]
    answer_markers = [uuid4().hex for _ in tokens]
    private_diagnostic = "PRIVATE_PROVIDER_DIAGNOSTIC"
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        assert payload["model"] == model_name
        # A new owner must see only their own question and authorized, freshly read material.
        body = request.content.decode()
        assert all(token not in body for token in tokens)
        assert not any(token in value for token in tokens for value in request.headers.values())
        assert request.headers["authorization"] == "Bearer test"
        assert private_diagnostic not in body
        text = json.dumps(payload["messages"])
        for previous in range(user):
            assert question_markers[previous] not in text and answer_markers[previous] not in text
        if payload.get("tools"):
            calls.append("native")
            return httpx.Response(422, json={"error": {"param": "tools",
                                                       "message": private_diagnostic}})
        frame = json.loads(payload["messages"][-1]["content"])
        assert frame["question"] == questions[user] and not frame.get("recent_conversation")
        finish = "stop"
        if not frame["observations"]:
            calls.append("select")
            decision = {"tool": "search_index", "args": {"index_id": "default",
                                                         "query": "daily limit"}}
        else:
            calls.append("answer")
            [observation] = frame["observations"]
            assert observation["tool"] == "search_index" and observation["status"] == "success"
            [hit] = json.loads(observation["result"])["results"]
            assert hit["source_path"] == "limits.md" and "500" in hit["excerpt"]
            if user == 1 and second_outcome == "denied":
                return httpx.Response(403, json={"error": {"message": private_diagnostic}})
            decision = {"answer": f"500 from limits.md. {answer_markers[user]}"}
            if user == 1 and second_outcome == "truncated":
                finish = "length"
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": model_name,
            "choices": [{"index": 0, "finish_reason": finish,
                         "message": {"role": "assistant", "content": json.dumps(decision)}}],
        })

    @asynccontextmanager
    async def model(*_, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
            yield ChatOpenAI(model=model_name, api_key="test", base_url=kwargs["base_url"],
                             http_async_client=upstream, max_retries=0, use_responses_api=False)

    monkeypatch.setattr(http, "create_model", model)
    # Reuse the external chat ID across distinct authenticated users to exercise owner isolation.
    chat_id = str(uuid4())
    for user, token in enumerate(tokens):
        calls.clear()
        # Start the mounted app again each time so successful learning must survive a restart.
        async with browser(portal) as client:
            headers = {"Authorization": f"Bearer {token}"}
            history = f"/agents/api/chats/{chat_id}"
            assert (await client.get(history, headers=headers)).json() == {"messages": []}
            response = await client.post("/agents/api/chat", headers=headers,
                                         json={"chat": chat_id, "question": questions[user]})
            response.raise_for_status()
            events = [json.loads(line) for line in response.text.splitlines()]
            assert private_diagnostic not in response.text
            assert [e["text"] for e in events if e["event"] == "tool_start"] == ["search_index"]
            # Even with a valid read, a denied or unfinished answer must not teach this switch.
            learned = user == 2 and second_outcome == "complete"
            expected = ["select", "answer"] if learned else ["native", "select", "answer"]
            transcript = (await client.get(history, headers=headers)).json()["messages"]
            if user == 1 and second_outcome != "complete":
                assert calls[:len(expected)] == expected
                assert events[-1]["event"] == "error" and transcript == []
                assert answer_markers[user] not in response.text
            else:
                assert calls == expected
                assert events[-1]["event"] == "answer"
                assert [m["text"] for m in transcript] == [questions[user], events[-1]["text"]]
                assert answer_markers[user] in events[-1]["text"]
