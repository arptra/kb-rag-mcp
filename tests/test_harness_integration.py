from __future__ import annotations

import asyncio
import builtins
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest

from corporate_kb.agents import activation
from corporate_kb.config import Settings
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.gigacode_runner import GigaCodeCancelled, GigaCodeJsonResult
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.service import KnowledgeService

TOKEN = "harness-integration-token-at-least-32-characters"
ADMIN = "harness-administrator-password"


def _service(settings_factory: Any, **overrides: Any) -> tuple[Any, KnowledgeService]:
    defaults = {
        "confluence_enabled": False,
        "a2a_enabled": False,
        "skills_registry_enabled": False,
        "mcp_tls_enabled": False,
        "mcp_http_bearer_token": TOKEN,
        "admin_password": ADMIN,
    }
    settings = settings_factory(**{**defaults, **overrides})
    settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
    (settings.knowledge_dir / "payments.md").write_text(
        "# Payments\n\npayments-service checks daily limits before payment processing.",
        encoding="utf-8",
    )
    service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    service.build_index(force=True)
    return settings, service


async def test_disabled_host_does_not_import_module_create_storage_or_serve_routes(
    settings_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, service = _service(settings_factory)
    original = builtins.__import__

    def reject_harness(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith(("agent_harness", "corporate_kb.harness_integration")):
            raise AssertionError("Disabled module must not be imported")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_harness)
    app: Any = create_http_app(service, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
        ) as client,
    ):
        assert (await client.get("/harness/")).status_code == 404
        assert (await client.get("/harness/api/status")).status_code == 404
        overview = await client.get(
            "/admin/api/overview",
            headers={"x-kb-admin-password": ADMIN},
        )
        assert overview.status_code == 200
        assert overview.json()["harness"] == {"enabled": False, "url": None}
    assert not settings.harness_state_dir.exists()


