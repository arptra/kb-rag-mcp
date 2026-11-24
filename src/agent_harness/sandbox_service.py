"""Exercise the configured HTTP endpoint inside a fresh, isolated application container."""

from __future__ import annotations

import time
from threading import Event
from typing import Any
from uuid import uuid4

from agent_harness.integrations_config import SandboxConfig
from agent_harness.sandbox import SandboxBackend, SandboxError

PROBE_IMAGE = "harness-sandbox:local"

# The target image can be distroless. Use the shipped Python image in the same
# isolated network namespace. Only an integer status crosses back into Harness.
_CHECK_HTTP = """
import http.client, sys, time
port, path, seconds = sys.argv[1:]
deadline = time.monotonic() + float(seconds)
status = 0
while time.monotonic() < deadline:
    conn = http.client.HTTPConnection('127.0.0.1', int(port), timeout=1)
    try:
        conn.request('GET', path, headers={'Connection': 'close'})
        status = conn.getresponse().status
        if status < 500:
            break
    except (OSError, http.client.HTTPException):
        pass
    finally:
        conn.close()
    time.sleep(0.25)
print(status)
"""


async def probe_service(
    backend: SandboxBackend, config: SandboxConfig, cancel: Event
) -> dict[str, Any]:
    if config.service_port is None:
        raise ValueError("Порт приложения для песочницы не настроен.")
    name = "harness-http-" + uuid4().hex
    helper = name + "-probe"
    deadline = time.monotonic() + config.timeout_seconds
    limits = [
        "--pull=never",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65532:65532",
        "--log-driver=none",
        "--label=agent-harness=true",
    ]
    endpoint = f"127.0.0.1:{config.service_port}{config.service_health_path}"
    response: dict[str, Any] = {"available": False}
    try:
        async with backend.probe_slot(config, cancel, deadline):
            try:
                started = await backend.engine_command(
                    config,
                    [
                        "run",
                        "--detach",
                        "--rm",
                        "--name",
                        name,
                        *limits,
                        "--network=none",
                        "--cpus",
                        str(config.cpus),
                        "--memory",
                        f"{config.memory_mb}m",
                        "--memory-swap",
                        f"{config.memory_mb}m",
                        "--pids-limit",
                        str(config.pids_limit),
                        "--tmpfs",
                        f"/tmp:rw,nosuid,nodev,size={config.workspace_mb}m",
                        config.image,
                    ],
                    cancel=cancel,
                    deadline=deadline,
                    container=name,
                )
                if started.code:
                    return {**response, "message": "Docker не смог запустить образ приложения."}
                seconds = max(0.1, deadline - time.monotonic() - 2)
                probe = await backend.engine_command(
                    config,
                    [
                        "run",
                        "--rm",
                        "--name",
                        helper,
                        *limits,
                        "--network",
                        "container:" + name,
                        "--cpus=0.25",
                        "--memory=64m",
                        "--memory-swap=64m",
                        "--pids-limit=16",
                        "--entrypoint=python3",
                        PROBE_IMAGE,
                        "-I",
                        "-B",
                        "-c",
                        _CHECK_HTTP,
                        str(config.service_port),
                        config.service_health_path,
                        str(seconds),
                    ],
                    cancel=cancel,
                    deadline=deadline,
                    container=helper,
                )
                if probe.code or not probe.stdout.strip().isdecimal():
                    return {
                        **response,
                        "message": (
                            f"HTTP-проверка не запустилась. Проверьте, что приложение продолжает "
                            f"работать и на выбранном Docker Engine есть {PROBE_IMAGE}."
                        ),
                    }
                status = int(probe.stdout.strip())
                if not 100 <= status <= 599:
                    return {**response, "message": f"Приложение не ответило на GET {endpoint}."}
                return {
                    "available": 200 <= status < 300,
                    "http_status": status,
                    "message": f"GET {endpoint} в тестовом контейнере вернул HTTP {status}.",
                }
            finally:
                # rm -v also removes anonymous volumes declared by the selected image.
                try:
                    await backend.remove_container(config, helper)
                finally:
                    await backend.remove_container(config, name)
    except TimeoutError:
        return {**response, "message": f"Истекло время ожидания проверки GET {endpoint}."}
    except (SandboxError, OSError):
        return {**response, "message": "Выбранный Docker Engine недоступен для проверки."}
