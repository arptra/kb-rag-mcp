from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from corporate_kb.catalog import RagCatalog
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import KnowledgeService
from corporate_kb.usage import UsageTracker


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _module(repository: Path, name: str, endpoint: str) -> None:
    _write(repository / name / "build.gradle", "plugins { id 'java' }\n")
    _write(
        repository / name / "src/main/java/example/Controller.java",
        f"""@RestController
public class Controller {{
  @GetMapping("/{endpoint}") public String get() {{ return "ok"; }}
}}
""",
    )


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "-A")
    _git(repository, "commit", "--quiet", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "upstream"
    repository.mkdir()
    _git(repository, "init", "--quiet", "--initial-branch=main")
    _git(repository, "config", "user.name", "Refresh Test")
    _git(repository, "config", "user.email", "refresh@example.test")
    _write(repository / "settings.gradle", "include ':documented', ':code'\n")
    _module(repository, "documented", "documented")
    _module(repository, "code", "obsolete-endpoint")
    _write(
        repository / "documented/openspec/current.md",
        "# Contract\n\nObsolete upstream contract marker.\n",
    )
    _commit(repository, "Initial services")
    return repository


def _catalog(settings_factory, tmp_path: Path) -> RagCatalog:
    settings = settings_factory(
        repository_cleanup_after_scan=False,
        gigacode_command=str(tmp_path / "missing-gigacode"),
    )
    settings.knowledge_dir.mkdir(parents=True)
    service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    service.build_index(force=True)
    usage = UsageTracker()
    return RagCatalog(settings, service, KnowledgeTools(service, usage=usage), usage)


def _completed(catalog: RagCatalog, job_id: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = next(item for item in catalog.payload()["jobs"] if item["id"] == job_id)
        if job["status"] not in {"queued", "running", "cancelling"}:
            assert job["status"] == "completed", catalog.job_log(job_id)["log"]
            return job
        time.sleep(0.01)
    raise AssertionError(f"Job did not finish: {job_id}")


def _contents(catalog: RagCatalog, index_id: str) -> str:
    documents = catalog.index_documents(index_id, limit=200)
    assert not documents["has_more"]
    return "\n".join(
        catalog.index_document(index_id, item["document_id"])["content"]
        for item in documents["documents"]
    )


@pytest.mark.parametrize("ref", [None, "master"], ids=["default-main", "explicit-master"])
def test_bulk_refresh_fetches_remote_before_rebuilding_current_documents(
    settings_factory,
    tmp_path: Path,
    ref: str | None,
) -> None:
    repository = _repository(tmp_path)
    if ref:
        _git(repository, "branch", ref)
    catalog = _catalog(settings_factory, tmp_path)
    index = catalog.create_index(name="Remote services")
    _completed(
        catalog,
        catalog.start_repository_ingestion(
            name="Remote services",
            git_url=repository.as_uri(),
            ref=ref,
            index_id=index.id,
        ).id,
    )
    _completed(catalog, catalog.start_all_services_ssot_refresh().id)
    initial = catalog.payload()["repositories"][0]
    checkout = Path(initial["checkout_path"])
    assert checkout != repository
    assert checkout.is_dir()
    assert "Obsolete upstream contract marker" in _contents(catalog, index.id)
    assert "/obsolete-endpoint" in _contents(catalog, index.id)

    if ref:
        _git(repository, "switch", "--quiet", ref)
    _write(
        repository / "documented/openspec/current.md",
        "# Contract\n\nFresh upstream contract marker.\n",
    )
    _module(repository, "code", "fresh-endpoint")
    latest_commit = _commit(repository, "Update services upstream")
    if ref:
        _git(repository, "switch", "--quiet", "main")
        _write(
            repository / "documented/openspec/current.md",
            "# Contract\n\nWrong default branch contract.\n",
        )
        _commit(repository, "Divergent main must not override explicit master")
    assert _git(repository, "symbolic-ref", "--short", "HEAD") == "main"

    refreshed = _completed(catalog, catalog.start_all_services_ssot_refresh().id)

    current = catalog.payload()["repositories"][0]
    assert current["commit"] == latest_commit
    assert current["commit"] != initial["commit"]
    assert current["checkout_path"] == str(checkout)
    assert checkout.is_dir()
    assert _git(checkout, "rev-parse", "HEAD") == latest_commit
    assert refreshed["result"]["openspec_service_ids"] == ["documented"]
    assert refreshed["result"]["analyzed_service_ids"] == ["code"]
    content = _contents(catalog, index.id)
    assert "Fresh upstream contract marker" in content
    assert "/fresh-endpoint" in content
    assert "Obsolete upstream contract marker" not in content
    assert "/obsolete-endpoint" not in content
    assert "Wrong default branch contract" not in content


def test_bulk_refresh_discovers_added_services_and_removes_deleted_service_documents(
    settings_factory,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    catalog = _catalog(settings_factory, tmp_path)
    index = catalog.create_index(name="Changing services")
    _completed(
        catalog,
        catalog.start_repository_ingestion(
            name="Changing services",
            git_url=repository.as_uri(),
            index_id=index.id,
        ).id,
    )
    _completed(catalog, catalog.start_all_services_ssot_refresh().id)
    assert "/obsolete-endpoint" in _contents(catalog, index.id)

    shutil.rmtree(repository / "code")
    _write(repository / "settings.gradle", "include ':documented', ':newcomer'\n")
    _module(repository, "newcomer", "new-service-endpoint")
    _commit(repository, "Replace retired service")

    refreshed = _completed(catalog, catalog.start_all_services_ssot_refresh().id)

    assert refreshed["result"]["openspec_service_ids"] == ["documented"]
    assert refreshed["result"]["analyzed_service_ids"] == ["newcomer"]
    assert {item["id"] for item in catalog.service_map()["services"]} == {
        "documented",
        "newcomer",
    }
    content = _contents(catalog, index.id)
    assert "/new-service-endpoint" in content
    assert "/obsolete-endpoint" not in content
    paths = {item["source_path"] for item in catalog.index_documents(index.id)["documents"]}
    assert not any(path.endswith("ssot/generated/code.md") for path in paths)


def test_bulk_refresh_reads_user_owned_local_checkout_without_modifying_git_state(
    settings_factory,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    catalog = _catalog(settings_factory, tmp_path)
    index = catalog.create_index(name="Local services")
    _completed(
        catalog,
        catalog.start_repository_ingestion(
            name="Local services",
            git_url=str(repository),
            ref="main",
            index_id=index.id,
        ).id,
    )
    _git(repository, "switch", "--quiet", "-c", "local-work")
    _write(
        repository / "documented/openspec/current.md",
        "# Contract\n\nLocally committed contract marker.\n",
    )
    local_commit = _commit(repository, "User work on another branch")
    _module(repository, "code", "local-uncommitted-endpoint")
    _git(repository, "add", "code/src/main/java/example/Controller.java")
    notes = repository / "user-notes.txt"
    _write(notes, "User notes must remain unchanged.\n")
    original_status = _git(repository, "status", "--porcelain=v1")
    original_index = _git(repository, "diff", "--cached")

    _completed(catalog, catalog.start_all_services_ssot_refresh().id)

    assert _git(repository, "symbolic-ref", "--short", "HEAD") == "local-work"
    assert _git(repository, "rev-parse", "HEAD") == local_commit
    assert _git(repository, "status", "--porcelain=v1") == original_status
    assert _git(repository, "diff", "--cached") == original_index
    assert notes.read_text(encoding="utf-8") == "User notes must remain unchanged.\n"
    current = catalog.payload()["repositories"][0]
    assert current["commit"] == local_commit
    assert current["checkout_path"] == str(repository)
    assert current["checkout_state"] == "external"
    content = _contents(catalog, index.id)
    assert "Locally committed contract marker" in content
    assert "/local-uncommitted-endpoint" in content
