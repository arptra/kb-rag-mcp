from __future__ import annotations

import asyncio
import json

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

        async with streamable_http_client(
            "http://testserver/mcp",
            http_client=client,
        ) as (read_stream, write_stream, _get_session_id), ClientSession(
            read_stream, write_stream
        ) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert "kb_search" in {tool.name for tool in listed.tools}


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
    repository = settings.cache_dir.parent / "sample-repository"
    openspec = repository / "openspec"
    openspec.mkdir(parents=True)
    (openspec / "system.md").write_text(
        "# System state\n\npayments-service delegates limits to limits-service.",
        encoding="utf-8",
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
            json={"name": "System OpenSpec", "description": "Current repository state"},
        )
        assert created.status_code == 201
        index_id = created.json()["id"]

        queued = await client.post(
            "/admin/api/repositories",
            headers=admin_headers,
            json={
                "name": "payments-service",
                "git_url": str(repository),
                "index_id": index_id,
            },
        )
        assert queued.status_code == 202
        job_id = queued.json()["id"]
        for _ in range(200):
            catalog = (
                await client.get("/admin/api/catalog", headers=admin_headers)
            ).json()
            job = next(item for item in catalog["jobs"] if item["id"] == job_id)
            if job["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.01)
        assert job["status"] == "completed"

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
