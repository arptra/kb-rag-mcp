"""Run the selected environment through the same HTTP and graph entry points as chat."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient
from test_harness_integrations import hub, wait_finished

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Principal, RunRequest
from agent_harness.http import create_app
from agent_harness.integrations_config import SandboxProfileConfig
from agent_harness.runtime import HarnessRuntime
from agent_harness.sandbox import SandboxBackend
from agent_harness.sandbox_service import PROBE_IMAGE


class SandboxModel:
    def status(self) -> dict[str, bool]:
        return {"available": True}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        return {"content": "Приложение в выбранном контейнере ответило HTTP 200.", "tool_calls": []}


async def test_selected_service_checked_by_graph_without_repository(tmp_path: Path) -> None:
    integration = hub(tmp_path)
    settings = integration.store.load()
    settings.sandbox_profiles = [
        SandboxProfileConfig(
            id=name,
            name=name,
            image=name + ":local",
            service_port=8080,
        )
        for name in ("orders", "unused")
    ]
    integration.store.save(settings)
    check = AsyncMock(
        return_value={
            "available": True,
            "http_status": 200,
            "message": "GET /health вернул HTTP 200.",
        }
    )
    integration._probe_config = check  # type: ignore[method-assign]
    runtime = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path), model=SandboxModel(), tools=integration
    )
    await runtime.start()
    try:
        run = await runtime.submit(
            Principal("alice"),
            RunRequest(
                question="Проверь API песочницы.",
                mode="investigate",
                sandbox_profile_id="orders",
                index_ids=["sandbox-api:orders", "sandbox-api:unused"],
            ),
        )
        result = await wait_finished(runtime, run["id"])
        assert result["index_ids"] == ["sandbox-api:orders"]
        assert any(
            item["index_id"] == "sandbox-api:orders" and "200" in item["text"]
            for item in result["evidence"]
        )
        assert check.await_args.args[0].image == "orders:local"
        check.return_value = {
            "available": False,
            "http_status": 404,
            "message": "GET /health вернул HTTP 404.",
        }
        (missing_path,) = await integration.call(
            "check_sandbox_api",
            {},
            principal=Principal("alice"),
            run_id="second",
            index_ids=["sandbox-api:orders"],
            sandbox_profile_id="orders",
            cancel=Event(),
        )
        assert missing_path.tool_outcome is None and "404" in missing_path.text
        with pytest.raises(PermissionError):
            await integration.call(
                "check_sandbox_api",
                {},
                principal=Principal("alice"),
                run_id="other",
                index_ids=["sandbox-api:unused"],
                sandbox_profile_id="orders",
                cancel=Event(),
            )
    finally:
        await runtime.stop()


def test_chat_probe_access_and_secret_entry(tmp_path: Path) -> None:
    integration = hub(tmp_path)

    async def identity(request: Request) -> Principal | None:
        return Principal(request.headers["user"]) if "user" in request.headers else None

    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path), tools=integration)
    with TestClient(create_app(runtime, identity, integrations=integration)) as client:
        alice = {"user": "alice"}
        path = "/api/sandbox/check"
        assert client.post(path, json={"profile_id": "java21"}).status_code == 401
        assert (
            client.post(path, headers={"user": "bob"}, json={"profile_id": ""}).status_code == 403
        )
        assert client.post(path, headers=alice, json={"profile_id": "missing"}).status_code == 403
        assert (
            client.post(path, headers=alice, json={"profile_id": "", "port": 22}).status_code == 400
        )
        for text in ("-----BEGIN OPENSSH PRIVATE KEY-----\nTEST", "repoPassword=synthetic123"):
            response = client.post("/api/runs", headers=alice, json={"question": text})
            assert response.status_code == 400 and text not in response.text
        assert client.get("/api/runs", headers=alice).json()["runs"] == []


@pytest.mark.skipif(
    os.environ.get("HARNESS_TEST_DOCKER") != "1",
    reason="requires a running Docker Engine and harness-sandbox:local",
)
def test_live_isolated_service_http_check(tmp_path: Path) -> None:
    image = "harness-http-test:" + uuid4().hex
    dockerfile = (
        f'FROM {PROBE_IMAGE}\nCMD ["python3","-m","http.server","18877","--bind","127.0.0.1"]\n'
    )
    subprocess.run(
        ["docker", "build", "--network=none", "-q", "-t", image, "-"],
        input=dockerfile,
        text=True,
        capture_output=True,
        check=True,
        timeout=90,
    )
    try:
        integration = hub(tmp_path)
        integration._sandbox = SandboxBackend()
        settings = integration.store.load()
        settings.sandbox_profiles = [
            SandboxProfileConfig(
                id="smoke",
                name="HTTP smoke",
                image=image,
                service_port=18877,
            )
        ]
        integration.store.save(settings)
        runtime = HarnessRuntime(
            HarnessSettings(enabled=True, state_dir=tmp_path), tools=integration
        )

        async def identity(_: Request) -> Principal:
            return Principal("alice")

        async def admin(_: Request) -> bool:
            return True

        app = create_app(runtime, identity, integrations=integration, authorize_model_manager=admin)
        with TestClient(app) as client:
            inventory = client.post("/api/settings/sandbox/images", json={"engine": {}})
            assert image in {row["reference"] for row in inventory.json()["images"]}
            check = client.post("/api/sandbox/check", json={"profile_id": "smoke"}).json()
            assert check["available"] and check["http_status"] == 200, check
            settings.sandbox_profiles[0].service_health_path = "/absent"
            integration.store.save(settings)
            check = client.post("/api/sandbox/check", json={"profile_id": "smoke"}).json()
            assert not check["available"] and check["http_status"] == 404, check
        containers = subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", "ancestor=" + image]
        )
        assert not containers.strip()
    finally:
        subprocess.run(["docker", "image", "rm", image], capture_output=True, check=True)
