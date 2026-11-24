"""Inventory, daemon routing and cleanup for named execution environments."""

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from starlette.requests import Request
from starlette.testclient import TestClient
from test_harness_sandbox import CONFIG, FakeDocker, invoke

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Principal
from agent_harness.http import create_app
from agent_harness.integrations import IntegrationHub
from agent_harness.integrations_config import (
    DockerEngineConfig,
    IntegrationSettings,
    IntegrationStore,
    SandboxProfileConfig,
)
from agent_harness.runtime import HarnessRuntime
from agent_harness.sandbox import SandboxBackend, SandboxError, _Result


@pytest.mark.parametrize(
    "values",
    [
        {"mode": "tls", "host": "http://docker", "tls_id": "cert"},
        {"mode": "tls", "host": "docker", "port": 65536, "tls_id": "cert"},
        {"mode": "tls", "host": "docker", "port": 2375},
        {"mode": "tls", "host": "user@docker", "tls_id": "cert"},
        {"mode": "default", "host": "other"},
        {"mode": "context", "context": "--flag"},
    ],
)
def test_invalid_docker_destinations_rejected(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DockerEngineConfig.model_validate(values)


def test_profiles_inherit_only_engine_and_global_limits(tmp_path: Path) -> None:
    store = IntegrationStore(tmp_path)
    engine = DockerEngineConfig(mode="tls", host="engine.lan", tls_id="engine-ca")
    settings = IntegrationSettings(sandbox=CONFIG.model_copy(update={"engine": engine}))
    profile = SandboxProfileConfig(
        id="orders",
        name="Заказы",
        image="orders:latest",
        service_port=8080,
        service_health_path="/api/health",
    )
    settings.sandbox_profiles = [profile]
    store.save(settings)
    saved = store.load()
    effective = saved.sandbox_profiles[0].apply_to(saved.sandbox)
    assert effective.engine == engine
    assert (effective.image, effective.service_port, effective.service_health_path) == (
        "orders:latest",
        8080,
        "/api/health",
    )
    assert saved.docker_tls_users("engine-ca") == ["sandbox"]
    for bad_path in ("//evil.host/health", "/../api", "http://engine", "/x\rHost"):
        with pytest.raises(ValidationError):
            SandboxProfileConfig.model_validate(
                {**profile.model_dump(), "service_health_path": bad_path}
            )


async def test_operations_and_cleanup_reach_selected_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker(monkeypatch)
    monkeypatch.setenv("DOCKER_HOST", "tcp://another")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    backend = SandboxBackend()
    config = CONFIG.model_copy(
        update={"engine": DockerEngineConfig(mode="context", context="engine-a")}
    )
    await invoke(backend, config=config)
    await invoke(backend, "run_in_sandbox", config=config, command=["python", "-V"])
    with pytest.raises(SandboxError, match="changed"):
        await invoke(backend, config=CONFIG)
    await backend.close_run("run-1")
    for call in docker.calls:
        assert call["env"]["DOCKER_CONTEXT"] == "engine-a"
        assert "DOCKER_HOST" not in call["env"] and "DOCKER_TLS_VERIFY" not in call["env"]
        assert "DOCKER_CONTEXT" not in call["args"]


def test_remote_host_uses_explicit_mtls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    class Material:
        @contextmanager
        def docker_client_directory(self, key_id: str):
            seen.append(key_id)
            yield tmp_path

    backend = SandboxBackend(tls_credentials=Material())
    monkeypatch.setenv("DOCKER_CONTEXT", "other")
    engine = DockerEngineConfig(mode="tls", host="::1", port=4243, tls_id="engine-ca")
    with backend._client_environment(engine) as env:
        assert env["DOCKER_HOST"] == "tcp://[::1]:4243" and env["DOCKER_TLS_VERIFY"] == "1"
        assert env["DOCKER_CERT_PATH"] == str(tmp_path) and "DOCKER_CONTEXT" not in env
    assert seen == ["engine-ca"]


async def test_inventory_reads_only_selected_engine_and_returns_tags_and_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = SandboxBackend()
    engine = DockerEngineConfig(mode="context", context="onprem")
    image_id = "sha256:" + "6" * 64
    rows = (
        f"<none>\t<none>\t{image_id}\t50MB\n"
        + f"internal/team/app\t6.1\t{image_id}\t50MB\n" * 2
        + "parti"
    )

    async def command(config, args, **kwargs):
        assert args[:3] == ["image", "ls", "--no-trunc"] and config.engine == engine
        return _Result(0, rows, "", truncated=True)

    monkeypatch.setattr(backend, "engine_command", command)
    assert await backend.list_images(engine) == {
        "images": [
            {"reference": "internal/team/app:6.1", "id": image_id, "size": "50MB"},
            {"reference": image_id, "id": image_id, "size": "50MB"},
        ],
        "truncated": True,
    }


async def test_cleanup_ignores_only_already_removed_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = SandboxBackend()
    stderr = "Error response from daemon: No such container: harness-a"

    async def execute(args, **kwargs):
        assert args == ["rm", "-f", "-v", "harness-a"]
        return _Result(1, "", stderr)

    monkeypatch.setattr(backend, "_exec", execute)
    await backend.remove_container(CONFIG, "harness-a")
    stderr = "Docker engine disconnected"
    with pytest.raises(SandboxError, match="cleanup"):
        await backend.remove_container(CONFIG, "harness-a")


def test_admin_inventory_can_use_unsaved_settings_without_granting_tools(tmp_path: Path) -> None:
    async def authenticate(request: Request) -> Principal | None:
        user = request.headers.get("x-user")
        return Principal(user) if user else None

    async def authorize(request: Request) -> bool:
        return request.headers.get("x-user") == "admin"

    async def allowed(_: Principal) -> bool:
        return True

    hub = IntegrationHub(IntegrationStore(tmp_path), allowed)
    calls = []

    class Inventory:
        async def list_images(self, engine):
            calls.append(engine)
            return {"images": [], "truncated": False}

    hub._sandbox = Inventory()
    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path), tools=hub)
    app = create_app(runtime, authenticate, integrations=hub, authorize_model_manager=authorize)
    url = "/api/settings/sandbox/images"
    request = {"engine": {"mode": "context", "context": "build"}}
    with TestClient(app) as client:
        assert client.post(url, json=request).status_code == 401
        assert client.post(url, json=request, headers={"x-user": "reader"}).status_code == 403
        client.headers["x-user"] = "admin"
        assert client.post(url, json={"engine": {"mode": "remote"}}).status_code == 400
        assert client.post(url, json=request).json() == {"images": [], "truncated": False}
    assert calls == [DockerEngineConfig(mode="context", context="build")]
    assert not hub.store.load().sandbox.enabled


