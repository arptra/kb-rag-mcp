# ruff: noqa: RUF001
from __future__ import annotations

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.service import KnowledgeService, create_ssot_service
from corporate_kb.ssot import SsotContextBuilder


def _write_ssot(path, *, service: str, content: str) -> None:
    path.write_text(
        f"""---
document_type: ssot
service: {service}
domain: payments
status: current
commit_sha: abc123
---
# {service}

{content}
""",
        encoding="utf-8",
    )


def test_ssot_context_discovers_referenced_service_and_groups_evidence(settings_factory) -> None:
    settings = settings_factory(
        ssot_candidate_k=10,
        ssot_max_services=4,
        ssot_facts_per_service=2,
        ssot_fact_tokens=80,
        ssot_context_tokens=1500,
    )
    settings.knowledge_dir.mkdir(parents=True)
    _write_ssot(
        settings.knowledge_dir / "payments.md",
        service="payments-service",
        content="""## Responsibility

payments-service orchestrates payment execution. Before payment it requests a daily limit decision
from limits-service. It must not calculate limits locally.
""",
    )
    _write_ssot(
        settings.knowledge_dir / "limits.md",
        service="limits-service",
        content="""## Business rules

limits-service owns daily limit calculation. A positive decision is required before payment.
""",
    )
    # A relevant-looking non-SSOT document must not become authoritative context.
    (settings.knowledge_dir / "old-notes.md").write_text(
        """---
document_type: meeting_notes
service: legacy-service
status: current
---
# Old notes

Daily limits and payment implementation ideas.
""",
        encoding="utf-8",
    )
    service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    service.build_index(force=True)

    payload = SsotContextBuilder(service).build(
        question="Как изменить оплату с повторной проверкой дневного лимита?",
        mode="implementation",
    )

    service_ids = {item["service"] for item in payload["services"]}
    assert service_ids == {"payments-service", "limits-service"}
    assert "legacy-service" not in service_ids
    assert payload["connections"] == [
        {
            "from": "payments-service",
            "mentions": "limits-service",
            "evidence_id": payload["connections"][0]["evidence_id"],
        }
    ]
    assert payload["context_token_count"] <= settings.ssot_context_tokens
    assert all(item["facts"] for item in payload["services"])
    assert all(
        fact["revision"] == "abc123"
        for item in payload["services"]
        for fact in item["facts"]
    )


def test_ssot_context_reports_missing_index_metadata(settings_factory) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "plain.md").write_text(
        "# Plain document\n\nNo SSOT metadata is present.",
        encoding="utf-8",
    )
    service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    service.build_index(force=True)

    payload = SsotContextBuilder(service).build(question="Кто владеет платежами?")

    assert payload["service_count"] == 0
    assert payload["context_token_count"] == 0
    assert payload["missing_information"]


def test_ssot_service_uses_separate_source_and_cache_directories(settings_factory) -> None:
    settings = settings_factory()
    settings = settings.model_copy(
        update={
            "ssot_knowledge_dir": settings.knowledge_dir.parent / "ssot",
            "ssot_cache_dir": settings.cache_dir.parent / "ssot-cache",
        }
    )

    service = create_ssot_service(settings)

    assert service.settings.knowledge_dir == settings.ssot_knowledge_dir
    assert service.settings.cache_dir == settings.ssot_cache_dir
