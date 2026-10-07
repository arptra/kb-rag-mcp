"""Historical v1 SQLite fixtures exercise the transactional CN identity migration."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from corporate_kb.access.models import AccessDenied, CertificateIdentity
from corporate_kb.access.store import AccessStore, _hash_password

NOW = 1_800_000_000
PASSWORD = "migration-test-admin-password"
ADMIN_TOKEN = "migration-test-admin-session-opaque-token"

# Deliberately frozen rather than derived from today's _SCHEMA: this must keep
# representing a database created by the fingerprint-based v1 application.
_V1_SCHEMA = (
    """CREATE TABLE users (
        id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
        subject TEXT NOT NULL, issuer TEXT NOT NULL, serial_number TEXT NOT NULL,
        not_before INTEGER NOT NULL, not_after INTEGER NOT NULL,
        certificate_pem TEXT NOT NULL, created_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','revoked')),
        revoked_at INTEGER, revocation_reason TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE user_tokens (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
        token_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        last_used_at INTEGER, revoked_at INTEGER
    )""",
    "CREATE INDEX user_tokens_user_id ON user_tokens(user_id)",
    """CREATE TABLE admins (
        id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','disabled')),
        created_at INTEGER NOT NULL, last_login_at INTEGER
    )""",
    """CREATE TABLE admin_sessions (
        id TEXT PRIMARY KEY, admin_id TEXT NOT NULL REFERENCES admins(id),
        token_hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL, revoked_at INTEGER
    )""",
    "CREATE INDEX admin_sessions_admin_id ON admin_sessions(admin_id)",
    """CREATE TABLE login_attempts (
        username TEXT NOT NULL, address TEXT NOT NULL, failures INTEGER NOT NULL,
        window_started_at INTEGER NOT NULL, blocked_until INTEGER NOT NULL,
        PRIMARY KEY(username,address)
    )""",
    """CREATE TABLE address_login_attempts (
        address TEXT PRIMARY KEY, failures INTEGER NOT NULL,
        window_started_at INTEGER NOT NULL, blocked_until INTEGER NOT NULL
    )""",
    """CREATE TABLE audit_events (
        id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, actor TEXT NOT NULL,
        action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
        address TEXT NOT NULL, details TEXT NOT NULL
    )""",
    "CREATE INDEX audit_events_created_at ON audit_events(created_at)",
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _id(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr("corporate_kb.access.store._now", lambda: NOW)


@pytest.fixture
def legacy_path(tmp_path: Path) -> Path:
    path = tmp_path / "v1.sqlite3"
    with sqlite3.connect(path) as connection:
        for statement in _V1_SCHEMA:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            "INSERT INTO admins VALUES (?,?,?,'active',?,?)",
            (_id(90), "admin", _hash_password(PASSWORD), NOW - 1000, NOW - 100),
        )
        connection.execute(
            "INSERT INTO admin_sessions VALUES (?,?,?,?,?,NULL)",
            (_id(91), _id(90), _digest(ADMIN_TOKEN), NOW - 100, NOW + 900),
        )
        connection.execute(
            "INSERT INTO login_attempts VALUES (?,?,?,?,?)",
            ("someone", "192.0.2.10", 5, NOW - 50, NOW + 850),
        )
        connection.execute(
            "INSERT INTO address_login_attempts VALUES (?,?,?,?)",
            ("192.0.2.10", 5, NOW - 50, NOW + 850),
        )
        connection.execute(
            "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?)",
            (_id(92), NOW - 99, _id(90), "user.enrolled", "user", _id(2), "", "old audit"),
        )
    return path


def _add_user(
    path: Path,
    number: int,
    subject: str,
    *,
    created_at: int = NOW - 100,
    last_seen_at: int = NOW - 10,
    revoked: bool = False,
    expires_at: int = NOW + 30,
) -> str:
    user_id = _id(number)
    token = f"legacy-user-token-{number}-long-enough-for-verification"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id,
                _digest(f"certificate-{number}"),
                subject,
                f"CN=Issuer {number}",
                str(number),
                NOW - 1000,
                NOW - 1,  # Historical certificate expiry does not invalidate a CN bearer.
                f"PUBLIC CERTIFICATE {number}",
                created_at,
                last_seen_at,
                "revoked" if revoked else "active",
                NOW - 5 if revoked else None,
                "Contract ended" if revoked else "",
            ),
        )
        connection.execute(
            "INSERT INTO user_tokens VALUES (?,?,?,?,?,?,?,?)",
            (
                _id(number + 100),
                user_id,
                _digest(token),
                token[:11],
                NOW - 50,
                expires_at,
                NOW - 10,
                NOW - 4 if revoked else None,
            ),
        )
    return token


def _rows(path: Path, table: str) -> list[dict]:
    assert table in {
        "users",
        "user_tokens",
        "admins",
        "admin_sessions",
        "login_attempts",
        "address_login_attempts",
        "audit_events",
        "legacy_user_identities",
    }
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _certificate(subject: str) -> CertificateIdentity:
    return CertificateIdentity(
        fingerprint="f" * 64,
        subject=subject,
        issuer="CN=A New Unrelated Issuer",
        serial_number="new",
        not_before=NOW + 100,
        not_after=NOW + 200,
        certificate_pem="NEW PUBLIC CERTIFICATE",
    )


def test_fresh_database_uses_v2_with_partial_unique_common_name(tmp_path):
    store = AccessStore(tmp_path / "new.sqlite3")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'users_common_name'"
        ).fetchone()[0]
        assert "UNIQUE INDEX" in index and "WHERE common_name IS NOT NULL" in index
    assert _rows(store.path, "legacy_user_identities") == []


def test_v1_migration_preserves_tokens_admins_sessions_audit_and_disables_unknown_cn(legacy_path):
    token = _add_user(legacy_path, 1, "CN=Developer,O=Example")
    unknown = _add_user(legacy_path, 2, "O=No Common Name", expires_at=NOW + 500)
    ambiguous = _add_user(legacy_path, 3, "CN=One,CN=Two", expires_at=NOW + 600)
    malformed = _add_user(legacy_path, 4, "not-a-subject", expires_at=NOW + 700)
    untouched = {
        table: _rows(legacy_path, table)
        for table in ("admins", "admin_sessions", "login_attempts", "address_login_attempts")
    }
    old_events = _rows(legacy_path, "audit_events")
    old_tokens = _rows(legacy_path, "user_tokens")
    store = AccessStore(legacy_path)
    for table, expected in untouched.items():
        assert _rows(legacy_path, table) == expected
    assert _rows(legacy_path, "user_tokens") == old_tokens
    assert _rows(legacy_path, "audit_events")[: len(old_events)] == old_events
    assert store.verify_admin_session(ADMIN_TOKEN)["id"] == _id(90)
    assert store.verify_user_token(token, common_name="Developer")["id"] == _id(1)
    assert all(store.verify_user_token(value) is None for value in (unknown, ambiguous, malformed))
    users = {item["id"]: item for item in store.list_users()["items"]}
    assert users[_id(1)]["common_name"] == "Developer"
    assert all(users[_id(number)]["common_name"] is None for number in (2, 3, 4))
    assert len(users) == 4
    assert store.list_tokens(_id(2))["items"][0]["common_name"] is None
    store.restore_user(_id(2), actor="admin")
    assert store.verify_user_token(unknown) is None
    reused = store.enroll(_certificate("CN=Developer"), existing_token=token)
    assert reused.token == token
    assert reused.expires_at == NOW + 30  # Never extend an existing token during migration/renewal.
    assert store.login_admin("admin", PASSWORD).admin["id"] == _id(90)
    for secret in (token, unknown, ambiguous, malformed, ADMIN_TOKEN, PASSWORD):
        assert secret.encode() not in legacy_path.read_bytes()


def test_duplicate_cn_merges_deterministically_and_archives_original_rows(legacy_path):
    second = _add_user(legacy_path, 2, "CN=Cafe\u0301,O=Second", created_at=NOW - 200)
    first = _add_user(legacy_path, 1, "CN=Café,O=First", created_at=NOW - 200)
    latest = _add_user(
        legacy_path, 3, "CN=Café,O=Latest", created_at=NOW - 100, last_seen_at=NOW - 1
    )
    original_users = {item["id"]: item for item in _rows(legacy_path, "users")}
    original_tokens = _rows(legacy_path, "user_tokens")
    original_events = _rows(legacy_path, "audit_events")
    store = AccessStore(legacy_path)
    user = store.list_users()["items"][0]
    assert store.list_users()["total"] == 1
    assert user["id"] == _id(1)
    assert user["common_name"] == "Café"
    assert user["fingerprint"] == original_users[_id(3)]["fingerprint"]
    assert user["subject"] == "CN=Café,O=Latest"
    assert _rows(legacy_path, "users")[0]["created_at"] == NOW - 200
    assert _rows(legacy_path, "user_tokens") == [
        {**row, "user_id": _id(1)} for row in original_tokens
    ]
    archives = _rows(legacy_path, "legacy_user_identities")
    assert {item["id"] for item in archives} == set(original_users)
    for record in archives:
        assert record["canonical_user_id"] == _id(1)
        assert json.loads(record["original_record"]) == original_users[record["id"]]
    assert _rows(legacy_path, "audit_events")[: len(original_events)] == original_events
    merged = [item for item in store.list_events()["items"] if item["action"] == "user.cn_merged"]
    assert {json.loads(item["details"])["legacy_user_id"] for item in merged} == {_id(2), _id(3)}
    assert all(item["target_id"] == _id(1) for item in merged)
    for token in (first, second, latest):
        assert store.verify_user_token(token, common_name="Café")["id"] == _id(1)
    events = _rows(legacy_path, "audit_events")
    AccessStore(legacy_path)
    assert _rows(legacy_path, "audit_events") == events
    assert _rows(legacy_path, "legacy_user_identities") == archives
    with sqlite3.connect(legacy_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.filterwarnings(
    "ignore:Attribute's length must be >= 1 and <= 64:UserWarning:corporate_kb.access.models"
)
def test_migration_reads_long_cyrillic_cn_from_real_certificate_not_rfc4514(legacy_path):
    common_name = "АлексейРазработчик" * 4
    assert len(common_name.encode("utf-8")) > 64 and len(common_name) <= 256
    # Only this legacy fixture bypasses certificate-construction length validation;
    # deployed certificates can already contain such names and must remain readable.
    with pytest.warns(UserWarning, match="Attribute's length must be"):
        attribute = x509.NameAttribute(NameOID.COMMON_NAME, common_name, _validate=False)
    subject = x509.Name([attribute])
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.fromtimestamp(NOW, UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    token = _add_user(legacy_path, 1, subject.rfc4514_string())
    pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    with sqlite3.connect(legacy_path) as connection:
        connection.execute(
            "UPDATE users SET certificate_pem = ?, fingerprint = ? WHERE id = ?",
            (pem, certificate.fingerprint(hashes.SHA256()).hex(), _id(1)),
        )
    store = AccessStore(legacy_path)
    user = store.verify_user_token(token, common_name=common_name)
    assert user is not None
    assert user["id"] == _id(1)
    assert user["common_name"] == common_name
    assert store.list_users()["items"][0]["common_name"] == common_name
    assert _rows(legacy_path, "users")[0]["certificate_pem"] == pem


def test_any_revoked_duplicate_revokes_entire_cn_without_losing_old_tokens(legacy_path):
    active = _add_user(legacy_path, 1, "CN=Developer", created_at=NOW - 200)
    revoked = _add_user(legacy_path, 2, "CN=Developer", revoked=True)
    original_hashes = {row["token_hash"] for row in _rows(legacy_path, "user_tokens")}
    store = AccessStore(legacy_path)
    user = store.list_users()["items"][0]
    assert user["id"] == _id(1)
    assert user["status"] == "revoked"
    assert user["revocation_reason"] == "Contract ended"
    assert store.verify_user_token(active) is None
    assert store.verify_user_token(revoked) is None
    rows = _rows(legacy_path, "user_tokens")
    assert {row["token_hash"] for row in rows} == original_hashes
    assert {row["user_id"] for row in rows} == {_id(1)}
    assert {row["revoked_at"] for row in rows} == {NOW - 4, NOW}
    with pytest.raises(AccessDenied, match="revoked"):
        AccessStore(legacy_path).enroll(_certificate("CN=Developer"), existing_token=active)
    store.restore_user(_id(1), actor="admin")
    assert all(store.verify_user_token(token) is None for token in (active, revoked))
    restored = store.enroll(_certificate("CN=Developer"), existing_token=active)
    assert restored.user["id"] == _id(1)
    assert restored.token not in (active, revoked)


def test_migration_never_extends_an_already_expired_token(legacy_path):
    expired = _add_user(legacy_path, 1, "CN=Developer", expires_at=NOW - 1)
    before = _rows(legacy_path, "user_tokens")
    store = AccessStore(legacy_path, token_ttl_seconds=600)
    assert _rows(legacy_path, "user_tokens") == before
    assert store.verify_user_token(expired) is None
    assert store.list_tokens()["items"][0]["status"] == "expired"
    renewed = store.enroll(_certificate("CN=Developer"), existing_token=expired)
    assert renewed.user["id"] == _id(1)
    assert renewed.token != expired
    assert renewed.expires_at == NOW + 600


def test_cn_migration_rolls_back_all_ddl_merges_and_token_changes_on_failure(
    legacy_path, monkeypatch
):
    _add_user(legacy_path, 1, "CN=Developer", created_at=NOW - 200)
    _add_user(legacy_path, 2, "CN=Developer")
    before = {
        table: _rows(legacy_path, table) for table in ("users", "user_tokens", "audit_events")
    }
    original_audit = AccessStore._audit

    def fail_on_merge(connection, **kwargs):
        if kwargs["action"] == "user.cn_merged":
            raise RuntimeError("simulated migration write failure")
        return original_audit(connection, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AccessStore, "_audit", staticmethod(fail_on_merge))
        with pytest.raises(RuntimeError, match="simulated migration"):
            AccessStore(legacy_path)
    with sqlite3.connect(legacy_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "common_name" not in {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'legacy_user_identities'"
            ).fetchone()
            is None
        )
    for table, expected in before.items():
        assert _rows(legacy_path, table) == expected
    assert AccessStore(legacy_path).list_users()["total"] == 1


def test_concurrent_initializers_migrate_one_time_without_losing_records(legacy_path):
    first = _add_user(legacy_path, 1, "CN=Developer", created_at=NOW - 200)
    second = _add_user(legacy_path, 2, "CN=Developer")
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: AccessStore(legacy_path), range(4)))
    for store in stores:
        assert store.verify_user_token(first)["id"] == _id(1)
        assert store.verify_user_token(second)["id"] == _id(1)
    actions = [event["action"] for event in stores[0].list_events()["items"]]
    assert actions.count("schema.cn_identity_migrated") == 1
    assert actions.count("user.cn_merged") == 1
    assert len(_rows(legacy_path, "legacy_user_identities")) == 2
