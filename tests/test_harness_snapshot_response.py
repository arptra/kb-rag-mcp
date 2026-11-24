"""A plain inventory count has a source-backed answer even when no model can respond."""

from pathlib import Path
from typing import Any

import pytest
from test_harness_goal_research import OWNER, finished

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest, ServiceInventorySnapshot
from agent_harness.runtime import HarnessRuntime

QUESTION = "Сколько сервисов в АС?"  # noqa: RUF001
GRAPH = "graph:system"


@pytest.mark.parametrize(("index", "question", "mode", "attested", "complete"), [
    (GRAPH, QUESTION, "static", True, True),
    (GRAPH, QUESTION, "partial", True, False),
    (GRAPH, QUESTION, "static", False, False),
    ("docs", QUESTION, "static", True, False),
    (GRAPH, "Сколько сервисов в проде сейчас работает?", "static", True, False),
    (GRAPH, "Сколько сервисов в АС и какие зависят от банка?", "static", True, False),  # noqa: RUF001
])
async def test_count_from_portal_snapshot_without_model(
    tmp_path: Path, index: str, question: str, mode: str, attested: bool, complete: bool,
) -> None:
    class GraphSource:
        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            if complete:
                return [{"id": str(i), "name": "Other index"} for i in range(11)] + [
                    {"id": index, "name": index},
                ]
            return [{"id": index, "name": index}]

        async def search(self, **kwargs: Any) -> list[Evidence]:
            assert kwargs["principal"] == OWNER
            assert kwargs["index_id"] == index
            snapshot = ServiceInventorySnapshot.model_validate({
                "generated_at": "2026-10-01T12:00:00+00:00", "analysis_mode": mode,
                "snapshot_id": "s1", "service_count": 27,
            })
            return [Evidence(index_id=index, id="graph:inventory", title="System graph",
                             text=snapshot.model_dump_json(), service_inventory=snapshot
                             if attested else None)]

        async def read(self, **kwargs: Any) -> Evidence:
            raise AssertionError("No duplicate read is needed for a current structured snapshot")

    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path), GraphSource(), None)
    await runtime.start()
    try:
        saved = await runtime.submit(OWNER, RunRequest(question=question, mode="autonomous"))
        run = await finished(runtime, saved["id"])
        assert run["metrics"]["model_calls"] == 0
        assert (run["status"] == "completed") == complete
        if complete:
            assert run["metrics"]["source_calls"] == 1
            assert "**27**" in run["answer"] and "[graph:inventory]" in run["answer"]
            assert "2026-10-01" in run["answer"] and run["research_plan"]["ready_to_answer"]
            memory = runtime.memory.list("colleague", set(run["index_ids"]))[0]
            assert memory["strategy"]["successful_tools"][0]["index_id"] == GRAPH
    finally:
        await runtime.stop()
