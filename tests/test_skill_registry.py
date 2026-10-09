"""Integration coverage for immutable releases from real, local Git repositories."""

from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from skill_registry import SkillsRegistry
from skill_registry.models import SourceConfig


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def commit(repository: Path, message: str = "update") -> str:
    git(repository, "add", "-A")
    git(repository, "commit", "-qm", message)
    return git(repository, "rev-parse", "HEAD")


def write_skill(repository: Path, directory: str = "skills/example", body: str = "First") -> Path:
    package = repository / directory
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        "---\nname: example\ndescription: Explain an example task\n---\n\n" + body + "\n",
        encoding="utf-8",
    )
    (package / "references").mkdir(exist_ok=True)
    (package / "references" / "contract.md").write_text("Contract one\n", encoding="utf-8")
    return package


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    directory = tmp_path / "repository"
    directory.mkdir()
    git(directory, "init", "-q", "-b", "main")
    git(directory, "config", "user.email", "skills-test@example.test")
    git(directory, "config", "user.name", "Skills registry test")
    write_skill(directory)
    commit(directory, "first")
    return directory


@pytest.fixture
def registry(tmp_path: Path) -> SkillsRegistry:
    return SkillsRegistry(tmp_path / "registry")


def add_source(registry: SkillsRegistry, repository: Path, **overrides: Any) -> dict[str, Any]:
    return registry.save_source(
        {
            "name": "Example source",
            "git_url": repository.as_uri(),
            "interval_minutes": 0,
            **overrides,
        }
    )


def first_skill(registry: SkillsRegistry) -> dict[str, Any]:
    return registry.list_skills()["skills"][0]


def test_warning_schema_migration_preserves_existing_releases(
    registry: SkillsRegistry, repository: Path,
) -> None:
    source = add_source(registry, repository)
    registry.sync_now(source["id"])
    skill = first_skill(registry)
    original = registry.get_release(skill["id"])
    with registry._connect() as connection:
        connection.execute("ALTER TABLE sources DROP COLUMN last_warnings_json")
    reader = SkillsRegistry(registry.root, read_only=True)
    assert reader.list_sources()[0]["last_warnings"] == []
    migrated = SkillsRegistry(registry.root)
    assert migrated.list_sources()[0]["last_warnings"] == []
    assert migrated.get_release(skill["id"]) == original
    with migrated._connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(sources)")}
    assert "last_warnings_json" in columns


