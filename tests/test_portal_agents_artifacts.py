import base64
import io
import os
import shlex
import subprocess
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from langchain_core.tools import ToolException

from portal_agents import artifacts, workspace
from portal_agents.artifacts import build_artifact_tools


def make_tools(sources, grant=None):
    room = AsyncMock()
    room.put_file.return_value = "/tmp/test-path/file"
    room.put_dependency.return_value = "/tmp/staged/file"
    tools = build_artifact_tools(sources, room, grant or AsyncMock(return_value=True))
    return {tool.name: tool for tool in tools}, room


def test_git_prohibits_passwords_nonssh_and_shell_in_paths():
    for url in ("file:///tmp/local", "ssh://git:p@corp/repo", "ssh://host/a;rm",
                "ssh://host/../../etc", "ssh://host/a%20b", "ssh://-o.example/repo"):
        with pytest.raises(ToolException):
            artifacts._url({"url": url}, "ssh")


async def test_clone_uses_real_read_only_git_protocol_with_pinned_ssh_args(tmp_path, monkeypatch):
    repo = tmp_path / "seed"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], check=True, cwd=repo, capture_output=True)

    git("init", "-b", "main")
    (repo / "service.txt").write_text("release-22")
    git("add", "service.txt")
    git("-c", "user.name=Test", "-c", "user.email=t@example.test", "commit", "-m", "init")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "args.txt"
    # Stand in for the pinned SSH transport; real git-upload-pack serves the local fixture.
    ssh = bindir / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        "test -z \"$SSH_AUTH_SOCK$GIT_CONFIG_COUNT\" || exit 75\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(log))}\n"
        "test -r \"$HOME/identity\" && test -r \"$HOME/known_hosts\" || exit 77\n"
        f"exec git-upload-pack {shlex.quote(str(repo))}\n"
    )
    ssh.chmod(0o700)
    monkeypatch.setattr(os, "defpath", str(bindir) + ":" + os.defpath)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/ambient/agent")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "10")
    sources = {"project": {"kind": "git", "url": "ssh://host/team/repo.git", "username": "git",
                            "secret": "test-key", "known_hosts": "test-host-key"}}
    tools, room = make_tools(sources)
    assert await tools["list_artifact_sources"].ainvoke({}) == [{"id": "project", "kind": "git"}]
    path = await tools["fetch_git_snapshot"].ainvoke({"source_id": "project", "ref": "main"})
    assert path == room.put_file.return_value
    name, payload = room.put_file.await_args.args
    assert name == "source.tar"
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        assert tar.getnames() == ["source", "source/service.txt"]
        assert tar.extractfile("source/service.txt").read() == b"release-22"
    args = log.read_text().splitlines()
    assert {"IdentityAgent=none", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
            "GlobalKnownHostsFile=/dev/null", "git@host"} <= set(args)
    key_file = Path(args[args.index("-i") + 1])
    assert not key_file.exists()
    assert "test-key" not in str(args)
    assert "fetch_maven_artifact" not in tools


class Body(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"jar-data"


@pytest.mark.parametrize("status", [200, 302])
async def test_maven_stream_is_scoped_to_coordinates_with_no_redirect(monkeypatch, status):
    seen = []

    async def handle(request):
        seen.append(request)
        return httpx.Response(status, stream=Body(), headers={"Location": "https://wrong.example"})

    client = httpx.AsyncClient
    monkeypatch.setattr(artifacts.httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(handle)))
    tools, room = make_tools({"internal": {
        "kind": "maven", "url": "https://packages.example/gradle", "username": "test-user",
        "secret": "test-password",
    }})
    invoke = tools["fetch_maven_artifact"].ainvoke
    coordinates = {"source_id": "internal", "group": "com.example", "artifact": "widget",
                   "version": "1.0.2", "classifier": "sources"}
    assert "without paths" in await invoke({**coordinates, "artifact": "../private"})
    assert not seen
    answer = await invoke(coordinates)
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == (
        "https://packages.example/gradle/com/example/widget/1.0.2/widget-1.0.2-sources.jar")
    assert request.headers["Authorization"] == (
        "Basic " + base64.b64encode(b"test-user:test-password").decode())
    if status == 200:
        assert answer == room.put_dependency.return_value
        room.put_dependency.assert_awaited_once_with(
            "internal", ("com", "example", "widget", "1.0.2"), "widget-1.0.2-sources.jar",
            b"jar-data",
        )
    else:
        assert answer == "Maven returned HTTP 302."
        room.put_dependency.assert_not_awaited()


async def test_revocation_before_artifact_delivery(monkeypatch):
    download = AsyncMock(return_value=("file.jar", b"data"))
    monkeypatch.setattr(artifacts, "_maven", download)
    rights = AsyncMock(side_effect=[True, False])
    tools, room = make_tools({"pkg": {"kind": "maven"}}, rights)
    with pytest.raises(PermissionError):
        await tools["fetch_maven_artifact"].ainvoke({
            "source_id": "pkg", "group": "g", "artifact": "a", "version": "v"})
    room.put_dependency.assert_not_awaited()


