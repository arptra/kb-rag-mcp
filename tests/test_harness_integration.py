from __future__ import annotations

import asyncio
import builtins
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from corporate_kb.access.http import USER_COOKIE
from corporate_kb.access.store import AccessStore
from corporate_kb.access.tls import identity_from_der
from corporate_kb.agents import activation
from corporate_kb.config import Settings
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.gigacode_runner import GigaCodeCancelled, GigaCodeJsonResult, GigaCodePolicyStop
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


@pytest.mark.parametrize("admin_password", [None, ADMIN])
async def test_control_requires_explicit_admin_authority_even_in_legacy_reader_mode(
    settings_factory: Any, admin_password: str | None,
) -> None:
    settings, service = _service(
        settings_factory, harness_enabled=True, admin_password=admin_password,
    )
    app: Any = create_http_app(service, settings)
    reader = {"authorization": f"Bearer {TOKEN}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost",
        ) as client,
    ):
        status = await client.get("/harness/api/status", headers=reader)
        assert status.status_code == 200
        assert status.json()["can_manage_control"] is False
        for endpoint in ("settings", "metrics"):
            assert (
                await client.get(f"/harness/api/control/{endpoint}", headers=reader)
            ).status_code == 403
        assert (
            await client.put("/harness/api/control/settings", json={}, headers=reader)
        ).status_code == 403
        assert (
            await client.post("/harness/api/control/runs/unknown/cancel", json={}, headers=reader)
        ).status_code == 403
        assert (
            await client.get(
                "/harness/api/control/settings",
                headers={**reader, "x-kb-admin-password": "invented-administrator-password"},
            )
        ).status_code == 403
        if admin_password is not None:
            admin = {"x-kb-admin-password": admin_password}
            status = await client.get("/harness/api/status", headers=admin)
            assert status.status_code == 200
            assert status.json()["can_manage_control"] is True
            response = await client.get("/harness/api/control/settings", headers=admin)
            assert response.status_code == 200
            assert response.json()["can_manage"] is True
            assert (
                await client.get("/harness/api/control/metrics", headers=admin)
            ).status_code == 200
            payload = response.json()
            assert (
                await client.put("/harness/api/control/settings", headers=admin,
                                 json={"revision": payload["revision"],
                                       "settings": payload["settings"]})
            ).status_code == 200
            assert (
                await client.post(
                    "/harness/api/control/runs/unknown/cancel", json={}, headers=admin,
                )
            ).status_code == 404


async def test_cookie_admin_saves_model_and_tls_only_with_session_csrf(
    settings_factory: Any, tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Harness test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    client_key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "harness-user")]))
        .issuer_name(ca_name).public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=10))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    settings, service = _service(
        settings_factory, harness_enabled=True, access_enabled=True, mcp_tls_enabled=True,
        access_db_path=tmp_path / "access.sqlite3", access_bootstrap_admin_password=ADMIN,
    )
    app: Any = create_http_app(service, settings)
    issued = AccessStore(settings.access_db_path).enroll(
        identity_from_der(certificate.public_bytes(serialization.Encoding.DER)), purpose="browser",
    )
    bundle = {
        "id": "corporate-model", "name": "Corporate model",
        "ca_certificate": ca.public_bytes(serialization.Encoding.PEM).decode(),
        "client_certificate": certificate.public_bytes(serialization.Encoding.PEM).decode(),
        "client_key": client_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    }
    connection = {
        "provider": "openai", "base_url": "https://model.example.test/v1",
        "model": "corporate-model", "tls_bundle_id": bundle["id"],
    }
    model_path, tls_path = "/harness/api/model/connection", "/harness/api/credentials/tls"
    origin = {"origin": "https://testserver"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://testserver",
        ) as client,
    ):
        client.cookies.set(USER_COOKIE, issued.token)
        assert (await client.get("/harness/api/status")).status_code == 200
        for path in (model_path, tls_path):
            assert (await client.get(path)).status_code == 403
        login = await client.post(
            "/access/api/login", json={"username": "admin", "password": ADMIN}, headers=origin,
        )
        assert login.status_code == 200, login.text
        assert (await client.get(model_path)).status_code == 200
        assert (await client.get(tls_path)).json()["credentials"] == []
        # Admin cookies authorize reads, but every write also needs the session CSRF token.
        assert (await client.put(model_path, json=connection, headers=origin)).status_code == 403
        assert (await client.post(tls_path, json=bundle, headers=origin)).status_code == 403
        session = await client.get("/access/api/session")
        assert session.status_code == 200
        headers = {**origin, "x-csrf-token": session.json()["csrf_token"]}
        uploaded = await client.post(tls_path, json=bundle, headers=headers)
        assert uploaded.status_code == 201, uploaded.text
        saved = await client.put(model_path, json=connection, headers=headers)
        assert saved.status_code == 200, saved.text
        reread = (await client.get(model_path)).json()["settings"]
        assert all(reread[key] == value for key, value in connection.items())
        assert reread["api_key_env"] == ""  # Certificate-only access needs no API key.
        certificates = await client.get(tls_path)
        assert certificates.json()["credentials"][0]["id"] == bundle["id"]
        assert certificates.json()["credentials"][0]["has_client_certificate"] is True
        assert "client_key" not in certificates.json()["credentials"][0]
        assert bundle["client_key"].splitlines()[1] not in certificates.text
        assert (
            await client.post("/access/api/logout", json={}, headers=headers)
        ).status_code == 200
        # Personal access and a formerly valid CSRF token never grant admin rights.
        assert (await client.get("/harness/api/status")).status_code == 200
        for path in (model_path, tls_path):
            assert (await client.get(path)).status_code == 403
        assert (await client.put(model_path, json=connection, headers=headers)).status_code == 403
        assert (await client.post(tls_path, json=bundle, headers=headers)).status_code == 403


