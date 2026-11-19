from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.testclient import TestClient

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Principal
from agent_harness.discovery import DiscoveryStore, resource_url
from agent_harness.http import create_app
from agent_harness.integrations import IntegrationHub
from agent_harness.integrations_config import IntegrationSettings, IntegrationStore
from agent_harness.runtime import HarnessRuntime
from agent_harness.store import timestamp


def sample_run(run_id: str = "run-one") -> dict[str, Any]:
    return {
        "id": run_id,
        "question": "Проверь https://git.example.com/team/app.git?token=secret",
        "evidence": [
            {
                "index_id": "docs",
                "text": "MCP: https://tools.example.com/mcp "
                "ещё git@git.example.com:team/ssh.git "
                "https://user:password@bad.example/private",
                "kind": "source",
            }
        ],
        "answer": "Выдуманный адрес https://hallucinated.example.com/mcp",
    }


def test_candidates_are_private_sanitized_deduplicated_and_do_not_modify_config(
    tmp_path: Path,
) -> None:
    store = DiscoveryStore(tmp_path)
    store.scan("alice", sample_run())
    store.scan("alice", sample_run())
    settings = IntegrationSettings()
    rows = store.list("alice", {"docs"}, settings)
    assert len(rows) == 3
    assert {row["kind"] for row in rows} == {"repository", "mcp"}
    assert all("secret" not in row["url"] and "password" not in row["url"] for row in rows)
    assert not store.list("bob", {"docs"}, settings)
    assert len(store.list("alice", set(), settings)) == 1  # only user's own URL
    assert not store.list("admin", set(), settings, requests=True)
    target = next(row for row in rows if row["kind"] == "mcp")
    store.set_status("alice", target["id"], "requested")
    requested = store.list("admin", set(), settings, requests=True)
    assert len(requested) == 1 and requested[0]["index_id"] == requested[0]["run_id"] == ""
    assert not settings.connections and not settings.repositories
    connected = IntegrationSettings.model_validate(
        {
            "connections": [
                {
                    "id": "tools",
                    "name": "Tools",
                    "kind": "mcp",
                    "url": target["url"],
                    "allowed_owners": ["alice"],
                    "allowed_tools": ["search"],
                    "enabled": True,
                }
            ]
        }
    )
    configured = store.list("alice", {"docs"}, connected)
    assert (
        next(row for row in configured if row["id"] == target["id"])["resource_id"]
        == "connection:tools"
    )
    connected.connections[0].enabled = False
    disabled = store.list("alice", {"docs"}, connected)
    assert next(row for row in disabled if row["id"] == target["id"])["status"] == "requested"
    store.remove_run("alice", "run-one")
    assert not store.list("alice", {"docs"}, connected)


def test_discovery_retains_other_sightings_and_rejects_unsafe_urls(tmp_path: Path) -> None:
    store = DiscoveryStore(tmp_path)
    store.scan("alice", sample_run())
    store.scan("alice", sample_run("run-two"))
    store.remove_run("alice", "run-one")
    assert len(store.list("alice", {"docs"}, IntegrationSettings())) == 3
    for bad in (
        "file:///etc/passwd",
        "https://a:b@host/path",
        "javascript:alert(1)",
        "ssh://-oProxyCommand@host/path",
        "https://host:invalid/path",
        "git@-host:repo",
    ):
        assert resource_url(bad) is None


class Sources:
    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": "docs", "name": "Docs"}] if principal.owner_id == "alice" else []


def test_discovery_http_requests_scope_origin_delete_and_no_auto_fetch(tmp_path: Path) -> None:
    async def authenticate(request: Request) -> Principal | None:
        return Principal(request.headers["x-user"]) if request.headers.get("x-user") else None

    async def manager(request: Request) -> bool:
        return request.headers.get("x-user") == "admin"

    async def allowed(principal: Principal) -> bool:
        return True

    integrations = IntegrationHub(IntegrationStore(tmp_path), allowed)
    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path), source=Sources())  # type: ignore[arg-type]
    app = create_app(
        runtime, authenticate, authorize_model_manager=manager, integrations=integrations
    )
    with TestClient(app) as client:
        run = {
            **sample_run(),
            "status": "completed",
            "created_at": timestamp(),
            "warnings": [],
            "mode": "investigate",
            "effort": "medium",
            "index_ids": ["docs"],
        }
        runtime.store.save("alice", run)
        alice, admin = {"x-user": "alice"}, {"x-user": "admin"}
        assert client.get("/api/discoveries").status_code == 401
        assert client.get("/api/discoveries?scope=all", headers=alice).status_code == 403
        discovered = client.get("/api/discoveries?run_id=run-one", headers=alice).json()[
            "discoveries"
        ]
        assert len(discovered) == 3
        endpoint = f"/api/discoveries/{discovered[0]['id']}/request"
        assert client.post(endpoint, headers={"x-user": "bob"}, json={}).status_code == 404
        assert (
            client.post(
                endpoint, headers={**alice, "origin": "https://evil.test"}, json={}
            ).status_code
            == 403
        )
        assert client.post(endpoint, headers=alice, json={}).status_code == 200
        assert (
            len(client.get("/api/discoveries?scope=all", headers=admin).json()["discoveries"]) == 1
        )
        assert not integrations.store.load().repositories
        assert client.delete("/api/runs/run-one", headers=alice).status_code == 200
        assert not client.get("/api/discoveries", headers=alice).json()["discoveries"]


def test_integration_database_imports_legacy_only_once(tmp_path: Path) -> None:
    legacy = IntegrationSettings.model_validate(
        {
            "repositories": [
                {
                    "id": "one",
                    "name": "One",
                    "url": "https://git.example/one.git",
                }
            ]
        }
    )
    (tmp_path / "integrations.json").write_text(legacy.model_dump_json())
    store = IntegrationStore(tmp_path)
    assert store.load() == legacy
    store.save(IntegrationSettings())
    assert not IntegrationStore(tmp_path).load().repositories
    assert store.path.name.endswith(".sqlite3")
