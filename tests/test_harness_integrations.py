"""Portable integration settings, authorization, and tool-only investigation regressions."""

import asyncio
import json
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from pydantic import ValidationError
from starlette.requests import Request
from starlette.testclient import TestClient

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.http import create_app
from agent_harness.integrations import IntegrationHub
from agent_harness.integrations_config import IntegrationSettings, IntegrationStore
from agent_harness.runtime import HarnessRuntime


async def allowed(principal: Principal) -> bool:
    return principal.owner_id == "alice"


def configuration() -> dict[str, Any]:
    return {
        "repositories": [
            {
                "id": "orders",
                "name": "Orders",
                "url": "https://git.test/orders.git",
                "allowed_owners": ["alice"],
            }
        ],
        "connections": [
            {
                "id": "jira",
                "name": "Jira",
                "kind": "mcp",
                "url": "https://jira.test/mcp",
                "allowed_tools": ["search"],
                "credential_env": "HARNESS_TEST_REMOTE_TOKEN",
                "allowed_owners": ["alice"],
            }
        ],
        "sandbox": {"enabled": True},
    }


class Backend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed: list[str] = []

    async def call(self, name: str, args: Any, **kwargs: Any) -> list[Evidence]:
        self.calls.append({"name": name, "args": args, **kwargs})
        return [
            Evidence(
                id="current-evidence",
                title="Current result",
                text="Found evidence secret-value",
                index_id="repo:orders" if "repository" in kwargs else "connection:jira",
            )
        ]

    async def close_run(self, run_id: str) -> None:
        self.closed.append(run_id)


def hub(tmp_path: Path) -> IntegrationHub:
    store = IntegrationStore(tmp_path)
    store.save(IntegrationSettings.model_validate(configuration()))
    result = IntegrationHub(store, allowed)
    result._sandbox = Backend()
    result._remote = Backend()
    return result


