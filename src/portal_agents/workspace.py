"""A lazy, offline Docker workspace shared by tool calls in one agent turn."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import PurePosixPath
from uuid import uuid4

from langchain_core.tools import BaseTool, tool

from portal_agents.gradle_repository import GRADLE_REPOSITORIES_DIR, GRADLE_REPOSITORY_INIT


async def _docker(
    *args: str, script: bytes = b"", timeout: float = 60, keep_tail: bool = False,
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdin is not None and proc.stdout is not None
    output = bytearray()
    truncated = False
    try:
        async with asyncio.timeout(timeout):
            proc.stdin.write(script)
            proc.stdin.close()
            while part := await proc.stdout.read(8192):
                output.extend(part)
                if len(output) > 65536:
                    if not keep_tail:
                        raise ValueError("Вывод превысил 64 КиБ. Сократите вывод проверки.")
                    del output[:-65536]
                    truncated = True
            prefix = "Показаны последние 64 КиБ вывода.\n" if truncated else ""
            return await proc.wait(), prefix + output.decode(errors="replace")
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.communicate()


class OfflineWorkspace:
    def __init__(self, image: str, authorize: Callable[[], Awaitable[bool]]) -> None:
        self.image = image
        self.authorize = authorize
        self.name = "portal-agent-" + uuid4().hex
        self._started = False
        self._gradle_limit_requested = False
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._started:
            # The container also exits and auto-removes itself after 10 minutes
            # if the portal process disappears before the request is cleaned up.
            try:
                await _docker("rm", "-f", self.name, timeout=15)
            except (OSError, TimeoutError):
                logging.getLogger(__name__).warning("Docker workspace cleanup unavailable")
            finally:
                self._started = False
                self._gradle_limit_requested = False

    async def _start(self) -> bool:
        if not self._started:
            self._started = True
            status, _ = await _docker(
                "run",
                "--detach",
                "--rm",
                "--pull=never",
                "--name",
                self.name,
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=128",
                "--memory=512m",
                "--memory-swap=512m",
                "--cpus=1",
                "--log-driver=none",
                "--user=65534:65534",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=128m",
                "--tmpfs",
                "/tmp/.portal-gradle:rw,nosuid,nodev,size=1g,mode=0700,uid=65534,gid=65534",
                "--workdir=/tmp",
                "--env=HOME=/tmp",
                "--entrypoint=/bin/sh",
                self.image,
                "-c",
                "exec sleep 600",
                timeout=20,
            )
            if status:
                await self.close()
                return False
        return True

    async def put_file(self, filename: str, contents: bytes) -> str:
        """Place bytes into a fresh directory without sharing a host path or credentials."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,230}", filename):
            raise ValueError("Некорректное имя файла.")
        directory = "/tmp/portal-artifact-" + uuid4().hex
        await self._upload(contents, 'umask 077 && mkdir "$1" && cat > "$1/$2"',
                           directory, filename)
        return directory + "/" + filename

    async def put_dependency(self, source_id: str, directories: tuple[str, ...],
                             filename: str, contents: bytes) -> str:
        """Atomically stage one dependency under its repository ID and Maven coordinates."""
        parts = (source_id, *directories, filename)
        if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,230}", part) for part in parts):
            raise ValueError("Некорректный путь зависимости.")
        # Walk the directory components one at a time. Never write through a symlink
        # left by an earlier build, or truncate a previously published file on failure.
        script = """
set -eu
umask 077
file=$1
temp=$2
shift 2
cd /tmp
expected=/tmp
for part do
    expected="$expected/$part"
    if [ ! -d "$part" ]; then mkdir "$part"; fi
    [ ! -L "$part" ]
    cd -P "./$part"
    [ "$(pwd -P)" = "$expected" ]
done
[ ! -L "$file" ]
trap 'rm -f "$temp"' EXIT
dd of="$temp" conv=excl bs=64k 2>/dev/null
mv -fT "$temp" "$file"
""".strip()
        await self._upload(contents, script, filename, ".stage-" + uuid4().hex,
                           GRADLE_REPOSITORIES_DIR.rsplit("/", 1)[1], *parts[:-1])
        return GRADLE_REPOSITORIES_DIR + "/" + "/".join(parts)

    async def _upload(self, contents: bytes, script: str, *args: str) -> None:
        if len(contents) > 32 * 1024 * 1024:
            raise ValueError("Файл превышает 32 МиБ.")
        async with self._lock:
            if not await self.authorize():
                raise PermissionError("Доступ к среде отозван.")
            if not await self._start():
                raise ValueError("Среда не запустилась. Проверьте образ и службу Docker.")
            if not await self.authorize():
                raise PermissionError("Доступ к среде отозван.")
            status, _ = await _docker(
                "exec", "-i", self.name, "/bin/sh", "-c",
                script, "upload", *args,
                script=contents,
            )
            if not await self.authorize():
                raise PermissionError("Доступ к среде отозван.")
            if status:
                raise ValueError("Ошибка записи файла в среду исполнения.")

    async def _execute(
        self, *args: str, script: bytes = b"", gradle: bool = False,
    ) -> str:
        async with self._lock:
            if not await self.authorize():
                raise PermissionError("Доступ к среде отозван.")
            try:
                if not await self._start():
                    return "Среда не запустилась. Проверьте образ и службу Docker."
                if gradle and not self._gradle_limit_requested:
                    # Kotlin DSL generates large API jars and runs a build JVM. Increase this
                    # container's ceiling only when somebody actually requests a Gradle task.
                    code, _ = await _docker("update", "--memory=2g", "--memory-swap=2g", self.name)
                    if code:
                        logging.getLogger(__name__).info(
                            "Running Gradle with the existing container memory limit"
                        )
                    self._gradle_limit_requested = True
                if not await self.authorize():
                    raise PermissionError("Доступ к среде отозван.")
                code, output = await _docker(
                    "exec", "-i", self.name, *args, script=script,
                    timeout=300 if gradle else 60, keep_tail=gradle,
                )
            except FileNotFoundError:
                self._started = False
                return "Docker CLI отсутствует в окружении портала."
            except (TimeoutError, ValueError):
                await self.close()
                raise
            if not await self.authorize():
                raise PermissionError("Доступ к среде отозван.")
            return f"exit_code={code}\n{output}"

    def gradle_tool(self) -> BaseTool:
        @tool
        async def run_gradle(project_dir: str, tasks: list[str]) -> str:
            """Run Gradle tasks in a Groovy/Kotlin project in this turn's offline container.

            First extract the Git snapshot with run_in_workspace. Pass the project root
            under /tmp (with settings.gradle[.kts] or build.gradle[.kts]) and tasks such as
            ["test"], [":service:test"] or ["tasks"]. Prefer the project's gradlew.
            The result contains the actual process exit code and the tail of the build log.
            """
            path = PurePosixPath(project_dir)
            if (not path.is_relative_to("/tmp") or ".." in path.parts
                    or any(ord(c) < 32 for c in project_dir)):
                return "Укажите путь к распакованному проекту внутри /tmp."
            if (not tasks or len(tasks) > 32 or
                    not all(re.fullmatch(r":?[\w.][\w.-]*(?::[\w.][\w.-]*)*", t)
                            and len(t) <= 240 for t in tasks)):
                return "Укажите имена задач Gradle, например test или :service:test."
            script = _GRADLE_SCRIPT.replace("__PORTAL_REPOSITORY_INIT__", GRADLE_REPOSITORY_INIT)
            try:
                result = await self._execute(
                    "/bin/sh", "-s", "--", project_dir, *tasks,
                    script=script.encode(), gradle=True,
                )
                if not result.startswith("exit_code=0\n") and (
                    "available for offline mode" in result or "UnknownHostException" in result
                ):
                    result += ("\nДля этой сборки нужны дистрибутив Wrapper, плагины и зависимости "
                               "в кэше образа или в локальном репозитории.")
                return result
            except TimeoutError:
                return "Сборка превысила 5 минут. Контейнер остановлен."

        return run_gradle

    def tool(self) -> BaseTool:
        @tool
        async def run_in_workspace(script: str) -> str:
            """Run a shell script inside this turn's offline container. Reuse files in /tmp.

            Use for calculations and tests on supplied materials. Programs and runtimes
            depend on the operator's image. Output includes the process exit status.
            """
            if not script.strip() or len(script.encode()) > 32768:
                raise ValueError("Введите скрипт размером до 32 КиБ.")
            return await self._execute("/bin/sh", "-s", script=script.encode())

        return run_in_workspace


