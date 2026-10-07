"""Expose TLS-authenticated certificates under the selected policy, never HTTP headers."""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.protocols.http.h11_impl import H11Protocol

from corporate_kb.access.models import CertificateIdentity

_CERTIFICATE_SCOPE_KEY = "corporate_kb.verified_client_certificate"


def identity_from_der(der: bytes) -> CertificateIdentity:
    """Parse DER; this function alone establishes neither key possession nor CA trust."""
    certificate = x509.load_der_x509_certificate(der)
    return CertificateIdentity(
        fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
        subject=certificate.subject.rfc4514_string(),
        issuer=certificate.issuer.rfc4514_string(),
        serial_number=format(certificate.serial_number, "x"),
        not_before=int(certificate.not_valid_before_utc.timestamp()),
        not_after=int(certificate.not_valid_after_utc.timestamp()),
        certificate_pem=certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
    )


def certificate_from_scope(scope: Scope) -> CertificateIdentity | None:
    """Only our TLS adapter can populate this key; forwarded headers are ignored."""
    identity = scope.get(_CERTIFICATE_SCOPE_KEY)
    if scope.get("scheme") != "https" or not isinstance(identity, CertificateIdentity):
        return None
    if identity.common_name is None:
        return None
    return identity


class _CertificateScopeApp:
    def __init__(self, app: ASGIApp, identity: CertificateIdentity | None) -> None:
        self.app = app
        self.identity = identity

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scoped = dict(scope)
        scoped.pop(_CERTIFICATE_SCOPE_KEY, None)
        if self.identity is not None:
            scoped[_CERTIFICATE_SCOPE_KEY] = self.identity
        await self.app(scoped, receive, send)


class CertificateH11Protocol(H11Protocol):
    """Small per-connection adapter for Uvicorn's otherwise certificate-blind scope.

    The OpenSSL handshake validates the chain, client purpose, expiry and private-key
    possession before connection_made runs. CERT_OPTIONAL permits password-only
    access-administrators and previously enrolled bearer clients, but rejects an
    untrusted certificate if one is presented. Enrollment requires a verified peer.
    """

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if not isinstance(transport, asyncio.Transport):
            transport.close()
            return
        super().connection_made(transport)
        peer: Any = transport.get_extra_info("ssl_object")
        identity = None
        if peer is not None and peer.context.verify_mode in (ssl.CERT_OPTIONAL, ssl.CERT_REQUIRED):
            der = peer.getpeercert(binary_form=True)
            if der:
                try:
                    identity = identity_from_der(der)
                except ValueError:
                    transport.close()
                    return
        self.app = _CertificateScopeApp(self.app, identity)
