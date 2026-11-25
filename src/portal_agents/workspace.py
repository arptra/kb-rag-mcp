"""A lazy, offline Docker workspace shared by tool calls in one agent turn."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from uuid import uuid4

from langchain_core.tools import BaseTool, tool


async def _docker(*args: str, script: bytes = b"", timeout: float = 60) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdin is not None and proc.stdout is not None
    output = bytearray()
    try:
        async with asyncio.timeout(timeout):
            proc.stdin.write(script)
            proc.stdin.close()
            while part := await proc.stdout.read(8192):
                output.extend(part)
                if len(output) > 65536:
                    raise ValueError("Вывод превысил 64 КиБ. Сократите вывод проверки.")
            return await proc.wait(), output.decode(errors="replace")
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

    def tool(self) -> BaseTool:
        @tool
        async def run_in_workspace(script: str) -> str:
            """Run a shell script inside this turn's offline container. Reuse files in /tmp.

            Use for calculations and tests on supplied materials. Programs and runtimes
            depend on the operator's image. Output includes the process exit status.
            """
            if not script.strip() or len(script.encode()) > 32768:
                raise ValueError("Введите скрипт размером до 32 КиБ.")
            async with self._lock:
                if not await self.authorize():
                    raise PermissionError("Доступ к среде отозван.")
                try:
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
                            return "Среда не запустилась. Проверьте образ и службу Docker."
                    if not await self.authorize():
                        raise PermissionError("Доступ к среде отозван.")
                    code, output = await _docker(
                        "exec",
                        "-i",
                        self.name,
                        "/bin/sh",
                        "-s",
                        script=script.encode(),
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

        return run_in_workspace
