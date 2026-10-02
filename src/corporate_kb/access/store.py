"""Transactional SQLite access registry. Never accepts private certificate keys.

The caller must authenticate TLS certificates before enrollment. This layer does
not trust certificate headers or perform certificate-chain verification. Secret
tokens are returned only at issuance, and only their SHA-256 hashes are stored.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard

from corporate_kb.access.models import (
    AccessDenied,
    AccessRateLimited,
    CertificateIdentity,
    IssuedAdminSession,
    IssuedToken,
)

_SCHEMA_VERSION = 1
_LOGIN_FAILURE_LIMIT = 5
_ADDRESS_FAILURE_LIMIT = 30
_LOGIN_WINDOW_SECONDS = 900
_MAX_TOKEN_LENGTH = 512
_SCHEMA = (
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


def _now() -> int:
    return int(time.time())


def _iso(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def _new_id() -> str:
    return str(uuid.uuid4())


def _secret_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_token(token: str | None) -> TypeGuard[str]:
    return isinstance(token, str) and 20 <= len(token) <= _MAX_TOKEN_LENGTH and token.isascii()


def _username(value: str) -> str:
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("Administrator username must contain 1 to 128 characters")
    value = value.strip().casefold()
    if not value or len(value) > 128 or any(character.isspace() for character in value):
        raise ValueError("Administrator username must contain 1 to 128 non-space characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Administrator username cannot contain control characters")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("Administrator username must be valid Unicode") from error
    return value


def _validate_password(password: str) -> None:
    if not isinstance(password, str) or not 16 <= len(password) <= 1024:
        raise ValueError("Administrator password must contain 16 to 1024 characters")
    try:
        encoded = password.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("Administrator password must be valid Unicode") from error
    if len(encoded) > 4096:
        raise ValueError("Administrator password must use at most 4096 UTF-8 bytes")


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=5,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    return f"scrypt$16384$8$5${salt.hex()}${digest.hex()}"


def _check_password(password: str, encoded: str) -> bool:
    try:
        name, n, r, p, salt, expected = encoded.split("$")
        # Bound parameters even when the local database has been modified.
        if (name, n, r, p) != ("scrypt", "16384", "8", "5"):
            return False
        salt_bytes = bytes.fromhex(salt)
        expected_bytes = bytes.fromhex(expected)
        if len(salt_bytes) != 16 or len(expected_bytes) != 32:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt_bytes,
            n=16384,
            r=8,
            p=5,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected_bytes)
    except (ValueError, TypeError):
        return False


def _user_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = {
        key: row[key]
        for key in (
            "id",
            "subject",
            "issuer",
            "serial_number",
            "fingerprint",
            "status",
            "revocation_reason",
        )
    }
    for key in ("not_before", "not_after", "created_at", "last_seen_at", "revoked_at"):
        payload[key] = _iso(row[key])
    return payload


def _admin_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "status": row["status"],
        "created_at": _iso(row["created_at"]),
        "last_login_at": _iso(row["last_login_at"]),
    }


def _page(limit: int, offset: int) -> tuple[int, int]:
    if not isinstance(limit, int) or not isinstance(offset, int) or limit < 1 or offset < 0:
        raise ValueError("Pagination requires a positive limit and non-negative offset")
    return min(limit, 1000), offset


class AccessStore:
    """Small independent access subsystem with one transaction per operation.

    A fresh connection per call supports threads and multiple server processes.
    Revocations and token verification are checked against persistent state, not
    process-local caches. Existing parent directory permissions are not changed.
    """

    def __init__(
        self,
        path: Path,
        *,
        token_ttl_seconds: int = 2592000,
        admin_session_ttl_seconds: int = 28800,
    ) -> None:
        if token_ttl_seconds <= 0 or admin_session_ttl_seconds <= 0:
            raise ValueError("Access token and administrator session TTLs must be positive")
        self.path = Path(path).absolute()
        self.token_ttl_seconds = token_ttl_seconds
        self.admin_session_ttl_seconds = admin_session_ttl_seconds
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self._dummy_password_hash = _hash_password(secrets.token_urlsafe(32))
        with self._connection(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > _SCHEMA_VERSION:
                raise ValueError("Access database schema is newer than this application")
            if version == 0:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        actor: str,
        action: str,
        target_type: str,
        target_id: str = "",
        address: str = "",
        details: str = "",
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?)",
            (
                _new_id(),
                _now(),
                str(actor)[:256],
                action,
                target_type,
                target_id,
                str(address)[:255],
                str(details)[:2000],
            ),
        )

    def bootstrap_admin(self, username: str | None, password: str | None) -> None:
        """Create the first admin once. Environment changes never reset accounts."""
        with self._connection(write=True) as connection:
            if connection.execute("SELECT 1 FROM admins LIMIT 1").fetchone():
                return
            if username is None or password is None:
                raise ValueError("First startup requires an administrator username and password")
            normalized = _username(username)
            _validate_password(password)
            admin_id = _new_id()
            connection.execute(
                "INSERT INTO admins VALUES (?,?,?,'active',?,NULL)",
                (admin_id, normalized, _hash_password(password), _now()),
            )
            self._audit(
                connection,
                actor="bootstrap",
                action="admin.bootstrap",
                target_type="admin",
                target_id=admin_id,
            )

    def enroll(
        self,
        certificate: CertificateIdentity,
        existing_token: str | None = None,
        address: str = "",
    ) -> IssuedToken:
        """Enroll a VERIFIED client certificate; explicit enrollment can replace a token."""
        now = _now()
        fingerprint = certificate.fingerprint.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("Certificate fingerprint must be a SHA-256 hexadecimal digest")
        if certificate.not_before > now or certificate.not_after <= now:
            raise AccessDenied("Client certificate is not currently valid")
        for value, maximum in (
            (certificate.subject, 8192),
            (certificate.issuer, 8192),
            (certificate.serial_number, 256),
            (certificate.certificate_pem, 131072),
        ):
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError("Certificate metadata exceeds the permitted size")
        denied = False
        result: IssuedToken | None = None
        with self._connection(write=True) as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if user is not None and user["status"] != "active":
                self._audit(
                    connection,
                    actor=user["id"],
                    action="user.enrollment_denied",
                    target_type="user",
                    target_id=user["id"],
                    address=address,
                    details="User is revoked",
                )
                denied = True
            else:
                user_id = user["id"] if user is not None else _new_id()
                connection.execute(
                    """INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?,'active',NULL,'')
                    ON CONFLICT(fingerprint) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
                    (
                        user_id,
                        fingerprint,
                        certificate.subject,
                        certificate.issuer,
                        certificate.serial_number,
                        certificate.not_before,
                        certificate.not_after,
                        certificate.certificate_pem,
                        now,
                        now,
                    ),
                )
                if user is None:
                    self._audit(
                        connection,
                        actor=user_id,
                        action="user.enrolled",
                        target_type="user",
                        target_id=user_id,
                        address=address,
                    )
                user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                token = None
                if _valid_token(existing_token):
                    token = connection.execute(
                        """SELECT * FROM user_tokens WHERE token_hash = ? AND user_id = ?
                        AND revoked_at IS NULL AND expires_at > ?""",
                        (_secret_hash(existing_token), user_id, now),
                    ).fetchone()
                if token is not None:
                    assert existing_token is not None
                    connection.execute(
                        "UPDATE user_tokens SET last_used_at = ? WHERE id = ?", (now, token["id"])
                    )
                    self._audit(
                        connection,
                        actor=user_id,
                        action="user.token_reused",
                        target_type="token",
                        target_id=token["id"],
                        address=address,
                    )
                    result = IssuedToken(
                        token=existing_token,
                        token_id=token["id"],
                        expires_at=token["expires_at"],
                        user=_user_payload(user),
                        created=False,
                    )
                else:
                    secret = "kb_" + secrets.token_urlsafe(32)
                    token_id = _new_id()
                    expires_at = min(now + self.token_ttl_seconds, certificate.not_after)
                    connection.execute(
                        "INSERT INTO user_tokens VALUES (?,?,?,?,?,?,NULL,NULL)",
                        (token_id, user_id, _secret_hash(secret), secret[:11], now, expires_at),
                    )
                    self._audit(
                        connection,
                        actor=user_id,
                        action="user.token_issued",
                        target_type="token",
                        target_id=token_id,
                        address=address,
                    )
                    result = IssuedToken(
                        token=secret,
                        token_id=token_id,
                        expires_at=expires_at,
                        user=_user_payload(user),
                        created=True,
                    )
        if denied:
            raise AccessDenied("Access has been revoked")
        assert result is not None
        return result

    def verify_user_token(
        self, token: str, *, fingerprint: str | None = None
    ) -> dict[str, Any] | None:
        if not _valid_token(token):
            return None
        now = _now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                """SELECT u.*, t.id AS token_id FROM user_tokens t
                JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?
                AND t.revoked_at IS NULL AND t.expires_at > ? AND u.status = 'active'
                AND u.not_before <= ? AND u.not_after > ?""",
                (_secret_hash(token), now, now, now),
            ).fetchone()
            if row is None:
                return None
            if fingerprint is not None and not hmac.compare_digest(
                row["fingerprint"], fingerprint.lower()
            ):
                return None
            connection.execute(
                "UPDATE user_tokens SET last_used_at = ? WHERE id = ?", (now, row["token_id"])
            )
            connection.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (now, row["id"]))
            payload = _user_payload(row)
            payload["last_seen_at"] = _iso(now)
            return payload

    def login_admin(self, username: str, password: str, address: str = "") -> IssuedAdminSession:
        now = _now()
        malformed = False
        try:
            normalized = _username(username)
        except ValueError:
            normalized = "<invalid>"
            malformed = True
        try:
            _validate_password(password)
        except ValueError:
            malformed = True
        address = str(address)[:255]
        failure: AccessDenied | None = None
        result: IssuedAdminSession | None = None
        with self._connection(write=True) as connection:
            attempts = connection.execute(
                "SELECT * FROM login_attempts WHERE username = ? AND address = ?",
                (normalized, address),
            ).fetchone()
            address_attempts = connection.execute(
                "SELECT * FROM address_login_attempts WHERE address = ?", (address,)
            ).fetchone()
            if (attempts is not None and attempts["blocked_until"] > now) or (
                address_attempts is not None and address_attempts["blocked_until"] > now
            ):
                # The failure activating the throttle is already audited below.
                # An anonymous flood must not create one persistent row per 429.
                failure = AccessRateLimited("Invalid credentials or temporarily unavailable login")
            else:
                admin = connection.execute(
                    "SELECT * FROM admins WHERE username = ?", (normalized,)
                ).fetchone()
                password_hash = admin["password_hash"] if admin else self._dummy_password_hash
                # Bound work and equalize nonexistent/disabled account password checks.
                checked_password = password if not malformed else "invalid-input-placeholder"
                valid = _check_password(checked_password, password_hash)
                if malformed or not valid or admin is None or admin["status"] != "active":
                    reset = (
                        attempts is None
                        or attempts["window_started_at"] <= now - _LOGIN_WINDOW_SECONDS
                    )
                    failures = 1 if reset else attempts["failures"] + 1
                    window_started = now if reset else attempts["window_started_at"]
                    blocked_until = (
                        now + _LOGIN_WINDOW_SECONDS if failures >= _LOGIN_FAILURE_LIMIT else 0
                    )
                    connection.execute(
                        """INSERT INTO login_attempts VALUES (?,?,?,?,?)
                        ON CONFLICT(username,address) DO UPDATE SET failures = excluded.failures,
                        window_started_at = excluded.window_started_at,
                        blocked_until = excluded.blocked_until""",
                        (normalized, address, failures, window_started, blocked_until),
                    )
                    address_reset = (
                        address_attempts is None
                        or address_attempts["window_started_at"] <= now - _LOGIN_WINDOW_SECONDS
                    )
                    address_failures = 1 if address_reset else address_attempts["failures"] + 1
                    address_started = (
                        now if address_reset else address_attempts["window_started_at"]
                    )
                    address_blocked = (
                        now + _LOGIN_WINDOW_SECONDS
                        if address_failures >= _ADDRESS_FAILURE_LIMIT
                        else 0
                    )
                    connection.execute(
                        """INSERT INTO address_login_attempts VALUES (?,?,?,?)
                        ON CONFLICT(address) DO UPDATE SET failures = excluded.failures,
                        window_started_at = excluded.window_started_at,
                        blocked_until = excluded.blocked_until""",
                        (address, address_failures, address_started, address_blocked),
                    )
                    connection.execute(
                        """DELETE FROM login_attempts
                        WHERE blocked_until <= ? AND window_started_at <= ?""",
                        (now, now - _LOGIN_WINDOW_SECONDS),
                    )
                    connection.execute(
                        """DELETE FROM address_login_attempts
                        WHERE blocked_until <= ? AND window_started_at <= ?""",
                        (now, now - _LOGIN_WINDOW_SECONDS),
                    )
                    self._audit(
                        connection,
                        actor="anonymous",
                        action="admin.login_failed",
                        target_type="admin",
                        address=address,
                        details=(
                            "Address throttle activated"
                            if address_blocked
                            else "Username/address throttle activated"
                            if blocked_until
                            else ""
                        ),
                    )
                    failure = AccessDenied("Invalid credentials or temporarily unavailable login")
                else:
                    connection.execute(
                        "DELETE FROM login_attempts WHERE username = ? AND address = ?",
                        (normalized, address),
                    )
                    connection.execute(
                        "UPDATE admins SET last_login_at = ? WHERE id = ?", (now, admin["id"])
                    )
                    secret = "ka_" + secrets.token_urlsafe(32)
                    expires_at = now + self.admin_session_ttl_seconds
                    connection.execute(
                        "INSERT INTO admin_sessions VALUES (?,?,?,?,?,NULL)",
                        (_new_id(), admin["id"], _secret_hash(secret), now, expires_at),
                    )
                    self._audit(
                        connection,
                        actor=admin["id"],
                        action="admin.login",
                        target_type="admin",
                        target_id=admin["id"],
                        address=address,
                    )
                    payload = _admin_payload(admin)
                    payload["last_login_at"] = _iso(now)
                    result = IssuedAdminSession(token=secret, expires_at=expires_at, admin=payload)
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def verify_admin_session(self, token: str) -> dict[str, Any] | None:
        if not _valid_token(token):
            return None
        with self._connection() as connection:
            admin = connection.execute(
                """SELECT a.* FROM admin_sessions s JOIN admins a ON a.id = s.admin_id
                WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?
                AND a.status = 'active'""",
                (_secret_hash(token), _now()),
            ).fetchone()
            return _admin_payload(admin) if admin is not None else None

    def logout_admin(self, token: str) -> None:
        if not _valid_token(token):
            return
        with self._connection(write=True) as connection:
            session = connection.execute(
                "SELECT * FROM admin_sessions WHERE token_hash = ? AND revoked_at IS NULL",
                (_secret_hash(token),),
            ).fetchone()
            if session is None:
                return
            connection.execute(
                "UPDATE admin_sessions SET revoked_at = ? WHERE id = ?", (_now(), session["id"])
            )
            self._audit(
                connection,
                actor=session["admin_id"],
                action="admin.logout",
                target_type="session",
                target_id=session["id"],
            )

    def logout_user(self, token: str) -> None:
        """Revoke only the presented user token; no list or token ID is required."""
        if not _valid_token(token):
            return
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM user_tokens WHERE token_hash = ? AND revoked_at IS NULL",
                (_secret_hash(token),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE user_tokens SET revoked_at = ? WHERE id = ?", (_now(), row["id"])
            )
            self._audit(
                connection,
                actor=row["user_id"],
                action="token.revoked",
                target_type="token",
                target_id=row["id"],
                details="User logout",
            )

    def list_users(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit, offset = _page(limit, offset)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY created_at DESC, id LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            total = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            return {"items": [_user_payload(row) for row in rows], "total": total}

    def list_tokens(
        self, user_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        limit, offset = _page(limit, offset)
        clause = "WHERE user_id = ?" if user_id is not None else ""
        parameters = (user_id,) if user_id is not None else ()
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM user_tokens {clause} ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            total = connection.execute(
                f"SELECT COUNT(*) FROM user_tokens {clause}", parameters
            ).fetchone()[0]
            items = []
            for row in rows:
                payload = {key: row[key] for key in ("id", "user_id", "prefix")}
                for key in ("created_at", "expires_at", "last_used_at", "revoked_at"):
                    payload[key] = _iso(row[key])
                payload["status"] = (
                    "revoked"
                    if row["revoked_at"] is not None
                    else "expired"
                    if row["expires_at"] <= _now()
                    else "active"
                )
                items.append(payload)
            return {"items": items, "total": total}

    def list_admins(self) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM admins ORDER BY created_at, id").fetchall()
            return {"items": [_admin_payload(row) for row in rows]}

    def list_events(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit, offset = _page(limit, offset)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            items = []
            for row in rows:
                payload = dict(row)
                payload["created_at"] = _iso(row["created_at"])
                items.append(payload)
            return {"items": items, "total": total}

    @staticmethod
    def _authorize_admin_mutation(
        connection: sqlite3.Connection, session: str | None, actor: str
    ) -> str:
        """Recheck within the write transaction; middleware checks alone can race.

        None is reserved for trusted in-process operations/bootstrap tooling. HTTP
        adapters must always pass their administrator cookie, including an empty
        string when missing. Actor attribution follows the verified session.
        """
        if session is None:
            return actor
        if not _valid_token(session):
            raise AccessDenied("Administrator session is no longer active")
        admin = connection.execute(
            """SELECT a.username FROM admin_sessions s JOIN admins a ON a.id = s.admin_id
            WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?
            AND a.status = 'active'""",
            (_secret_hash(session), _now()),
        ).fetchone()
        if admin is None:
            raise AccessDenied("Administrator session is no longer active")
        return str(admin["username"])

    def revoke_user(
        self, user_id: str, actor: str, reason: str = "", *, admin_session: str | None = None
    ) -> None:
        with self._connection(write=True) as connection:
            actor = self._authorize_admin_mutation(connection, admin_session, actor)
            if (
                connection.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
                is None
            ):
                raise KeyError("Unknown user")
            now = _now()
            connection.execute(
                """UPDATE users SET status = 'revoked', revoked_at = ?, revocation_reason = ?
                WHERE id = ?""",
                (now, reason[:2000], user_id),
            )
            connection.execute(
                "UPDATE user_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            self._audit(
                connection,
                actor=actor,
                action="user.revoked",
                target_type="user",
                target_id=user_id,
                details=reason,
            )

    def restore_user(self, user_id: str, actor: str, *, admin_session: str | None = None) -> None:
        with self._connection(write=True) as connection:
            actor = self._authorize_admin_mutation(connection, admin_session, actor)
            if (
                connection.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
                is None
            ):
                raise KeyError("Unknown user")
            connection.execute(
                """UPDATE users SET status = 'active', revoked_at = NULL, revocation_reason = ''
                WHERE id = ?""",
                (user_id,),
            )
            self._audit(
                connection,
                actor=actor,
                action="user.restored",
                target_type="user",
                target_id=user_id,
            )

    def revoke_token(
        self, token_id: str, actor: str, reason: str = "", *, admin_session: str | None = None
    ) -> None:
        with self._connection(write=True) as connection:
            actor = self._authorize_admin_mutation(connection, admin_session, actor)
            token = connection.execute(
                "SELECT * FROM user_tokens WHERE id = ?", (token_id,)
            ).fetchone()
            if token is None:
                raise KeyError("Unknown token")
            connection.execute(
                "UPDATE user_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
                (_now(), token_id),
            )
            self._audit(
                connection,
                actor=actor,
                action="token.revoked",
                target_type="token",
                target_id=token_id,
                details=reason,
            )

    def add_admin(
        self, username: str, password: str, actor: str, *, admin_session: str | None = None
    ) -> dict[str, Any]:
        normalized = _username(username)
        _validate_password(password)
        encoded = _hash_password(password)
        with self._connection(write=True) as connection:
            actor = self._authorize_admin_mutation(connection, admin_session, actor)
            admin_id = _new_id()
            try:
                connection.execute(
                    "INSERT INTO admins VALUES (?,?,?,'active',?,NULL)",
                    (admin_id, normalized, encoded, _now()),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("Administrator username already exists") from error
            self._audit(
                connection,
                actor=actor,
                action="admin.created",
                target_type="admin",
                target_id=admin_id,
            )
            row = connection.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()
            return _admin_payload(row)

    def deactivate_admin(
        self, admin_id: str, actor: str, *, admin_session: str | None = None
    ) -> None:
        with self._connection(write=True) as connection:
            actor = self._authorize_admin_mutation(connection, admin_session, actor)
            admin = connection.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()
            if admin is None:
                raise KeyError("Unknown administrator")
            if admin["status"] == "disabled":
                return
            count = connection.execute(
                "SELECT COUNT(*) FROM admins WHERE status = 'active'"
            ).fetchone()[0]
            if count <= 1:
                raise ValueError("Cannot deactivate the last active administrator")
            now = _now()
            connection.execute("UPDATE admins SET status = 'disabled' WHERE id = ?", (admin_id,))
            connection.execute(
                """UPDATE admin_sessions SET revoked_at = ?
                WHERE admin_id = ? AND revoked_at IS NULL""",
                (now, admin_id),
            )
            self._audit(
                connection,
                actor=actor,
                action="admin.disabled",
                target_type="admin",
                target_id=admin_id,
            )
