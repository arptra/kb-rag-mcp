from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from corporate_kb.confluence.models import Page
from corporate_kb.confluence.registry import ConfluenceRegistry
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.service import KnowledgeService


class Source:
    version = 1
    cancel = threading.Event()

    def close(self):
        pass

    def root_id(self):
        return "1"

    def page_ids(self, root):
        yield root
        yield "2"

    def page(self, page_id, *, body=True, version=None):
        revision = version or self.version
        return Page(
            id=page_id,
            title=f"Page {page_id}",
            version=revision,
            updated_at=f"2026-01-0{revision}T12:00:00Z",
            url=f"https://confluence.example/pages/{page_id}",
            body=f"<h1>Page {page_id}</h1><p>Limit is {100 * revision}</p>" if body else "",
        )

    def attachments(self, page_id):
        return []

    def versions(self, page_id):
        return list(range(1, self.version + 1))


@pytest.mark.asyncio
async def test_confluence_http_to_history_and_published_index(settings_factory):
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "existing.md").write_text("# Existing\n\nPreserved knowledge.")
    service = KnowledgeService(
        settings, provider=HashEmbeddingProvider(settings.embedding_dimension)
    )
    service.build_index(force=True)
    app = create_http_app(service, settings)
    registry = next(item for item in app.state.integrations if isinstance(item, ConfluenceRegistry))
    remote = Source()
    registry.client_factory = lambda config, token: remote
    # Deliberately do not start scheduler; control both jobs deterministically.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        payload = {
            "name": "Docs",
            "index_id": "default",
            "base_url": "https://confluence.example",
            "root_page_url": "https://confluence.example/pages/1",
            "token": "secret-do-not-return",
            "schedule": {"kind": "manual"},
            "analyze_images": False,
            "include_history": True,
        }
        created = await client.post("/admin/api/confluence/sources", json=payload)
        assert created.status_code == 200, created.text
        assert "secret-do-not-return" not in created.text
        source = created.json()["source"]
        first = (await client.post(f"/admin/api/confluence/sources/{source['id']}/sync")).json()[
            "job"
        ]
        completed = await asyncio.to_thread(registry.run_job, first["id"])
        assert completed["status"] == "completed", completed
        assert service.stats().document_count == 3
        latest = service.read_page(f"confluence:{source['id']}:1")
        assert latest.source_version == "1"
        remote.version = 2
        second = (await client.post(f"/admin/api/confluence/sources/{source['id']}/sync")).json()[
            "job"
        ]
        completed = await asyncio.to_thread(registry.run_job, second["id"])
        assert completed["status"] == "completed", completed
        current = service.read_page(latest.page_id)
        assert current.source_version == "2" and current.document_id != latest.document_id
        assert service.stats().document_count == 3
        versions = (
            await client.get(
                "/admin/api/indexes/document/versions",
                params={"index_id": "default", "page_id": latest.page_id},
            )
        ).json()["versions"]
        assert len(versions) == 2
        assert versions[0]["is_latest"] is True and versions[1]["is_latest"] is False
        old = (
            await client.get(
                "/admin/api/indexes/document",
                params={"index_id": "default", "document_id": latest.document_id},
            )
        ).json()
        assert old["source_version"] == "1" and old["is_latest"] is False
        assert "100" in old["content"]
        logs = (await client.get(f"/admin/api/confluence/jobs/{second['id']}/log")).json()
        assert any(item["stage"] == "indexing" for item in logs["items"])
        assert all("secret-do-not-return" not in str(item) for item in logs["items"])