@pytest.mark.parametrize(
    "patch",
    [
        {"repositories": [{"id": "a", "name": "A", "url": "file:///tmp/repo"}]},
        {
            "repositories": [
                {"id": "a", "name": "A", "url": "https://git.test/a", "ref": "--upload-pack=x"}
            ]
        },
        {
            "connections": [
                {"id": "a", "name": "A", "kind": "mcp", "url": "https://secret@host.test"}
            ]
        },
        {
            "connections": [
                {"id": "a", "name": "A", "kind": "mcp", "url": "https://host.test?token=x"}
            ]
        },
        {
            "connections": [
                {
                    "id": "a",
                    "name": "A",
                    "kind": "mcp",
                    "url": "http://host.test",
                    "credential_env": "TOKEN",
                }
            ]
        },
        {"sandbox": {"image": "--privileged"}},
        {"sandbox": {"cpus": 100}},
    ],
)
def test_unsafe_configuration_rejected(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IntegrationSettings.model_validate({**configuration(), **patch})


async def test_hub_rechecks_acl_and_run_scope_and_redacts_secret(
    tmp_path: Path, monkeypatch: Any
) -> None:
    integrations = hub(tmp_path)
    monkeypatch.setenv("HARNESS_TEST_REMOTE_TOKEN", "secret-value")
    assert {r["id"] for r in await integrations.indexes(Principal("alice"))} == {
        "repo:orders",
        "connection:jira",
    }
    with pytest.raises(PermissionError):
        await integrations.indexes(Principal("bob"))
    args = {"connection_id": "jira", "tool": "search", "arguments": {}}
    with pytest.raises(PermissionError):
        await integrations.call(
            "mcp_call",
            args,
            principal=Principal("alice"),
            run_id="run1",
            index_ids=["repo:orders"],
            cancel=Event(),
        )
    result = await integrations.call(
        "mcp_call",
        args,
        principal=Principal("alice"),
        run_id="run1",
        index_ids=["connection:jira"],
        cancel=Event(),
    )
    assert result[0].text == "Found evidence [redacted]"
    changed = integrations.store.load()
    changed.connections[0].enabled = False
    integrations.store.save(changed)
    with pytest.raises(PermissionError):
        await integrations.call(
            "mcp_call",
            args,
            principal=Principal("alice"),
            run_id="run1",
            index_ids=["connection:jira"],
            cancel=Event(),
        )
    assert len(integrations._remote.calls) == 1
    assert integrations.store.path.stat().st_mode & 0o777 == 0o600
    assert b"secret-value" not in integrations.store.path.read_bytes()


@pytest.mark.parametrize("tool_name", ["mcp_call", "mcp_tool_schema"])
async def test_tool_revoked_during_backend_call_rejects_result(
    tmp_path: Path, tool_name: str
) -> None:
    integrations = hub(tmp_path)
    settings = integrations.store.load()
    settings.connections[0].allowed_tools = ["search", "list_public"]
    integrations.store.save(settings)
    entered = asyncio.Event()
    release = asyncio.Event()

    class PendingBackend(Backend):
        async def call(self, name: str, args: Any, **kwargs: Any) -> list[Evidence]:
            result = await super().call(name, args, **kwargs)
            entered.set()
            await release.wait()
            return result

    integrations._remote = PendingBackend()
    args: dict[str, Any] = {"connection_id": "jira", "tool": "search"}
    if tool_name == "mcp_call":
        args["arguments"] = {}
    operation = asyncio.create_task(
        integrations.call(
            tool_name,
            args,
            principal=Principal("alice"),
            run_id="revoked-tool-run",
            index_ids=["connection:jira"],
            cancel=Event(),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        settings = integrations.store.load()
        settings.connections[0].allowed_tools = ["list_public"]
        integrations.store.save(settings)
        # The connection remains visible; only the in-flight tool loses permission.
        assert "connection:jira" in {
            item["id"] for item in await integrations.indexes(Principal("alice"))
        }
        release.set()
        with pytest.raises(PermissionError, match="permission changed"):
            await asyncio.wait_for(operation, timeout=3)
        assert len(integrations._remote.calls) == 1
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("kind,resource_id", [("sandbox", ""), ("repository", "orders")])
async def test_sandbox_probe_reason_is_exposed_as_message(
    tmp_path: Path, kind: str, resource_id: str
) -> None:
    integrations = hub(tmp_path)
    reason = "Образ песочницы отсутствует."

    class UnavailableSandbox(Backend):
        async def probe(self, config: Any) -> dict[str, Any]:
            return {"available": False, "reason": reason}

    integrations._sandbox = UnavailableSandbox()
    result = await integrations.probe(kind, resource_id)
    assert result["available"] is False
    assert result["message"] == reason


class ToolModel:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        if len(self.requests) == 1 and kwargs["tools"]:
            return {
                "tool_calls": [
                    {
                        "name": "search_repository",
                        "args": {"repository_id": "orders", "query": "handleOrder"},
                    }
                ]
            }
        return {"content": "Проверен код [current-evidence].", "tool_calls": []}


async def wait_finished(runtime: HarnessRuntime, run_id: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            run = await runtime.get(Principal("alice"), run_id)
            if run["phase"] == "finished":
                return run
            await asyncio.sleep(0.01)


async def test_repository_only_investigation_without_rag_and_cleanup(tmp_path: Path) -> None:
    integrations = hub(tmp_path)
    model = ToolModel()
    runtime = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path), model=model, tools=integrations
    )
    await runtime.start()
    try:
        run = await runtime.submit(
            Principal("alice"),
            RunRequest(
                question="Как обрабатывается заказ?",
                mode="investigate",
                index_ids=["repo:orders"],
            ),
        )
        result = await wait_finished(runtime, run["id"])
        assert result["status"] == "completed"
        assert result["evidence"][0]["index_id"] == "repo:orders"
        assert result["metrics"]["tool_calls"] == 1
        assert integrations._sandbox.closed == [run["id"]]
        assert {t["function"]["name"] for t in model.requests[0]["tools"]} == {
            "clone_repository",
            "search_repository",
            "read_repository",
            "run_in_sandbox",
        }
        changed = integrations.store.load()
        changed.repositories[0].enabled = False
        integrations.store.save(changed)
        hidden = await runtime.get(Principal("alice"), run["id"])
        assert not hidden["answer"] and not hidden["evidence"]
    finally:
        await runtime.stop()


async def test_tool_only_answer_without_external_calls_is_unverified(tmp_path: Path) -> None:
    integrations = hub(tmp_path)
    answer = "Заказ проходит через сервис Orders."

    class DirectAnswerModel(ToolModel):
        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            self.requests.append(kwargs)
            return {"content": answer, "tool_calls": []}

    model = DirectAnswerModel()
    runtime = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path), model=model, tools=integrations
    )
    await runtime.start()
    try:
        run = await runtime.submit(
            Principal("alice"),
            RunRequest(
                question="Как обрабатывается заказ?",
                mode="investigate",
                index_ids=["repo:orders"],
            ),
        )
        result = await wait_finished(runtime, run["id"])
        assert result["answer"] == answer
        assert result["status"] == "partial"
        assert result["evidence"] == []
        assert any("не подтверждён источниками" in warning for warning in result["warnings"])
        assert result["metrics"]["tool_calls"] == 0
        assert not integrations._sandbox.calls and not integrations._remote.calls
        assert len(model.requests) == 1
        assert model.requests[0]["tools"]
    finally:
        await runtime.stop()


