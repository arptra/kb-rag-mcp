"""Optional Docker-only repository tools. No host checkout or execution fallback.

An administrator builds the image separately. Each run owns disposable named
tmpfs volumes; only the clone container receives network access and its credential.
A networkless keeper holds each tmpfs mount until the run is closed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Any, Literal
from uuid import uuid4

from agent_harness.contracts import (
    ANALYSIS_FILENAME,
    MAX_ANALYSIS_SOURCE_BYTES,
    Evidence,
    Principal,
)
from agent_harness.credentials import CredentialStore
from agent_harness.integrations_config import DockerEngineConfig, RepositoryConfig, SandboxConfig
from agent_harness.tls_credentials import TLSCredentialStore

_HELPER_IMAGE = "harness-sandbox:local"


class SandboxError(RuntimeError):
    """A bounded, user-safe sandbox failure without subprocess output or secrets."""


PROBE_TOOLS_SCRIPT = """
import json, shutil, sys
names = ("git", "ssh", "sleep", "java", "gradle", "mvn")
tools = {name: bool(shutil.which(name)) for name in names}
tools["python"] = "%d.%d" % sys.version_info[:2]
print(json.dumps(tools))
sys.exit(0 if sys.version_info >= (3, 9) else 1)
"""


# These programs are supplied by the server, never loaded from the repository.
# Isolated Python (-I -S) prevents the checkout from supplying imported modules.
CLONE_SCRIPT = r"""
import json, os, shutil, subprocess, sys
url, ref = sys.argv[1:]
token = os.environ.pop("HARNESS_GIT_TOKEN", "")
env = {
    "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false",
    "GIT_LFS_SKIP_SMUDGE": "1", "GCM_INTERACTIVE": "Never",
}
if token:
    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
               GIT_CONFIG_VALUE_0="Authorization: Bearer " + token)
if not url.startswith("https://"):
    credentials = json.loads(sys.stdin.buffer.read(65537))
    os.mkdir("/tmp/harness-ssh", 0o700)
    for name, content in (("key", credentials["private_key"]),
                          ("known_hosts", credentials["known_hosts"])):
        fd = os.open("/tmp/harness-ssh/" + name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
    env["GIT_SSH_COMMAND"] = (
        "ssh -F /dev/null -i /tmp/harness-ssh/key "
        "-o UserKnownHostsFile=/tmp/harness-ssh/known_hosts "
        "-o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes "
        "-o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none "
        "-o ForwardAgent=no -o ClearAllForwardings=yes -o PasswordAuthentication=no "
        "-o KbdInteractiveAuthentication=no -o PreferredAuthentications=publickey "
        "-o ConnectTimeout=15 -o LogLevel=ERROR"
    )
    env["GIT_SSH_VARIANT"] = "ssh"
git = ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
       "-c", "protocol.ssh.allow=always",
       "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
       "-c", "core.hooksPath=/dev/null", "-c", "http.followRedirects=false"]
def execute(args, cwd=None):
    result = subprocess.run(git + args, cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode:
        print("Git operation failed", file=sys.stderr)
        sys.exit(1)
    return result.stdout.decode("utf-8", "replace").strip()
execute(["init", "--template=", "repo"])
execute(["remote", "add", "origin", url], "repo")
execute(["fetch", "--depth", "1", "--no-tags", "--no-recurse-submodules",
         "origin", ref], "repo")
execute(["checkout", "--detach", "--force", "FETCH_HEAD"], "repo")
print(json.dumps({"commit": execute(["rev-parse", "HEAD"], "repo")}))
if not url.startswith("https://"):
    shutil.rmtree("/tmp/harness-ssh")
"""

WRITE_ANALYSIS_SCRIPT = r"""
import os, secrets, sys
workspace, name = sys.argv[1:]
if not name or name in (".", "..") or name != os.path.basename(name):
    sys.exit(2)
directory_flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
root = os.open(workspace, directory_flags)
try:
    os.mkdir("analysis", mode=0o700, dir_fd=root)
except FileExistsError:
    pass
directory = os.open("analysis", directory_flags, dir_fd=root)
temporary = ".write-" + secrets.token_hex(16)
fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY,
             0o600, dir_fd=directory)
try:
    with os.fdopen(fd, "wb") as output:
        output.write(sys.stdin.buffer.read())
    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
finally:
    try:
        os.unlink(temporary, dir_fd=directory)
    except FileNotFoundError:
        pass
