"""Integration coverage for immutable releases from real, local Git repositories."""

from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    source = add_source(registry, repository)
    registry.sync_now(source["id"])
    skill = first_skill(registry)
    release = registry.get_release(skill["id"])
    last_success = registry.list_sources()[0]["last_success_at"]
    (repository / "skills/example/SKILL.md").write_text("No YAML metadata\n")
    commit(repository)
    failed = registry.sync_now(source["id"])
    assert failed["status"] == "failed"
    assert "frontmatter" in failed["error"]
    assert registry.get_release(skill["id"]) == release
    source = registry.list_sources()[0]
    assert source["last_success_at"] == last_success
    assert source["last_error"]
    assert source["failure_count"] == 1


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
    assert job["status"] == "failed"
    assert "Symlink" in job["error"]
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


def test_invalid_one_of_many_packages_is_atomic(
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
    assert job["status"] == "failed"
    assert registry.list_skills()["total"] == 1
    assert registry.get_release(first_skill(registry)["id"]) == original


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
