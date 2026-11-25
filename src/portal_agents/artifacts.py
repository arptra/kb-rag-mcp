"""Transfer configured source snapshots and Maven artifacts to an offline turn workspace."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import signal
import subprocess
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from langchain_core.tools import BaseTool, StructuredTool, ToolException

from portal_agents.workspace import OfflineWorkspace

_LIMIT = 32 * 1024 * 1024
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+.-]{0,127}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,190}")


def _url(source: dict[str, str], scheme: str) -> str:
    url = source["url"].rstrip("/")
    endpoint = urlsplit(url)
    if (endpoint.scheme != scheme or not endpoint.hostname or endpoint.password is not None
            or endpoint.query or endpoint.fragment or endpoint.port == 0 or "\\" in url
            or any(ord(c) <= 32 or c.isspace() for c in url)):
        raise ToolException("Invalid artifact source configuration.")
    if scheme == "https":
        if endpoint.username is not None:
            raise ToolException("Use separate credentials for Maven.")
        return url
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", endpoint.path) or ".." in endpoint.path.split("/"):
        raise ToolException("Invalid Git repository path.")
    username = endpoint.username or source.get("username") or "git"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", username):
        raise ToolException("Invalid Git SSH username.")
    netloc = endpoint.netloc.rsplit("@", 1)[-1]
    if not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[a-fA-F0-9:]+\])(?::\d{1,5})?", netloc):
        raise ToolException("Invalid Git SSH host.")
    return f"ssh://{username}@{netloc}{endpoint.path}"


def validate_artifact_source(source: dict[str, str]) -> None:
    """Verify the configured endpoint and local credentials without contacting the host."""
    is_git = source["kind"] == "git"
    try:
        endpoint = urlsplit(_url(source, "ssh" if is_git else "https"))
    except (ValueError, ToolException):
        raise ValueError("Проверьте адрес репозитория, порт и путь.") from None
    if not is_git:
        return
    if not source.get("secret") or not source.get("known_hosts"):
        raise ValueError("Для Git загрузите ключ и проверенную запись known_hosts.")
    raw = source["secret"].strip().encode() + b"\n"
    try:
        loader = (serialization.load_ssh_private_key if raw.startswith(b"-----BEGIN OPENSSH ")
                  else serialization.load_pem_private_key)
        key = loader(raw, password=None)
        source["secret"] = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()
    except (ValueError, TypeError, AttributeError, NotImplementedError, UnsupportedAlgorithm):
        raise ValueError("Нужен приватный SSH-ключ OpenSSH или PEM без пароля.") from None

    # OpenSSH itself matches hashes, patterns, port-qualified hosts and CA markers,
    # and verifies each returned public-key blob when asked for its fingerprint.
    host = endpoint.hostname
    target = host if endpoint.port in (None, 22) else f"[{host}]:{endpoint.port}"
    try:
        with NamedTemporaryFile("w") as hosts:
            hosts.write(source["known_hosts"].strip() + "\n")
            hosts.flush()
            result = subprocess.run(
                ["ssh-keygen", "-F", target, "-l", "-f", hosts.name],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath, "LC_ALL": "C"}, timeout=5, check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Для проверки known_hosts на сервере требуется ssh-keygen.") from None
    if result.returncode or not any(
        line.startswith(b"# Host ") and not line.rstrip().endswith(b" REVOKED")
        for line in result.stdout.splitlines()
    ):
        raise ValueError("Добавьте в known_hosts действующий ключ для указанного хоста и порта.")


async def _git(directory: Path, env: dict[str, str], *args: str) -> bytes:
    # A shallow bare clone keeps one pack on disk and never checks files out on the host.
    # Limit output and size of each temporary pack/index. Kill SSH along with Git on timeout.
    git = shutil.which("git")
    if git is None:
        raise ToolException("Git is not installed on the portal host.")
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", 'ulimit -f 65536 || exit 1; exec "$@"', "git-limited",
        git, "-c", "core.hooksPath=/dev/null", "-c", "fetch.unpackLimit=1",
        "-c", "transfer.fsckObjects=true", *args,
        cwd=directory, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdout is not None
    data = bytearray()
    try:
        while block := await proc.stdout.read(64 * 1024):
            data.extend(block)
            if len(data) > _LIMIT:
                raise ToolException("The source snapshot exceeds 32 MiB.")
        if await proc.wait():
            raise ToolException("Git read failed. Check access, host key, ref and repository size.")
        return bytes(data)
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.communicate()


async def _snapshot(source: dict[str, str], ref: str) -> bytes:
    url = _url(source, "ssh")
    if ref and (not _BRANCH.fullmatch(ref) or ".." in ref or "//" in ref):
        raise ToolException("Use a branch or tag name.")
    if not source.get("secret") or not source.get("known_hosts"):
        raise ToolException("Configure the Git SSH key and verified known_hosts.")
    with TemporaryDirectory(prefix="portal-git-") as temp:
        directory = Path(temp)
        for field, filename in (("secret", "identity"), ("known_hosts", "known_hosts")):
            with os.fdopen(os.open(directory / filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                                   0o600), "w") as stream:
                stream.write(source[field].strip() + "\n")
        ssh = [
            "ssh", "-F", "/dev/null", "-i", str(directory / "identity"),
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "UserKnownHostsFile=" + str(directory / "known_hosts"), "-o", "UpdateHostKeys=no",
            "-o", "PreferredAuthentications=publickey", "-o", "ClearAllForwardings=yes",
            "-o", "ConnectTimeout=10",
        ]
        env = {
            "PATH": os.defpath, "HOME": temp, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1", "GIT_ALLOW_PROTOCOL": "ssh",
            "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_VARIANT": "ssh",
            "GIT_SSH_COMMAND": shlex.join(ssh),
        }
        branch = ["--branch", ref] if ref else []
        async with asyncio.timeout(60):
            await _git(directory, env, "clone", "--bare", "--single-branch", "--depth=1",
                       "--no-tags", "--template=", *branch, "--", url, "repository.git")
            return await _git(directory, env, "--git-dir=repository.git", "archive",
                              "--format=tar", "--prefix=source/", "HEAD")


async def _maven(
    source: dict[str, str], group: str, artifact: str, version: str,
    extension: str, classifier: str,
) -> tuple[str, bytes]:
    if (not _COMPONENT.fullmatch(group)
            or not all(_COMPONENT.fullmatch(p) for p in group.split("."))
            or not all(_COMPONENT.fullmatch(p) for p in (artifact, version, extension))
            or (classifier and not _COMPONENT.fullmatch(classifier))):
        raise ToolException("Use Maven coordinates without paths.")
    suffix = "-" + classifier if classifier else ""
    filename = f"{artifact}-{version}{suffix}.{extension}"
    if len(filename) > 230:
        raise ToolException("Artifact filename is too long.")
    url = f"{_url(source, 'https')}/{group.replace('.', '/')}/{artifact}/{version}/{filename}"
    username = source.get("username", "")
    password = source.get("secret", "")
    auth = httpx.BasicAuth(username, password) if username or password else None
    data = bytearray()
    async with (
        asyncio.timeout(60),
        httpx.AsyncClient(auth=auth, follow_redirects=False) as client,
        client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response,
    ):
        if response.status_code != 200:
            raise ToolException(f"Maven returned HTTP {response.status_code}.")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ToolException("Unexpected transfer encoding from the Maven repository.")
        async for block in response.aiter_raw(64 * 1024):
            if len(data) + len(block) > _LIMIT:
                raise ToolException("The artifact exceeds 32 MiB.")
            data.extend(block)
    return filename, bytes(data)


def build_artifact_tools(
    sources: dict[str, dict[str, str]], workspace: OfflineWorkspace,
    authorize: Callable[[], Awaitable[bool]],
) -> list[BaseTool]:
    if not sources:
        return []

    async def check() -> None:
        if not await authorize():
            raise PermissionError("Artifact source access has been revoked.")

    def lookup(source_id: str, kind: str) -> dict[str, str]:
        source = sources.get(source_id)
        if not source or source["kind"] != kind:
            raise ToolException("Choose an available source of the correct kind.")
        return source

    async def list_artifact_sources() -> list[dict[str, str]]:
        """List configured Git SSH repositories and Maven repositories by source ID."""
        await check()
        return [{"id": key, "kind": item["kind"]} for key, item in sources.items()]

    async def fetch_git_snapshot(source_id: str, ref: str = "") -> str:
        """Read a configured Git repo branch/tag (default HEAD) into the offline workspace.

        Returns a tar file path, inspect or extract its source/ directory with run_in_workspace.
        """
        await check()
        try:
            data = await _snapshot(lookup(source_id, "git"), ref)
            await check()
            return await workspace.put_file("source.tar", data)
        except PermissionError:
            raise
        except (ValueError, OSError, TimeoutError):
            raise ToolException("Could not transfer Git snapshot into the workspace.") from None

    async def fetch_maven_artifact(
        source_id: str, group: str, artifact: str, version: str, extension: str = "jar",
        classifier: str = "",
    ) -> str:
        """Fetch a dependency file by Gradle/Maven coordinates into the turn's local repository.

        The Gradle build can resolve staged files. For transitive dependencies fetch their
        metadata (extension "pom" or "module") and binaries as required. Returns the file path.
        """
        await check()
        try:
            name, data = await _maven(lookup(source_id, "maven"), group, artifact, version,
                                      extension, classifier)
            await check()
            directories = (*group.split("."), artifact, version)
            return await workspace.put_dependency(source_id, directories, name, data)
        except PermissionError:
            raise
        except (ValueError, OSError, httpx.HTTPError, TimeoutError):
            raise ToolException("Could not transfer Maven artifact into the workspace.") from None

    tools = [StructuredTool.from_function(coroutine=list_artifact_sources, handle_tool_error=True)]
    if any(item["kind"] == "git" for item in sources.values()):
        tools.append(StructuredTool.from_function(
            coroutine=fetch_git_snapshot, handle_tool_error=True,
        ))
    if any(item["kind"] == "maven" for item in sources.values()):
        tools.append(StructuredTool.from_function(
            coroutine=fetch_maven_artifact, handle_tool_error=True,
        ))
    return tools
