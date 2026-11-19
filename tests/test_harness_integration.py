from __future__ import annotations

import asyncio
import builtins
from pathlib import Path
from typing import Any

import httpx
import pytest

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.service import KnowledgeService

TOKEN = "harness-integration-token-at-least-32-characters"
ADMIN = "harness-administrator-password"


def _service(settings_factory: Any, **overrides: Any) -> tuple[Any, KnowledgeService]:
    settings = settings_factory(
        confluence_enabled=False,
        a2a_enabled=False,
        skills_registry_enabled=False,
        mcp_tls_enabled=False,
        mcp_http_bearer_token=TOKEN,
        admin_password=ADMIN,
        **overrides,
    )
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
