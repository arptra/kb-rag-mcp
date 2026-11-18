"""Existing v2 credentials survive the switch to permanent MCP tokens."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from corporate_kb.access.models import AccessDenied, CertificateIdentity
from corporate_kb.access.store import AccessStore

NOW = 1_800_000_000

# Freeze the historical table layout: the old application assigned every token
# an expiry and could not distinguish browser sessions from MCP credentials.
_V2_TOKEN_SCHEMA = """CREATE TABLE user_tokens_v2 (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    last_used_at INTEGER, revoked_at INTEGER
)"""


def _certificate(name: str) -> CertificateIdentity:
    return CertificateIdentity(
        fingerprint=hashlib.sha256(name.encode()).hexdigest(),
        subject=f"CN={name}",
        issuer="CN=Test CA",
        serial_number=name,
        not_before=NOW - 1000,
        not_after=NOW + 3600,
        certificate_pem="PUBLIC CERTIFICATE",
    )


def _rows(path: Path, table: str) -> list[dict]:
    assert table in {"users", "user_tokens", "admin_sessions", "audit_events"}
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr("corporate_kb.access.store._now", lambda: NOW)


@pytest.fixture
def legacy_v2(tmp_path):
    path = tmp_path / "v2.sqlite3"
    store = AccessStore(path, token_ttl_seconds=600, admin_session_ttl_seconds=300)
    credentials = {
        name: store.enroll(_certificate(name))
        for name in ("past", "live", "revoked-token", "revoked-user")
    }
    store.revoke_token(credentials["revoked-token"].token_id, actor="admin")
    store.revoke_user(credentials["revoked-user"].user["id"], actor="admin")
    store.add_admin("admin", "a-long-test-admin-password", actor="setup")
    session = store.login_admin("admin", "a-long-test-admin-password")
    with sqlite3.connect(path) as connection:
        connection.execute(_V2_TOKEN_SCHEMA)
        connection.execute(
            """INSERT INTO user_tokens_v2
            SELECT id, user_id, token_hash, prefix, created_at,
                CASE WHEN id = ? THEN ? ELSE ? END, last_used_at, revoked_at
            FROM user_tokens""",
            (credentials["live"].token_id, NOW + 600, NOW - 1),
        )
        connection.execute("DROP TABLE user_tokens")
        connection.execute("ALTER TABLE user_tokens_v2 RENAME TO user_tokens")
        connection.execute("CREATE INDEX user_tokens_user_id ON user_tokens(user_id)")
        connection.execute("PRAGMA user_version = 2")
    return path, credentials, session


def test_v2_migration_preserves_credentials_revocations_and_admin_sessions(legacy_v2, monkeypatch):
    path, credentials, session = legacy_v2
    before_tokens = _rows(path, "user_tokens")
    before_users = _rows(path, "users")
    before_sessions = _rows(path, "admin_sessions")
    before_events = _rows(path, "audit_events")

    store = AccessStore(path, token_ttl_seconds=5, admin_session_ttl_seconds=5)
    assert _rows(path, "user_tokens") == [
        {**row, "expires_at": None, "purpose": "mcp"} for row in before_tokens
    ]
    assert _rows(path, "users") == before_users
    assert _rows(path, "admin_sessions") == before_sessions
    events = _rows(path, "audit_events")
    assert events[:-1] == before_events
    assert events[-1]["action"] == "schema.permanent_tokens_migrated"
    assert json.loads(events[-1]["details"]) == {"tokens_preserved": 4}
    assert store.verify_admin_session(session.token) is not None

    # A changed TTL, elapsed time and a new process must not rotate either of
    # the previously expired/live credentials or make a revoked one usable.
    monkeypatch.setattr("corporate_kb.access.store._now", lambda: NOW + 10 * 365 * 86400)
    reopened = AccessStore(path, token_ttl_seconds=1)
    for name in ("past", "live"):
        original = credentials[name]
        verified = reopened.verify_user_token(original.token, common_name=name)
        assert verified["id"] == original.user["id"]
        assert reopened.verify_user_token(original.token, common_name="another-user") is None
        reused = reopened.enroll(_certificate(name), existing_token=original.token)
        assert reused.token == original.token
        assert reused.token_id == original.token_id
        assert reused.expires_at is None
        assert reused.created is False
    for name in ("revoked-token", "revoked-user"):
        assert reopened.verify_user_token(credentials[name].token) is None
    with pytest.raises(AccessDenied, match="revoked"):
        reopened.enroll(
            _certificate("revoked-user"), existing_token=credentials["revoked-user"].token
        )
    assert reopened.verify_admin_session(session.token) is None
    assert _rows(path, "admin_sessions") == before_sessions
    assert reopened.list_tokens()["total"] == 4
    listed = {row["id"]: row for row in reopened.list_tokens()["items"]}
    for name, credential in credentials.items():
        row = listed[credential.token_id]
        assert row["expires_at"] is None
        assert row["purpose"] == "mcp"
        assert row["status"] == ("revoked" if name.startswith("revoked-") else "active")

    after_tokens = _rows(path, "user_tokens")
    after_events = _rows(path, "audit_events")
    AccessStore(path)
    AccessStore(path)
    assert _rows(path, "user_tokens") == after_tokens
    assert _rows(path, "audit_events") == after_events
    assert {row["token_hash"] for row in after_tokens} == {
        hashlib.sha256(credential.token.encode()).hexdigest()
        for credential in credentials.values()
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v2_migration_failure_leaves_original_schema_and_credentials(legacy_v2, monkeypatch):
    path, _, _ = legacy_v2
    before = {table: _rows(path, table) for table in ("users", "user_tokens", "audit_events")}
    original_audit = AccessStore._audit

    def fail_migration_audit(connection, **kwargs):
        if kwargs["action"] == "schema.permanent_tokens_migrated":
            raise RuntimeError("simulated token migration failure")
        return original_audit(connection, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AccessStore, "_audit", staticmethod(fail_migration_audit))
        with pytest.raises(RuntimeError, match="simulated token migration"):
            AccessStore(path)
    for table, expected in before.items():
        assert _rows(path, table) == expected
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(user_tokens)")}
        assert "purpose" not in columns
        assert columns["expires_at"][3] == 1
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'user_tokens_v3'"
        ).fetchone() is None
    assert AccessStore(path).list_tokens()["total"] == 4