_GRADLE_SCRIPT = r'''set -eu
cd -P "$1" || { echo "Каталог проекта отсутствует. Сначала распакуйте исходники."; exit 2; }
shift
case "$(pwd -P)" in /tmp|/tmp/*) ;; *) echo "Проект должен находиться в /tmp."; exit 2 ;; esac
if [ ! -f settings.gradle ] && [ ! -f settings.gradle.kts ] &&
   [ ! -f build.gradle ] && [ ! -f build.gradle.kts ]; then
    echo "Проверьте путь к settings.gradle(.kts) или build.gradle(.kts)."
    exit 2
fi

seed=/opt/portal-gradle-cache
if [ ! -d "$seed" ]; then seed=${GRADLE_USER_HOME:-$seed}; fi
export GRADLE_USER_HOME=/tmp/.portal-gradle
if [ -z "${GRADLE_RO_DEP_CACHE:-}" ] && [ -d "$seed/caches/modules-2" ]; then
    export GRADLE_RO_DEP_CACHE="$seed/caches"
fi
if [ -f gradlew ]; then
    # Gradle Wrapper creates a lock next to its distribution, even with a prepared cache.
    # Keep its locks writable and reuse the unpacked read-only distribution from the image.
    if [ "$seed" != "$GRADLE_USER_HOME" ]; then
        for entry in "$seed"/wrapper/dists/*/*; do
            [ -d "$entry" ] || continue
            dest="$GRADLE_USER_HOME/wrapper/dists/${entry#"$seed"/wrapper/dists/}"
            mkdir -p "$dest"
            for file in "$entry"/gradle-*; do
                [ -d "$file" ] || continue
                [ -e "$dest/${file##*/}" ] || ln -s "$file" "$dest/${file##*/}"
            done
            for marker in "$entry"/*.zip.ok; do
                [ ! -f "$marker" ] || touch "$dest/${marker##*/}"
            done
        done
    fi
    set -- sh ./gradlew "$@"
elif command -v gradle >/dev/null 2>&1; then
    set -- gradle "$@"
else
    echo "Проект без gradlew требует образ, где установлены Gradle и JDK."
    exit 127
fi
if [ -d /tmp/portal-gradle-repositories ]; then
    init="$GRADLE_USER_HOME/portal-repositories.gradle"
    cat > "$init" <<'PORTAL_INIT'
__PORTAL_REPOSITORY_INIT__
PORTAL_INIT
    set -- "$@" --init-script "$init"
fi
exec "$@" --offline --console=plain --no-daemon --max-workers=1
'''
