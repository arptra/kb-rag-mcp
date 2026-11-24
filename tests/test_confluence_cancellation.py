"""Cancellation remains effective across HTTP workers, schedules and restarts."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime

import pytest
from test_confluence import FakeClient, config, registry

from corporate_kb.confluence.models import SyncCancelled
from corporate_kb.confluence.registry import _SCHEMA, SourceBusyError


def test_other_worker_can_cancel_waiting_request_and_release_only_its_source(tmp_path):
    entered, observed, release = threading.Event(), threading.Event(), threading.Event()

    class WaitingClient(FakeClient):
        def root_id(self):
            entered.set()
            assert self.cancel.wait(5), "Owner did not receive the durable cancellation"
            observed.set()
            assert release.wait(5)
            raise SyncCancelled("Cancelled upstream request")

    owner, _, _, _, _ = registry(tmp_path, client=WaitingClient())
    controller, _, _, _, _ = registry(tmp_path)
    source = owner.upsert_source(config().model_dump(), "secret")
    other = owner.upsert_source(config(name="Other source").model_dump(), "secret")
    job = owner.queue_sync(source["id"])
    worker = threading.Thread(target=owner.run_job, args=(job["id"],))
    worker.start()
    try:
        assert entered.wait(3)
        assert controller.cancel_job(job["id"])["status"] == "cancelling"
        assert observed.wait(2)
        pending = controller.get_source(source["id"])
        assert pending["status"] == "cancelling"
        assert pending["active_job"]["id"] == job["id"]
        assert pending["active_job"]["status"] == "cancelling"
        with pytest.raises(SourceBusyError):
            controller.queue_sync(source["id"])
        other_job = controller.queue_sync(other["id"])
        assert other_job["status"] == "queued"
        assert controller.cancel_job(other_job["id"])["status"] == "cancelled"
        assert controller.queue_sync(other["id"])["id"] != other_job["id"]
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert controller.job_status(job["id"])["status"] == "cancelled"
    assert controller.get_source(source["id"])["active_job"] is None
    replacement = controller.queue_sync(source["id"])
    assert replacement["id"] != job["id"]
    assert controller.run_job(replacement["id"])["status"] == "completed"


def test_other_worker_cancels_while_waiting_for_index_lock(tmp_path):
    locked, waiting = threading.Lock(), threading.Event()

    def guard(index):
        waiting.set()
        return locked

    owner, _, _, _, _ = registry(tmp_path, index_guard=guard)
    controller, _, _, _, _ = registry(tmp_path)
    source = owner.upsert_source(config().model_dump(), "secret")
    job = owner.queue_sync(source["id"])
    locked.acquire()
    worker = threading.Thread(target=owner.run_job, args=(job["id"],))
    worker.start()
    try:
        assert waiting.wait(3)
        controller.cancel_job(job["id"])
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert controller.job_status(job["id"])["status"] == "cancelled"
    finally:
        locked.release()
        worker.join(timeout=3)


def test_cancelled_overdue_schedule_is_not_immediately_queued_again(tmp_path, monkeypatch):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(
        config(schedule={"kind": "interval", "interval_minutes": 60}).model_dump(), "secret"
    )
    with instance._db() as db:
        db.execute(
            "UPDATE sources SET next_check_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (source["id"],),
        )
    job = instance.queue_sync(source["id"])
    instance.cancel_job(job["id"])
    assert instance.get_source(source["id"])["next_check_at"] > datetime.now(UTC).isoformat()
    monkeypatch.setattr(instance._wake, "wait", lambda **kwargs: instance._stop.set())
    instance._loop()
    assert [item["id"] for item in instance.jobs()] == [job["id"]]
    instance._stop.clear()
    assert instance.queue_sync(source["id"])["id"] != job["id"]


def test_restart_finishes_explicit_cancel_and_resumes_only_uncancelled_job(tmp_path, monkeypatch):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    other = instance.upsert_source(config(name="Other").model_dump(), "secret")
    cancelled = instance.queue_sync(source["id"])
    resumable = instance.queue_sync(other["id"])
    with instance._db() as db:
        db.execute("UPDATE jobs SET status='running'")
    assert instance.cancel_job(cancelled["id"])["status"] == "cancelling"
    reopened, _, _, _, _ = registry(tmp_path)
    monkeypatch.setattr(reopened, "_loop", lambda: None)
    reopened.start()
    try:
        assert reopened.job_status(cancelled["id"])["status"] == "cancelled"
        assert reopened.job_status(resumable["id"])["status"] == "queued"
    finally:
        reopened.stop()


def test_cancel_during_last_operation_cannot_be_reported_as_completed(tmp_path):
    owner, _, _, _, _ = registry(tmp_path)
    controller, _, _, _, _ = registry(tmp_path)
    source = owner.upsert_source(config().model_dump(), "secret")
    job = owner.queue_sync(source["id"])

    def publish(index, **kwargs):
        controller.cancel_job(job["id"])
        return {"status": "completed"}

    owner.publish = publish
    assert owner.run_job(job["id"])["status"] == "cancelled"


def test_cancellation_accepted_while_handling_error_wins_over_failure(tmp_path, monkeypatch):
    owner, _, _, _, _ = registry(tmp_path)
    controller, _, _, _, _ = registry(tmp_path)
    source = owner.upsert_source(config().model_dump(), "secret")
    job = owner.queue_sync(source["id"])

    def fail(*args, **kwargs):
        raise RuntimeError("Upstream operation failed")

    def describe(error):
        controller.cancel_job(job["id"])
        return "Upstream operation failed"

    monkeypatch.setattr(owner, "_synchronize", fail)
    monkeypatch.setattr(owner, "_error_summary", describe)
    assert owner.run_job(job["id"])["status"] == "cancelled"
    assert controller.get_source(source["id"])["last_error"] is None


def test_stale_scheduler_snapshot_cannot_requeue_cancelled_source(tmp_path, monkeypatch):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(
        config(schedule={"kind": "interval", "interval_minutes": 60}).model_dump(), "secret"
    )
    with instance._db() as db:
        db.execute(
            "UPDATE sources SET next_check_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (source["id"],),
        )
    job = instance.queue_sync(source["id"])
    stale_sources = instance.list_sources()
    instance.cancel_job(job["id"])
    monkeypatch.setattr(instance, "list_sources", lambda: stale_sources)
    monkeypatch.setattr(instance._wake, "wait", lambda **kwargs: instance._stop.set())
    instance._loop()
    assert [item["id"] for item in instance.jobs()] == [job["id"]]


def test_explicit_cancellation_takes_precedence_over_shutdown_resume(tmp_path):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    job = instance.queue_sync(source["id"])

    def publish(index, **kwargs):
        instance.cancel_job(job["id"])
        instance._stop.set()
        raise SyncCancelled("Server stopping after user cancellation")

    instance.publish = publish
    assert instance.run_job(job["id"])["status"] == "cancelled"


def test_existing_database_gets_durable_cancellation_column(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    with sqlite3.connect(state / "confluence.sqlite3") as db:
        db.executescript(_SCHEMA.replace(" cancel_requested INTEGER NOT NULL DEFAULT 0,\n", ""))
        assert "cancel_requested" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    job = instance.queue_sync(source["id"])
    assert instance.cancel_job(job["id"])["status"] == "cancelled"
    assert instance._cancel == {}
    with pytest.raises(KeyError):
        instance.cancel_job("unknown")
    assert instance._cancel == {}


def test_source_active_job_is_available_beyond_recent_jobs_limit(tmp_path):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    other = instance.upsert_source(config(name="Other").model_dump(), "secret")
    active = instance.queue_sync(source["id"])
    for _ in range(102):
        instance.cancel_job(instance.queue_sync(other["id"])["id"])
    assert active["id"] not in {job["id"] for job in instance.jobs()}
    assert instance.get_source(source["id"])["active_job"]["id"] == active["id"]
