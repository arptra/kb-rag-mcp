"""Personal access, automatic SSH reuse and separated Gradle registry/build processes."""

from __future__ import annotations

import asyncio
import base64
import http.server
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from test_harness_sandbox import CONFIG, FakeDocker, invoke
from test_harness_ssh import upload  # noqa: F401
from test_harness_tls import materials  # noqa: F401

from agent_harness import gradle_relay, sandbox
from agent_harness.contracts import Principal
from agent_harness.credentials import CredentialStore, PersonalUpload
from agent_harness.credentials_http import create_personal_credential_routes
from agent_harness.integrations_config import (
    IntegrationSettings,
    IntegrationStore,
    RepositoryConfig,
)

REPO = RepositoryConfig(id="service", name="Service", url="git@git.example:s.git",
                        allowed_owners=["alice"])
GRADLE = {"kind": "gradle", "repository_id": "service", "registry_url": "https://reg.test/maven",
          "name": "Registry", "username": "build", "password": "demo-password"}


async def test_credentials_scope_rotation_metadata_and_removal(tmp_path: Path):
    store = CredentialStore(tmp_path).personal
    settings = IntegrationStore(tmp_path)
    settings.save(IntegrationSettings(repositories=[REPO]))

    async def identify(request: Request) -> Principal:
        owner = request.headers["owner"]
        return Principal(owner, shared=owner == "shared")

    app = Starlette(routes=create_personal_credential_routes(
        store, settings, identify=identify, read_body=Request.body,
    ))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        url = "/api/credentials/personal"
        c.headers["owner"] = "shared"
        assert (await c.post(url, json=GRADLE)).status_code == 403
        c.headers["owner"] = "alice"
        for invalid in ["http://insecure", "https://user@x/r", "https://x/../escape",
                        "https://x/r?token=aa", "https://x/%2e%2e"]:
            result = await c.post(url, json={**GRADLE, "registry_url": invalid})
            assert result.status_code == 400 and GRADLE["password"] not in result.text
        result = await c.post(url, json=GRADLE)
        assert result.status_code == 201 and "demo-password" not in result.text
        key = result.json()["credential"]["id"]
        result = await c.post(url, json={**GRADLE, "password": "rotated-secret"})
        assert result.json()["credential"]["id"] == key
        result = await c.get(url)
        assert len(result.json()["credentials"]) == 1 and "rotated-secret" not in result.text
        assert b"rotated-secret" not in store.path.read_bytes()
        assert store.path.with_name("encryption.key").stat().st_mode & 0o777 == 0o600
        assert store.gradle_access("alice", REPO.id, "git@other.example:s.git") is None
        assert store.gradle_access("alice", REPO.id, REPO.url)[2].get_secret_value() == (
            "rotated-secret"
        )
        c.headers["owner"] = "bob"
        assert (await c.get(url)).json() == {"credentials": [], "repositories": []}
        assert (await c.post(url, json=GRADLE)).status_code == 403
        assert (await c.delete(url + "/" + key)).status_code == 404
        c.headers["owner"] = "alice"
        assert (await c.delete(url + "/" + key)).status_code == 200
        assert store.gradle_access("alice", REPO.id, REPO.url) is None
        assert store.path.stat().st_mode & 0o777 == 0o600


def test_concurrent_first_save_and_restore_after_missing_encryption_key(tmp_path):
    users = ["alice", "bob", "carol", "dave"]
    payload = PersonalUpload.validate_python(GRADLE)

    def save(owner):
        CredentialStore(tmp_path).personal.save(owner, REPO.url, payload)

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(save, users))
    store = CredentialStore(tmp_path).personal
    for user in users:
        assert store.gradle_access(user, REPO.id, REPO.url)[2].get_secret_value() == (
            GRADLE["password"]
        )
    key = store.path.with_name("encryption.key")
    backup = key.read_bytes()
    key.unlink()
    with pytest.raises(ValueError):
        save("alice")
    assert not key.exists() and len(store.list("alice")) == 1
    key.write_bytes(backup)
    assert store.gradle_access("bob", REPO.id, REPO.url)[2].get_secret_value() == GRADLE["password"]


async def test_ssh_personal_key_auto_reused_only_for_pinned_user_and_url(
    tmp_path, monkeypatch, upload,  # noqa: F811
):
    docker = FakeDocker(monkeypatch)
    store = CredentialStore(tmp_path)
    record = store.personal.save("alice", REPO.url, PersonalUpload.validate_python({
        "kind": "ssh", "repository_id": REPO.id, **upload.model_dump(exclude={"id"}),
    }))
    assert "PRIVATE KEY" not in json.dumps(store.personal.list("alice"))
    backend = sandbox.SandboxBackend(credentials=store)
    inputs = []
    original = backend._container

    async def capture(config, checkout, command, **kwargs):
        if data := kwargs.get("stdin_data"):
            inputs.append((command, data))
        return await original(config, checkout, command, **kwargs)

    monkeypatch.setattr(backend, "_container", capture)
    config = CONFIG.model_copy(update={"image": "profile:java"})
    await invoke(backend, repository=REPO, config=config)
    await invoke(backend, "run_in_sandbox", repository=REPO, config=config,
                 command=["python", "-V"])
    assert len(inputs) == 1 and sandbox.CLONE_SCRIPT in inputs[0][0]
    assert inputs[0][1] == store.personal.clone_input("alice", REPO.id, REPO.url)
    clone = next(c["args"] for c in docker.calls if sandbox.CLONE_SCRIPT in c["args"])
    keeper = next(c["args"] for c in docker.calls if "--detach" in c["args"])
    assert "harness-sandbox:local" in clone and "harness-sandbox:local" in keeper
    assert any("profile:java" in c["args"] for c in docker.calls)
    assert store.personal.clone_input("alice", REPO.id, "git@evil.test:r") is None
    assert store.personal.clone_input("bob", REPO.id, REPO.url) is None
    await backend.close_run("run-1")
    with pytest.raises(sandbox.SandboxError, match="SSH"):
        await invoke(backend, repository=REPO, principal=Principal("alice", shared=True))
    assert store.personal.delete("alice", record["id"])