async def test_probe_checks_trusted_helper_and_reports_missing_gradle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_harness import sandbox

    backend = SandboxBackend()
    config = CONFIG.model_copy(update={"image": "java-without-gradle:local"})
    inspected = []
    helper_has_git = True

    async def command(profile, args, **kwargs):
        if args[:2] == ["image", "inspect"]:
            inspected.append(profile.image)
        if args[0] != "run":
            return _Result(0, "available", "")
        in_helper = profile.image == sandbox._HELPER_IMAGE
        caps = {
            "python": "3.12",
            "git": helper_has_git,
            "sleep": True,
            "ssh": True,
            "java": not in_helper,
            "mvn": not in_helper,
            "gradle": False,
        }
        return _Result(0, json.dumps(caps), "")

    monkeypatch.setattr(backend, "engine_command", command)
    report = await backend.probe(config)
    assert inspected == [sandbox._HELPER_IMAGE, config.image]
    assert report["available"] and "Gradle отсутствует" in report["reason"]
    assert report["capabilities"]["java"] and report["capabilities"]["mvn"]
    assert not report["capabilities"]["gradle"]
    inspected.clear()
    helper_has_git = False
    report = await backend.probe(config)
    assert not report["available"] and "git" in report["reason"]
    assert inspected == [sandbox._HELPER_IMAGE]
