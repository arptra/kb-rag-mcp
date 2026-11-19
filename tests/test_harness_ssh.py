"""SSH inputs, secret storage and the clone-only stdin boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from test_harness_sandbox import FakeDocker, invoke

from agent_harness import sandbox
from agent_harness.credentials import CredentialStore, SSHKeyUpload
from agent_harness.credentials_http import create_credential_routes
from agent_harness.integrations_config import RepositoryConfig, normalize_repository_url
from agent_harness.sandbox import SandboxBackend, SandboxError


@pytest.fixture
def upload() -> SSHKeyUpload:
    private = Ed25519PrivateKey.generate()
    host = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    return SSHKeyUpload(
        id="service-key",
        name="Service deployment key",
        private_key=private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode(),
        known_hosts="git.example " + host + "\n",
    )


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("git@git.example:team/service.git", "git@git.example:team/service.git"),
        (
            "ssh://git@git.example:2222/team/service.git",
            "ssh://git@git.example:2222/team/service.git",
        ),
        ("ssh://git@[2001:db8::1]/team/service.git", "ssh://git@[2001:db8::1]/team/service.git"),
        ("https://git.example/team/service.git", "https://git.example/team/service.git"),
    ],
)
def test_repository_ssh_urls_are_validated_and_normalized(supplied: str, expected: str) -> None:
    assert normalize_repository_url(supplied) == expected
    assert RepositoryConfig(id="repo", name="Service", url=supplied).url == expected


@pytest.mark.parametrize(
    "url",
    [
        "ssh://-oProxyCommand=bad@git.example/repo",
        "ssh://git@-option/repo",
        "ssh://git:password@git.example/repo",
        "ssh://git@git.example/repo?command=bad",
        "ssh://git@git.example/repo#fragment",
        "ssh://git@git.example/repo;touch-file",
        "ssh://git@git.example/../secret",
        "ssh://git@git.example/%2e%2e/secret",
        "ssh://git@git.example/-option",
        "ssh://git@git.example/repo\nother",
        "ssh://git@git.example:70000/repo",
        "ext::bad-helper",
        "file:///tmp/repo",
        "/tmp/repo",
        "git@git.example:repo'$(command)'",
    ],
)
def test_repository_unsafe_transports_and_ssh_options_are_rejected(url: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        RepositoryConfig(id="repo", name="Service", url=url)


def test_ssh_credentials_cannot_be_mixed_with_https_tokens() -> None:
    with pytest.raises(ValidationError):
        RepositoryConfig(id="r", name="R", url="https://git.example/r", ssh_key_id="key")
    with pytest.raises(ValidationError):
        RepositoryConfig(id="r", name="R", url="git@git.example:r", credential_env="TOKEN")


def test_store_files_private_and_metadata_never_contains_secret(
    tmp_path: Path, upload: SSHKeyUpload
) -> None:
    store = CredentialStore(tmp_path)
    metadata = store.save(upload)
    assert metadata["fingerprint"].startswith("SHA256:")
    assert metadata["key_type"] == "ssh-ed25519" and metadata["host_count"] == 1
    assert store.list() == [metadata]
    assert "PRIVATE KEY" not in json.dumps(store.list())
    assert upload.known_hosts not in json.dumps(store.list())
    assert os.stat(store.directory).st_mode & 0o777 == 0o700
    assert os.stat(store.directory / "service-key.json").st_mode & 0o777 == 0o600
    loaded = store.load(upload.id)
    assert "PRIVATE KEY" not in repr(loaded)
    assert json.loads(loaded.clone_input())["known_hosts"] == upload.known_hosts
    store.delete(upload.id)
    assert store.list() == []
    with pytest.raises(KeyError):
        store.load(upload.id)


def test_key_rotation_and_validation(tmp_path: Path, upload: SSHKeyUpload) -> None:
    store = CredentialStore(tmp_path)
    original = store.save(upload)
    store.save(upload.model_copy(update={"name": "Rotated"}))
    assert store.list()[0]["name"] == "Rotated"
    assert store.list()[0]["fingerprint"] == original["fingerprint"]
    encrypted = (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"test-only-passphrase"),
        )
        .decode()
    )
    for private, hosts in [
        (encrypted, upload.known_hosts),
        ("not a private key", upload.known_hosts),
        (upload.private_key, "# only comments"),
        (upload.private_key, "git.example ssh-ed25519 invalid"),
        (upload.private_key, "@cert-authority " + upload.known_hosts),
    ]:
        with pytest.raises(ValueError) as raised:
            store.save(upload.model_copy(update={"private_key": private, "known_hosts": hosts}))
        assert private not in str(raised.value)
        assert "test-only-passphrase" not in str(raised.value)
    assert store.list()[0]["name"] == "Rotated"
    with pytest.raises(ValueError):
        store.load("../escape")


async def test_credentials_http_authorization_safe_errors_and_in_use_delete(
    tmp_path: Path, upload: SSHKeyUpload
) -> None:
    store = CredentialStore(tmp_path)
    referenced: list[str] = []

    async def authorize(request: Request) -> bool:
        return request.headers.get("authorization") == "Bearer manager"

    async def read_body(request: Request) -> bytes:
        return await request.body()

    async def error(_: Request, exc: Any) -> JSONResponse:
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    app = Starlette(
        routes=create_credential_routes(
            store, authorize=authorize, read_body=read_body, in_use=lambda _: referenced
        ),
        exception_handlers={400: error, 403: error, 404: error, 409: error, 415: error},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/credentials/ssh")).status_code == 403
        assert (
            await client.post("/api/credentials/ssh", json=upload.model_dump())
        ).status_code == 403
        client.headers["authorization"] = "Bearer manager"
        result = await client.post("/api/credentials/ssh", json=upload.model_dump())
        assert result.status_code == 201 and "PRIVATE KEY" not in result.text
        result = await client.get("/api/credentials/ssh")
        assert len(result.json()["credentials"]) == 1 and "PRIVATE KEY" not in result.text
        invalid = await client.post(
            "/api/credentials/ssh", json={**upload.model_dump(), "unexpected": "sensitive-value"}
        )
        assert invalid.status_code == 400
        assert "sensitive-value" not in invalid.text and "PRIVATE KEY" not in invalid.text
        referenced.append("service")
        assert (await client.delete("/api/credentials/ssh/service-key")).status_code == 409
        referenced.clear()
        assert (await client.delete("/api/credentials/ssh/service-key")).status_code == 200
        assert (await client.delete("/api/credentials/ssh/service-key")).status_code == 404


async def test_ssh_key_only_reaches_clone_stdin_never_argv_env_or_workspace(
    tmp_path: Path, upload: SSHKeyUpload, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(monkeypatch)
    store = CredentialStore(tmp_path)
    store.save(upload)
    backend = SandboxBackend(credentials=store)
    supplied: list[dict[str, Any]] = []
    original = backend._exec

    async def record(args: list[str], **kwargs: Any) -> Any:
        supplied.append({"args": args, **kwargs})
        return await original(args, **kwargs)

    monkeypatch.setattr(backend, "_exec", record)
    repository = RepositoryConfig(
        id="repo", name="Repo", url="git@git.example:service.git", ssh_key_id=upload.id
    )
    await invoke(backend, repository=repository)
    await invoke(backend, "run_in_sandbox", repository=repository, command=["python", "-V"])
    with_input = [entry for entry in supplied if entry.get("stdin_data")]
    assert len(with_input) == 1 and sandbox.CLONE_SCRIPT in with_input[0]["args"]
    assert "--interactive" in with_input[0]["args"]
    secret = json.loads(with_input[0]["stdin_data"])
    assert "PRIVATE KEY" in secret["private_key"] and secret["known_hosts"] == upload.known_hosts
    for entry in docker.calls:
        assert "PRIVATE KEY" not in json.dumps(entry)
        assert "SSH_AUTH_SOCK" not in entry["env"]
        assert "type=bind" not in " ".join(entry["args"])
    assert 'os.mkdir("/tmp/harness-ssh", 0o700)' in sandbox.CLONE_SCRIPT
    assert "StrictHostKeyChecking=yes" in sandbox.CLONE_SCRIPT
    assert "IdentityAgent=none" in sandbox.CLONE_SCRIPT
    assert "-F /dev/null" in sandbox.CLONE_SCRIPT and "ForwardAgent=no" in sandbox.CLONE_SCRIPT
    assert "BatchMode=yes" in sandbox.CLONE_SCRIPT and "IdentitiesOnly=yes" in sandbox.CLONE_SCRIPT
    await backend.close_run("run-1")


async def test_ssh_missing_key_fails_before_creating_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker(monkeypatch)
    repository = RepositoryConfig(id="repo", name="Repo", url="git@git.example:service.git")
    with pytest.raises(SandboxError, match="SSH"):
        await invoke(SandboxBackend(), repository=repository)
    assert docker.calls == []