async def test_transfer_into_workspace_never_mounts_credentials_or_host_paths(monkeypatch):
    docker = AsyncMock(return_value=(0, ""))
    monkeypatch.setattr(workspace, "_docker", docker)
    room = workspace.OfflineWorkspace("test-image", AsyncMock(return_value=True))
    for bad in ("../a", "a/b", "x;id"):
        with pytest.raises(ValueError):
            await room.put_file(bad, b"data")
    docker.assert_not_awaited()
    saved = await room.put_file("source.tar", b"archive")
    assert saved.startswith("/tmp/portal-artifact-") and saved.endswith("/source.tar")
    create, copy = docker.await_args_list
    assert "--network=none" in create.args and "--read-only" in create.args
    assert not any(arg.startswith("--mount") for arg in create.args)
    assert copy.args[-2:] == (saved.rsplit("/", 1)[0], "source.tar")
    assert copy.kwargs["script"] == b"archive"


async def test_gradle_resolves_staged_files_using_its_native_offline_resolver():
    image = os.environ.get("PORTAL_AGENTS_TEST_GRADLE_IMAGE")
    if not image:
        pytest.skip("Set PORTAL_AGENTS_TEST_GRADLE_IMAGE to a cached image containing Gradle")
    room = workspace.OfflineWorkspace(image, AsyncMock(return_value=True))
    invoke = room.tool().ainvoke
    try:
        jar = io.BytesIO()
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("example.txt", "binary")
        for name in ("client", "helper"):
            dependency = ("<dependencies><dependency><groupId>org.example</groupId>"
                          "<artifactId>helper</artifactId><version>1</version></dependency>"
                          "</dependencies>") if name == "client" else ""
            pom = (f"<project><modelVersion>4.0.0</modelVersion><groupId>org.example</groupId>"
                   f"<artifactId>{name}</artifactId><version>1</version>{dependency}</project>")
            parts = ("org", "example", name, "1")
            await room.put_dependency("corp", parts, f"{name}-1.pom", pom.encode())
            path = await room.put_dependency("corp", parts, f"{name}-1.jar", jar.getvalue())
            assert path.endswith(f"/corp/org/example/{name}/1/{name}-1.jar")
        setup = """mkdir -p /tmp/project && cd /tmp/project
cat > settings.gradle <<'SETTINGS'
dependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS) }
rootProject.name = 'staging-test'
SETTINGS
cat > build.gradle <<'BUILD'
configurations { libraries }
dependencies { libraries 'org.example:client:1' }
tasks.register('verifyFiles') {
    doLast {
        def names = configurations.libraries.files.collect { it.name }.sort()
        assert names == ['client-1.jar', 'helper-1.jar']
        println 'Verified staged direct and transitive dependencies'
    }
}
BUILD"""
        assert await invoke({"script": setup}) == "exit_code=0\n"
        result = await room.gradle_tool().ainvoke({"project_dir": "/tmp/project",
                                                 "tasks": ["verifyFiles"]})
        assert result.startswith("exit_code=0\n"), result
        assert "Verified staged direct and transitive dependencies" in result
        # A project process cannot trick later staging into overwriting another directory.
        assert await invoke({"script": "ln -s /tmp /tmp/portal-gradle-repositories/bad"}) == (
            "exit_code=0\n"
        )
        with pytest.raises(ValueError, match="Ошибка записи"):
            await room.put_dependency("bad", ("org",), "trap.jar", b"x")
    finally:
        await room.close()

def test_verify_hashed_nonstandard_port_host_and_authority_pins(tmp_path):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                          serialization.PublicFormat.OpenSSH).decode()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    source = {"kind": "git", "url": "ssh://repo.test:2222/team/app.git", "secret": private}
    hosts = tmp_path / "known_hosts"
    hosts.write_text("[repo.test]:2222 " + public + "\n")
    subprocess.run(["ssh-keygen", "-q", "-H", "-f", str(hosts)], check=True, capture_output=True)
    for pin in (hosts.read_text(), "@cert-authority [repo.test]:2222 " + public):
        assert artifacts.validate_artifact_source({**source, "known_hosts": pin}) is None
    for pin in ("repo.test " + public, "@revoked [repo.test]:2222 " + public):
        with pytest.raises(ValueError, match="known_hosts"):
            artifacts.validate_artifact_source({**source, "known_hosts": pin})
    locked = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.BestAvailableEncryption(b"test")).decode()
    with pytest.raises(ValueError, match="без пароля"):
        artifacts.validate_artifact_source({**source, "secret": locked,
                                            "known_hosts": hosts.read_text()})
