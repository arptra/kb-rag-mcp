"""Transport-independent identities and results for access management."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from cryptography import x509
from cryptography.x509.oid import NameOID


def normalize_common_name(value: str) -> str:
    """Canonical account key: NFC, trimmed, case-sensitive, never control characters."""
    if not isinstance(value, str) or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise ValueError("Client CN must be text without control characters")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > 256:
        raise ValueError("Client CN must contain 1 to 256 characters")
    return normalized


def _common_name_from_name(subject: x509.Name) -> str | None:
    try:
        attributes = subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if len(attributes) != 1 or not isinstance(attributes[0].value, str):
            return None
        return normalize_common_name(attributes[0].value)
    except (ValueError, TypeError):
        return None


def common_name_from_subject(subject: str) -> str | None:
    """Fallback for historical text-only subjects; never split escaped DNs on commas."""
    try:
        return _common_name_from_name(x509.Name.from_rfc4514_string(subject))
    except (ValueError, TypeError):
        return None


def common_name_from_certificate(certificate_pem: str, subject: str = "") -> str | None:
    """Prefer actual certificate attributes, avoiding RFC4514 constructor length limits."""
    try:
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
    except (ValueError, UnicodeError):
        # Old test/legacy records may contain only the serialized subject.
        return common_name_from_subject(subject)
    return _common_name_from_name(certificate.subject)


class AccessDenied(PermissionError):
    """Authentication failed or the authenticated principal has been revoked."""


class AccessRateLimited(AccessDenied):
    """Persisted authentication throttling is in effect."""


@dataclass(frozen=True)
class CertificateIdentity:
    """Certificate metadata from TLS; CN is claimed identity, not verified employee identity."""

    fingerprint: str
    subject: str
    issuer: str
    serial_number: str
    not_before: int
    not_after: int
    certificate_pem: str

    @property
    def common_name(self) -> str | None:
        return common_name_from_certificate(self.certificate_pem, self.subject)


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
