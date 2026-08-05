from __future__ import annotations

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


def _indexed_service(settings_factory) -> tuple[KnowledgeService, Settings]:
    settings = settings_factory(mcp_http_bearer_token=TOKEN)
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

        api_chunk = await anonymous_client.get(
            "/api/v1/chunk",
            params={"chunk_id": api_search.json()["results"][0]["chunk_id"]},
            headers=api_headers,
        )
        assert api_chunk.status_code == 200
        assert api_chunk.json()["text_tokens"] <= settings.document_context_tokens

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
                "kb_list_documents",
                "kb_stats",
            }