async def test_registry_and_build_are_isolated_and_cleaned_up(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path)
    store.personal.save("alice", REPO.url, PersonalUpload.validate_python(GRADLE))
    backend = sandbox.SandboxBackend(credentials=store)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    cleaned = asyncio.Event()

    async def capture(config, checkout, command, **kwargs):
        calls.append((command, {"image": config.image, **kwargs}))
        if "registry" in command:
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        return sandbox._Result(0, "success", "")

    monkeypatch.setattr(backend, "_container", capture)
    checkout = sandbox._Checkout("volume", ("test",))
    for owner in ["alice", "bob"]:
        result = await backend._repository_command(
            ["gradle", "test"], REPO, CONFIG.model_copy(update={"image": "profile:java"}),
            checkout, Event(), sandbox._Run(owner), 100,
        )
        assert result.stdout == "success"
    assert cleaned.is_set() and len(calls) == 3
    command, args = next(c for c in calls if "registry" in c[0])
    assert args["image"] == "harness-sandbox:local"
    assert args["clone"] and json.loads(args["stdin_data"])["password"] == GRADLE["password"]
    assert GRADLE["password"] not in repr(command)
    command, args = next(c for c in calls if "build" in c[0])
    assert args["image"] == "profile:java"
    assert "stdin_data" not in args and not args.get("clone")
    assert GRADLE["password"] not in repr(command)
    assert calls[-1][0] == ["gradle", "test"]


def test_actual_gradle_resolves_transitive_artifact_through_authenticated_registry(
    tmp_path, materials,  # noqa: F811
):
    gradle = shutil.which("gradle")
    if not gradle:
        pytest.skip("Install Gradle for the end-to-end resolver test")
    auth = "Basic " + base64.b64encode(b"build:demo-password").decode()
    requests = []
    root = tmp_path / "registry"
    for name in ["app", "lib"]:
        folder = root / "allowed" / "org" / "demo" / name / "1"
        folder.mkdir(parents=True)
        dependencies = ""
        if name == "app":
            dependencies = ("<dependencies><dependency><groupId>org.demo</groupId>"
                            "<artifactId>lib</artifactId><version>1</version>"
                            "</dependency></dependencies>")
        (folder / f"{name}-1.pom").write_text(
            "<project><modelVersion>4.0.0</modelVersion><groupId>org.demo</groupId>"
            f"<artifactId>{name}</artifactId><version>1</version>{dependencies}</project>"
        )
        (folder / f"{name}-1.jar").write_bytes(b"mock jar content")

    class Registry(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=root, **kwargs)

        def send_head(self):
            requests.append(self.path)
            if self.headers.get("Authorization") != auth:
                self.send_error(401)
                return None
            return super().send_head()

        def log_message(self, *args):
            pass

    for key in ["ca_certificate", "server_certificate", "server_key"]:
        (tmp_path / key).write_text(materials[key])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "server_certificate", tmp_path / "server_key")
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Registry) as upstream:
        upstream.socket = ctx.wrap_socket(upstream.socket, server_side=True)
        worker = threading.Thread(target=upstream.serve_forever, daemon=True)
        worker.start()
        # Short AF_UNIX path also works on macOS.
        with tempfile.TemporaryDirectory(dir="/tmp") as sockets:
            sock = sockets + "/registry"
            program = [sys.executable, "-I", "-S", "-c", Path(gradle_relay.__file__).read_text()]
            service = subprocess.Popen(
                [*program, "registry", sock, "1000000", "100"], stdin=subprocess.PIPE,
                env={**os.environ, "SSL_CERT_FILE": str(tmp_path / "ca_certificate")},
            )
            try:
                assert service.stdin is not None
                service.stdin.write(json.dumps({
                    "url": f"https://localhost:{upstream.server_port}/allowed", "username": "build",
                    "password": "demo-password",
                }).encode())
                service.stdin.close()
                (tmp_path / "settings.gradle").write_text(
                    "dependencyResolutionManagement { repositoriesMode.set("
                    "RepositoriesMode.FAIL_ON_PROJECT_REPOS) }; rootProject.name='harness-test'"
                )
                (tmp_path / "build.gradle").write_text("""
configurations { verify }
dependencies { verify 'org.demo:app:1' }
tasks.register('probe') {
    doLast { println configurations.verify.files.collect { it.name }.sort() }
}
""")
                result = subprocess.run(
                    [*program, "build", sock, "1000000", "100", gradle, "-q", "probe"],
                    env={**os.environ, "GRADLE_USER_HOME": str(tmp_path / "gradle-home")},
                    cwd=tmp_path, capture_output=True, text=True, timeout=100,
                )
                assert result.returncode == 0, result.stderr
                assert "[app-1.jar, lib-1.jar]" in result.stdout
                assert requests and all(p.startswith("/allowed/org/demo/") for p in requests)
                assert "demo-password" not in result.stdout + result.stderr
            finally:
                service.terminate()
                service.wait(timeout=5)
                upstream.shutdown()
                worker.join(timeout=2)


@pytest.mark.parametrize("path", [
    "//evil.test/p.jar", "/a/x/1/../key", "/a/x/1/%2f", "/a/x/1/x-1.jar?url=evil",
    "/admin", "/a/b/1/token.txt", "/a/b/1/unrelated.jar",
])
def test_only_maven_paths_reach_authenticated_origin(path):
    assert not gradle_relay.maven_path(path)
