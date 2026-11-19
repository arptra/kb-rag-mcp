"""Selected sandbox images are fixed by administrator profiles and checked per call."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from pydantic import ValidationError
from test_harness_integrations import Backend, ToolModel, hub, wait_finished
from test_harness_sandbox import CONFIG, FakeDocker, invoke

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Principal, RunRequest
from agent_harness.integrations_config import IntegrationSettings, SandboxProfileConfig
from agent_harness.runtime import HarnessRuntime
from agent_harness.sandbox import SandboxBackend, SandboxError


def test_default_profile_and_bounded_profile_validation() -> None:
    settings = IntegrationSettings()
    assert len(settings.sandbox_profiles) == 1
    assert settings.sandbox_profiles[0].id == "java21"
    assert settings.sandbox_profiles[0].image == "harness-sandbox-java21:local"
    assert settings.sandbox.enabled is False
    assert IntegrationSettings(sandbox_profiles=[]).sandbox_profiles == []
    assert RunRequest(question="question").sandbox_profile_id == ""
    for value in ("--privileged", "custom image", "x" * 65, "../../../image"):
        with pytest.raises(ValidationError):
            RunRequest(question="question", sandbox_profile_id=value)
    for patch in ({"image": "--privileged"}, {"id": ""}, {"allowed_owners": [""]}):
        with pytest.raises(ValidationError):
            SandboxProfileConfig.model_validate(
                {"id": "java", "name": "Java", "image": "java:local", **patch}
            )
    with pytest.raises(ValidationError):
        IntegrationSettings(sandbox_profiles=settings.sandbox_profiles * 2)
    with pytest.raises(ValidationError):
        IntegrationSettings(
            sandbox_profiles=[
                SandboxProfileConfig(id=f"p{i}", name="P", image="p:local") for i in range(21)
            ]
        )


async def test_profile_catalog_is_authenticated_filtered_and_has_no_images(tmp_path: Path) -> None:
    integrations = hub(tmp_path)
    settings = integrations.store.load()
    settings.sandbox_profiles.extend(
        [
            SandboxProfileConfig(
                id="private", name="Private", image="internal:secret", allowed_owners=["bob"]
            ),
            SandboxProfileConfig(
                id="disabled", name="Disabled", image="disabled:local", enabled=False
            ),
        ]
    )
    integrations.store.save(settings)
    assert await integrations.profiles(Principal("alice")) == [
        {"id": "", "name": "Базовая среда"},
        {"id": "java21", "name": "Java 21"},
    ]
    with pytest.raises(PermissionError):
        await integrations.profiles(Principal("bob"))
    settings.sandbox.enabled = False
    integrations.store.save(settings)
    assert await integrations.profiles(Principal("alice")) == []


async def call(integrations: Any, profile: str = "") -> list[Any]:
    return await integrations.call(
        "clone_repository",
        {"repository_id": "orders"},
        principal=Principal("alice"),
        run_id="run",
        index_ids=["repo:orders"],
        cancel=Event(),
        sandbox_profile_id=profile,
    )


async def test_profile_changes_only_image_preserving_global_caps(tmp_path: Path) -> None:
    integrations = hub(tmp_path)
    await call(integrations, "java21")
    selected = integrations._sandbox.calls[0]["config"]
    settings = integrations.store.load()
    assert selected.image == "harness-sandbox-java21:local"
    assert selected.model_dump(exclude={"image"}) == settings.sandbox.model_dump(exclude={"image"})
    await call(integrations)
    assert integrations._sandbox.calls[1]["config"].image == settings.sandbox.image
    definitions = await integrations.describe(Principal("alice"), ["repo:orders"])
    assert not any(
        "sandbox_profile_id" in item["function"]["parameters"]["properties"] for item in definitions
    )
    with pytest.raises(ValidationError):
        await integrations.call(
            "clone_repository",
            {"repository_id": "orders", "image": "injected"},
            principal=Principal("alice"),
            run_id="run",
            index_ids=["repo:orders"],
            cancel=Event(),
        )


async def test_runtime_forwards_selected_profile_to_repository_execution(tmp_path: Path) -> None:
    integrations = hub(tmp_path)
    runtime = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path), model=ToolModel(), tools=integrations
    )
    await runtime.start()
    try:
        run = await runtime.submit(
            Principal("alice"),
            RunRequest(
                question="Проверь Java-код сервиса.",
                mode="investigate",
                index_ids=["repo:orders"],
                sandbox_profile_id="java21",
            ),
        )
        result = await wait_finished(runtime, run["id"])
        assert result["status"] == "completed" and result["sandbox_profile_id"] == "java21"
        assert integrations._sandbox.calls[0]["config"].image == "harness-sandbox-java21:local"
    finally:
        await runtime.stop()


@pytest.mark.parametrize("change", ["missing", "disabled", "forbidden"])
async def test_profile_not_allowed_never_reaches_backend(tmp_path: Path, change: str) -> None:
    integrations = hub(tmp_path)
    settings = integrations.store.load()
    if change == "missing":
        settings.sandbox_profiles = []
    elif change == "disabled":
        settings.sandbox_profiles[0].enabled = False
    else:
        settings.sandbox_profiles[0].allowed_owners = ["bob"]
    integrations.store.save(settings)
    with pytest.raises(PermissionError):
        await call(integrations, "java21")
    assert integrations._sandbox.calls == []


async def test_profile_revoked_during_operation_hides_result(tmp_path: Path) -> None:
    integrations = hub(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    class Pending(Backend):
        async def call(self, name: str, args: Any, **kwargs: Any) -> list[Any]:
            result = await super().call(name, args, **kwargs)
            entered.set()
            await release.wait()
            return result

    integrations._sandbox = Pending()
    operation = asyncio.create_task(call(integrations, "java21"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        settings = integrations.store.load()
        settings.sandbox_profiles[0].enabled = False
        integrations.store.save(settings)
        release.set()
        with pytest.raises(PermissionError):
            await operation
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)


async def test_changing_image_does_not_reuse_existing_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker(monkeypatch)
    backend = SandboxBackend()
    await invoke(backend)
    previous_calls = len(docker.calls)
    with pytest.raises(SandboxError, match="changed"):
        await invoke(backend, config=CONFIG.model_copy(update={"image": "different:local"}))
    assert len(docker.calls) == previous_calls
    await backend.close_run("run-1")


async def test_profile_probe_checks_saved_image_even_when_execution_disabled(
    tmp_path: Path,
) -> None:
    integrations = hub(tmp_path)
    seen = []

    class Probe(Backend):
        async def probe(self, config: Any) -> dict[str, Any]:
            seen.append(config)
            return {"available": True, "reason": "Image exists"}

    integrations._sandbox = Probe()
    settings = integrations.store.load()
    settings.sandbox.enabled = False
    integrations.store.save(settings)
    result = await integrations.probe("sandbox_profile", "java21")
    assert result["available"] and result["message"] == "Image exists"
    assert seen[0].image == "harness-sandbox-java21:local" and not seen[0].enabled
    assert not (await integrations.probe("sandbox_profile", "unconfigured"))["available"]
    assert len(seen) == 1