async def test_enabled_module_runs_real_rag_separately_and_hides_other_owners(
    settings_factory: Any,
) -> None:
    settings, service = _service(settings_factory, harness_enabled=True)
    app: Any = create_http_app(service, settings)
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
        ) as client,
    ):
        assert (await client.get("/harness/")).status_code == 200
        assert (await client.get("/harness/assets/app.js")).status_code == 200
        assert (await client.get("/harness/api/status")).status_code == 401
        status = await client.get("/harness/api/status", headers=headers)
        assert status.status_code == 200
        response = await client.post(
            "/harness/api/runs",
            headers=headers,
            json={"question": "payments daily limits", "mode": "quick", "effort": "low"},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        result: dict[str, Any] = {}
        for _ in range(100):
            fetched = await client.get(f"/harness/api/runs/{run_id}", headers=headers)
            assert fetched.status_code == 200
            result = fetched.json()
            if result["status"] not in {"queued", "running", "cancelling"}:
                break
            await asyncio.sleep(0.02)
        assert result["status"] == "partial", result
        assert result["evidence"]
        assert "limits" in str(result["evidence"])
        assert (
            await client.get(
                f"/harness/api/runs/{run_id}",
                headers={"x-kb-admin-password": ADMIN},
            )
        ).status_code == 404
        assert (
            await client.post(
                "/harness/api/runs",
                headers=headers,
                json={"question": "test", "owner_id": "another-person"},
            )
        ).status_code == 400
    assert settings.harness_state_dir.exists()
    # Restarting the host with the feature disabled leaves its data intact and
    # removes the entire application, not merely its navigation button.
    disabled: Any = create_http_app(service, settings.model_copy(update={"harness_enabled": False}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(disabled),
        base_url="http://localhost",
    ) as client:
        assert (await client.get("/harness/api/runs", headers=headers)).status_code == 404
    assert settings.harness_state_dir.exists()


@pytest.mark.parametrize("a2a_enabled", [False, True])
async def test_existing_launcher_activates_harness_without_profile_and_keeps_login_private(
    settings_factory: Any, monkeypatch: pytest.MonkeyPatch, a2a_enabled: bool,
) -> None:
    login_url = "https://login.example.test/device?code=private-harness-authorization"
    answer = "payments-service проверяет суточные лимиты перед обработкой платежа [1]."
    authorization = Event()
    calls: list[dict[str, Any]] = []

    class FakeRunner:
        def status(self, **_kwargs: Any) -> dict[str, Any]:
            return {"available": True, "version": "existing-analysis-launcher"}

        def run_json(self, **kwargs: Any) -> GigaCodeJsonResult:
            calls.append(kwargs)
            assert kwargs["decision_only"] is True
            assert kwargs["allow_edits"] is False
            assert "profile" not in kwargs
            assert kwargs["checkout"].is_dir()
            if "ready" in kwargs["schema"]["properties"]:
                kwargs["authentication_url"](login_url)
                while not authorization.wait(0.01):
                    if kwargs["cancel"].is_set():
                        raise GigaCodeCancelled("Cancelled")
                kwargs["authentication_complete"]()
                payload = {"ready": True}
            else:
                assert "payments-service checks daily limits" in kwargs["prompt"]
                payload = {"content": answer, "tool_calls": []}
            return GigaCodeJsonResult(payload, (), None, "fake-model", 1, {})

    controllers: list[Any] = []
    original_controller = activation.GigaCodeAgentController

    class TrackedController(original_controller):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings, runner=FakeRunner())
            self.stop_count = 0
            controllers.append(self)

        def stop(self) -> None:
            self.stop_count += 1
            super().stop()

    monkeypatch.setattr(activation, "GigaCodeAgentController", TrackedController)
    settings, service = _service(
        settings_factory, harness_enabled=True, a2a_enabled=a2a_enabled,
        harness_model_profile_path=None,
    )
    app: Any = create_http_app(service, settings)
    reader = {"authorization": f"Bearer {TOKEN}"}
    manager = {"x-kb-admin-password": ADMIN}

    async def wait_state(client: httpx.AsyncClient, state: str) -> dict[str, Any]:
        async with asyncio.timeout(5):
            while True:
                response = await client.get("/harness/api/model", headers=manager)
                assert response.status_code == 200, response.text
                if response.json()["state"] == state:
                    return response.json()
                await asyncio.sleep(0.01)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost",
        ) as client,
    ):
        assert len(controllers) == 1
        controller = controllers[0]
        harness = dict(app.state.extension_apps)["/harness"]
        assert harness.state.harness_runtime.model.fallback.model_control is controller
        if a2a_enabled:
            assert app.state.integrations[0].research.controller is controller
        status = await client.get("/harness/api/status", headers=reader)
        assert status.status_code == 200
        assert status.json()["model"]["state"] == "disabled"
        assert "не настроена" not in status.text
        assert not calls  # Starting the host never starts a CLI or OAuth flow.
        assert (await client.post("/harness/api/model/enable", headers=reader)).status_code == 403

        enable_path = (
            "/admin/api/a2a/runtime/enable" if a2a_enabled else "/harness/api/model/enable"
        )
        enabled = await client.post(enable_path, headers=manager, json={})
        assert enabled.status_code == 200, enabled.text
        waiting = await wait_state(client, "authentication_required")
        assert waiting["authentication_url"] == login_url
        assert waiting["events"]
        assert waiting["can_manage"] is True

        public = await client.get("/harness/api/status", headers=reader)
        assert public.json()["model"]["state"] == "authentication_required"
        assert "authentication_url" not in public.text
        assert "events" not in public.json()["model"]
        assert "private-harness-authorization" not in public.text
        reader_model = await client.get("/harness/api/model", headers=reader)
        assert reader_model.json() == {"can_manage": False}
        assert (await client.get("/harness/api/model")).status_code == 401

        authorization.set()
        assert (await wait_state(client, "ready"))["available"] is True
        response = await client.post(
            "/harness/api/runs", headers=reader,
            json={"question": "payments daily limits", "mode": "quick", "effort": "low"},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        async with asyncio.timeout(5):
            while True:
                response = await client.get(f"/harness/api/runs/{run_id}", headers=reader)
                assert response.status_code == 200
                result = response.json()
                if result["status"] not in {"queued", "running", "cancelling"}:
                    break
                await asyncio.sleep(0.01)
        assert result["status"] == "completed", result
        assert result["answer"] == answer
        assert result["evidence"]
        assert result["metrics"]["model_calls"] == 1
        assert len(calls) == 2  # One activation probe, one answer from actual RAG evidence.
        assert controller.stop_count == 0
    assert controllers[0].stop_count == 1
    assert controllers[0].status()["state"] == "disabled"
    assert all(not call["checkout"].exists() for call in calls)


def test_portable_package_has_no_host_imports() -> None:
    import ast

    package = Path(__file__).resolve().parents[1] / "src" / "agent_harness"
    forbidden = {"corporate_kb", "gigacode_graph", "service_map", "skill_registry", "dev_console"}
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] not in forbidden for alias in node.names), path
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden, path
