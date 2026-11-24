"""Repair an uncited model answer through the same HTTP run path as the chat."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_harness_api_recovery import client_for, finish

QUOTE = "payment-service retries require investigation."
ANSWER = "Для повторов в payment-service причина задержки ещё требует проверки."
DRAFT = "Операции завершаются через 999 секунд [foreign-id]."


@pytest.mark.parametrize("selection", [
    "valid", "encoded", "unknown_number", "foreign_id", "empty", "missing_answer", "foreign_cite",
])
async def test_passage_repair_only_credits_offered_source_text(
    tmp_path: Path, selection: str,
) -> None:
    selections = 0
    drafts = 0
    reads = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal reads, drafts, selections
        payload = json.loads(request.content)
        names = [tool["function"]["name"] for tool in payload.get("tools", [])]
        message: dict[str, Any]
        if names == ["select_passages"]:
            selections += 1
            passages = json.loads(payload["messages"][-1]["content"])["passages"]
            offered = payload["tools"][0]["function"]["parameters"]["properties"]["numbers"]
            assert set(offered["items"]["enum"]) == set(passages)
            match = next(number for number, item in passages.items()
                         if item == {"source_id": "entry", "quote": QUOTE})
            numbers = {"valid": [match], "encoded": repr([match]), "unknown_number": ["9999"],
                       "foreign_id": ["foreign-id"], "empty": [],
                       "missing_answer": [match], "foreign_cite": [match]}[selection]
            args = {"numbers": numbers}
            if selection != "missing_answer":
                args["answer"] = ANSWER + (" [foreign-id]" if selection == "foreign_cite" else "")
            message = {"tool_calls": [{"type": "function", "function": {
                "name": "select_passages", "arguments": json.dumps(args),
            }}]}
        elif not reads and "read_document" in names:
            reads += 1
            message = {"tool_calls": [{"type": "function", "function": {
                "name": "read_document", "arguments": '{"index_id":"a","document_id":"entry"}',
            }}]}
        else:
            drafts += 1
            message = {"content": DRAFT}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    async with client_for(tmp_path, provider) as (client, _source):
        submitted = await client.post("/api/runs", json={
            "question": "Как устроены повторы запросов?", "mode": "autonomous", "index_ids": ["a"],
        })
        submitted.raise_for_status()
        run = await finish(client, "/api/runs/" + submitted.json()["id"])

    assert reads == 1 and drafts >= 1 and selections >= 1
    assert any(step["name"] == "answer_quality_rejected" for step in run["research_trace"])
    assert "999 секунд" not in run["answer"] and "foreign-id" not in run["answer"]
    if selection in {"valid", "encoded"}:
        assert run["status"] == "completed", run["warnings"]
        assert run["answer"] == f"{ANSWER} [entry]"
    else:
        assert run["status"] == "partial" and run["stop_reason"] == "answer_quality"
        assert run["metrics"]["decision_errors"] >= 1
