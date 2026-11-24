"""Plain RAG search uses authorized sources and never waits for a model."""

import asyncio
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.requests import Request
from test_harness_api_recovery import finish
from test_harness_goal_research import OWNER, Source
from test_harness_integration import TOKEN, _service

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal
from agent_harness.http import create_app
from agent_harness.model_connection import ModelRouter
from agent_harness.runtime import HarnessRuntime
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.mcp.tools import KnowledgeTools


class DirectSource(Source):
    def __init__(self) -> None:
        super().__init__()
        self.allowed = ["kb", "graph:system"]
        self.searches: list[tuple[str, str, int]] = []
        self.failure = False

    async def search(self, **kwargs: Any) -> list[Evidence]:
        query, index, limit = kwargs["query"], kwargs["index_id"], kwargs["limit"]
        assert kwargs["principal"] == OWNER
        self.searches.append((query, index, limit))
        if index == "graph:system":
            return []
        if self.failure:
            raise OSError("Remote index unavailable")
        # Cross the run clock polling interval to detect accidental model probes.
        await asyncio.sleep(0.075)
        return [
            Evidence(id=f"hit-{n}", title=f"Doc {n}", index_id=index, document_id=f"doc-{n}",
                     text=f"Search result {n} from the catalog.", url=f"https://wiki.test/{n}")
            for n in range(limit)
        ]


@asynccontextmanager
async def direct_client(path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, DirectSource]]:
    source, model, integrations = DirectSource(), Mock(), AsyncMock()
    model.status.side_effect = AssertionError("Search contacted the unavailable model")
    model.decide = AsyncMock(side_effect=AssertionError("Search invoked the model"))
    integrations.indexes.return_value = [{"id": "connection:jira", "name": "Jira"}]
    runtime = HarnessRuntime(HarnessSettings(_env_file=None, enabled=True, state_dir=path),
                             source, model, integrations)

    async def authenticate(_: Request) -> Principal:
        return OWNER

    app = create_app(runtime, authenticate)
    await runtime.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://portal.test") as client:
            yield client, source
    finally:
        await runtime.stop()
    model.status.assert_not_called()
    model.decide.assert_not_awaited()
    integrations.describe.assert_not_awaited()
    integrations.call.assert_not_awaited()
    integrations.close_run.assert_not_awaited()


async def search(client: httpx.AsyncClient, question: str, **kwargs: Any) -> dict[str, Any]:
    created = await client.post("/api/runs", json={
        "question": question, "mode": "search", **kwargs,
    })
    assert created.status_code == 202
    return await finish(client, "/api/runs/" + created.json()["id"])


async def test_all_direct_indexes_return_ranked_hits_and_followups_use_actual_query(
    tmp_path: Path,
) -> None:
    async with direct_client(tmp_path) as (client, source):
        run = await search(client, "customer traces", effort="low")
        assert run["status"] == "completed", run["warnings"]
        assert run["answer_kind"] == "search_results" and not run["warnings"]
        assert set(run["index_ids"]) == set(source.allowed)
        assert set(source.searches) == {("customer traces", source_id, 5)
                                        for source_id in source.allowed}
        assert run["coverage"] == {"kb": {"status": "searched", "count": 5},
                                   "graph:system": {"status": "empty", "count": 0}}
        assert all(item["text"] in run["answer"] for item in run["evidence"])
        assert run["metrics"]["model_calls"] == run["budget"]["current"]["model_calls"] == 0
        assert not run.get("memory_contribution")
        previous = run["id"]
        run = await search(client, "routing setting", index_ids=["kb"], parent_run_id=previous)
        assert source.searches[-1] == ("routing setting", "kb", 5)
        assert run["parent_run_id"] == previous and not run["parent_question"]
        source.allowed.remove("kb")
        hidden = (await client.get("/api/runs/" + run["id"])).json()
        assert not hidden["answer"] and not hidden["evidence"]


async def test_empty_failed_and_skipped_indexes_are_distinguishable(tmp_path: Path) -> None:
    async with direct_client(tmp_path) as (client, source):
        empty = await search(client, "absent", index_ids=["graph:system"])
        assert empty["status"] == "completed" and not empty["evidence"]
        assert empty["answer"] and not empty["warnings"]
        mixed = await search(client, "routing", index_ids=["kb", "connection:jira"])
        assert mixed["status"] == "partial" and mixed["coverage"]["connection:jira"] == {
            "status": "skipped", "count": 0,
        }
        assert mixed["coverage"]["kb"]["count"] == 5
        assert mixed["evidence"][0]["text"] in mixed["answer"]
        retry = await client.post("/api/runs/" + mixed["id"] + "/resume",
                                  json={"index_ids": ["graph:system"]})
        assert retry.status_code == 202
        refreshed = await finish(client, "/api/runs/" + mixed["id"])
        assert refreshed["status"] == refreshed["stop_reason"] == "completed"
        assert refreshed["answer"] == empty["answer"] and not refreshed["evidence"]
        source.failure = True
        failed = await search(client, "routing", index_ids=["kb"])
        assert failed["status"] == "partial" and failed["coverage"]["kb"]["status"] == "failed"
        assert failed["answer"] != empty["answer"] and failed["warnings"]
        before = len(source.searches)
        external = await search(client, "tickets", index_ids=["connection:jira"])
        assert external["status"] == "partial" and external["answer"] != empty["answer"]
        assert len(source.searches) == before


async def test_embedded_chat_matches_native_rag_search(
    tmp_path: Path, settings_factory: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutil.copytree(Path(__file__).resolve().parents[1] / "knowledge", tmp_path / "knowledge")
    settings, service = _service(settings_factory, harness_enabled=True)
    question = "Как рассчитывается дневной лимит платежей?"
    hits = KnowledgeTools(service).search(query=question, top_k=5)["results"]
    assert len(hits) == 5 and any(item["source_url"] and item["heading_path"] for item in hits)
    model_status, model_call = Mock(), AsyncMock()
    monkeypatch.setattr(ModelRouter, "status", model_status)
    monkeypatch.setattr(ModelRouter, "decide", model_call)
    portal: Any = create_http_app(service, settings)
    async with (
        portal.router.lifespan_context(portal),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=portal), base_url="http://portal.test",
                          headers={"Authorization": f"Bearer {TOKEN}"}) as client,
    ):
        created = await client.post("/harness/api/runs", json={
            "question": question, "mode": "search", "index_ids": ["default"],
        })
        assert created.status_code == 202
        run = await finish(client, "/harness/api/runs/" + created.json()["id"])
    assert run["status"] == "completed" and not run["warnings"]
    assert len(run["evidence"]) == len(hits)
    for saved, hit in zip(run["evidence"], hits, strict=True):
        assert saved["id"] == hit["chunk_id"] and saved["document_id"] == hit["document_id"]
        assert saved["text"] == hit["excerpt"] and saved["rank"] == hit["rank"]
        assert saved["heading_path"] == hit["heading_path"] and saved["url"] == hit["source_url"]
        assert saved["source_path"] == hit["source_path"]
        assert f"[{saved['id']}]" in run["answer"]
    model_status.assert_not_called()
    model_call.assert_not_awaited()
