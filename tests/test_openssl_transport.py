"""Socket-free regression checks for the direct TLS/HTTP protocol adapter."""

from __future__ import annotations

import asyncio
import ssl
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from OpenSSL import SSL
from uvicorn.config import Config
from uvicorn.server import ServerState

from corporate_kb.access.dev_pki import prepare_local_identity
from corporate_kb.access.openssl_transport import (
    _MAX_INPUT,
    _MAX_OUTPUT,
    _presented_identity,
    certificate_http_protocol,
)
from corporate_kb.access.tls import certificate_from_scope


class MemoryTransport(asyncio.Transport):
    def __init__(self, protocol):
        super().__init__()
        self.protocol = protocol
        self.output = bytearray()
        self.closed = False
        self.aborted = False
        self.read_paused = False

    def write(self, data):
        self.output.extend(data)

    def get_write_buffer_size(self):
        return 0  # The receiving Memory BIO immediately consumes these records.

    def get_extra_info(self, name, default=None):
        return {"sockname": ("127.0.0.1", 8443), "peername": ("127.0.0.1", 50000)}.get(
            name, default
        )

    def set_write_buffer_limits(self, high=None, low=None):
        pass

    def pause_reading(self):
        self.read_paused = True

    def resume_reading(self):
        self.read_paused = False

    def close(self):
        self.closed = True

    def abort(self):
        self.aborted = True
        self.closed = True


@pytest.fixture
def identity(tmp_path):
    return prepare_local_identity(tmp_path / "identity")


async def _exchange(protocol, raw, incoming, outgoing):
    if outgoing.pending:
        protocol.data_received(outgoing.read())
    await asyncio.sleep(0)
    if raw.output:
        incoming.write(bytes(raw.output))
        raw.output.clear()


async def _handshake(identity, app, *, certificate=True, version=ssl.TLSVersion.TLSv1_3):
    config = Config(app, access_log=False, lifespan="off", ws="none")
    state = ServerState()
    protocol_class = certificate_http_protocol(identity.server_cert, identity.server_key)
    protocol = protocol_class(config, state, {})
    raw = MemoryTransport(protocol)
    protocol.connection_made(raw)
    context = ssl.create_default_context(cafile=str(identity.ca_cert))
    context.minimum_version = context.maximum_version = version
    context.set_alpn_protocols(["h2", "http/1.1"])
    if certificate:
        context.load_cert_chain(identity.client_cert, identity.client_key)
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(incoming, outgoing, server_hostname="localhost")
    client_ready = False
    for _ in range(50):
        try:
            client.do_handshake()
            client_ready = True
        except ssl.SSLWantReadError:
            pass
        await _exchange(protocol, raw, incoming, outgoing)
        if client_ready and protocol._handshaken:
            break
    assert client_ready and protocol._handshaken
    assert client.selected_alpn_protocol() == "http/1.1"
    assert protocol._tls.get_client_ca_list() == []
    assert state.connections == {protocol}
    assert protocol._handshake_timer is None
    assert protocol._plain.get_protocol() is protocol._http
    return protocol, state, raw, incoming, outgoing, client


@pytest.mark.parametrize("version", [ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3])
@pytest.mark.parametrize("certificate", [False, True])
async def test_direct_memory_bio_handshake_http_scope_and_graceful_close(
    identity, version, certificate
):
    scopes = []

    async def app(scope, receive, send):
        scopes.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"private-response-marker"})

    protocol, state, raw, incoming, outgoing, client = await _handshake(
        identity, app, certificate=certificate, version=version
    )
    try:
        client.write(
            b"GET /connect HTTP/1.1\r\nHost: localhost\r\n"
            b"x-forwarded-client-cert: forged\r\nConnection: close\r\n\r\n"
        )
        result = b""
        for _ in range(50):
            await _exchange(protocol, raw, incoming, outgoing)
            with suppress(ssl.SSLWantReadError, ssl.SSLZeroReturnError):
                result += client.read(65536)
            if b"private-response-marker" in result:
                break
        assert b"200 OK" in result
        assert b"private-response-marker" in result
        assert scopes[0]["scheme"] == "https"
        peer = certificate_from_scope(scopes[0])
        assert (peer is not None) is certificate
        if peer is not None:
            assert "forged" not in peer.subject
        await _exchange(protocol, raw, incoming, outgoing)
        assert raw.closed
        assert not raw.aborted
    finally:
        protocol.connection_lost(None)
    assert not state.connections
    assert protocol._close_timer is None
    assert protocol._pump_handle is None


