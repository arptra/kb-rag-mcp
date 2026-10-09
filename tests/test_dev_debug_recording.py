from __future__ import annotations

import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from corporate_kb.dev_debug import recording
from corporate_kb.dev_debug.recording import SessionStore, redact_text


def git(project: Path, *args: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Dev Test",
            "-c",
            "user.email=dev@example.invalid",
            *args,
        ],
        cwd=project,
        text=True,
    ).strip()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--quiet")
    (tmp_path / ".gitignore").write_text(".cache/\nnode_modules/\n")
    (tmp_path / "app.py").write_text("value = 1\n")
    git(tmp_path, "add", ".gitignore", "app.py")
    git(tmp_path, "commit", "--quiet", "-m", "Initial")
    return tmp_path


def artifact_text(session: Path) -> str:
    return "\n".join(
        path.read_text() for path in session.iterdir() if path.suffix in {".json", ".jsonl"}
    )


def test_redaction_removes_credentials_and_authorization_urls() -> None:
    raw = (
        "fatal https://person:topsecret@git.example/team/repo?key=hidden#fragment\n"
        "Authorization: Bearer bearer-secret\nCookie: user=admin; session=secret-cookie\n"
        'password="two word secret" access_token=visible-token\n'
        "https://login.example/oauth/device?code=secret-code\n"
        "-----BEGIN RSA PRIVATE KEY-----\nprivate-material\n-----END RSA PRIVATE KEY-----\n"
        "benign error: cannot find remote ref main"
    )
    cleaned = redact_text(raw)
    for secret in (
        "person",
        "topsecret",
        "hidden",
        "bearer-secret",
        "secret-cookie",
        "two word secret",
        "visible-token",
        "login.example",
        "private-material",
        "secret-code",
    ):
        assert secret not in cleaned
    assert "https://git.example/team/repo" in cleaned
    assert "cannot find remote ref main" in cleaned


def test_incremental_logs_partial_lines_and_private_key_block(project: Path) -> None:
    store = SessionStore(project)
    session = store.create("clone debugging")
    log = project / "server.log"
    log.write_text("INFO ready\nERROR token=first-secret\npassword=half-")
    store.collect_once(session, [log])
    first = artifact_text(session)
    assert "first-secret" not in first
    assert "half-" not in first
    with log.open("a") as handle:
        handle.write("secret\n-----BEGIN PRIVATE KEY-----\nkey-in-first-chunk\n")
    store.collect_once(session, [log])
    with log.open("a") as handle:
        handle.write("key-in-next-chunk\n-----END PRIVATE KEY-----\nERROR done\n")
    store.collect_once(session, [log])
    count = len((session / "events.jsonl").read_text().splitlines())
    store.collect_once(session, [log])
    assert len((session / "events.jsonl").read_text().splitlines()) == count
    result = artifact_text(session)
    assert "half-secret" not in result
    assert "key-in-first-chunk" not in result
    assert "key-in-next-chunk" not in result
    assert "ERROR done" in result
    assert len(store.load(session)["errors"]) == 2


def test_rotation_and_copy_truncation_are_observable(project: Path) -> None:
    store = SessionStore(project)
    session = store.create()
    log = project / "server.log"
    log.write_text("ERROR old-value\n")
    store.collect_once(session, [log])
    log.write_text("ERROR new-value\n")
    store.collect_once(session, [log])
    log.rename(project / "server.old.log")
    log.write_text("ERROR after rotation\n")
    store.collect_once(session, [log])
    events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
    assert sum(event["kind"] == "log_rotated" for event in events) == 2
    assert "ERROR new-value" in artifact_text(session)
    assert "ERROR after rotation" in artifact_text(session)


def test_tail_backlog_and_event_storage_are_bounded(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "MAX_LOG_BYTES", 100)
    monkeypatch.setattr(recording, "MAX_EVENT_BYTES", 1000)
    store = SessionStore(project)
    session = store.create()
    log = project / "server.log"
    log.write_text("before\n" * 100 + "ERROR latest\n")
    store.collect_once(session, [log])
    assert "log_tail" in artifact_text(session)
    assert store.load(session)["last_collection"]["bytes"] <= 100
    for number in range(40):
        store.event(session, "test", f"event {number}")
    for path in session.glob("events*.jsonl"):
        assert path.stat().st_size <= 1000
    assert len(list(session.glob("events*.jsonl"))) == 2