@pytest.mark.parametrize(("a2a_enabled", "policy_stop"), [
    (False, False), (True, False), (False, True),
])
async def test_existing_launcher_activates_harness_without_profile_and_keeps_login_private(
    settings_factory: Any, monkeypatch: pytest.MonkeyPatch, a2a_enabled: bool, policy_stop: bool,
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
            elif "RouteTurn" in kwargs["prompt"]:
                assert "payments-service checks daily limits" not in kwargs["prompt"]
                payload = {"content": "", "tool_calls": [{
                    "name": "RouteTurn", "args": {"route": "chat", "answer": "56"},
                }]}
                if a2a_enabled and len(calls) == 2:
                    # A truncated tool argument is rejected by the actual CLI controller.
                    payload["tool_calls"][0]["args"] = {"answer": "56"}
            else:
                assert "payments-service checks daily limits" in kwargs["prompt"]
                if policy_stop:
                    raise GigaCodePolicyStop()
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

    async def finished_run(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
        async with asyncio.timeout(5):
            while True:
                response = await client.get(f"/harness/api/runs/{run_id}", headers=reader)
                assert response.status_code == 200
                result: dict[str, Any] = response.json()
                if result["status"] not in {"queued", "running", "cancelling"}:
                    return result
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
        greeting = await client.post("/harness/api/runs", headers=reader, json={
            "question": "Привет, как дела?", "routing": "auto", "mode": "autonomous",
        })
        assert greeting.json()["status"] == "completed"
        assert greeting.json()["metrics"]["source_calls"] == 0
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
        chat = await client.post("/harness/api/runs", headers=reader, json={
            "question": "Сколько будет семь раз восемь?", "routing": "auto",
        })
        assert chat.status_code == 202
        social = await finished_run(client, chat.json()["id"])
        assert social["answer"] == "56" and social["status"] == "completed"
        chat_calls = 2 if a2a_enabled else 1
        assert social["metrics"]["model_calls"] == chat_calls
        assert social["metrics"]["source_calls"] == 0
        response = await client.post(
            "/harness/api/runs", headers=reader,
            json={"question": "payments daily limits", "mode": "quick", "effort": "low"},
        )
        assert response.status_code == 202, response.text
        result = await finished_run(client, response.json()["id"])
        if policy_stop:
            assert result["status"] == "partial", result
            assert result["stop_reason"] == "model_policy_stop"
            assert result["answer_kind"] == "restricted"
            assert result["provider_diagnostic"]["signal"] == "refusal"
            assert controller.status()["available"]
        else:
            assert result["status"] == "completed", result
            assert result["answer"] == answer
        assert result["evidence"]
        assert result["metrics"]["model_calls"] == 1
        assert len(calls) == 2 + chat_calls  # Activation, chat and an answer from RAG evidence.
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


async def test_harness_reads_original_document_beyond_portal_excerpt(settings_factory: Any) -> None:
    from types import SimpleNamespace

    from agent_harness.contracts import Principal
    from corporate_kb.harness_integration import CatalogSource
    from corporate_kb.mcp.tools import KnowledgeTools

    settings, service = _service(settings_factory)
    full_text = '# Deep payments\n\n' + ('Payment operation and checks.\n' * 500)
    full_text += '\nTAIL_VERIFIED_BEHAVIOR: rollback after a rejected transaction.\n'
    (settings.knowledge_dir / 'deep.md').write_text(full_text, encoding='utf-8')
    service.build_index(force=True)
    documents = service.search('TAIL_VERIFIED_BEHAVIOR rollback', top_k=10)
    document_id = next(item.document_id for item in documents if item.title == 'Deep payments')
    tools = KnowledgeTools(service)
    compressed = tools.get_document(document_id=document_id)
    assert len(compressed['content']) < len(full_text)
    authorized = True

    async def check(principal: Principal) -> bool:
        return authorized

    catalog = SimpleNamespace(service_for=lambda index: service, tools_for=lambda index: tools)
    source = CatalogSource(catalog, check)
    actual = await source.read(document_id=document_id, index_id='main',
                               principal=Principal('alice'), cancel=Event())
    assert 'TAIL_VERIFIED_BEHAVIOR' in actual.text
    assert len(actual.text) > 12_000
    authorized = False
    with pytest.raises(PermissionError):
        await source.read(document_id=document_id, index_id='main',
                          principal=Principal('alice'), cancel=Event())


def test_distinct_search_chunks_keep_document_id_for_reading() -> None:
    from corporate_kb.harness_integration import CatalogSource

    first = CatalogSource._evidence({
        'document_id': 'doc', 'chunk_id': 'chunk-1', 'excerpt': 'First relevant section',
    }, 'main')
    second = CatalogSource._evidence({
        'document_id': 'doc', 'chunk_id': 'chunk-2', 'excerpt': 'Second relevant section',
    }, 'main')
    assert first.id != second.id
    assert first.document_id == second.document_id == 'doc'


async def test_search_returns_source_chunks_before_excerpt_packing_and_reads_deep_anchor(
    settings_factory: Any,
) -> None:
    from agent_harness.contracts import Principal
    from agent_harness.search_results import source_passage
    from corporate_kb.catalog import RagCatalog
    from corporate_kb.harness_integration import CatalogSource
    from corporate_kb.mcp.tools import KnowledgeTools
    from corporate_kb.usage import UsageTracker

    settings, service = _service(settings_factory, search_excerpt_tokens=40)
    filler = "Проверен регламент. " * 45
    body = "Заявка ORION-7735. Комиссия приняла установку. Итоговый показатель 671."
    page = "# Протокол 17\n\n" + ((filler + "\n\n") * 30) + body + "\n\n" + filler
    (settings.knowledge_dir / "sheet.md").write_text(page, encoding="utf-8")
    service.build_index(force=True)
    usage = UsageTracker()
    catalog = RagCatalog(settings, service, KnowledgeTools(service, usage=usage), usage)

    async def permitted(_principal: Principal) -> bool:
        return True

    source = CatalogSource(catalog, permitted)
    scope = {"index_id": "default", "principal": Principal("user"), "cancel": Event()}
    hits = await source.search(query="Заявка ORION-7735 комиссия приняла установку",
                               limit=5, **scope)
    hit = next(item for item in hits if body in item.text)
    doc = await source.read(document_id=hit.document_id, **scope)
    assert doc.title == "Протокол 17" and len(doc.text) > 10_000
    excerpt = source_passage(doc.text, "Каковы итоги приемки?", 1000, anchors=[hit.text])
    assert body in excerpt


async def test_harness_finds_body_outside_vector_window_and_reuses_only_current_postings(
    settings_factory: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3
    from threading import get_ident

    import numpy as np

    from agent_harness.contracts import Principal
    from corporate_kb.catalog import RagCatalog
    from corporate_kb.harness_integration import CatalogSource
    from corporate_kb.mcp.tools import KnowledgeTools
    from corporate_kb.models import SearchFilters
    from corporate_kb.stores.fulltext import ChunkTextIndex
    from corporate_kb.usage import UsageTracker

    settings, service = _service(settings_factory, search_candidate_k=20)
    sample = service.search("payments", top_k=1)[0]
    doc = service.get_document(sample.document_id)
    chunk = service.get_chunk(sample.chunk_id)
    docs, chunks = [], []
    # More than twenty better vector hits and more than twenty forbidden, stronger
    # lexical matches. Neither top-K cutoff should hide the allowed body match.
    kinds = ["other"] * 24 + ["hidden", "old", "graph", "retired"] * 8 + ["target"]
    for i, kind in enumerate(kinds):
        key = f"record-{i}"
        body = "ordinary details" if kind == "other" else "ORION-7735"
        if kind == "target":
            body += " passed with coefficient 671."
        page = "private" if kind == "hidden" else key
        meta = {"status": "archived" if kind == "retired" else "current"}
        if kind == "graph":
            meta["document_type"] = "system_graph"
        common = dict(document_id=key, page_id=page, title="Minutes", is_latest=kind != "old",
                      metadata=meta)
        docs.append(doc.model_copy(update={**common, "content": body}))
        chunks.append(chunk.model_copy(update={**common, "chunk_id": key, "text": body}))
    matrix = np.zeros((len(chunks), service.provider.dimension), dtype=np.float32)
    matrix[:, 0] = 1
    matrix[-1, :2] = [0, 1]
    service.store.replace_index(docs, chunks, matrix)
    service.set_page_visibility("private", False)
    vector = matrix[0]
    calls: list[str] = []

    def embed(query: str) -> Any:
        calls.append(query)
        return vector

    monkeypatch.setattr(service.provider, "embed_query", embed)
    builds: list[int] = []
    connections: list[sqlite3.Connection] = []
    original_connect = sqlite3.connect

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = original_connect(database, *args, **kwargs)
        if database == ":memory:":
            def trace(sql: str) -> None:
                if sql.startswith("CREATE VIRTUAL TABLE body "):
                    builds.append(get_ident())
                    connections.append(db)
            db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(sqlite3, "connect", connect)
    tool = KnowledgeTools(service)
    target = docs[-1].document_id
    assert target not in {r.document_id for r in tool.search_candidates(
        "ORION-7735", 20, filters=SearchFilters(status="current"))}
    assert not builds

    async def permit(_principal: Principal) -> bool:
        return True

    catalog = RagCatalog(settings, service, tool, UsageTracker())
    source = CatalogSource(catalog, permit)
    scope = {"index_id": "default", "principal": Principal("user"), "cancel": Event()}
    hits, again = await asyncio.gather(
        source.search(query="ORION-7735", limit=20, **scope),
        source.search(query="ORION-7735", limit=1, **scope),
    )
    allowed = {d.document_id for d in [*docs[:24], docs[-1]]}
    assert again[0].document_id == hits[0].document_id == target
    assert all(hit.document_id in allowed for hit in hits)
    assert len(builds) == 1 and builds[0] != get_ident()
    assert calls == ["ORION-7735"] * 3
    service.set_page_visibility(target, False)
    assert target not in {hit.document_id for hit in await source.search(
        query="ORION-7735", limit=20, **scope)}
    service.set_page_visibility(target, True)
    assert len(builds) == 1

    replacement = "Separate body containing TRITON-4821."
    docs[-1] = docs[-1].model_copy(update={"content": replacement})
    chunks[-1] = chunks[-1].model_copy(update={"text": replacement})
    reading_old, release_old = Event(), Event()
    original_search = ChunkTextIndex.search

    def pause_old(index: ChunkTextIndex, text: str, *args: Any) -> list[int]:
        if text == "ORION-7735":
            reading_old.set()
            assert release_old.wait(10)
        return original_search(index, text, *args)

    monkeypatch.setattr(ChunkTextIndex, "search", pause_old)
    in_flight = asyncio.create_task(source.search(query="ORION-7735", limit=1, **scope))
    try:
        assert await asyncio.to_thread(reading_old.wait, 5)
        service.store.replace_index(docs, chunks, matrix)
        new = await source.search(query="TRITON-4821", limit=1, **scope)
        assert new[0].document_id == target and new[0].text == replacement
        assert connections[0].execute("SELECT 1").fetchone() == (1,)
    finally:
        release_old.set()
    previous = await in_flight
    assert previous[0].document_id == target and "671" in previous[0].text
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    old = await source.search(query="ORION-7735", limit=20, **scope)
    assert all(hit.document_id != target for hit in old)
    assert len(builds) == 2 and all(thread != get_ident() for thread in builds)
    assert len(calls) == 7


async def test_harness_catalog_does_not_read_admin_job_history(
    settings_factory: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_harness.contracts import Principal
    from corporate_kb.catalog import RagCatalog
    from corporate_kb.harness_integration import CatalogSource
    from corporate_kb.mcp.tools import KnowledgeTools
    from corporate_kb.usage import UsageTracker

    settings, service = _service(settings_factory)
    usage = UsageTracker()
    catalog = RagCatalog(settings, service, KnowledgeTools(service, usage=usage), usage)
    managed = catalog.create_index(name="Сводки", description="Итоги испытаний по подразделениям")

    def no_admin_payload() -> Any:
        pytest.fail("search loaded admin catalog/analysis history")

    monkeypatch.setattr(catalog, "payload", no_admin_payload)

    async def permitted(_principal: Principal) -> bool:
        return True

    sources = await CatalogSource(catalog, permitted).indexes(Principal("user"))
    assert next(item for item in sources if item["id"] == managed.id) == {
        "id": managed.id, "name": managed.name,
        "description": managed.description, "revision": managed.updated_at.isoformat(),
    }
