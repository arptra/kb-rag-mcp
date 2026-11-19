from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from corporate_kb.a2a.compat import (
    LEGACY_METHOD_MAP,
    legacy_card,
    legacy_request,
    legacy_result,
)
from corporate_kb.a2a.service import A2AService, RpcError
from corporate_kb.a2a.store import A2AStore


def request(**configuration):
    return {
        "message": {
            "kind": "message",
            "role": "user",
            "messageId": "stable-message-id",
            "parts": [{"kind": "text", "text": "limits"}],
            "metadata": {"role": "user", "kind": "message"},
        },
        "configuration": configuration,
    }


@pytest.fixture
def service(tmp_path):
    store = A2AStore(tmp_path)
    issued = store.issue("Legacy agent", ["allowed"])
    agent = store.authenticate(issued["token"])
    engine = A2AService(store, SimpleNamespace(has_index=lambda value: value == "allowed"))
    engine.retrieve = lambda agent, command: {"query": command["query"]}
    yield engine, agent
    engine.stop()


def test_request_conversion_is_stable_and_does_not_mutate_input():
    original = request(blocking=True, historyLength=2)
    original["message"]["parts"].append(
        {"kind": "data", "data": {"action": "search", "role": "user", "kind": "message"}}
    )
    snapshot = deepcopy(original)
    method, converted = legacy_request("message/send", original)
    assert method == "SendMessage"
    assert converted["message"]["role"] == "ROLE_USER"
    assert "kind" not in converted["message"]
    assert converted["message"]["messageId"] == "stable-message-id"
    assert converted["message"]["metadata"] == original["message"]["metadata"]
    assert converted["message"]["parts"][1]["data"] == original["message"]["parts"][1]["data"]
    assert converted["configuration"] == {"returnImmediately": False, "historyLength": 2}
    assert legacy_request("message/send", original) == (method, converted)
    converted["message"]["parts"][1]["data"]["role"] = "changed"
    assert original == snapshot


@pytest.mark.parametrize("blocking", [False, True])
def test_blocking_configuration(blocking):
    _, converted = legacy_request("message/send", request(blocking=blocking))
    assert converted["configuration"]["returnImmediately"] is (not blocking)


@pytest.mark.parametrize("blocking", [None, 0, 1, "false", [], {}])
def test_invalid_blocking_is_not_silently_ignored(blocking):
    with pytest.raises(ValueError, match="blocking must be a boolean"):
        legacy_request("message/send", request(blocking=blocking))


@pytest.mark.parametrize(
    "field,value",
    [("kind", None), ("kind", "task"), ("role", "ROLE_USER"), ("role", "agent")],
)
def test_invalid_message_discriminator_or_role(field, value):
    params = request()
    params["message"][field] = value
    with pytest.raises(ValueError):
        legacy_request("message/send", params)


@pytest.mark.parametrize(
    "part",
    [
        {"text": "missing kind"},
        {"kind": "text", "data": {}},
        {"kind": "data", "text": "wrong kind"},
        {"kind": "text", "text": "ambiguous", "data": {}},
        {"kind": "text", "text": "ambiguous", "url": "https://example.test"},
        {"kind": "text", "text": 1},
        {"kind": "data", "data": []},
        {"kind": "unknown", "data": {}},
        "not an object",
    ],
)
def test_invalid_parts_are_rejected(part):
    params = request()
    params["message"]["parts"] = [part]
    with pytest.raises(ValueError):
        legacy_request("message/send", params)


def test_file_part_is_explicitly_unsupported():
    params = request()
    params["message"]["parts"] = [{"kind": "file", "file": {"uri": "https://example.test"}}]
    with pytest.raises(RpcError) as error:
        legacy_request("message/send", params)
    assert error.value.code == -32005


@pytest.mark.parametrize("field", ["returnImmediately", "taskPushNotificationConfig"])
def test_native_configuration_is_rejected_in_legacy_request(field):
    with pytest.raises(ValueError, match=r"A2A 0\.3 configuration"):
        legacy_request("message/send", request(**{field: False}))


def test_legacy_push_configuration_reaches_core_unsupported_error(service):
    engine, agent = service
    method, params = legacy_request(
        "message/send", request(pushNotificationConfig={"url": "https://callback.test"})
    )
    with pytest.raises(RpcError) as error:
        engine.dispatch(agent, method, params)
    assert error.value.code == -32003


@pytest.mark.parametrize("push", [None, "https://callback.test", False])
def test_invalid_push_configuration_is_rejected(push):
    with pytest.raises(ValueError, match="pushNotificationConfig must be an object"):
        legacy_request("message/send", request(pushNotificationConfig=push))


