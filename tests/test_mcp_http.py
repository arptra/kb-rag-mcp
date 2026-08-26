from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from corporate_kb.config import Settings
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.http_server import (
    create_http_app,
    validate_http_settings,
)
from corporate_kb.mcp.servers import McpServerRegistry
from corporate_kb.service import KnowledgeService

TOKEN = "test-token-that-is-at-least-32-characters-long"
BENCHMARK_PASSWORD = "separate-benchmark-password"
ADMIN_PASSWORD = "separate-admin-password"


def _indexed_service(
    settings_factory,
    *,
    protected: bool = True,
) -> tuple[KnowledgeService, Settings]:
    auth = (
        {"mcp_http_bearer_token": TOKEN, "admin_password": ADMIN_PASSWORD}
        if protected
        else {}
    )
    settings = settings_factory(benchmark_password=BENCHMARK_PASSWORD, **auth)
    settings.benchmark_questions_path.parent.mkdir(parents=True)
    settings.benchmark_questions_path.write_text(
        json.dumps(
            [
                {
                    "question": "Кто владеет дневными лимитами?",
                    "expected_documents": ["limits.md"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "limits.md").write_text(
        "# Limits\n\nlimits-service owns daily limits.",
        encoding="utf-8",
    )
    service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    service.build_index(force=True)
    return service, settings


def test_http_settings_allow_open_access_or_a_strong_token(
    settings_factory,
) -> None:
    assert validate_http_settings(settings_factory()) is None
    assert validate_http_settings(settings_factory(mcp_http_bearer_token="")) is None
    with pytest.raises(ValueError, match="at least 32"):
        validate_http_settings(settings_factory(mcp_http_bearer_token="short"))
    assert (
        validate_http_settings(
            settings_factory(mcp_http_host="0.0.0.0", mcp_http_bearer_token=TOKEN)
        )
        == TOKEN
    )


@pytest.mark.asyncio
async def test_http_mcp_and_admin_allow_password_free_local_access(settings_factory) -> None:
    service, settings = _indexed_service(settings_factory, protected=False)
    app = create_http_app(service, settings)
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        stats = await client.get("/api/v1/stats")
        assert stats.status_code == 200
        assert stats.json()["document_count"] == 1

        overview = await client.get("/admin/api/overview")
        assert overview.status_code == 200
        assert overview.json()["index"]["document_count"] == 1
        assert overview.json()["service_map"]["service_count"] == 0
        assert (await client.get("/admin/api/service-map")).status_code == 200
        assert (
            await client.post(
                "/admin/api/jobs/cancel",
                json={"job_id": "missing-job"},
            )
        ).status_code == 404
        local_server = overview.json()["mcp_servers"]["servers"][0]
        assert local_server["name"] == "corporate-knowledge"
        assert local_server["status"] == "online"
        assert local_server["url"] == "http://testserver/mcp"
        assert {tool["name"] for tool in local_server["tools"]} >= {
            "kb_search",
            "kb_generate_system_ssot",
            "kb_get_document",
            "kb_stats",
        }
        ssot_choices = await client.post("/admin/api/analysis/ssot-generate", json={})
        assert ssot_choices.status_code == 200
        assert ssot_choices.json()["status"] == "selection_required"
        assert ssot_choices.json()["workflow"]["server_llm_required"] is False
        assert ssot_choices.json()["workflow"]["gigacode"]["server_llm_url_required"] is False
        assert ssot_choices.json()["selection"]["cloned_repository_count"] == 0
        assert ssot_choices.json()["selection"]["clone_if_missing"]["action"] == "clone"

        async with streamable_http_client(
            "http://testserver/mcp",
            http_client=client,
        ) as (read_stream, write_stream, _get_session_id), ClientSession(
            read_stream, write_stream
        ) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert {
                "kb_search",
                "kb_feature_context",
                "kb_system_graph",
                "kb_generate_system_ssot",
            } <= {
                tool.name for tool in listed.tools
            }
            ssot_tool = next(
                tool for tool in listed.tools if tool.name == "kb_generate_system_ssot"
            )
            assert ssot_tool.inputSchema["properties"]["action"]["enum"] == [
                "options",
                "clone",
                "prepare",
                "status",
                "context",
                "read_file",
                "submit",
            ]
            assert ssot_tool.inputSchema["properties"]["generation_mode"]["enum"] == [
                "client",
                "gigacode",
            ]
            ssot_options = await session.call_tool(
                "kb_generate_system_ssot",
                {"action": "options"},
            )
            assert ssot_options.isError is False
            assert ssot_options.structuredContent["workflow"]["server_llm_required"] is False
            feature = await session.call_tool(
                "kb_feature_context",
                {"feature": "daily limits"},
            )
            assert feature.isError is False
            assert feature.structuredContent["status"] == "empty_graph"
            system_graph = await session.call_tool(
                "kb_system_graph",
                {"feature": "daily limits"},
            )
            assert system_graph.isError is False
            assert system_graph.structuredContent["status"] == "empty_graph"
            assert system_graph.structuredContent["rag_queried"] is False


@pytest.mark.asyncio
async def test_admin_browses_and_uploads_documents_to_selected_index(settings_factory) -> None:
    service, settings = _indexed_service(settings_factory)
    app = create_http_app(service, settings)
    transport = httpx.ASGITransport(app=app)
    headers = {"X-KB-Admin-Password": ADMIN_PASSWORD}

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        initial = await client.get(
            "/admin/api/indexes/documents",
            headers=headers,
            params={"index_id": "default", "limit": 1},
        )
        assert initial.status_code == 200
        assert initial.json()["total"] == 1
        assert initial.json()["documents"][0]["source_path"] == "limits.md"
        document_id = initial.json()["documents"][0]["document_id"]
        detail = await client.get(
            "/admin/api/indexes/document",
            headers=headers,
            params={"index_id": "default", "document_id": document_id},
        )
        assert detail.status_code == 200
        assert detail.json()["title"] == "Limits"
        assert detail.json()["content"] == "# Limits\n\nlimits-service owns daily limits."
        assert detail.json()["index"]["id"] == "default"

        uploaded = await client.post(
            "/admin/api/indexes/documents",
            headers=headers,
            json={
                "index_id": "default",
                "documents": [
                    {
                        "path": "manual-note.yaml",
                        "content": "owner: payments-service\nstatus: current\n",
                    }
                ],
                "overwrite": False,
            },
        )
        assert uploaded.status_code == 202
        job_id = uploaded.json()["job"]["id"]
        for _ in range(200):
            catalog = (await client.get("/admin/api/catalog", headers=headers)).json()
            job = next(item for item in catalog["jobs"] if item["id"] == job_id)
            if job["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.01)
        assert job["status"] == "completed"

        filtered = await client.get(
            "/admin/api/indexes/documents",
            headers=headers,
            params={"index_id": "default", "query": "manual-note"},
        )
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 1
        assert filtered.json()["documents"][0]["origin"] == "upload"
        assert filtered.json()["documents"][0]["source_type"] == "text"

        rejected = await client.post(
            "/admin/api/indexes/documents",
            headers=headers,
            json={
                "index_id": "default",
                "documents": [{"path": "archive.zip", "content": "not really a zip"}],
            },
        )
        assert rejected.status_code == 400


@pytest.mark.asyncio
async def test_admin_registers_checks_and_deletes_external_mcp_servers(
    settings_factory,
    monkeypatch,
) -> None:
    async def fake_probe(self: McpServerRegistry, server_id: str):
        return self.update_probe(
            server_id,
            status="online",
            tools=[{"name": "remote_search", "description": "Search a remote index."}],
            error=None,
        )

    monkeypatch.setattr(McpServerRegistry, "probe", fake_probe)
    service, settings = _indexed_service(settings_factory, protected=False)
    app = create_http_app(service, settings)
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        created = await client.post(
            "/admin/api/mcp-servers",
            json={"name": "Remote docs", "url": "http://mcp.example.test/mcp"},
        )
        assert created.status_code == 201
        assert created.json()["status"] == "online"
        server_id = created.json()["id"]

        listing = (await client.get("/admin/api/mcp-servers")).json()
        assert listing["server_count"] == 2
        assert listing["online_count"] == 2
        assert listing["servers"][1]["tools"][0]["name"] == "remote_search"

        local_delete = await client.post(
            "/admin/api/mcp-servers/delete",
            json={"id": "local"},
        )
        assert local_delete.status_code == 400
        deleted = await client.post(
            "/admin/api/mcp-servers/delete",
            json={"id": server_id},
        )
        assert deleted.status_code == 200
        assert (await client.get("/admin/api/mcp-servers")).json()["server_count"] == 1


@pytest.mark.asyncio
async def test_http_mcp_rejects_missing_token_and_serves_tools_with_valid_token(
    settings_factory,
) -> None:
    service, settings = _indexed_service(settings_factory)
    app = create_http_app(service, settings)

    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as anonymous_client,
    ):
        health = await anonymous_client.get("/health")
        assert health.status_code == 200
        assert health.json()["documents"] == 1

        unauthorized_api = await anonymous_client.get("/api/v1/stats")
        assert unauthorized_api.status_code == 401

        api_headers = {"Authorization": f"Bearer {TOKEN}"}
        api_stats = await anonymous_client.get("/api/v1/stats", headers=api_headers)
        assert api_stats.status_code == 200
        assert api_stats.json()["document_count"] == 1

        api_search = await anonymous_client.get(
            "/api/v1/search",
            params={"query": "daily limits"},
            headers=api_headers,
        )
        assert api_search.status_code == 200
        assert api_search.json()["results"][0]["source_path"] == "limits.md"
        assert "text" not in api_search.json()["results"][0]

        empty_ssot = await anonymous_client.get(
            "/api/v1/ssot/context",
            params={"question": "Who owns daily limits?"},
            headers=api_headers,
        )
        assert empty_ssot.status_code == 200
        assert empty_ssot.json()["service_count"] == 0
        assert (
            await anonymous_client.get(
                "/api/v1/ssot/context",
                params={"question": "daily limits", "mode": "invalid"},
                headers=api_headers,
            )
        ).status_code == 400

        admin_page = await anonymous_client.get("/admin")
        assert admin_page.status_code == 200
        assert "RAG Control Plane" in admin_page.text
        assert (await anonymous_client.get("/admin/api/overview")).status_code == 403
        admin_headers = {"X-KB-Admin-Password": ADMIN_PASSWORD}
        admin_overview = await anonymous_client.get(
            "/admin/api/overview",
            headers=admin_headers,
        )
        assert admin_overview.status_code == 200
        assert admin_overview.json()["usage"]["search_count"] == 1
        assert admin_overview.json()["usage"]["calls_last_minute"] >= 1
        server_metrics = admin_overview.json()["server_metrics"]
        assert server_metrics["cpu_cores"] >= 1
        assert 0 <= server_metrics["load_percent"] <= 100
        assert server_metrics["peak_rss_mb"] > 0

        runtime_catalog = admin_overview.json()["tool_catalog"]
        assert runtime_catalog["built_in_count"] == 11
        assert {
            "ssot_context",
            "kb_feature_context",
            "kb_system_graph",
            "kb_search_index",
            "kb_generate_system_ssot",
            "kb_search",
            "kb_get_document",
            "kb_get_chunk",
            "kb_run_context_benchmark",
            "kb_list_documents",
            "kb_stats",
        } <= {item["name"] for item in runtime_catalog["tools"]}

        stats_test = await anonymous_client.post(
            "/admin/api/tools/test",
            headers=admin_headers,
            json={"name": "kb_stats", "arguments": {}},
        )
        assert stats_test.status_code == 200
        assert stats_test.json()["structured_content"]["document_count"] == 1
        assert stats_test.json()["elapsed_ms"] >= 0

        updated_description = (
            "Return compact RAG health, cache identity and resolved storage diagnostics."
        )
        updated_builtin = await anonymous_client.post(
            "/admin/api/tools/builtin",
            headers=admin_headers,
            json={"name": "kb_stats", "description": updated_description},
        )
        assert updated_builtin.status_code == 200
        refreshed_catalog = await anonymous_client.get(
            "/admin/api/tools/catalog",
            headers=admin_headers,
        )
        stats_definition = next(
            item for item in refreshed_catalog.json()["tools"] if item["name"] == "kb_stats"
        )
        assert stats_definition["description"] == updated_description
        assert stats_definition["description_overridden"] is True
        assert settings.builtin_tool_overrides_path.is_file()

        managed_definition = {
            "name": "kb_search_limits",
            "description": "Search only current limits knowledge and return cited excerpts.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            "defaults": {"top_k": 2, "status": "current"},
        }
        saved_tool = await anonymous_client.post(
            "/admin/api/tools",
            headers=admin_headers,
            json=managed_definition,
        )
        assert saved_tool.status_code == 201
        tool_catalog = await anonymous_client.get("/api/v1/tools", headers=api_headers)
        assert tool_catalog.json()["tools"][0]["name"] == "kb_search_limits"
        managed_call = await anonymous_client.post(
            "/api/v1/tools/call",
            headers=api_headers,
            json={"name": "kb_search_limits", "arguments": {"query": "daily limits"}},
        )
        assert managed_call.status_code == 200
        assert managed_call.json()["result_count"] == 1

        unsafe_upload = await anonymous_client.post(
            "/admin/api/documents",
            headers=admin_headers,
            json={"path": "../outside.md", "content": "unsafe", "overwrite": False},
        )
        assert unsafe_upload.status_code == 400
        uploaded = await anonymous_client.post(
            "/admin/api/documents",
            headers=admin_headers,
            json={
                "path": "new-runbook.md",
                "content": "# New Runbook\n\nRestart the worker.",
                "overwrite": False,
            },
        )
        assert uploaded.status_code == 201
        index_started = await anonymous_client.post("/admin/api/index", headers=admin_headers)
        assert index_started.status_code == 202
        for _ in range(100):
            state = (
                await anonymous_client.get("/admin/api/overview", headers=admin_headers)
            ).json()["index_job"]
            if state["status"] != "running":
                break
            await asyncio.sleep(0.02)
        assert state["status"] == "completed"
        assert state["documents"] == 2

        api_chunk = await anonymous_client.get(
            "/api/v1/chunk",
            params={"chunk_id": api_search.json()["results"][0]["chunk_id"]},
            headers=api_headers,
        )
        assert api_chunk.status_code == 200
        assert api_chunk.json()["text_tokens"] <= settings.document_context_tokens

        denied_benchmark = await anonymous_client.post(
            "/api/v1/admin/context-benchmark",
            headers={**api_headers, "X-KB-Benchmark-Password": "wrong-password-value"},
        )
        assert denied_benchmark.status_code == 403

        api_benchmark = await anonymous_client.post(
            "/api/v1/admin/context-benchmark",
            headers={**api_headers, "X-KB-Benchmark-Password": BENCHMARK_PASSWORD},
        )
        assert api_benchmark.status_code == 200
        assert api_benchmark.json()["question_count"] == 1

        missing_query = await anonymous_client.get(
            "/api/v1/search",
            headers=api_headers,
        )
        assert missing_query.status_code == 400

        unauthorized = await anonymous_client.post("/mcp", json={})
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"].startswith("Bearer")

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as authenticated_client, streamable_http_client(
            "http://testserver/mcp",
            http_client=authenticated_client,
        ) as (read_stream, write_stream, _get_session_id), ClientSession(
            read_stream, write_stream
        ) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == {
                "ssot_context",
                "kb_feature_context",
                "kb_system_graph",
                "kb_search_index",
                "kb_generate_system_ssot",
                "kb_search",
                "kb_get_document",
                "kb_get_chunk",
                "kb_run_context_benchmark",
                "kb_list_documents",
                "kb_stats",
                "kb_search_limits",
            }


@pytest.mark.asyncio
async def test_admin_manages_indexes_repositories_and_bound_tools(settings_factory) -> None:
    service, settings = _indexed_service(settings_factory)
    settings.ssot_skill_path.mkdir(parents=True)
    (settings.ssot_skill_path / "SKILL.md").write_text(
        "---\nname: build-service-ssot\ndescription: Test skill.\n---\n",
        encoding="utf-8",
    )
    repository = settings.cache_dir.parent / "sample-repository"
    openspec = repository / "openspec"
    openspec.mkdir(parents=True)
    (openspec / "system.md").write_text(
        "# System state\n\npayments-service delegates limits to limits-service.",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "rag-control-plane@example.test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "RAG Control Plane Test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", "openspec/system.md"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "Add OpenSpec"],
        cwd=repository,
        check=True,
    )
    app = create_http_app(service, settings)
    transport = httpx.ASGITransport(app=app)
    admin_headers = {"X-KB-Admin-Password": ADMIN_PASSWORD}
    api_headers = {"Authorization": f"Bearer {TOKEN}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        created = await client.post(
            "/admin/api/indexes",
            headers=admin_headers,
            json={"name": "Unused manual index", "description": "Manual index API check"},
        )
        assert created.status_code == 201

        queued = await client.post(
            "/admin/api/repositories",
            headers=admin_headers,
            json={
                "name": "payments-service",
                "git_url": repository.as_uri(),
                "index_id": None,
                "index_name": "System OpenSpec",
                "generation_mode": "static",
            },
        )
        assert queued.status_code == 202
        job_id = queued.json()["id"]
        index_id = queued.json()["index_id"]
        for _ in range(200):
            catalog = (
                await client.get("/admin/api/catalog", headers=admin_headers)
            ).json()
            job = next(item for item in catalog["jobs"] if item["id"] == job_id)
            if job["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.01)
        assert job["status"] == "completed"
        imported_index = next(item for item in catalog["indexes"] if item["id"] == index_id)
        assert imported_index["name"] == "System OpenSpec"
        assert imported_index["status"] == "ready"
        imported_repository = next(
            item for item in catalog["repositories"] if item["name"] == "payments-service"
        )
        assert imported_repository["checkout_path"] != str(repository)
        assert imported_repository["commit"]

        definition = {
            "name": "kb_search_system_state",
            "description": "Search the current OpenSpec state imported from service repositories.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            "defaults": {"top_k": 2, "status": "current"},
            "index_ids": [index_id],
        }
        saved = await client.post(
            "/admin/api/tools",
            headers=admin_headers,
            json=definition,
        )
        assert saved.status_code == 201
        result = await client.post(
            "/api/v1/tools/call",
            headers=api_headers,
            json={
                "name": "kb_search_system_state",
                "arguments": {"query": "who delegates limits"},
            },
        )
        assert result.status_code == 200
        assert result.json()["index_ids"] == [index_id]
        assert result.json()["results"][0]["index_id"] == index_id
        assert result.json()["results"][0]["source_path"].endswith("system.md")

        graph = await client.get("/admin/api/graph/overview", headers=admin_headers)
        assert graph.status_code == 200
        assert graph.json()["node_count"] >= 2
        assert {service["label"] for service in graph.json()["services"]} == {
            "payments-service"
        }
        service_map_overview = await client.get(
            "/admin/api/service-map/overview",
            headers=admin_headers,
        )
        assert service_map_overview.status_code == 200
        assert service_map_overview.json()["service_count"] == 1
        service_map = await client.get("/admin/api/service-map", headers=admin_headers)
        assert service_map.status_code == 200
        assert {item["name"] for item in service_map.json()["services"]} == {
            "payments-service"
        }
        service_id = service_map.json()["services"][0]["id"]

        filtered_graph = await client.get(
            "/admin/api/graph",
            headers=admin_headers,
            params={"view": "full", "node_types": "Service"},
        )
        assert filtered_graph.status_code == 200
        assert {item["type"] for item in filtered_graph.json()["nodes"]} == {"Service"}
        assert filtered_graph.json()["snapshot_id"]

        legacy_graph_dir = Path(imported_index["knowledge_dir"]) / "system-graph"
        legacy_graph_dir.mkdir(parents=True, exist_ok=True)
        (legacy_graph_dir / "payments-service.md").write_text(
            "---\ndocument_type: system_graph\nauthority: source-derived-graph\n---\n",
            encoding="utf-8",
        )
        graph_rebuild = await client.post(
            "/admin/api/graph/rebuild",
            headers=admin_headers,
            json={"generation_mode": "static", "verify_all": False},
        )
        assert graph_rebuild.status_code == 202
        graph_job_id = graph_rebuild.json()["id"]
        for _ in range(300):
            catalog = (
                await client.get("/admin/api/catalog", headers=admin_headers)
            ).json()
            graph_job = next(item for item in catalog["jobs"] if item["id"] == graph_job_id)
            if graph_job["status"] not in {"queued", "running", "cancelling"}:
                break
            await asyncio.sleep(0.01)
        assert graph_job["status"] == "completed"
        graph_documents = list(
            (Path(imported_index["knowledge_dir"]) / "system-graph").glob("*.md")
        )
        assert graph_documents == []
        assert graph_job["result"]["phase"] == "published"

        job_log = await client.get(
            "/admin/api/jobs/log",
            headers=admin_headers,
            params={"job_id": job_id},
        )
        assert job_log.status_code == 200
        assert "Analysis archived at" in job_log.json()["log"]

        refreshed_repository = await client.post(
            "/admin/api/repositories/refresh",
            headers=admin_headers,
            json={"repository_id": imported_repository["id"]},
        )
        assert refreshed_repository.status_code == 202
        refresh_job_id = refreshed_repository.json()["id"]
        assert refreshed_repository.json()["target_id"] == imported_repository["id"]
        for _ in range(300):
            catalog = (
                await client.get("/admin/api/catalog", headers=admin_headers)
            ).json()
            refresh_job = next(item for item in catalog["jobs"] if item["id"] == refresh_job_id)
            if refresh_job["status"] not in {"queued", "running", "cancelling"}:
                break
            await asyncio.sleep(0.01)
        assert refresh_job["status"] == "completed"
        refresh_log = await client.get(
            "/admin/api/jobs/log",
            headers=admin_headers,
            params={"job_id": refresh_job_id},
        )
        assert "OpenSpec scan ready: roots=1" in refresh_log.json()["log"]

        reanalyze = await client.post(
            "/admin/api/services/analyze",
            headers=admin_headers,
            json={"service_id": service_id},
        )
        assert reanalyze.status_code == 202
        analysis_job_id = reanalyze.json()["id"]
        for _ in range(300):
            catalog = (
                await client.get("/admin/api/catalog", headers=admin_headers)
            ).json()
            analysis_job = next(
                item for item in catalog["jobs"] if item["id"] == analysis_job_id
            )
            if analysis_job["status"] not in {"queued", "running", "cancelling"}:
                break
            await asyncio.sleep(0.01)
        assert analysis_job["status"] == "completed"

        bundle = await client.post(
            "/admin/api/analysis/ssot-bundle",
            headers=admin_headers,
            json={"service_id": service_id},
        )
        assert bundle.status_code == 201
        downloaded = await client.get(bundle.json()["download_url"], headers=admin_headers)
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"] == "application/zip"

        deleted_tool = await client.post(
            "/admin/api/tools/delete",
            headers=admin_headers,
            json={"name": definition["name"]},
        )
        assert deleted_tool.status_code == 200

        deleted_service = await client.post(
            "/admin/api/services/delete",
            headers=admin_headers,
            json={"service_id": service_id},
        )
        assert deleted_service.status_code == 202
        service_delete_job_id = deleted_service.json()["id"]
        for _ in range(300):
            catalog = (
                await client.get("/admin/api/catalog", headers=admin_headers)
            ).json()
            service_delete_job = next(
                item for item in catalog["jobs"] if item["id"] == service_delete_job_id
            )
            if service_delete_job["status"] not in {"queued", "running", "cancelling"}:
                break
            await asyncio.sleep(0.01)
        assert service_delete_job["status"] == "completed"

        deleted_repository = await client.post(
            "/admin/api/repositories/delete",
            headers=admin_headers,
            json={"repository_id": imported_repository["id"]},
        )
        assert deleted_repository.status_code == 202
        repository_delete_job_id = deleted_repository.json()["id"]
        for _ in range(300):
            catalog = (
                await client.get("/admin/api/catalog", headers=admin_headers)
            ).json()
            repository_delete_job = next(
                item for item in catalog["jobs"] if item["id"] == repository_delete_job_id
            )
            if repository_delete_job["status"] not in {"queued", "running", "cancelling"}:
                break
            await asyncio.sleep(0.01)
        assert repository_delete_job["status"] == "completed"
        assert catalog["repository_count"] == 0