async def test_tool_metadata_stays_in_context_but_does_not_confirm_answer(tmp_path: Path) -> None:
    integrations = hub(tmp_path)

    class SchemaBackend(Backend):
        async def call(self, name: str, args: Any, **kwargs: Any) -> list[Evidence]:
            await super().call(name, args, **kwargs)
            return [
                Evidence(
                    id="jira-search-schema",
                    title="Search tool schema",
                    text='{"name":"search","inputSchema":{"type":"object"}}',
                    index_id="connection:jira",
                    kind="tool_metadata",
                )
            ]

    class SchemaModel(ToolModel):
        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                return {
                    "tool_calls": [
                        {
                            "name": "mcp_tool_schema",
                            "args": {"connection_id": "jira", "tool": "search"},
                        }
                    ]
                }
            return {"content": "Состояние заказа известно.", "tool_calls": []}

    integrations._remote = SchemaBackend()
    model = SchemaModel()
    runtime = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path), model=model, tools=integrations
    )
    await runtime.start()
    try:
        run = await runtime.submit(
            Principal("alice"),
            RunRequest(
                question="Каково состояние заказа?",
                mode="investigate",
                index_ids=["connection:jira"],
            ),
        )
        result = await wait_finished(runtime, run["id"])
        assert result["answer"] == "Состояние заказа известно."
        assert result["status"] == "partial"
        assert any("не подтверждён источниками" in warning for warning in result["warnings"])
        assert result["metrics"]["tool_calls"] == 1
        assert len(result["evidence"]) == 1
        assert result["evidence"][0]["kind"] == "tool_metadata"
        assert [call["name"] for call in integrations._remote.calls] == ["mcp_tool_schema"]
        assert len(model.requests) == 2
        context = json.loads(model.requests[1]["messages"][-1]["content"])
        assert context["evidence"] == result["evidence"]
    finally:
        await runtime.stop()


async def test_quick_mode_never_starts_repository_or_remote_tools(tmp_path: Path) -> None:
    integrations = hub(tmp_path)
    model = ToolModel()
    runtime = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path), model=model, tools=integrations
    )
    await runtime.start()
    try:
        run = await runtime.submit(Principal("alice"), RunRequest(question="Check", mode="quick"))
        result = await wait_finished(runtime, run["id"])
        assert result["status"] == "partial"
        assert not integrations._sandbox.calls and not integrations._remote.calls
        assert not model.requests
    finally:
        await runtime.stop()


def test_settings_http_admin_origin_validation_and_persistence(tmp_path: Path) -> None:
    integrations = hub(tmp_path)

    async def authenticate(request: Request) -> Principal | None:
        return Principal(request.headers["x-owner"]) if request.headers.get("x-owner") else None

    async def manager(request: Request) -> bool:
        return request.headers.get("x-owner") == "alice"

    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path), tools=integrations)
    app = create_app(
        runtime, authenticate, authorize_model_manager=manager, integrations=integrations
    )
    with TestClient(app) as client:
        assert client.get("/api/settings").status_code == 401
        assert client.get("/api/settings", headers={"x-owner": "bob"}).json() == {
            "can_manage": False
        }
        assert client.put("/api/settings", headers={"x-owner": "bob"}, json={}).status_code == 403
        headers = {"x-owner": "alice"}
        config = configuration()
        response = client.put("/api/settings", headers=headers, json=config)
        assert response.status_code == 200
        assert response.json()["settings"]["connections"][0]["id"] == "jira"
        assert (
            client.put(
                "/api/settings", headers={**headers, "origin": "https://other.test"}, json={}
            ).status_code
            == 403
        )
        assert (
            client.put("/api/settings", headers=headers, json={"secret": "do-not-echo"}).status_code
            == 400
        )
        for payload in (
            {"kind": []},
            {"kind": "unknown"},
            {"kind": "sandbox", "url": "http://evil"},
        ):
            assert (
                client.post("/api/settings/probe", headers=headers, json=payload).status_code == 400
            )
        assert (
            client.post(
                "/api/settings/probe", headers={"x-owner": "bob"}, json={"kind": "sandbox"}
            ).status_code
            == 403
        )