def test_bundle_preserves_source_metadata_and_excludes_secret_files(project: Path) -> None:
    secret = project / ".env"
    secret.write_text("SUPER_SECRET=do-not-copy-this\n")
    source = project / "app.py"
    store = SessionStore(project)
    session = store.create()
    source.write_text('value = 2\npassword = "new-hidden-secret"\n')
    (project / "user-note.md").write_text("Untracked confidential prose must not be copied")
    (project / "credentials.json").write_text('{"private":"must-not-read"}')
    bundle_path = store.prepare_bundle(session, note="Git clone fails while testing")
    bundle = json.loads(bundle_path.read_text())
    assert bundle["note"] == "Git clone fails while testing"
    assert "+value = 2" in bundle["tracked_source_diff"]
    assert "app.py" in {item["path"] for item in bundle["changed_source_metadata"]}
    assert "user-note.md" in {item["path"] for item in bundle["changed_source_metadata"]}
    for value in (
        "do-not-copy-this",
        "new-hidden-secret",
        "must-not-read",
        "Untracked confidential prose",
    ):
        assert value not in artifact_text(session)
    original = bundle_path.read_bytes()
    store.prepare_bundle(session, note="second immutable snapshot")
    assert bundle_path.read_bytes() == original
    assert secret.read_text() == "SUPER_SECRET=do-not-copy-this\n"
    assert source.read_text() == 'value = 2\npassword = "new-hidden-secret"\n'


def test_failed_jobs_are_read_without_mutating_database(project: Path) -> None:
    directory = project / ".cache/skills-registry"
    directory.mkdir(parents=True)
    database = directory / "registry.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE jobs(id, source_id, status, created_at, finished_at, error)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('j1','s1','failed','a','b','fatal password=secret')"
        )
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    store = SessionStore(project)
    session = store.create()
    store.collect_once(session, [])
    store.collect_once(session, [])
    assert len(store.load(session)["errors"]) == 1
    assert "password=secret" not in artifact_text(session)
    assert before == {path.name: path.read_bytes() for path in directory.iterdir()}


def test_concurrent_writers_keep_events_and_independent_state_fields(project: Path) -> None:
    store = SessionStore(project)
    session = store.create()

    def write(number: int) -> None:
        other_store = SessionStore(project)
        other_store.event(session, "parallel", str(number))
        other_store.update(session, **{f"worker_{number}": number})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(20)))
    state = store.load(session)
    assert all(state[f"worker_{number}"] == number for number in range(20))
    events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
    assert len([event for event in events if event["kind"] == "parallel"]) == 20


def test_sessions_cannot_escape_artifact_root_or_change_identity(project: Path) -> None:
    store = SessionStore(project)
    assert store.latest() is None
    session = store.create()
    assert store.latest() == session
    with pytest.raises(ValueError, match="directly below"):
        store.load(project)
    with pytest.raises(ValueError, match="immutable"):
        store.update(session, id="other")
    linked = store.root / "sessions" / "linked"
    linked.symlink_to(session, target_is_directory=True)
    with pytest.raises(ValueError, match="directly below"):
        store.load(linked)


def test_log_symlinks_and_secret_paths_are_skipped(project: Path) -> None:
    store = SessionStore(project)
    session = store.create()
    key_dir = project / "keys"
    key_dir.mkdir()
    secret = key_dir / "private.log"
    secret.write_text("not-an-obvious-secret-that-must-still-be-excluded\n")
    alias = project / "alias.log"
    alias.symlink_to(secret)
    state = store.collect_once(session, [secret, alias])
    assert state["last_collection"]["files"] == 0
    assert "not-an-obvious-secret" not in artifact_text(session)


def test_bundle_continues_configured_logs_and_does_not_lose_other_state(project: Path) -> None:
    store = SessionStore(project)
    session = store.create()
    custom = project / "custom.log"
    custom.write_text("ERROR custom clone failure\n")
    store.update(session, extra_log_paths=[str(custom)], repair_state="awaiting_approval")
    store.collect_once(session)
    with custom.open("a") as handle:
        handle.write("ERROR second clone failure\n")
    bundle = json.loads(store.prepare_bundle(session).read_text())
    state = store.load(session)
    assert str(custom) in state["log_offsets"]
    assert state["repair_state"] == "awaiting_approval"
    assert "second clone failure" in bundle["recent_events_jsonl"]


def test_oversized_partial_line_is_never_written_raw(project: Path) -> None:
    store = SessionStore(project)
    session = store.create()
    log = project / "server.log"
    log.write_text("password=" + "x" * (recording.MAX_LOG_BYTES * 2))
    store.collect_once(session, [log])
    with log.open("a") as handle:
        handle.write("secret-tail\nERROR recovered\n")
    store.collect_once(session, [log])
    assert "secret-tail" not in artifact_text(session)
    assert "ERROR recovered" in artifact_text(session)


def test_corrupted_state_is_rejected_and_control_paths_are_not_sources(project: Path) -> None:
    store = SessionStore(project)
    session = store.create()
    state = store.load(session)
    state["log_offsets"] = "invalid"
    (session / "session.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="state structure"):
        store.collect_once(session)
    assert not recording.is_source_path("src/fake\nname.py")
    assert not recording.is_source_path("src/\x1b[2Jname.py")
    assert redact_text("\x1b[31mERROR\x1b[0m password=hidden") == "ERROR password=[REDACTED]"


def test_bundle_resanitizes_manually_added_event_data(project: Path) -> None:
    store = SessionStore(project)
    session = store.create()
    with (session / "events.jsonl").open("a") as handle:
        handle.write(json.dumps({"message": "password=manual-secret"}) + "\n")
    bundle = store.prepare_bundle(session)
    assert "manual-secret" not in bundle.read_text()
