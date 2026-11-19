"""Profile selection is constrained before a request enters the model queue."""

from pathlib import Path

from starlette.requests import Request
from starlette.testclient import TestClient

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Principal
from agent_harness.http import create_app
from agent_harness.integrations import IntegrationHub
from agent_harness.integrations_config import IntegrationSettings, IntegrationStore
from agent_harness.runtime import HarnessRuntime


def test_profile_list_and_request_scope(tmp_path: Path) -> None:
    async def authenticate(request: Request) -> Principal | None:
        owner = request.headers.get("x-owner")
        return Principal(owner) if owner else None

    async def allowed(principal: Principal) -> bool:
        return bool(principal.owner_id)

    store = IntegrationStore(tmp_path)
    settings = IntegrationSettings.model_validate(
        {
            "sandbox": {"enabled": True},
            "sandbox_profiles": [
                {
                    "id": "java21",
                    "name": "Java 21",
                    "image": "java:local",
                    "allowed_owners": ["alice"],
                },
                {"id": "offline", "name": "Hidden", "image": "hidden:local", "enabled": False},
            ],
        }
    )
    store.save(settings)
    hub = IntegrationHub(store, allowed)
    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path), tools=hub)
    with TestClient(create_app(runtime, authenticate, integrations=hub)) as client:
        assert client.get("/api/sandbox/profiles").status_code == 401
        alice = {"x-owner": "alice"}
        bob = {"x-owner": "bob"}
        assert client.get("/api/sandbox/profiles", headers=alice).json() == {
            "profiles": [{"id": "", "name": "Базовая среда"}, {"id": "java21", "name": "Java 21"}],
            "default_id": "",
        }
        assert client.get("/api/sandbox/profiles", headers=bob).json()["profiles"] == [
            {"id": "", "name": "Базовая среда"}
        ]
        payload = {
            "question": "Проверь Java",
            "mode": "investigate",
            "sandbox_profile_id": "java21",
        }
        assert client.post("/api/runs", headers=bob, json=payload).status_code == 403
        assert client.get("/api/runs", headers=bob).json()["runs"] == []
        response = client.post("/api/runs", headers=alice, json=payload)
        assert response.status_code == 202
        run_id = response.json()["id"]
        assert response.json()["sandbox_profile_id"] == "java21"
        assert (
            client.get(f"/api/runs/{run_id}", headers=alice).json()["sandbox_profile_id"]
            == "java21"
        )
        settings.sandbox.enabled = False
        store.save(settings)
        assert client.get("/api/sandbox/profiles", headers=alice).json()["profiles"] == []
        assert client.post("/api/runs", headers=alice, json=payload).status_code == 403