def test_content_versions_include_support_files_and_ignore_unrelated_commits(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    source = add_source(registry, repository)
    job = registry.sync_now(source["id"])
    assert job["status"] == "succeeded", job
    assert job["result"]["created"] == 1
    skill = first_skill(registry)
    first = registry.get_release(skill["id"])
    assert first["version"] == 1
    assert first["commit"] == git(repository, "rev-parse", "HEAD")
    assert [file["path"] for file in first["files"]] == ["SKILL.md", "references/contract.md"]

    (repository / "README.md").write_text("Unrelated repository docs\n")
    commit(repository)
    second_job = registry.sync_now(source["id"])
    assert second_job["result"]["created"] == 0
    assert registry.get_release(skill["id"])["revision"] == first["revision"]
    assert registry.get_release(skill["id"])["commit"] == first["commit"]

    (repository / "skills/example/references/contract.md").write_text("Contract two\n")
    commit(repository)
    assert registry.sync_now(source["id"])["result"]["created"] == 1
    second = registry.get_release(skill["id"])
    assert second["version"] == 2
    assert first["revision"] != second["revision"]
    assert (
        registry.read_file(skill["id"], first["revision"], "references/contract.md")["content"]
        == "Contract one\n"
    )
    diff = registry.diff(skill["id"], first["revision"], second["revision"])
    assert diff["files"][0]["status"] == "modified"
    assert "+Contract two" in diff["files"][0]["diff"]

    registry.publish(skill["id"], first["revision"])
    assert registry.get_release(skill["id"])["revision"] == first["revision"]
    registry.sync_now(source["id"])
    assert registry.get_release(skill["id"])["revision"] == first["revision"]
    assert registry.skill_detail(skill["id"])["skill"]["latest_revision"] == second["revision"]


def test_invalid_source_update_preserves_all_published_snapshots(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    source = add_source(registry, repository, interval_minutes=1)
    registry.sync_now(source["id"])
    skill = first_skill(registry)
    release = registry.get_release(skill["id"])
    last_success = registry.list_sources()[0]["last_success_at"]
    (repository / "skills/example/SKILL.md").write_text("No YAML metadata\n")
    commit(repository)
    partial = registry.sync_now(source["id"])
    assert partial["status"] == "succeeded_with_warnings"
    assert partial["error"] is None
    assert partial["result"]["valid"] == 0
    assert partial["result"]["skipped"] == 1
    assert "frontmatter" in partial["result"]["warnings"][0]["error"]
    assert registry.get_release(skill["id"]) == release
    source = registry.list_sources()[0]
    assert source["last_success_at"] == last_success
    assert source["last_error"] is None
    assert source["last_warnings"] == partial["result"]["warnings"]
    assert source["failure_count"] == 0
    scheduled_delay = (
        datetime.fromisoformat(source["next_check_at"])
        - datetime.fromisoformat(source["last_checked_at"])
    ).total_seconds()
    assert 59 <= scheduled_delay <= 61


def test_manual_publish_candidates_rollback_and_client_update_status(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    source = add_source(registry, repository, auto_publish=False)
    registry.sync_now(source["id"])
    skill = first_skill(registry)
    with pytest.raises(ValueError, match="no published"):
        registry.get_release(skill["id"])
    first_revision = skill["latest_revision"]
    assert registry.get_release(skill["id"], first_revision)["version"] == 1
    registry.publish(skill["id"], first_revision)
    write_skill(repository, body="Second")
    commit(repository)
    registry.sync_now(source["id"])
    skill = first_skill(registry)
    assert skill["latest_revision"] != first_revision
    assert registry.get_release(skill["id"])["revision"] == first_revision
    registry.publish(skill["id"], skill["latest_revision"])
    installed = {"skill_id": skill["id"], "revision": first_revision}
    updates = registry.check_updates([installed])
    assert updates["updates"][0]["status"] == "update_available"
    assert (
        registry.check_updates([{**installed, "pinned": True}])["updates"][0]["status"] == "pinned"
    )
    registry.publish(skill["id"], first_revision)
    assert registry.check_updates([installed])["updates"][0]["status"] == "current"


def test_multiple_paths_refs_and_dirty_local_files_are_isolated(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    main_commit = git(repository, "rev-parse", "HEAD")
    write_skill(repository, "team-skills/example", body="Team")
    commit(repository)
    git(repository, "branch", "team")
    (repository / "skills/example/SKILL.md").write_text("Dirty uncommitted content\n")
    main = add_source(registry, repository, name="Main", ref=main_commit)
    team = add_source(registry, repository, name="Team", ref="team", skills_path="team-skills")
    assert registry.sync_now(main["id"])["status"] == "succeeded"
    assert registry.sync_now(team["id"])["status"] == "succeeded"
    skills = registry.list_skills()["skills"]
    assert len(skills) == 2
    assert len({skill["id"] for skill in skills}) == 2
    for skill in skills:
        release = registry.get_release(skill["id"])
        assert release["commit"] == (
            main_commit
            if skill["source_id"] == main["id"]
            else git(repository, "rev-parse", "team")
        )
        assert "Dirty" not in registry.read_file(skill["id"], None, "SKILL.md")["content"]


def test_symlink_cannot_escape_package_and_previous_release_remains(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    source = add_source(registry, repository)
    registry.sync_now(source["id"])
    skill = first_skill(registry)
    release = registry.get_release(skill["id"])
    (repository / "skills/example/references/escape").symlink_to("/etc/passwd")
    commit(repository)
    job = registry.sync_now(source["id"])
    assert job["status"] == "succeeded_with_warnings"
    assert job["result"]["skipped"] == 1
    assert "symlink" in job["result"]["warnings"][0]["error"].lower()
    assert registry.get_release(skill["id"]) == release
    with pytest.raises(ValueError, match="Path"):
        registry.read_file(skill["id"], release["revision"], "../SKILL.md")
    with pytest.raises(ValueError, match="Path"):
        registry.read_file(skill["id"], release["revision"], "/etc/passwd")


def test_archival_preserves_history_and_never_requests_client_deletion(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    source = add_source(registry, repository)
    registry.sync_now(source["id"])
    skill = first_skill(registry)
    release = registry.get_release(skill["id"])
    archived = registry.delete_source(source["id"])
    assert archived["archived"] and not archived["enabled"]
    assert archived["next_check_at"] is None
    assert registry.get_release(skill["id"], release["revision"]) == release
    assert registry.skill_detail(skill["id"])["skill"]["retired"]
    with pytest.raises(ValueError, match="retired"):
        registry.get_release(skill["id"])
    status = registry.check_updates([{"skill_id": skill["id"], "revision": release["revision"]}])
    assert status["updates"][0]["status"] == "retired"
    assert not status["updates"][0]["update_available"]


def test_removed_skill_retires_but_other_skills_continue(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    write_skill(repository, "skills/another")
    commit(repository)
    source = add_source(registry, repository)
    registry.sync_now(source["id"])
    removed = next(s for s in registry.list_skills()["skills"] if s["path"] == "another")
    git(repository, "rm", "-r", "skills/another")
    commit(repository)
    job = registry.sync_now(source["id"])
    assert job["result"]["retired"] == 1
    assert registry.skill_detail(removed["id"])["skill"]["retired"]
    assert registry.get_release(removed["id"], removed["published_revision"])


def test_root_package_and_preview_do_not_mutate_registry(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    preview = registry.validate_source(
        {
            "name": "Root package",
            "git_url": repository.as_uri(),
            "skills_path": "skills/example",
            "recursive": False,
        }
    )
    assert preview["skills"][0]["relative_path"] == "."
    assert not registry.list_sources()
    assert registry.list_skills()["total"] == 0
    source = add_source(registry, repository, skills_path="skills/example")
    registry.sync_now(source["id"])
    assert first_skill(registry)["path"] == "."


def test_queue_deduplicates_concurrent_manual_and_scheduled_requests(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    source = add_source(registry, repository, interval_minutes=30)
    with ThreadPoolExecutor(max_workers=8) as executor:
        jobs = list(executor.map(lambda _: registry.queue_sync(source["id"]), range(16)))
    assert len({job["id"] for job in jobs}) == 1
    registry.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if registry.get_job(jobs[0]["id"])["status"] == "succeeded":
                break
            time.sleep(0.02)
        else:
            pytest.fail(str(registry.list_jobs()))
        assert len(registry.list_jobs()) == 1
        assert registry.list_sources()[0]["next_check_at"]
        assert first_skill(registry)["published_version"] == 1
    finally:
        registry.stop()


def test_durable_queue_and_single_scheduler_owner(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    source = add_source(registry, repository)
    queued = registry.queue_sync(source["id"])
    other = SkillsRegistry(registry.root)
    registry.start()
    other.start()
    try:
        assert other._thread is None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if other.get_job(queued["id"])["status"] == "succeeded":
                break
            time.sleep(0.02)
        assert other.get_job(queued["id"])["status"] == "succeeded"
        assert other.list_skills()["total"] == 1
    finally:
        other.stop()
        registry.stop()


def test_failure_backoff_does_not_erase_last_success(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    source = add_source(registry, repository, interval_minutes=1)
    registry.sync_now(source["id"])
    success = registry.list_sources()[0]["last_success_at"]
    registry.save_source({"id": source["id"], "ref": "missing-branch"})
    job = registry.sync_now(source["id"])
    assert job["status"] == "failed"
    source = registry.list_sources()[0]
    assert source["last_success_at"] == success
    assert source["next_check_at"] > source["last_checked_at"]
    assert registry.get_release(first_skill(registry)["id"])


def test_config_change_during_sync_cannot_publish_stale_source(
    registry: SkillsRegistry,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = add_source(registry, repository)
    original = registry._record_success

    def save_before_publish(*args: Any, **kwargs: Any) -> None:
        registry.save_source({"id": source["id"], "skills_path": "different"})
        original(*args, **kwargs)

    monkeypatch.setattr(registry, "_record_success", save_before_publish)
    job = registry.sync_now(source["id"])
    assert job["status"] == "failed"
    assert "configuration changed" in job["error"]
    assert registry.list_skills()["total"] == 0


@pytest.mark.parametrize(
    "git_url",
    [
        "/etc",
        "https://user:secret@example.test/repo",
        "https://token@example.test/repo",
        "ext::sh -c whoami",
        "https://example.test/repo?token=secret",
    ],
)
def test_source_rejects_unsafe_git_locations(git_url: str) -> None:
    with pytest.raises(ValueError):
        SourceConfig(name="Unsafe", git_url=git_url)


@pytest.mark.parametrize("skills_path", ["../skills", "/etc", "a/../../b", "a\\b", ".git"])
def test_source_rejects_unsafe_paths(skills_path: str) -> None:
    with pytest.raises(ValueError):
        SourceConfig(name="Unsafe", git_url="https://example.test/repo", skills_path=skills_path)


def test_jobs_remain_deduplicated_while_running(
    registry: SkillsRegistry,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = add_source(registry, repository)
    entered = threading.Event()
    release = threading.Event()
    original = registry._record_success

    def blocked(*args: Any, **kwargs: Any) -> None:
        entered.set()
        assert release.wait(10)
        original(*args, **kwargs)

    monkeypatch.setattr(registry, "_record_success", blocked)
    queued = registry.queue_sync(source["id"])
    registry.start()
    try:
        assert entered.wait(10)
        assert registry.queue_sync(source["id"])["id"] == queued["id"]
        assert registry.get_job(queued["id"])["status"] == "running"
    finally:
        release.set()
        registry.stop()
    assert len(registry.list_jobs()) == 1


def test_enabling_auto_publish_publishes_existing_candidate(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    source = add_source(registry, repository, auto_publish=False)
    registry.sync_now(source["id"])
    skill = first_skill(registry)
    registry.save_source({"id": source["id"], "auto_publish": True})
    assert registry.sync_now(source["id"])["result"]["published"] == 1
    assert registry.get_release(skill["id"])["revision"] == skill["latest_revision"]

    registry.save_source({"id": source["id"], "auto_publish": False})
    write_skill(repository, body="Candidate")
    commit(repository)
    registry.sync_now(source["id"])
    skill = first_skill(registry)
    assert skill["published_revision"] != skill["latest_revision"]
    registry.save_source({"id": source["id"], "auto_publish": True})
    assert registry.sync_now(source["id"])["result"]["published"] == 1
    assert registry.get_release(skill["id"])["revision"] == skill["latest_revision"]


def test_invalid_new_package_does_not_block_a_valid_update(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    source = add_source(registry, repository)
    registry.sync_now(source["id"])
    original = registry.get_release(first_skill(registry)["id"])
    write_skill(repository, body="Changed valid package")
    bad = write_skill(repository, "skills/bad")
    (bad / "SKILL.md").write_text("---\nname: bad\n---\nNo description")
    commit(repository)
    job = registry.sync_now(source["id"])
    assert job["status"] == "succeeded_with_warnings"
    assert job["result"]["valid"] == 1
    assert job["result"]["skipped"] == 1
    assert job["result"]["created"] == 1
    assert registry.list_skills()["total"] == 1
    latest = registry.get_release(first_skill(registry)["id"])
    assert latest["revision"] != original["revision"]
    assert registry.get_release(original["skill_id"], original["revision"]) == original


def test_partial_scan_keeps_invalid_package_publication_candidate_and_client_pin(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    old_bad = write_skill(repository, "skills/old-bad")
    commit(repository)
    source = add_source(registry, repository)
    assert registry.sync_now(source["id"])["status"] == "succeeded"
    skills = {skill["path"]: skill for skill in registry.list_skills()["skills"]}
    original = registry.get_release(skills["old-bad"]["id"])
    healthy_original = registry.get_release(skills["example"]["id"])
    registry.save_source({"id": source["id"], "auto_publish": False})
    write_skill(repository, "skills/old-bad", body="Unpublished candidate")
    commit(repository)
    registry.sync_now(source["id"])
    candidate = registry.skill_detail(original["skill_id"])["skill"]["latest_revision"]
    assert candidate != original["revision"]
    registry.save_source({"id": source["id"], "auto_publish": True})

    (old_bad / "SKILL.md").write_text("Broken existing package\n")
    new_bad = write_skill(repository, "skills/new-bad")
    (new_bad / "SKILL.md").write_text("---\nname: new-bad\n---\nMissing description\n")
    write_skill(repository, body="Healthy package update")
    commit(repository)
    job = registry.sync_now(source["id"])

    assert job["status"] == "succeeded_with_warnings"
    assert job["result"]["discovered"] == 3
    assert job["result"]["valid"] == 1
    assert job["result"]["skipped"] == 2
    assert job["result"]["retired"] == 0
    assert {warning["relative_path"] for warning in job["result"]["warnings"]} == {
        "old-bad",
        "new-bad",
    }
    assert registry.list_skills()["total"] == 2
    preserved = registry.skill_detail(original["skill_id"])["skill"]
    assert preserved["published_revision"] == original["revision"]
    assert preserved["latest_revision"] == candidate
    assert not preserved["retired"]
    assert registry.get_release(original["skill_id"]) == original
    assert (
        registry.get_release(healthy_original["skill_id"])["revision"]
        != healthy_original["revision"]
    )
    update = registry.check_updates(
        [
            {
                "skill_id": original["skill_id"],
                "revision": original["revision"],
                "pinned": True,
            }
        ]
    )["updates"][0]
    assert update["status"] == "current"
    assert not update["update_available"]
    assert len(registry.skill_detail(original["skill_id"])["versions"]) == 2


def test_partial_scan_can_retire_an_actually_deleted_unrelated_skill(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    write_skill(repository, "skills/broken")
    write_skill(repository, "skills/deleted")
    commit(repository)
    source = add_source(registry, repository)
    registry.sync_now(source["id"])
    before = {skill["path"]: skill for skill in registry.list_skills()["skills"]}
    (repository / "skills/broken/SKILL.md").write_text("Invalid metadata\n")
    git(repository, "rm", "-r", "skills/deleted")
    commit(repository)

    job = registry.sync_now(source["id"])
    assert job["status"] == "succeeded_with_warnings"
    assert job["result"]["retired"] == 1
    assert not registry.skill_detail(before["broken"]["id"])["skill"]["retired"]
    assert registry.skill_detail(before["deleted"]["id"])["skill"]["retired"]
    assert registry.get_release(before["deleted"]["id"], before["deleted"]["published_revision"])
    assert registry.list_skills(published_only=True)["total"] == 2


@pytest.mark.parametrize("invalid_parent", [".", "group"])
def test_invalid_parent_preserves_previously_published_descendants(
    registry: SkillsRegistry,
    repository: Path,
    invalid_parent: str,
) -> None:
    write_skill(repository, "skills/group/child")
    commit(repository)
    source = add_source(registry, repository)
    registry.sync_now(source["id"])
    original = {
        skill["path"]: registry.get_release(skill["id"])
        for skill in registry.list_skills()["skills"]
    }
    (repository / "skills" / invalid_parent / "SKILL.md").write_text("Broken parent package\n")
    write_skill(repository, body="Healthy sibling update")
    commit(repository)

    job = registry.sync_now(source["id"])
    assert job["status"] == "succeeded_with_warnings"
    assert job["result"]["warnings"][0]["relative_path"] == invalid_parent
    assert job["result"]["retired"] == 0
    assert registry.get_release(original["group/child"]["skill_id"]) == original["group/child"]
    healthy = registry.get_release(original["example"]["skill_id"])
    if invalid_parent == ".":
        assert job["result"]["valid"] == 0
        assert healthy == original["example"]
    else:
        assert job["result"]["valid"] == 1
        assert healthy["revision"] != original["example"]["revision"]


def test_warnings_survive_restart_and_clear_after_recovery(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    source = add_source(registry, repository)
    registry.sync_now(source["id"])
    successful_at = registry.list_sources()[0]["last_success_at"]
    (repository / "skills/example/SKILL.md").write_text("Missing metadata\n")
    commit(repository)
    partial = registry.sync_now(source["id"])

    reopened = SkillsRegistry(registry.root)
    assert reopened.get_job(partial["id"])["result"]["warnings"] == partial["result"]["warnings"]
    assert reopened.list_sources()[0]["last_warnings"] == partial["result"]["warnings"]
    assert reopened.list_sources()[0]["last_success_at"] == successful_at
    write_skill(repository, body="Recovered package")
    commit(repository)
    recovered = reopened.sync_now(source["id"])
    assert recovered["status"] == "succeeded"
    assert recovered["result"]["warnings"] == []
    assert recovered["result"]["skipped"] == 0
    final_source = reopened.list_sources()[0]
    assert final_source["last_warnings"] == []
    assert final_source["last_error"] is None
    assert final_source["last_success_at"] == final_source["last_checked_at"]
    assert final_source["last_success_at"] != successful_at


def test_preview_returns_valid_packages_and_warnings_without_persisting_them(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    bad = write_skill(repository, "skills/bad")
    (bad / "SKILL.md").write_text("---\nname: bad\n---\nMissing description\n")
    commit(repository)
    result = registry.validate_source({"name": "Preview", "git_url": repository.as_uri()})
    assert [skill["relative_path"] for skill in result["skills"]] == ["example"]
    assert result["skipped"] == 1
    assert result["discovered"] == 2
    assert result["warnings"][0]["relative_path"] == "bad"
    assert "description" in result["warnings"][0]["error"]
    assert registry.list_sources() == []
    assert registry.list_jobs() == []
    assert registry.list_skills()["total"] == 0


def test_pending_auto_publication_recovers_skipped_candidate_without_reverting_rollback(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    write_skill(repository, "skills/retry")
    commit(repository)
    source = add_source(registry, repository)
    registry.sync_now(source["id"])
    original = {skill["path"]: skill for skill in registry.list_skills()["skills"]}
    registry.save_source({"id": source["id"], "auto_publish": False})
    write_skill(repository, body="Healthy candidate")
    write_skill(repository, "skills/retry", body="Retry candidate")
    commit(repository)
    registry.sync_now(source["id"])
    candidates = {skill["path"]: skill for skill in registry.list_skills()["skills"]}
    retry_markdown = (repository / "skills/retry/SKILL.md").read_bytes()
    (repository / "skills/retry/SKILL.md").write_text("Temporarily invalid\n")
    commit(repository)
    registry.save_source({"id": source["id"], "auto_publish": True})
    assert registry.sync_now(source["id"])["status"] == "succeeded_with_warnings"
    assert (
        registry.get_release(original["example"]["id"])["revision"]
        == candidates["example"]["latest_revision"]
    )
    assert (
        registry.get_release(original["retry"]["id"])["revision"]
        == original["retry"]["published_revision"]
    )

    registry.publish(original["example"]["id"], original["example"]["published_revision"])
    repeated = registry.sync_now(source["id"])
    assert repeated["status"] == "succeeded_with_warnings"
    assert repeated["result"]["published"] == 0
    assert (
        registry.get_release(original["example"]["id"])["revision"]
        == original["example"]["published_revision"]
    )
    (repository / "skills/retry/SKILL.md").write_bytes(retry_markdown)
    commit(repository)
    recovered = registry.sync_now(source["id"])
    assert recovered["status"] == "succeeded"
    assert recovered["result"]["published"] == 1
    assert (
        registry.get_release(original["retry"]["id"])["revision"]
        == candidates["retry"]["latest_revision"]
    )
    assert (
        registry.get_release(original["example"]["id"])["revision"]
        == original["example"]["published_revision"]
    )
    assert not registry.list_sources()[0]["publish_pending"]


def test_enabling_auto_publish_after_existing_warnings_publishes_healthy_candidates(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    write_skill(repository, "skills/bad")
    commit(repository)
    source = add_source(registry, repository, auto_publish=False)
    registry.sync_now(source["id"])
    healthy = next(
        skill for skill in registry.list_skills()["skills"] if skill["path"] == "example"
    )
    (repository / "skills/bad/SKILL.md").write_text("Broken metadata\n")
    commit(repository)
    assert registry.sync_now(source["id"])["status"] == "succeeded_with_warnings"
    registry.save_source({"id": source["id"], "auto_publish": True})

    result = registry.sync_now(source["id"])
    assert result["status"] == "succeeded_with_warnings"
    assert result["result"]["published"] == 1
    assert registry.get_release(healthy["id"])["revision"] == healthy["latest_revision"]


def test_binary_assets_and_executable_script_mode_are_part_of_revision(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    package = repository / "skills/example"
    asset = package / "icon.bin"
    asset.write_bytes(b"\x89\xff\x00\x01")
    script = package / "task.sh"
    script.write_text("#!/bin/sh\nprintf 'never executed'\n")
    commit(repository)
    source = add_source(registry, repository)
    registry.sync_now(source["id"])
    skill = first_skill(registry)
    first = registry.get_release(skill["id"])
    assert registry.read_file(skill["id"], first["revision"], "icon.bin")["encoding"] == "base64"
    assert not next(file for file in first["files"] if file["path"] == "task.sh")["executable"]
    script.chmod(0o755)
    commit(repository)
    registry.sync_now(source["id"])
    second = registry.get_release(skill["id"])
    assert second["revision"] != first["revision"]
    assert next(file for file in second["files"] if file["path"] == "task.sh")["executable"]


def test_scheduler_recovers_interrupted_job_from_persisted_queue(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    source = add_source(registry, repository)
    job = registry.queue_sync(source["id"])
    with registry._connect() as connection:
        connection.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    # Merely constructing a second instance never changes somebody else's running job.
    restarted = SkillsRegistry(registry.root)
    assert restarted.get_job(job["id"])["status"] == "running"
    restarted.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if restarted.get_job(job["id"])["status"] == "succeeded":
                break
            time.sleep(0.02)
        assert restarted.get_job(job["id"])["status"] == "succeeded"
        assert len(restarted.list_jobs()) == 1
    finally:
        restarted.stop()


def test_git_credential_helper_stderr_is_not_persisted(
    registry: SkillsRegistry,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = add_source(registry, repository)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            "Git operation failed for https://example.test/repo: "
            "unable to access https://user:private-password@redirect.test/repo?token=secret-token"
        )

    monkeypatch.setattr(registry, "_manager", fail)
    job = registry.sync_now(source["id"])
    assert job["status"] == "failed"
    assert job["error"] == (
        "Git connection failed; check repository connectivity and server Git configuration"
    )
    assert "secret-token" not in str(registry.list_sources())
    assert "private-password" not in str(registry.list_jobs())


def test_published_discovery_searches_public_metadata_and_paginates_before_selection(
    registry: SkillsRegistry,
    repository: Path,
) -> None:
    for name in ("alpha", "beta", "gamma"):
        package = write_skill(repository, f"skills/{name}")
        (package / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Published {name} task\n---\nContent\n"
        )
    commit(repository)
    source = add_source(registry, repository, auto_publish=False)
    registry.sync_now(source["id"])
    skills = {skill["name"]: skill for skill in registry.list_skills()["skills"]}
    for name in ("beta", "gamma"):
        registry.publish(skills[name]["id"], skills[name]["latest_revision"])
    # The draft rename must not change published discovery text or search behavior.
    (repository / "skills/beta/SKILL.md").write_text(
        "---\nname: renamed-beta\ndescription: Draft-only candidate wording\n---\nChanged\n"
    )
    commit(repository)
    registry.sync_now(source["id"])
    assert registry.list_skills()["total"] == 4
    public = registry.list_skills(published_only=True, limit=1, offset=0)
    assert public["total"] == 2
    assert public["skills"][0]["name"] == "beta"
    assert public["skills"][0]["description"] == "Published beta task"
    assert (
        registry.list_skills(published_only=True, limit=1, offset=1)["skills"][0]["name"] == "gamma"
    )
    assert registry.list_skills(query="Published beta", published_only=True)["total"] == 1
    assert registry.list_skills(query="Draft-only", published_only=True)["total"] == 0
    assert registry.list_skills(query="Draft-only")["total"] == 1
    assert registry.list_skills(source_id="missing", published_only=True)["total"] == 0
    # Retired published entries disappear before pagination, while dashboard history remains.
    git(repository, "rm", "-r", "skills/beta")
    commit(repository)
    registry.sync_now(source["id"])
    public = registry.list_skills(published_only=True, limit=1)
    assert public["total"] == 1
    assert public["skills"][0]["name"] == "gamma"
    assert registry.list_skills()["total"] == 4