@pytest.mark.parametrize(
    "method,code",
    [
        ("message/stream", -32004),
        ("tasks/resubscribe", -32004),
        ("tasks/pushNotificationConfig/set", -32003),
        ("tasks/pushNotificationConfig/get", -32003),
        ("tasks/pushNotificationConfig/list", -32003),
        ("tasks/pushNotificationConfig/delete", -32003),
        ("agent/getAuthenticatedExtendedCard", -32007),
    ],
)
def test_unsupported_optional_methods_have_capability_errors(service, method, code):
    engine, agent = service
    canonical, params = legacy_request(method, {})
    assert canonical == LEGACY_METHOD_MAP[method]
    with pytest.raises(RpcError) as error:
        engine.dispatch(agent, canonical, params)
    assert error.value.code == code


def test_unknown_methods_preserved_for_http_diagnostics():
    original = {"nested": {"text": "unchanged"}}
    method, params = legacy_request("tools/list", original)
    assert method == "tools/list"
    assert params == original
    params["nested"]["text"] = "changed"
    assert original["nested"]["text"] == "unchanged"


def test_legacy_send_get_and_idempotent_retry_use_same_task(service):
    engine, agent = service
    original = request(blocking=True)
    method, params = legacy_request("message/send", original)
    result = engine.dispatch(agent, method, params)
    task = legacy_result("message/send", result)
    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["history"][0]["kind"] == "message"
    assert task["history"][0]["role"] == "user"
    assert task["history"][0]["parts"] == [{"kind": "text", "text": "limits"}]
    assert task["artifacts"][0]["parts"] == [{"kind": "data", "data": {"query": "limits"}}]
    retry = engine.dispatch(agent, *legacy_request("message/send", original))
    assert retry["task"]["id"] == task["id"]
    fetched = engine.dispatch(agent, *legacy_request("tasks/get", {"id": task["id"]}))
    assert legacy_result("tasks/get", fetched) == task
    assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "kind" not in result["task"]


def test_result_converts_protocol_objects_only_and_is_independent():
    data = {
        "role": "ROLE_AGENT",
        "state": "TASK_STATE_FAILED",
        "kind": "text",
        "task": {"status": {"state": "TASK_STATE_FAILED"}},
        "parts": [{"text": "this is knowledge, not a protocol part"}],
    }
    message = {"role": "ROLE_AGENT", "messageId": "m", "parts": [{"data": data}]}
    task = {
        "id": "t",
        "contextId": "c",
        "status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": deepcopy(message)},
        "history": [deepcopy(message)],
        "metadata": deepcopy(data),
        "artifacts": [{"artifactId": "a", "parts": [{"data": deepcopy(data)}]}],
    }
    snapshot = deepcopy(task)
    converted = legacy_result("GetTask", task)
    assert converted["status"]["state"] == "input-required"
    assert converted["status"]["message"]["role"] == "agent"
    assert converted["status"]["message"]["parts"][0] == {"kind": "data", "data": data}
    assert converted["history"][0]["parts"][0]["data"] == data
    assert converted["artifacts"][0]["parts"][0]["data"] == data
    assert converted["metadata"] == data
    converted["artifacts"][0]["parts"][0]["data"]["role"] = "changed"
    assert task == snapshot


@pytest.mark.parametrize(
    "native,legacy",
    [
        ("TASK_STATE_CANCELED", "canceled"),
        ("TASK_STATE_AUTH_REQUIRED", "auth-required"),
        ("TASK_STATE_UNSPECIFIED", "unknown"),
        ("TASK_STATE_FAILED", "failed"),
    ],
)
def test_task_state_serialization(native, legacy):
    assert legacy_result("tasks/cancel", {"status": {"state": native}}) == {
        "kind": "task",
        "status": {"state": legacy},
    }


def test_send_direct_message_result():
    result = {"message": {"messageId": "m", "role": "ROLE_AGENT", "parts": [{"text": "ok"}]}}
    assert legacy_result("SendMessage", result) == {
        "kind": "message",
        "role": "agent",
        "messageId": "m",
        "parts": [{"kind": "text", "text": "ok"}],
    }


def test_legacy_card_discovery_and_security_shape(service):
    engine, _ = service
    current = engine.card("https://example.test/a2a")
    snapshot = deepcopy(current)
    current["signatures"] = [{"signature": "invalid-after-conversion"}]
    converted = legacy_card(current)
    assert converted["protocolVersion"] == "0.3.0"
    assert "message/send" in converted["description"]
    assert "result.artifacts" in converted["description"]
    assert "ROLE_USER" not in converted["description"]
    assert converted["url"] == "https://example.test/a2a"
    assert converted["preferredTransport"] == "JSONRPC"
    assert converted["additionalInterfaces"] == [
        {"url": "https://example.test/a2a", "transport": "JSONRPC"}
    ]
    assert converted["securitySchemes"] == {
        "agentToken": {"type": "http", "scheme": "Bearer", "bearerFormat": "opaque"}
    }
    assert converted["security"] == [{"agentToken": []}]
    assert converted["supportsAuthenticatedExtendedCard"] is False
    assert converted["skills"] == snapshot["skills"]
    assert "securityRequirements" not in converted
    assert "supportedInterfaces" not in converted
    assert "extendedAgentCard" not in converted["capabilities"]
    assert "signatures" not in converted
    current.pop("signatures")
    assert current == snapshot