"""

READ_SCRIPT = r"""
import json, os, stat, sys
from pathlib import Path, PurePosixPath
expected_root, path, first, last, budget = sys.argv[1:]
first, last, budget = int(first), int(last), int(budget)
root = Path(expected_root)
if root.is_symlink() or str(root) != os.getcwd():
    sys.exit(2)
parts = PurePosixPath(path).parts
if not parts or path.startswith("/") or "\\" in path or any(p in ("..", ".git") for p in parts):
    sys.exit(2)
target = root
try:
    for part in parts:
        target = target / part
        if stat.S_ISLNK(target.lstat().st_mode):
            sys.exit(2)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(target, flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
            sys.exit(2)
        content = stream.read(1048577)
    if b"\x00" in content or len(content) > 1048576:
        sys.exit(2)
    lines = content.decode("utf-8", "replace").splitlines()
    text = "\n".join(f"{i+1}: {line}" for i, line in enumerate(lines) if first <= i+1 <= last)
    print(json.dumps({"path": path, "start_line": first, "text": text[:budget],
                      "truncated": len(text) > budget}, ensure_ascii=False))
except (OSError, ValueError):
    sys.exit(2)
"""

SEARCH_SCRIPT = r"""
import json, os, stat, sys
expected_root, query, budget = sys.argv[1], sys.argv[2], int(sys.argv[3])
if os.path.islink(expected_root) or expected_root != os.getcwd():
    sys.exit(2)
query = query.casefold()
matches, scanned, read_bytes, used, limited = [], 0, 0, 0, False
for base, dirs, files in os.walk(".", followlinks=False):
    dirs[:] = sorted(d for d in dirs if d != ".git" and not os.path.islink(os.path.join(base, d)))
    for filename in sorted(files):
        path = os.path.join(base, filename)
        if filename == ".git" or os.path.islink(path):
            continue
        scanned += 1
        if scanned > 5000 or read_bytes >= 16777216:
            limited = True
            break
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
                    continue
                content = stream.read(1048577)
            read_bytes += len(content)
            if b"\x00" in content or len(content) > 1048576:
                continue
            relative = path.removeprefix("./")
            for number, line in enumerate(content.decode("utf-8", "replace").splitlines(), 1):
                if query in line.casefold():
                    text = line[:min(1200, max(0, budget-used))]
                    used += len(text) + len(relative) + 40
                    matches.append({"path": relative, "line": number, "text": text})
                    if len(matches) >= 30 or used >= budget:
                        limited = True
                        break
        except OSError:
            continue
        if limited:
            break
    if limited:
        break
print(json.dumps({"matches": matches, "scanned_files": scanned, "limited": limited},
                 ensure_ascii=False))
"""


@dataclass
class _Result:
    code: int
    stdout: str
    stderr: str
    truncated: bool = False


@dataclass
class _Checkout:
    volume: str
    fingerprint: tuple[str, ...]
    engine: DockerEngineConfig = field(default_factory=DockerEngineConfig)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    commit: str = ""
    dirty: bool = False
    created: bool = False
    keeper: str = ""


@dataclass
class _Run:
    owner_id: str
    shared: bool = False
    cancel: Event = field(default_factory=Event)
    repositories: dict[str, _Checkout] = field(default_factory=dict)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False


class SandboxBackend:
    def __init__(
        self,
        credentials: CredentialStore | None = None,
        *,
        tls_credentials: TLSCredentialStore | None = None,
    ) -> None:
        self.credentials = credentials
        self.tls_credentials = tls_credentials
        self._runs: dict[str, _Run] = {}
        self._capacity = asyncio.Condition()
        self._active = 0

    @staticmethod
    def _check(cancel: Event, run: _Run, deadline: float) -> None:
        if cancel.is_set() or run.cancel.is_set():
            raise asyncio.CancelledError("Sandbox operation cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Sandbox operation exceeded its time limit")

    @asynccontextmanager
    async def _slot(
        self, maximum: int, cancel: Event, run: _Run, deadline: float
    ) -> AsyncIterator[None]:
        async with self._capacity:
            while self._active >= maximum:
                self._check(cancel, run, deadline)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._capacity.wait(), 0.05)
            self._check(cancel, run, deadline)
            self._active += 1
        try:
            yield
        finally:
            async with self._capacity:
                self._active -= 1
                self._capacity.notify_all()

    @staticmethod
    def _environment(token: str = "") -> dict[str, str]:
        # These configure the host Docker client, not the container environment.
        names = (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "XDG_RUNTIME_DIR",
        )
        env = {name: os.environ[name] for name in names if name in os.environ}
        if token:
            env["HARNESS_GIT_TOKEN"] = token
        return env

    @staticmethod
    def _docker() -> str:
        executable = shutil.which("docker")
        if not executable:
            raise SandboxError("Установите Docker CLI на сервер Harness.")
        return executable

    @contextmanager
    def _client_environment(
        self, engine: DockerEngineConfig, token: str = ""
    ) -> Iterator[dict[str, str]]:
        env = self._environment(token)
        with ExitStack() as stack:
            if engine.mode != "default":
                for key in (
                    "DOCKER_HOST",
                    "DOCKER_CONTEXT",
                    "DOCKER_TLS_VERIFY",
                    "DOCKER_CERT_PATH",
                ):
                    env.pop(key, None)
            if engine.mode == "context":
                env["DOCKER_CONTEXT"] = engine.context
            elif engine.mode == "tls":
                if self.tls_credentials is None:
                    raise SandboxError("Хранилище сертификатов Docker недоступно.")
                try:
                    directory = stack.enter_context(
                        self.tls_credentials.docker_client_directory(engine.tls_id)
                    )
                except (ValueError, KeyError, OSError):
                    raise SandboxError(
                        "Проверьте CA, сертификат и ключ клиента Docker API."
                    ) from None
                host = f"[{engine.host}]" if ":" in engine.host else engine.host
                env.update(
                    DOCKER_HOST=f"tcp://{host}:{engine.port}",
                    DOCKER_TLS_VERIFY="1",
                    DOCKER_CERT_PATH=str(directory),
                )
            yield env

    async def _cleanup_command(self, args: list[str], engine: DockerEngineConfig) -> None:
        """Cleanup has its own deadline and never inherits a cancelled run signal."""
        result = await self._exec(
            args,
            engine=engine,
            cancel=Event(),
            run=_Run("cleanup"),
            deadline=time.monotonic() + 10,
            output_limit=1500,
        )
        if not result.code:
            return
        if args[0] == "rm" and not result.truncated:
            name = re.escape(args[-1])
            if re.fullmatch(
                rf"(?:Error(?: response from daemon)?: )?No such container: {name}",
                result.stderr.strip(),
                re.IGNORECASE,
            ):
                return
        raise SandboxError("Docker cleanup failed; retry closing this run")

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

    async def _exec(
        self,
        args: list[str],
        *,
        cancel: Event,
        run: _Run,
        engine: DockerEngineConfig,
        deadline: float,
        output_limit: int,
        container: str = "",
        token: str = "",
        stdin_data: bytes | None = None,
    ) -> _Result:
        self._check(cancel, run, deadline)
        with self._client_environment(engine, token) as env:
            try:
                proc = await asyncio.create_subprocess_exec(
                    self._docker(),
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE
                    if stdin_data is not None
                    else asyncio.subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
            except OSError as exc:
                raise SandboxError("Сервер Harness не смог запустить Docker CLI.") from exc
            return await self._communicate(
                proc,
                engine=engine,
                cancel=cancel,
                run=run,
                deadline=deadline,
                output_limit=output_limit,
                container=container,
                stdin_data=stdin_data,
            )

    async def _communicate(
        self,
        proc: asyncio.subprocess.Process,
        *,
        engine: DockerEngineConfig,
        cancel: Event,
        run: _Run,
        deadline: float,
        output_limit: int,
        container: str,
        stdin_data: bytes | None,
    ) -> _Result:
        buffers = [bytearray(), bytearray()]
        truncated = False

        async def drain(stream: asyncio.StreamReader | None, target: bytearray) -> None:
            nonlocal truncated
            assert stream is not None
            while chunk := await stream.read(8192):
                remaining = max(0, output_limit * 4 - sum(map(len, buffers)))
                target.extend(chunk[:remaining])
                truncated |= len(chunk) > remaining

        readers = [
            asyncio.create_task(drain(proc.stdout, buffers[0])),
            asyncio.create_task(drain(proc.stderr, buffers[1])),
        ]
        waiter = asyncio.create_task(proc.wait())

        async def supply_input() -> None:
            if stdin_data is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(stdin_data)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()

        writer = asyncio.create_task(supply_input())
        try:
            while not waiter.done():
                self._check(cancel, run, deadline)
                await asyncio.wait({waiter}, timeout=0.05)
            self._check(cancel, run, deadline)
            await asyncio.wait_for(asyncio.gather(*readers), max(0, deadline - time.monotonic()))
            await asyncio.wait_for(writer, max(0, deadline - time.monotonic()))
        except BaseException:
            self._kill(proc)
            await waiter
            for reader in readers:
                reader.cancel()
            writer.cancel()
            await asyncio.gather(*readers, writer, return_exceptions=True)
            if container:
                await self._cleanup_command(["rm", "-f", "-v", container], engine)
            raise
        out, err = (bytes(buf).decode("utf-8", "replace") for buf in buffers)
        truncated |= len(out) + len(err) > output_limit
        return _Result(
            proc.returncode or 0,
            out[:output_limit],
            err[: max(0, output_limit - len(out))],
            truncated,
        )

    @asynccontextmanager
    async def probe_slot(
        self,
        config: SandboxConfig,
        cancel: Event,
        deadline: float,
    ) -> AsyncIterator[None]:
        async with self._slot(config.max_parallel, cancel, _Run("probe"), deadline):
            yield

    async def engine_command(
        self,
        config: SandboxConfig,
        args: list[str],
        *,
        cancel: Event,
        deadline: float,
        output_limit: int = 1000,
        container: str = "",
    ) -> _Result:
        return await self._exec(
            args,
            engine=config.engine,
            cancel=cancel,
            run=_Run("probe"),
            deadline=deadline,
            output_limit=output_limit,
            container=container,
        )

    async def remove_container(self, config: SandboxConfig, name: str) -> None:
        await self._cleanup_command(["rm", "-f", "-v", name], config.engine)

    async def list_images(self, engine: DockerEngineConfig) -> dict[str, Any]:
        config = SandboxConfig(engine=engine)
        cancel, deadline = Event(), time.monotonic() + 15
        async with self.probe_slot(config, cancel, deadline):
            result = await self.engine_command(
                config,
                [
                    "image",
                    "ls",
                    "--no-trunc",
                    "--format",
                    "{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}",
                ],
                cancel=cancel,
                deadline=deadline,
                output_limit=512 * 1024,
            )
        if result.code:
            raise SandboxError("Docker не вернул список образов. Проверьте подключение к Engine.")
        images: dict[str, dict[str, str]] = {}
        truncated = result.truncated
        rows = result.stdout.splitlines()
        if truncated and not result.stdout.endswith("\n"):
            rows = rows[:-1]
        for row in rows:
            columns = row.split("\t")
            if len(columns) != 4:
                continue
            repository, tag, image_id, size = columns
            reference = f"{repository}:{tag}" if "<none>" not in (repository, tag) else image_id
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,250}", reference):
                continue
            if len(images) >= 1000 and reference not in images:
                truncated = True
                break
            images[reference] = {"reference": reference, "id": image_id, "size": size}
        return {"images": [images[key] for key in sorted(images)], "truncated": truncated}

    async def _probe_image(
        self,
        config: SandboxConfig,
        *,
        cancel: Event,
        deadline: float,
    ) -> dict[str, Any]:
        inspected = await self.engine_command(
            config,
            ["image", "inspect", "--format", "{{.Id}}", config.image],
            cancel=cancel,
            deadline=deadline,
        )
        if inspected.code:
            raise SandboxError(f"Образ {config.image} отсутствует на выбранном Docker Engine.")
        # A readiness check needs no persistent volume.
        checkout = _Checkout("", (), config.engine)
        name, args = self._command(
            config, checkout, ["python", "-I", "-S", "-c", PROBE_TOOLS_SCRIPT], clone=False
        )
        mount = args.index("--mount")
        args[mount : mount + 2] = ["--tmpfs", "/workspace:rw,nosuid,nodev,size=1m"]
        args[args.index("--workdir") + 1] = "/workspace"
        output = await self.engine_command(
            config,
            args,
            cancel=cancel,
            deadline=deadline,
            container=name,
        )
        if output.code:
            raise SandboxError(f"Контейнеру {config.image} требуется рабочий python 3.9+.")
        capabilities = self._json(output)
        python = capabilities.get("python")
        optional = ("git", "ssh", "sleep", "java", "gradle", "mvn")
        if (
            not isinstance(python, str)
            or not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}", python)
            or any(type(capabilities.get(tool)) is not bool for tool in optional)
        ):
            raise SandboxError(f"Образ {config.image} вернул некорректный результат проверки.")
        return {tool: capabilities[tool] for tool in ("python", *optional)}

    async def probe(self, config: SandboxConfig) -> dict[str, Any]:
        result = {"enabled": config.enabled, "available": False, "image": config.image}
        cancel, deadline = Event(), time.monotonic() + 30
        try:
            async with self.probe_slot(config, cancel, deadline):
                version = await self.engine_command(
                    config,
                    ["version", "--format", "{{.Server.Version}}"],
                    cancel=cancel,
                    deadline=deadline,
                )
                if version.code:
                    raise SandboxError("Проверьте подключение к Docker Engine.")
                helper = config.model_copy(update={"image": _HELPER_IMAGE})
                tools = await self._probe_image(helper, cancel=cancel, deadline=deadline)
                missing = [tool for tool in ("git", "ssh", "sleep") if not tools[tool]]
                if missing:
                    raise SandboxError(
                        f"Образу {_HELPER_IMAGE} требуются {', '.join(missing)}."
                    )
                if config.image != _HELPER_IMAGE:
                    tools = await self._probe_image(config, cancel=cancel, deadline=deadline)
        except SandboxError as exc:
            return {**result, "reason": str(exc)}
        except (TimeoutError, OSError):
            return {**result, "reason": "Docker не ответил вовремя или недоступен."}
        installed = ["Python " + tools["python"]]
        labels = {"git": "Git", "java": "Java", "gradle": "Gradle", "mvn": "Maven"}
        installed.extend(label for key, label in labels.items() if tools[key])
        reason = "Контейнер проверен. Доступны " + ", ".join(installed) + "."
        if tools["java"] and not tools["gradle"]:
            reason += " Gradle отсутствует. Установите Gradle при сборке образа."
        if not config.enabled:
            reason += " Сейчас выполнение выключено."
        return {**result, "available": True, "capabilities": tools, "reason": reason}

    @staticmethod
    def _validate(name: str, args: dict[str, Any], repository: RepositoryConfig) -> None:
        expected = {
            "clone_repository": {"repository_id"},
            "search_repository": {"repository_id", "query"},
            "read_repository": {"repository_id", "path", "start_line", "end_line"},
            "write_analysis_script": {"repository_id", "filename", "source"},
            "run_in_sandbox": {"repository_id", "command"},
        }
        if name not in expected or set(args) - expected[name]:
            raise ValueError("Unsupported repository tool or arguments")
        if args.get("repository_id") != repository.id:
            raise ValueError("Repository ID does not match the selected resource")
        if name == "search_repository":
            query = args.get("query")
            if (
                not isinstance(query, str)
                or not query.strip()
                or len(query) > 1000
                or "\0" in query
            ):
                raise ValueError(
                    "Search requires a nonempty literal query of at most 1000 characters"
                )
        if name == "read_repository":
            path = args.get("path")
            if (
                not isinstance(path, str)
                or not path
                or len(path) > 1000
                or "\0" in path
                or "\\" in path
                or PurePosixPath(path).is_absolute()
                or any(part in {"..", ".git"} for part in PurePosixPath(path).parts)
            ):
                raise ValueError("A relative repository file path without traversal is required")
            first, last = args.get("start_line", 1), args.get("end_line", 160)
            if (
                type(first) is not int
                or type(last) is not int
                or not 1 <= first <= last <= 100000
                or last - first >= 400
            ):
                raise ValueError("Read at most 400 lines using positive line numbers")
        if name == "write_analysis_script":
            filename, source = args.get("filename"), args.get("source")
            if not isinstance(filename, str) or not re.fullmatch(ANALYSIS_FILENAME, filename):
                raise ValueError("Choose a simple .py or .java filename")
            if (
                not isinstance(source, str) or not source.strip() or "\0" in source
                or len(source.encode("utf-8")) > MAX_ANALYSIS_SOURCE_BYTES
            ):
                raise ValueError("Provide nonempty UTF-8 source of at most 24 KiB")
        if name == "run_in_sandbox":
            command = args.get("command")
            if (
                not isinstance(command, list)
                or not 1 <= len(command) <= 64
                or any(
                    not isinstance(arg, str) or "\0" in arg or len(arg) > 4096 for arg in command
                )
                or not command[0]
                or command[0].startswith("-")
                or sum(map(len, command)) > 20000
            ):
                raise ValueError("Command must be a bounded, nonempty argument list")

    @staticmethod
    def _command(
        config: SandboxConfig,
        checkout: _Checkout,
        command: list[str],
        *,
        clone: bool,
        token: str = "",
        stdin: bool = False,
    ) -> tuple[str, list[str]]:
        name = "harness-" + uuid4().hex
        args = [
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            name,
            "--log-driver",
            "none",
            "--label",
            "agent-harness=true",
            "--network",
            "bridge" if clone else "none",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--cpus",
            str(config.cpus),
            "--memory",
            f"{config.memory_mb}m",
            "--memory-swap",
            f"{config.memory_mb}m",
            "--pids-limit",
            str(config.pids_limit),
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m",
            "--mount",
            f"type=volume,source={checkout.volume},target=/workspace",
            "--workdir",
            "/workspace" if clone else "/workspace/repo",
            "--env",
            "HOME=/tmp",
        ]
        if clone and token:
            args += ["--env", "HARNESS_GIT_TOKEN"]
        if stdin:
            args += ["--interactive"]
        return name, [*args, "--entrypoint", command[0], config.image, *command[1:]]

    async def _container(
        self,
        config: SandboxConfig,
        checkout: _Checkout,
        command: list[str],
        *,
        cancel: Event,
        run: _Run,
        deadline: float,
        clone: bool = False,
        token: str = "",
        stdin_data: bytes | None = None,
    ) -> _Result:
        name, args = self._command(
            config, checkout, command, clone=clone, token=token, stdin=stdin_data is not None
        )
        result = await self._exec(
            args,
            engine=checkout.engine,
            cancel=cancel,
            run=run,
            deadline=deadline,
            output_limit=config.max_output_chars * 2,
            container=name,
            token=token,
            stdin_data=stdin_data,
        )
        if result.code and clone:
            raise SandboxError(
                "Repository clone failed; check the ref, access, SSH key/known_hosts "
                "and local sandbox image"
            )
        return result

    @staticmethod
    def _json(result: _Result) -> dict[str, Any]:
        if result.code or result.truncated:
            raise SandboxError("Repository tool failed or returned an oversized result")
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError("Invalid result")
            return value
        except (ValueError, TypeError) as exc:
            raise SandboxError("Sandbox returned an invalid tool result") from exc

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        *,
        repository: RepositoryConfig,
        config: SandboxConfig,
        principal: Principal,
        run_id: str,
        cancel: Event,
    ) -> list[Evidence]:
        if not config.enabled:
            raise SandboxError("Repository sandbox is disabled")
        if not repository.allows(principal.owner_id):
            raise PermissionError("Repository is unavailable to this user")
        self._validate(name, args, repository)
        run = self._runs.setdefault(run_id, _Run(principal.owner_id, shared=principal.shared))
        if (run.owner_id, run.shared) != (principal.owner_id, principal.shared):
            raise PermissionError("Sandbox run belongs to another user")
        if run.closing:
            raise SandboxError("Sandbox run is already closing")
        task = asyncio.current_task()
        assert task is not None
        run.tasks.add(task)
        run.idle.clear()
        deadline = time.monotonic() + config.timeout_seconds
        fingerprint = (
            repository.url,
            repository.ref,
            repository.credential_env,
            repository.ssh_key_id,
            config.image,
            config.engine.model_dump_json(),
        )
        checkout = run.repositories.setdefault(
            repository.id, _Checkout("harness-" + uuid4().hex, fingerprint, config.engine)
        )
        abort = False
        try:
            if checkout.fingerprint != fingerprint:
                raise SandboxError("Repository configuration changed; start a new run")
            async with self._slot(config.max_parallel, cancel, run, deadline), checkout.lock:
                self._check(cancel, run, deadline)
                if not checkout.commit:
                    await self._clone(repository, config, checkout, cancel, run, deadline)
                return await self._tool(
                    name, args, repository, config, checkout, cancel, run, deadline
                )
        except (asyncio.CancelledError, TimeoutError):
            run.cancel.set()
            run.closing = abort = True
            raise
        finally:
            run.tasks.discard(task)
            if not run.tasks:
                run.idle.set()
            if abort:
                await asyncio.shield(self.close_run(run_id))

    async def _clone(
        self,
        repository: RepositoryConfig,
        config: SandboxConfig,
        checkout: _Checkout,
        cancel: Event,
        run: _Run,
        deadline: float,
    ) -> None:
        token = os.environ.get(repository.credential_env, "") if repository.credential_env else ""
        if repository.credential_env and not token:
            raise SandboxError("Repository credential is not configured on the server")
        if "\r" in token or "\n" in token:
            raise SandboxError("Repository credential has an invalid format")
        stdin_data = None
        if not repository.url.startswith("https://"):
            if self.credentials is None:
                raise SandboxError("Добавьте SSH-ключ и known_hosts для этого репозитория.")
            try:
                if not run.shared:
                    stdin_data = self.credentials.personal.clone_input(
                        run.owner_id, repository.id, repository.url
                    )
                if stdin_data is None and repository.ssh_key_id:
                    stdin_data = self.credentials.load(repository.ssh_key_id).clone_input()
                if stdin_data is None:
                    raise KeyError
            except (KeyError, ValueError):
                raise SandboxError(
                    "SSH-ключ недоступен. Проверьте настройки репозитория."
                ) from None
        if checkout.keeper or checkout.created:
            await self._remove_volume(checkout)
        # Mark before spawning: an interrupted Docker client may have created the volume.
        checkout.created = True
        try:
            created = await self._exec(
                [
                    "volume",
                    "create",
                    "--label",
                    "agent-harness=true",
                    "--driver",
                    "local",
                    "--opt",
                    "type=tmpfs",
                    "--opt",
                    "device=tmpfs",
                    "--opt",
                    f"o=size={config.workspace_mb}m,uid=65532,gid=65532,mode=0700,nosuid,nodev",
                    checkout.volume,
                ],
                engine=checkout.engine,
                cancel=cancel,
                run=run,
                deadline=deadline,
                output_limit=1000,
            )
            if created.code:
                raise SandboxError("Docker could not create a sandbox volume")
            # Local-driver tmpfs is unmounted when no container holds it. Keep a
            # trusted, networkless sleeper alive between clone/search/read calls.
            helper = config.model_copy(update={"image": _HELPER_IMAGE})
            keeper, keeper_args = self._command(helper, checkout, ["sleep", "7500"], clone=False)
            keeper_args.insert(1, "--detach")
            keeper_args[keeper_args.index("--workdir") + 1] = "/workspace"
            checkout.keeper = keeper
            kept = await self._exec(
                keeper_args,
                engine=checkout.engine,
                cancel=cancel,
                run=run,
                deadline=deadline,
                output_limit=1000,
                container=keeper,
            )
            if kept.code:
                raise SandboxError(
                    "Docker cannot mount the bounded workspace or start the local image"
                )
            output = await self._container(
                helper,
                checkout,
                ["python", "-I", "-S", "-c", CLONE_SCRIPT, repository.url, repository.ref],
                cancel=cancel,
                run=run,
                deadline=deadline,
                clone=True,
                token=token,
                stdin_data=stdin_data,
            )
            commit = self._json(output).get("commit", "")
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
                raise SandboxError("Clone did not return a valid commit")
            checkout.commit = commit
        except (SandboxError, ValueError):
            await self._remove_volume(checkout)
            raise

    async def _repository_command(
        self, command: list[str], repository: RepositoryConfig, config: SandboxConfig,
        checkout: _Checkout, cancel: Event, run: _Run, deadline: float,
    ) -> _Result:
        access = None
        if command[0] in {"gradle", "./gradlew"} and self.credentials and not run.shared:
            access = self.credentials.personal.gradle_access(run.owner_id, repository.id,
                                                             repository.url)
        if access is None:
            return await self._container(
                config, checkout, command, cancel=cancel, run=run, deadline=deadline
            )
        url, username, password = access
        # The two processes use the same Docker volume and different PID, network and tmp mounts.
        # No host path is required, including on remote Docker Engines.
        program = Path(__file__).with_name("gradle_relay.py").read_text(encoding="utf-8")
        runner = ["python", "-I", "-S", "-c", program]
        socket_path = "/workspace/.maven-" + uuid4().hex + ".sock"
        limits = [str(config.workspace_mb * 1024 * 1024), str(config.timeout_seconds)]
        credentials = json.dumps({"url": url, "username": username.get_secret_value(),
                                  "password": password.get_secret_value()}).encode()
        helper = config.model_copy(update={"image": _HELPER_IMAGE})
        registry = asyncio.create_task(self._container(
            helper, checkout, [*runner, "registry", socket_path, *limits],
            cancel=cancel, run=run, deadline=deadline, clone=True, stdin_data=credentials,
        ))
        build = asyncio.create_task(self._container(
            config, checkout, [*runner, "build", socket_path, *limits, *command],
            cancel=cancel, run=run, deadline=deadline,
        ))
        try:
            done, _ = await asyncio.wait((registry, build), return_when=asyncio.FIRST_COMPLETED)
            if registry in done:
                with suppress(SandboxError):
                    await registry
                raise SandboxError("Maven-репозиторий недоступен для Gradle.")
            return await build
        finally:
            for task in (registry, build):
                if not task.done():
                    task.cancel()
            await asyncio.gather(registry, build, return_exceptions=True)

    async def _tool(
        self,
        name: str,
        args: dict[str, Any],
        repository: RepositoryConfig,
        config: SandboxConfig,
        checkout: _Checkout,
        cancel: Event,
        run: _Run,
        deadline: float,
    ) -> list[Evidence]:
        suffix, text = "checkout", ""
        tool_outcome: Literal["empty", "failed"] | None = None
        if name == "clone_repository":
            text = f"Repository {repository.name}; ref {repository.ref}; commit {checkout.commit}."
        elif name == "write_analysis_script":
            filename = args["filename"]
            writer = ["python", "-I", "-S", "-c", WRITE_ANALYSIS_SCRIPT, "/workspace", filename]
            output = await self._container(
                config, checkout, writer, cancel=cancel, run=run, deadline=deadline,
                stdin_data=args["source"].encode("utf-8"),
            )
            if output.code:
                raise SandboxError("Could not write the analysis file in this run's workspace")
            suffix = "analysis/" + filename
            path = "/workspace/" + suffix
            command = ["python", "-I", path] if filename.endswith(".py") else ["java", path]
            text = "Analysis file ready. Call run_in_sandbox with " + json.dumps({
                "repository_id": repository.id, "command": command,
            })
        elif name == "run_in_sandbox":
            # Commands can edit the volume even if their exit status is nonzero.
            checkout.dirty = True
            output = await self._repository_command(
                args["command"], repository, config, checkout, cancel, run, deadline
            )
            suffix = "command/" + uuid4().hex
            if output.code:
                tool_outcome = "failed"
            elif not (output.stdout.strip() or output.stderr.strip()):
                tool_outcome = "empty"
            text = f"Exit code: {output.code}\n{output.stdout}\n{output.stderr}"
            if output.truncated or len(text) > config.max_output_chars:
                text = text[: config.max_output_chars - 40] + "\n[Command output truncated]"
        else:
            command = ["python", "-I", "-S", "-c"]
            budget = str(max(100, config.max_output_chars // 2))
            if name == "search_repository":
                command += [SEARCH_SCRIPT, "/workspace/repo", args["query"], budget]
            else:
                command += [
                    READ_SCRIPT,
                    "/workspace/repo",
                    args["path"],
                    str(args.get("start_line", 1)),
                    str(args.get("end_line", 160)),
                    budget,
                ]
            output = await self._container(
                config, checkout, command, cancel=cancel, run=run, deadline=deadline
            )
            data = self._json(output)
            if name == "read_repository":
                suffix = f"{args['path']}#L{args.get('start_line', 1)}"
                text = str(data.get("text", ""))
                if not text.strip():
                    tool_outcome = "empty"
                if data.get("truncated"):
                    text += "\n[File excerpt truncated]"
            else:
                matches = data.get("matches", [])
                if not isinstance(matches, list):
                    raise SandboxError("Sandbox returned invalid search results")
                text = "\n".join(f"{m['path']}:{m['line']}: {m['text']}" for m in matches)
                if not matches:
                    tool_outcome = "empty"
                    text = "No literal matches in the inspected files."
                if data.get("limited"):
                    text += "\n[Search limits reached; results are incomplete]"
                suffix = "search/" + uuid4().hex
        revision = checkout.commit + ("+workspace" if checkout.dirty else "")
        if checkout.dirty:
            text = "[Workspace may contain changes made by commands]\n" + text
        if len(text) > config.max_output_chars:
            text = text[: config.max_output_chars - 25] + "\n[Output truncated]"
        return [
            Evidence(
                id=f"{repository.id}/{revision}/{suffix}",
                title=f"{repository.name}: {suffix}",
                index_id="repo:" + repository.id,
                text=text[: config.max_output_chars],
                url=repository.url,
                revision=revision,
                kind="tool_metadata" if name == "write_analysis_script" else "source",
                tool_outcome=tool_outcome,
            )
        ]

    async def _remove_volume(self, checkout: _Checkout) -> None:
        try:
            if checkout.keeper:
                await self._cleanup_command(["rm", "-f", "-v", checkout.keeper], checkout.engine)
                checkout.keeper = ""
        finally:
            if checkout.created:
                await self._cleanup_command(
                    ["volume", "rm", "-f", checkout.volume], checkout.engine
                )
                checkout.created = False
                checkout.commit = ""

    async def close_run(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        run.closing = True
        run.cancel.set()
        async with run.cleanup:
            if run.tasks:
                await run.idle.wait()
            for checkout in run.repositories.values():
                await self._remove_volume(checkout)
            if self._runs.get(run_id) is run:
                del self._runs[run_id]
