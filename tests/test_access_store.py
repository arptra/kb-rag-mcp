from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from corporate_kb.access import (
    AccessDenied,
    AccessRateLimited,
    AccessStore,
    CertificateIdentity,
)

NOW = 1_800_000_000
PASSWORD = "a-long-unique-admin-password"
SECOND_PASSWORD = "a-different-strong-password"


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("corporate_kb.access.store._now", lambda: NOW)


@pytest.fixture
def certificate() -> CertificateIdentity:
    return CertificateIdentity(
        fingerprint=hashlib.sha256(b"public-client-certificate").hexdigest(),
        subject="CN=Developer One,O=Example",
        issuer="CN=Trusted Client CA",
        serial_number="42",
        not_before=NOW - 60,
        not_after=NOW + 3600,
        certificate_pem="-----BEGIN CERTIFICATE-----\npublic-data\n-----END CERTIFICATE-----",
    )


@pytest.fixture
def store(tmp_path: Path) -> AccessStore:
    return AccessStore(tmp_path / "access.sqlite3", token_ttl_seconds=600)


def test_enroll_reuse_and_public_payload(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    issued = store.enroll(certificate, address="192.0.2.10")
    assert issued.created is True
    assert issued.expires_at == NOW + 600
    assert issued.user["subject"] == certificate.subject
    assert issued.user["common_name"] == "Developer One"
    assert issued.user["created_at"].endswith("Z")
    assert issued.user["status"] == "active"
    assert "certificate_pem" not in issued.user
    assert store.verify_user_token(issued.token) == issued.user
    assert store.verify_user_token(issued.token, common_name=certificate.common_name) == issued.user
    assert store.verify_user_token(issued.token, common_name="Another Person") is None
    assert store.verify_user_token(issued.token, common_name="") is None
    assert store.verify_user_token(issued.token, common_name="developer one") is None
    assert store.verify_user_token("unknown") is None
    assert store.verify_user_token("x" * 10000) is None
    reused = store.enroll(certificate, existing_token=issued.token)
    assert reused.token == issued.token
    assert reused.token_id == issued.token_id
    assert reused.created is False
    assert store.list_users()["total"] == 1
    assert store.list_tokens()["total"] == 1
    assert store.list_tokens()["items"][0]["last_used_at"].endswith("Z")
    assert store.list_tokens()["items"][0]["common_name"] == "Developer One"
    actions = {event["action"] for event in store.list_events()["items"]}
    assert {"user.enrolled", "user.token_issued", "user.token_reused"} <= actions


def test_token_expiry_is_independent_of_certificate_dates(
    store: AccessStore, certificate: CertificateIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    short_certificate = replace(certificate, not_after=NOW + 20)
    issued = store.enroll(short_certificate)
    assert issued.expires_at == NOW + 600
    monkeypatch.setattr("corporate_kb.access.store._now", lambda: NOW + 20)
    assert store.verify_user_token(issued.token) is not None
    assert store.list_tokens()["items"][0]["status"] == "active"
    assert store.enroll(short_certificate, existing_token=issued.token).token == issued.token
    monkeypatch.setattr("corporate_kb.access.store._now", lambda: NOW + 600)
    assert store.verify_user_token(issued.token) is None
    assert store.list_tokens()["items"][0]["status"] == "expired"


@pytest.mark.parametrize("changes", [{"not_before": NOW + 1}, {"not_after": NOW}])
def test_certificate_dates_are_metadata_not_account_authorization(
    store: AccessStore, certificate: CertificateIdentity, changes: dict[str, int]
) -> None:
    issued = store.enroll(replace(certificate, **changes))
    assert issued.expires_at == NOW + 600
    assert store.verify_user_token(issued.token) is not None
    assert store.list_users()["total"] == 1


@pytest.mark.parametrize("subject", ["O=Example", "CN=", "CN=One,CN=Two", "not-a-subject"])
def test_missing_ambiguous_or_invalid_common_name_denies_enrollment(
    store: AccessStore, certificate: CertificateIdentity, subject: str
) -> None:
    with pytest.raises(AccessDenied, match="Common Name"):
        store.enroll(replace(certificate, subject=subject))
    assert store.list_users()["total"] == 0
    assert store.list_tokens()["total"] == 0


def test_same_common_name_reuses_account_and_token_across_certificate_and_ca_change(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    first = store.enroll(certificate)
    renewed = replace(
        certificate,
        fingerprint="f" * 64,
        issuer="CN=Completely Different CA",
        serial_number="99",
        subject="OU=Another Company,CN=Developer One",
        certificate_pem="new-public-certificate",
        not_before=NOW + 50,
        not_after=NOW + 100,
    )
    reused = store.enroll(renewed, existing_token=first.token)
    assert reused.token == first.token
    assert reused.token_id == first.token_id
    assert reused.expires_at == first.expires_at
    assert reused.created is False
    assert reused.user["id"] == first.user["id"]
    assert reused.user["created_at"] == first.user["created_at"]
    assert reused.user["fingerprint"] == renewed.fingerprint
    assert reused.user["subject"] == renewed.subject
    assert reused.user["issuer"] == renewed.issuer
    assert store.verify_user_token(first.token, common_name="Developer One") == reused.user
    assert store.list_users()["total"] == 1
    assert store.list_tokens()["total"] == 1


def test_common_name_normalization_is_unicode_aware_and_case_sensitive(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    first = store.enroll(replace(certificate, subject="CN=Cafe\u0301"))
    reused = store.enroll(
        replace(certificate, fingerprint="e" * 64, subject="CN=Café"),
        existing_token=first.token,
    )
    assert first.user["common_name"] == "Café"
    assert reused.token == first.token
    assert store.verify_user_token(first.token, common_name="  Cafe\u0301  ") is not None
    other = store.enroll(replace(certificate, fingerprint="d" * 64, subject="CN=café"))
    assert other.user["id"] != first.user["id"]


def test_cyrillic_common_name_token_verification(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    issued = store.enroll(replace(certificate, subject="CN=Алексей,O=Пример"))
    assert store.verify_user_token(issued.token, common_name="Алексей") == issued.user
    assert store.verify_user_token(issued.token, common_name="Александр") is None


def test_unknown_or_expired_token_can_be_replaced_only_by_enrollment(
    store: AccessStore, certificate: CertificateIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = store.enroll(certificate)
    monkeypatch.setattr("corporate_kb.access.store._now", lambda: NOW + 600)
    assert store.verify_user_token(first.token) is None
    second = store.enroll(certificate, existing_token=first.token)
    assert second.created is True
    assert first.token != second.token
    assert first.user["id"] == second.user["id"]
    third = store.enroll(certificate, existing_token="unknown-token-but-long-enough")
    assert third.created is True
    assert store.list_tokens()["total"] == 3


def test_existing_token_from_other_common_name_is_never_reused(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    original = store.enroll(certificate)
    other = store.enroll(
        replace(certificate, fingerprint="f" * 64, subject="CN=Another Person"),
        existing_token=original.token,
    )
    assert other.created is True
    assert other.user["id"] != original.user["id"]
    assert other.token != original.token
    assert store.verify_user_token(original.token) == original.user


def test_revoked_user_cannot_reenroll_even_after_restart_or_restore_old_token(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    first = store.enroll(certificate)
    second = store.enroll(certificate)
    store.revoke_user(first.user["id"], actor="admin-1", reason="Contract ended")
    reopened = AccessStore(store.path)
    assert reopened.verify_user_token(first.token) is None
    assert reopened.verify_user_token(second.token) is None
    for token in (None, first.token, "unknown-token-but-long-enough"):
        with pytest.raises(AccessDenied, match="revoked"):
            reopened.enroll(
                replace(certificate, fingerprint="a" * 64, issuer="CN=Renewal CA"),
                existing_token=token,
            )
    user = reopened.list_users()["items"][0]
    assert user["status"] == "revoked"
    assert user["revocation_reason"] == "Contract ended"
    assert all(token["status"] == "revoked" for token in reopened.list_tokens()["items"])
    reopened.restore_user(first.user["id"], actor="admin-1")
    assert reopened.verify_user_token(first.token) is None
    restored = reopened.enroll(certificate, existing_token=first.token)
    assert restored.created is True
    assert restored.user["id"] == first.user["id"]
    assert restored.token not in {first.token, second.token}
    assert reopened.verify_user_token(restored.token) is not None
    actions = {item["action"] for item in reopened.list_events()["items"]}
    assert {"user.revoked", "user.enrollment_denied", "user.restored"} <= actions


def test_individual_token_revocation_and_user_logout(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    first = store.enroll(certificate)
    second = store.enroll(certificate)
    store.revoke_token(first.token_id, actor="admin-1", reason="Laptop lost")
    assert store.verify_user_token(first.token) is None
    assert store.verify_user_token(second.token) is not None
    replacement = store.enroll(certificate, existing_token=first.token)
    assert replacement.created is True
    store.logout_user(second.token)
    store.logout_user(second.token)
    store.logout_user("unknown")
    assert store.verify_user_token(second.token) is None
    assert store.verify_user_token(replacement.token) is not None
    logout_events = [
        event for event in store.list_events()["items"] if event["details"] == "User logout"
    ]
    assert len(logout_events) == 1
    assert logout_events[0]["actor"] == second.user["id"]


def test_bootstrap_requires_explicit_strong_credentials_and_never_resets(
    store: AccessStore,
) -> None:
    with pytest.raises(ValueError, match="First startup"):
        store.bootstrap_admin(None, None)
    with pytest.raises(ValueError, match="16"):
        store.bootstrap_admin("admin", "short")
    store.bootstrap_admin(" Admin ", PASSWORD)
    reopened = AccessStore(store.path)
    reopened.bootstrap_admin("different", SECOND_PASSWORD)
    reopened.bootstrap_admin(None, None)
    assert len(reopened.list_admins()["items"]) == 1
    assert reopened.list_admins()["items"][0]["username"] == "admin"
    session = reopened.login_admin("ADMIN", PASSWORD)
    assert reopened.verify_admin_session(session.token) == session.admin
    with pytest.raises(AccessDenied):
        reopened.login_admin("admin", SECOND_PASSWORD)


def test_admin_logout_expiry_and_account_deactivation(
    store: AccessStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.bootstrap_admin("admin", PASSWORD)
    original = store.login_admin("admin", PASSWORD)
    added = store.add_admin("another", SECOND_PASSWORD, actor=original.admin["id"])
    second = store.login_admin("another", SECOND_PASSWORD)
    assert store.verify_admin_session(second.token) is not None
    store.logout_admin(original.token)
    assert store.verify_admin_session(original.token) is None
    store.logout_admin(original.token)
    store.logout_admin("nonexistent")
    store.deactivate_admin(added["id"], actor=original.admin["id"])
    assert store.verify_admin_session(second.token) is None
    with pytest.raises(AccessDenied):
        store.login_admin("another", SECOND_PASSWORD)
    with pytest.raises(ValueError, match="last active"):
        store.deactivate_admin(original.admin["id"], actor=original.admin["id"])
    session = store.login_admin("admin", PASSWORD)
    monkeypatch.setattr("corporate_kb.access.store._now", lambda: session.expires_at)
    assert store.verify_admin_session(session.token) is None


def test_bootstrap_does_not_resurrect_disabled_admin(store: AccessStore) -> None:
    store.bootstrap_admin("first", PASSWORD)
    first = store.list_admins()["items"][0]
    second = store.add_admin("second", SECOND_PASSWORD, actor=first["id"])
    store.deactivate_admin(first["id"], actor=second["id"])
    reopened = AccessStore(store.path)
    reopened.bootstrap_admin("first", "yet-another-long-admin-password")
    with pytest.raises(AccessDenied):
        reopened.login_admin("first", PASSWORD)
    assert reopened.login_admin("second", SECOND_PASSWORD).admin["id"] == second["id"]
    with pytest.raises(ValueError, match="already exists"):
        reopened.add_admin("FIRST", PASSWORD, actor=second["id"])


def test_admin_and_user_tokens_are_not_interchangeable(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    store.bootstrap_admin("admin", PASSWORD)
    admin = store.login_admin("admin", PASSWORD)
    user = store.enroll(certificate)
    assert store.verify_admin_session(user.token) is None
    assert store.verify_user_token(admin.token) is None


def test_login_throttle_persists_and_expires(
    store: AccessStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.bootstrap_admin("admin", PASSWORD)
    messages = []
    for _ in range(5):
        with pytest.raises(AccessDenied) as caught:
            store.login_admin("admin", SECOND_PASSWORD, address="192.0.2.1")
        assert not isinstance(caught.value, AccessRateLimited)
        messages.append(str(caught.value))
    reopened = AccessStore(store.path)
    events_before = reopened.list_events()
    assert events_before["items"][0]["details"] == "Username/address throttle activated"
    with pytest.raises(AccessRateLimited) as caught:
        reopened.login_admin("admin", PASSWORD, address="192.0.2.1")
    assert str(caught.value) == messages[0]
    for _ in range(100):
        with pytest.raises(AccessRateLimited):
            reopened.login_admin("admin", PASSWORD, address="192.0.2.1")
    assert reopened.list_events() == events_before
    assert reopened.login_admin("admin", PASSWORD, address="192.0.2.2")
    monkeypatch.setattr("corporate_kb.access.store._now", lambda: NOW + 900)
    assert reopened.login_admin("admin", PASSWORD, address="192.0.2.1")


def test_address_throttle_prevents_username_rotation(store: AccessStore) -> None:
    store.bootstrap_admin("admin", PASSWORD)
    for number in range(30):
        with pytest.raises(AccessDenied) as caught:
            store.login_admin(f"unknown{number}", PASSWORD, address="192.0.2.1")
        assert not isinstance(caught.value, AccessRateLimited)
    reopened = AccessStore(store.path)
    events_before = reopened.list_events()
    assert events_before["items"][0]["details"] == "Address throttle activated"
    for number in range(100):
        with pytest.raises(AccessRateLimited):
            reopened.login_admin(f"rotated{number}", PASSWORD, address="192.0.2.1")
    assert reopened.list_events() == events_before
    assert store.login_admin("admin", PASSWORD, address="192.0.2.2")


def test_generic_bounded_login_failures(store: AccessStore) -> None:
    store.bootstrap_admin("admin", PASSWORD)
    messages = []
    for number, (username, password) in enumerate(
        [
            ("admin", "wrong"),
            ("unknown", PASSWORD),
            ("x" * 10000, PASSWORD),
            ("admin", "p" * 10000),
            ("admin", "🔐" * 1025),
        ]
    ):
        with pytest.raises(AccessDenied) as caught:
            store.login_admin(username, password, address=f"192.0.2.{number}")
        messages.append(str(caught.value))
    assert len(set(messages)) == 1


def test_password_hashes_are_salted_and_no_secret_is_stored_or_listed(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    store.bootstrap_admin("admin", PASSWORD)
    store.add_admin("other", PASSWORD, actor="admin")
    admin = store.login_admin("admin", PASSWORD)
    user = store.enroll(certificate)
    raw = store.path.read_bytes()
    for secret in (PASSWORD, admin.token, user.token):
        assert secret.encode() not in raw
    with sqlite3.connect(store.path) as connection:
        hashes = [item[0] for item in connection.execute("SELECT password_hash FROM admins")]
        assert hashes[0] != hashes[1]
        assert all(value.startswith("scrypt$") for value in hashes)
        assert connection.execute("SELECT certificate_pem FROM users").fetchone()[0] == (
            certificate.certificate_pem
        )
    lists = json.dumps(
        {
            "users": store.list_users(),
            "tokens": store.list_tokens(),
            "admins": store.list_admins(),
            "events": store.list_events(),
        }
    )
    for secret in (PASSWORD, admin.token, user.token, *hashes, certificate.certificate_pem):
        assert secret not in lists
    assert "token_hash" not in lists
    assert "password_hash" not in lists


def test_permissions_and_parent_preservation(tmp_path: Path) -> None:
    parent = tmp_path / "existing-dir"
    parent.mkdir(mode=0o755)
    original_mode = parent.stat().st_mode & 0o777
    store = AccessStore(parent / "access.sqlite3")
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert parent.stat().st_mode & 0o777 == original_mode
    os.chmod(store.path, 0o644)
    AccessStore(store.path)
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_database_symlink_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "real.sqlite3"
    AccessStore(target)
    alias = tmp_path / "linked.sqlite3"
    alias.symlink_to(target)
    with pytest.raises(OSError):
        AccessStore(alias)


def test_unknown_new_schema_fails_closed(store: AccessStore) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(ValueError, match="newer"):
        AccessStore(store.path)


def test_concurrent_enrollment_creates_one_user(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda number: store.enroll(
                    replace(
                        certificate, fingerprint=hashlib.sha256(str(number).encode()).hexdigest()
                    )
                ),
                range(20),
            )
        )
    assert len({result.user["id"] for result in results}) == 1
    assert len({result.token for result in results}) == 20
    assert store.list_users()["total"] == 1
    assert store.list_tokens()["total"] == 20
    actions = [event["action"] for event in store.list_events()["items"]]
    assert actions.count("user.enrolled") == 1


def test_concurrent_revoke_and_enroll_never_resurrect_user(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    original = store.enroll(certificate)
    gate = Barrier(8)

    def operation(number: int) -> str | None:
        gate.wait(timeout=10)
        if number == 0:
            store.revoke_user(original.user["id"], actor="admin")
            return None
        try:
            return store.enroll(
                replace(certificate, fingerprint=hashlib.sha256(str(number).encode()).hexdigest())
            ).token
        except AccessDenied:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(operation, range(8)))
    assert store.list_users()["items"][0]["status"] == "revoked"
    assert store.verify_user_token(original.token) is None
    for token in results:
        if token is not None:
            assert store.verify_user_token(token) is None
    with pytest.raises(AccessDenied):
        store.enroll(certificate)


def test_concurrent_admin_deactivation_preserves_last_admin(store: AccessStore) -> None:
    store.bootstrap_admin("first", PASSWORD)
    first = store.list_admins()["items"][0]
    second = store.add_admin("second", SECOND_PASSWORD, actor=first["id"])
    gate = Barrier(2)

    def deactivate(admin_id: str) -> bool:
        gate.wait(timeout=10)
        try:
            store.deactivate_admin(admin_id, actor="admin")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(deactivate, [first["id"], second["id"]]))
    assert sorted(outcomes) == [False, True]
    assert sum(admin["status"] == "active" for admin in store.list_admins()["items"]) == 1


def test_pagination_unknown_targets_and_validation(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    user = store.enroll(certificate)
    store.enroll(certificate)
    assert len(store.list_tokens(user.user["id"], limit=1)["items"]) == 1
    assert store.list_tokens("missing")["total"] == 0
    assert store.list_users(limit=1, offset=1) == {"items": [], "total": 1}
    assert store.list_events(limit=1)["total"] == 3
    with pytest.raises(ValueError):
        store.list_users(limit=0)
    with pytest.raises(ValueError):
        store.list_events(offset=-1)
    with pytest.raises(KeyError):
        store.revoke_user("missing", actor="admin")
    with pytest.raises(KeyError):
        store.restore_user("missing", actor="admin")
    with pytest.raises(KeyError):
        store.revoke_token("missing", actor="admin")
    with pytest.raises(KeyError):
        store.deactivate_admin("missing", actor="admin")
    with pytest.raises(ValueError):
        store.enroll(replace(certificate, fingerprint="not-a-fingerprint"))
    with pytest.raises(ValueError):
        store.enroll(replace(certificate, subject="x" * 8193))


@pytest.mark.parametrize("revocation", ["logout", "disabled", "expiry"])
def test_admin_mutations_recheck_session_inside_transaction(
    store: AccessStore,
    certificate: CertificateIdentity,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
) -> None:
    store.bootstrap_admin("first", PASSWORD)
    session = store.login_admin("first", PASSWORD)
    second = store.add_admin("second", SECOND_PASSWORD, actor=session.admin["id"])
    user = store.enroll(certificate)
    # An HTTP adapter has already accepted this session, before reading a body.
    assert store.verify_admin_session(session.token) is not None
    if revocation == "logout":
        store.logout_admin(session.token)
    elif revocation == "disabled":
        store.deactivate_admin(session.admin["id"], actor=second["id"])
    else:
        monkeypatch.setattr("corporate_kb.access.store._now", lambda: session.expires_at)
    with pytest.raises(AccessDenied, match="no longer active"):
        store.add_admin("resurrection", PASSWORD, actor="forged", admin_session=session.token)
    with pytest.raises(AccessDenied):
        store.revoke_user(user.user["id"], actor="forged", admin_session=session.token)
    with pytest.raises(AccessDenied):
        store.restore_user(user.user["id"], actor="forged", admin_session=session.token)
    with pytest.raises(AccessDenied):
        store.revoke_token(user.token_id, actor="forged", admin_session=session.token)
    with pytest.raises(AccessDenied):
        store.deactivate_admin(second["id"], actor="forged", admin_session=session.token)
    assert len(store.list_admins()["items"]) == 2
    assert store.list_users()["items"][0]["status"] == "active"
    assert store.list_tokens()["items"][0]["revoked_at"] is None
    assert all(event["actor"] != "forged" for event in store.list_events()["items"])


def test_admin_mutations_accept_live_session_and_attribute_real_actor(
    store: AccessStore, certificate: CertificateIdentity
) -> None:
    store.bootstrap_admin("first", PASSWORD)
    session = store.login_admin("first", PASSWORD)
    user = store.enroll(certificate)
    with pytest.raises(AccessDenied):
        store.revoke_user(user.user["id"], actor="forged", admin_session="")
    with pytest.raises(AccessDenied):
        store.revoke_user(user.user["id"], actor="forged", admin_session=user.token)
    second = store.add_admin("second", SECOND_PASSWORD, actor="forged", admin_session=session.token)
    store.revoke_token(user.token_id, actor="forged", admin_session=session.token)
    store.revoke_user(user.user["id"], actor="forged", admin_session=session.token)
    store.restore_user(user.user["id"], actor="forged", admin_session=session.token)
    store.deactivate_admin(second["id"], actor="forged", admin_session=session.token)
    for event in store.list_events()["items"]:
        if event["action"] in {
            "admin.created",
            "token.revoked",
            "user.revoked",
            "user.restored",
            "admin.disabled",
        }:
            assert event["actor"] == "first"
