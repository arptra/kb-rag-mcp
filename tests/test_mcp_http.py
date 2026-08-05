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


def _indexed_service(settings_factory) -> tuple[KnowledgeService, Settings]:
    settings = settings_factory(
        mcp_http_bearer_token=TOKEN,
        benchmark_password=BENCHMARK_PASSWORD,
        admin_password=ADMIN_PASSWORD,
    )
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


def test_http_settings_require_a_strong_token_but_allow_any_external_host(
    settings_factory,
) -> None:
    with pytest.raises(ValueError, match="KB_MCP_HTTP_BEARER_TOKEN is required"):
        validate_http_settings(settings_factory())
    with pytest.raises(ValueError, match="at least 32"):
        validate_http_settings(settings_factory(mcp_http_bearer_token="short"))
    assert (
        validate_http_settings(
            settings_factory(mcp_http_host="0.0.0.0", mcp_http_bearer_token=TOKEN)
        )
        == TOKEN
    )


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
        wide_search = await anonymous_client.get(
            "/api/v1/search",
            params={"query": "daily limits", "top_k": 10},
            headers=api_headers,
        )
        assert wide_search.status_code == 200
        assert wide_search.json()["result_count"] <= settings.search_max_results

        admin_page = await anonymous_client.get("/admin")
        assert admin_page.status_code == 200
        assert "Corporate RAG Admin" in admin_page.text
        assert (await anonymous_client.get("/admin/api/overview")).status_code == 403
        admin_headers = {"X-KB-Admin-Password": ADMIN_PASSWORD}
        admin_overview = await anonymous_client.get(
            "/admin/api/overview",
            headers=admin_headers,
        )
        assert admin_overview.status_code == 200
        assert admin_overview.json()["usage"]["search_count"] == 2
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
                "kb_search",
                "kb_get_document",
                "kb_get_chunk",
                "kb_run_context_benchmark",
                "kb_list_documents",
                "kb_stats",
                "kb_search_limits",
            }
