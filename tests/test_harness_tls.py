"""Private TLS uploads and a real in-memory OpenSSL mutual-TLS handshake."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from agent_harness.model_connection import ModelConnection, ModelRouter
from agent_harness.tls_credentials import TLSCredentialStore, TLSCredentialUpload
from agent_harness.tls_http import create_tls_routes


def pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def materials() -> dict[str, str]:
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Harness test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    result = {"ca_certificate": ca.public_bytes(serialization.Encoding.PEM).decode()}
    for role, usage in [
        ("client", ExtendedKeyUsageOID.CLIENT_AUTH),
        ("server", ExtendedKeyUsageOID.SERVER_AUTH),
    ]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, role)]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=10))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        result[role + "_certificate"] = cert.public_bytes(serialization.Encoding.PEM).decode()
        result[role + "_key"] = pem(key)
    return result


def upload(materials: dict[str, str]) -> TLSCredentialUpload:
    return TLSCredentialUpload(
        id="gateway",
        name="Corporate gateway",
        ca_certificate=materials["ca_certificate"],
        client_certificate=materials["client_certificate"],
        client_key=materials["client_key"],
    )


def test_tls_store_private_files_public_metadata_and_verified_context(
    tmp_path: Path, materials: dict[str, str]
) -> None:
    store = TLSCredentialStore(tmp_path)
    metadata = store.save(upload(materials))
    assert metadata["has_ca_certificate"] and metadata["has_client_certificate"]
    assert metadata["expires_at"]
    assert store.list() == [metadata] and store.get("gateway") == metadata
    assert "PRIVATE KEY" not in json.dumps(store.list())
    assert "CERTIFICATE" not in json.dumps(store.list())
    assert os.stat(store.directory).st_mode & 0o777 == 0o700
    assert os.stat(store.directory / "gateway.json").st_mode & 0o777 == 0o600
    context = store.ssl_context("gateway")
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert len(list(store.directory.iterdir())) == 1  # Temporary PEM files removed.
    with store.docker_client_directory("gateway") as directory:
        assert {path.name for path in directory.iterdir()} == {"ca.pem", "cert.pem", "key.pem"}
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())
        assert (directory / "key.pem").read_text() == materials["client_key"]
    assert not directory.exists()
    with pytest.raises(ValueError):
        store.get("../arbitrary-path")
    store.delete("gateway")
    assert store.list() == []


def test_tls_invalid_ca_mismatch_missing_pair_and_encrypted_key(
    tmp_path: Path, materials: dict[str, str]
) -> None:
    store = TLSCredentialStore(tmp_path)
    valid = upload(materials)
    encrypted = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"test-passphrase"),
        )
        .decode()
    )
    for changes in [
        {"ca_certificate": "invalid"},
        {"ca_certificate": materials["client_certificate"]},
        {"client_key": materials["server_key"]},
        {"client_key": ""},
        {"client_certificate": ""},
        {"client_key": encrypted},
        {"ca_certificate": "", "client_certificate": "", "client_key": ""},
    ]:
        with pytest.raises(ValueError) as error:
            store.save(valid.model_copy(update=changes))
        assert "PRIVATE KEY" not in str(error.value)
        assert "test-passphrase" not in str(error.value)
    assert store.list() == []


def handshake(
    client: ssl.SSLContext, server: ssl.SSLContext
) -> tuple[ssl.SSLObject, ssl.SSLObject]:
    incoming_client, outgoing_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    incoming_server, outgoing_server = ssl.MemoryBIO(), ssl.MemoryBIO()
    client_socket = client.wrap_bio(incoming_client, outgoing_client, server_hostname="localhost")
    server_socket = server.wrap_bio(incoming_server, outgoing_server, server_side=True)
    done = [False, False]
    for _ in range(30):
        for index, sock in enumerate((client_socket, server_socket)):
            if not done[index]:
                try:
                    sock.do_handshake()
                    done[index] = True
                except ssl.SSLWantReadError:
                    pass
        incoming_server.write(outgoing_client.read())
        incoming_client.write(outgoing_server.read())
        if all(done):
            return client_socket, server_socket
    raise AssertionError("Handshake did not finish")


def test_real_openssl_mutual_tls_handshake_and_missing_identity_rejected(
    tmp_path: Path, materials: dict[str, str]
) -> None:
    store = TLSCredentialStore(tmp_path)
    store.save(upload(materials))
    cert, key = tmp_path / "server.pem", tmp_path / "server-key.pem"
    cert.write_text(materials["server_certificate"])
    key.write_text(materials["server_key"])
    key.chmod(0o600)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    server.load_verify_locations(cadata=materials["ca_certificate"])
    server.verify_mode = ssl.CERT_REQUIRED
    client, peer = handshake(store.ssl_context("gateway"), server)
    assert client.getpeercert() and peer.getpeercert()
    untrusted = ssl.create_default_context()
    with pytest.raises(ssl.SSLError):
        handshake(untrusted, server)
    no_identity = ssl.create_default_context(cadata=materials["ca_certificate"])
    with pytest.raises(ssl.SSLError):
        handshake(no_identity, server)


async def test_tls_http_manager_only_safe_metadata_and_in_use_delete(
    tmp_path: Path, materials: dict[str, str]
) -> None:
    store = TLSCredentialStore(tmp_path)
    referenced: list[str] = []

    async def authorize(request: Request) -> bool:
        return request.headers.get("authorization") == "Bearer manager"

    async def read_body(request: Request) -> bytes:
        return await request.body()

    async def error(_: Request, exc: Any) -> JSONResponse:
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    app = Starlette(
        routes=create_tls_routes(
            store, authorize=authorize, read_body=read_body, in_use=lambda _: referenced
        ),
        exception_handlers={400: error, 403: error, 409: error},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/credentials/tls")).status_code == 403
        client.headers["authorization"] = "Bearer manager"
        response = await client.post("/api/credentials/tls", json=upload(materials).model_dump())
        assert response.status_code == 201 and "PRIVATE KEY" not in response.text
        response = await client.get("/api/credentials/tls")
        assert response.json()["credentials"][0]["has_client_certificate"]
        assert "PRIVATE KEY" not in response.text and "CERTIFICATE" not in response.text
        response = await client.post(
            "/api/credentials/tls", json={**upload(materials).model_dump(), "extra": True}
        )
        assert response.status_code == 400 and "PRIVATE KEY" not in response.text
        referenced.append("model")
        assert (await client.delete("/api/credentials/tls/gateway")).status_code == 409
        referenced.clear()
        assert (await client.delete("/api/credentials/tls/gateway")).status_code == 200


def test_uploaded_root_first_client_chain_is_reordered_for_openssl(
    tmp_path: Path, materials: dict[str, str]
) -> None:
    store = TLSCredentialStore(tmp_path)
    value = upload(materials).model_copy(
        update={"client_certificate": materials["ca_certificate"] + materials["client_certificate"]}
    )
    store.save(value)
    persisted = json.loads((store.directory / "gateway.json").read_text())
    assert persisted["client_certificate"].startswith(materials["client_certificate"])
    assert store.ssl_context("gateway").check_hostname


async def test_keyless_model_probe_and_completion_perform_real_mutual_tls_requests(
    tmp_path: Path, materials: dict[str, str], monkeypatch: Any
) -> None:
    store = TLSCredentialStore(tmp_path)
    store.save(upload(materials))
    cert, key = tmp_path / "server.pem", tmp_path / "server-key.pem"
    cert.write_text(materials["server_certificate"])
    key.write_text(materials["server_key"])
    key.chmod(0o600)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    server_context.load_verify_locations(cadata=materials["ca_certificate"])
    server_context.verify_mode = ssl.CERT_REQUIRED
    requests: list[tuple[bytes, dict[str, Any]]] = []

    async def gateway(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            assert writer.get_extra_info("peercert")
            assert b"authorization:" not in headers.lower()
            assert headers.startswith(b"POST /v1/chat/completions HTTP/1.1")
            content_length = next(
                int(line.split(b":", 1)[1])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            body = json.loads(await reader.readexactly(content_length))
            assert "model" not in body
            requests.append((headers, body))
            response = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(response)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + response
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    server = await asyncio.start_server(gateway, "127.0.0.1", 0, ssl=server_context)
    async with server:
        port = server.sockets[0].getsockname()[1]
        router = ModelRouter(tmp_path, None)
        settings = ModelConnection(
            provider="openai",
            base_url=f"https://localhost:{port}/v1/chat/completions",
            tls_bundle_id="gateway",
            timeout_seconds=5,
        )
        router.store.save(settings)
        assert (await router.probe())["available"] is True
        assert (
            await router.decide(
                messages=[{"role": "user", "content": "Explain the system"}],
                tools=[],
                cancel=Event(),
                max_input_chars=10000,
            )
        )["content"] == "OK"
        assert len(requests) == 2
        assert requests[0][1]["messages"] == [{"role": "user", "content": "Reply with OK."}]
        assert requests[1][1]["messages"][0]["content"] == "Explain the system"

        # An uploaded CA alone cannot authenticate to the corporate mTLS gateway.
        store.save(
            TLSCredentialUpload(
                id="ca-only", name="Trust only", ca_certificate=materials["ca_certificate"]
            )
        )
        result = await router.probe(settings.model_copy(update={"tls_bundle_id": "ca-only"}))
        assert result["available"] is False
        assert len(requests) == 2


@pytest.mark.skipif(shutil.which("docker") is None, reason="Requires the Docker CLI")
async def test_docker_cli_inventory_over_mutual_tls(
    tmp_path: Path, materials: dict[str, str]
) -> None:
    from agent_harness.integrations_config import DockerEngineConfig
    from agent_harness.sandbox import SandboxBackend

    store = TLSCredentialStore(tmp_path)
    store.save(upload(materials))
    cert, key = tmp_path / "server.pem", tmp_path / "server-key.pem"
    cert.write_text(materials["server_certificate"])
    key.write_text(materials["server_key"])
    key.chmod(0o600)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    server_context.load_verify_locations(cadata=materials["ca_certificate"])
    server_context.verify_mode = ssl.CERT_REQUIRED
    paths = []
    image_id = "sha256:" + "a" * 64

    async def engine(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            assert writer.get_extra_info("peercert")
            headers = await reader.readuntil(b"\r\n\r\n")
            method, path, _ = headers.split(b"\r\n", 1)[0].decode().split()
            paths.append(path)
            if path.endswith("/_ping"):
                data = b"OK" if method != "HEAD" else b""
            elif path.split("?")[0].endswith("/images/json"):
                data = json.dumps(
                    [{"Id": image_id, "RepoTags": ["qa/build:1"], "Size": 1024}]
                ).encode()
            elif path.endswith("/version"):
                data = b'{"Version":"24.0.7","ApiVersion":"1.43","MinAPIVersion":"1.24"}'
            else:
                raise AssertionError(path)
            writer.write(
                b"HTTP/1.1 200 OK\r\nAPI-Version: 1.43\r\nContent-Type: application/json\r\n"
                b"Connection: close\r\nContent-Length: "
                + str(len(data)).encode()
                + b"\r\n\r\n"
                + data
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(engine, "127.0.0.1", 0, ssl=server_context)
    async with server:
        port = server.sockets[0].getsockname()[1]
        inventory = await SandboxBackend(tls_credentials=store).list_images(
            DockerEngineConfig(mode="tls", host="localhost", port=port, tls_id="gateway")
        )
        assert inventory["images"][0]["reference"] == "qa/build:1"
        assert inventory["images"][0]["id"] == image_id
        assert any("/images/json" in path for path in paths)
    assert len(list(store.directory.iterdir())) == 1
