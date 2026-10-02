"""Transport-independent identities and results for access management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class AccessDenied(PermissionError):
    """Authentication failed or the authenticated principal has been revoked."""


class AccessRateLimited(AccessDenied):
    """Persisted authentication throttling is in effect."""


@dataclass(frozen=True)
class CertificateIdentity:
    """Public identity extracted from a separately verified TLS client certificate."""

    fingerprint: str
    subject: str
    issuer: str
    serial_number: str
    not_before: int
    not_after: int
    certificate_pem: str


@dataclass(frozen=True)
class IssuedToken:
    token: str
    token_id: str
    expires_at: int
    user: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class IssuedAdminSession:
    token: str
    expires_at: int
    admin: dict[str, Any]
