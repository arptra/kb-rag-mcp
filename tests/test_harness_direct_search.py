"""Only model-backed modes accept new chat requests. Historical results remain readable."""

from pathlib import Path

import httpx
from starlette.requests import Request
from test_harness_goal_research import OWNER, Source

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Principal
from agent_harness.http import create_app
from agent_harness.runtime import HarnessRuntime
from agent_harness.store import timestamp


async def test_removed_search_mode_is_read_only_for_old_conversations(tmp_path: Path) -> None:
    runtime = HarnessRuntime(HarnessSettings(_env_file=None, enabled=True, state_dir=tmp_path),
                             Source(), None)

    async def owner(_: Request) -> Principal:
        return OWNER

    app = create_app(runtime, owner)
    await runtime.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://portal.test") as client:
            old = {"id": "old", "mode": "search", "status": "partial", "phase": "finished",
                   "question": "Где журнал?", "answer": "Сохранённая выдача",
                   "answer_kind": "search_results", "warnings": [], "created_at": timestamp(),
                   "evidence": [], "index_ids": [], "metrics": {}, "effort": "medium"}
            runtime.store.save(OWNER.owner_id, old)
            saved = (await client.get("/api/runs/old")).json()
            assert saved["answer"] == old["answer"] and not saved["resumable"]
            response = await client.post("/api/runs",
                                         json={"question": "Где журнал?", "mode": "search"})
            assert response.status_code == 400 and "mode" in response.text
            status = (await runtime.status(OWNER))["limits"]["effort_policy"]
            assert set(status) == {"quick", "investigate", "autonomous"}
    finally:
        await runtime.stop()