async def test_single_five_megabyte_asgi_write_is_delivered(identity):
    body = b"x" * (5 * 1024 * 1024)

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", str(len(body)).encode("ascii"))],
            }
        )
        await send({"type": "http.response.body", "body": body})

    protocol, state, raw, incoming, outgoing, client = await _handshake(identity, app)
    try:
        client.write(b"GET /large HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        result = bytearray()
        for _ in range(1000):
            await _exchange(protocol, raw, incoming, outgoing)
            with suppress(ssl.SSLWantReadError, ssl.SSLZeroReturnError):
                result.extend(client.read(65536))
            if len(result) > len(body) and result.endswith(body[-32:]):
                break
        head, separator, payload = bytes(result).partition(b"\r\n\r\n")
        assert b"200 OK" in head
        assert separator and payload == body
        assert not raw.aborted
    finally:
        protocol.connection_lost(None)
    assert not state.connections


async def test_ssl_write_retries_identical_buffer_before_ssl_read(identity):
    async def app(scope, receive, send):
        pass

    protocol, _, _, *_ = await _handshake(identity, app)
    actual_tls = protocol._tls
    operations = []
    buffers = []

    class RetryWrite:
        def __getattr__(self, name):
            return getattr(actual_tls, name)

        def bio_write(self, data):
            return len(data)  # Wake the mock's blocked write without a network record.

        def send(self, data):
            buffers.append(data)
            operations.append("send")
            if len(buffers) == 1:
                raise SSL.WantReadError()
            if len(buffers) == 2:
                raise SSL.WantWriteError()
            return actual_tls.send(data)

        def recv(self, size):
            operations.append("recv")
            return actual_tls.recv(size)

    protocol._tls = RetryWrite()
    try:
        protocol._plain.write(b"retry-exactly-this-buffer")
        await asyncio.sleep(0)
        assert operations == ["send"]
        protocol.data_received(b"mock TLS control record")
        assert operations[:4] == ["send", "send", "send", "recv"]
        assert len(buffers) == 3 and all(buffer is buffers[0] for buffer in buffers)
    finally:
        protocol.connection_lost(None)


async def test_ssl_read_want_write_completes_before_queued_application_write(identity):
    async def app(scope, receive, send):
        pass

    protocol, _, _, *_ = await _handshake(identity, app)
    actual_tls = protocol._tls
    operations = []

    class RetryRead:
        def __getattr__(self, name):
            return getattr(actual_tls, name)

        def recv(self, size):
            operations.append("recv")
            if len(operations) == 1:
                protocol._plain.write(b"queued-while-TLS-read-wants-write")
                raise SSL.WantWriteError()
            return actual_tls.recv(size)

        def send(self, data):
            operations.append("send")
            return actual_tls.send(data)

    protocol._tls = RetryRead()
    try:
        protocol._pump()
        await asyncio.sleep(0)
        assert operations[:3] == ["recv", "recv", "send"]
    finally:
        protocol.connection_lost(None)


async def test_flow_control_and_output_limit(identity):
    async def app(scope, receive, send):
        pass

    protocol, state, raw, *_ = await _handshake(identity, app)
    try:
        protocol.pause_writing()
        assert raw.read_paused
        assert protocol._http.flow.write_paused
        protocol._plain.write(b"queued")
        assert protocol._plain.get_write_buffer_size() == 6
        protocol.resume_writing()
        await asyncio.sleep(0)
        assert not raw.read_paused
        assert not protocol._http.flow.write_paused
        assert protocol._plain.get_write_buffer_size() == 0
        protocol._plain.pause_reading()
        assert raw.read_paused
        protocol._plain.resume_reading()
        assert not raw.read_paused
        protocol._plain.write(b"x" * (_MAX_OUTPUT + 1))
        assert raw.aborted
    finally:
        protocol.connection_lost(None)
    assert not state.connections


@pytest.mark.parametrize("action", ["shutdown", "timeout", "oversized_input", "invalid_tls"])
async def test_incomplete_handshake_is_bounded_and_tracked(identity, action):
    async def app(scope, receive, send):
        pytest.fail("HTTP must not run before TLS handshake")

    state = ServerState()
    protocol = certificate_http_protocol(identity.server_cert, identity.server_key)(
        Config(app, access_log=False, lifespan="off", ws="none"), state, {}
    )
    raw = MemoryTransport(protocol)
    protocol.connection_made(raw)
    assert state.connections == {protocol}
    try:
        if action == "shutdown":
            protocol.shutdown()
        elif action == "timeout":
            protocol._handshake_timer._run()
        elif action == "oversized_input":
            protocol.data_received(b"x" * (_MAX_INPUT + 1))
        else:
            protocol.data_received(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert raw.aborted
        assert protocol._handshake_timer is None
    finally:
        protocol.connection_lost(None)
    assert not state.connections


@pytest.mark.parametrize(
    ("purpose", "signature", "valid"),
    [
        (None, None, True),
        (ExtendedKeyUsageOID.CLIENT_AUTH, True, True),
        (ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE, True, True),
        (ExtendedKeyUsageOID.SERVER_AUTH, True, True),
        (ExtendedKeyUsageOID.CLIENT_AUTH, False, True),
        (ExtendedKeyUsageOID.CLIENT_AUTH, True, False),
    ],
)
def test_presented_certificate_is_cn_source_without_leaf_policy(purpose, signature, valid):
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Self-signed personal certificate")])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=1) if valid else now - timedelta(days=1))
    )
    if purpose is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage([purpose]), False)
    if signature is not None:
        builder = builder.add_extension(
            x509.KeyUsage(signature, False, False, False, True, False, False, False, False), False
        )
    parsed = _presented_identity(builder.sign(key, hashes.SHA256()))
    assert parsed is not None
    assert parsed.subject == "CN=Self-signed personal certificate"
    assert _presented_identity(None) is None


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize(
    "purpose", [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
)
async def test_memory_bio_preserves_cn_from_expired_or_wrong_purpose_certificates(
    identity, tmp_path, expired, purpose
):
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CN-only account")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=1))
        .add_extension(x509.ExtendedKeyUsage([purpose]), False)
        .sign(key, hashes.SHA256())
    )
    certificate_file, key_file = tmp_path / "presented.pem", tmp_path / "presented.key"
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    async def app(scope, receive, send):
        pass

    protocol, state, _, *_ = await _handshake(
        replace(identity, client_cert=certificate_file, client_key=key_file), app
    )
    try:
        presented = protocol._http.app.identity
        assert presented is not None
        assert presented.subject == "CN=CN-only account"
        assert presented.fingerprint == certificate.fingerprint(hashes.SHA256()).hex()
    finally:
        protocol.connection_lost(None)
    assert not state.connections
