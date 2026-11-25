"""Allowlisted structural diagnostics for rejected model requests."""
from __future__ import annotations

import json
import re
from collections import Counter
from itertools import pairwise
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

_ROLES = {"assistant", "user", "system", "developer", "tool", "function"}
_FIELDS = {
    "body", "model", "messages", "tools", "functions", "function", "tool_calls", "function_call",
    "tool_choice", "role", "content", "name", "arguments", "parameters", "properties", "items",
    "required", "type", "default", "anyOf", "oneOf", "allOf", "enum", "additionalProperties",
    "description", "tool_call_id", "functions_state_id", "tools_state_id", "attachments",
    "max_tokens", "max_completion_tokens", "stream", "temperature", "top_p", "thinking",
}
_REASONS = {
    "missing", "value_error", "extra_forbidden", "enum", "literal_error", "int_type",
    "int_parsing", "string_type", "string_too_short", "string_too_long", "greater_than",
    "greater_than_equal", "less_than", "less_than_equal", "list_type", "dict_type", "model_type",
    "model_attributes_type", "union_tag_invalid", "bool_type", "float_type", "too_short",
    "too_long", "json_invalid", "invalid_request_error", "unsupported_parameter", "invalid_value",
    "invalid_type", "missing_required_parameter", "unknown_parameter", "content_filter",
    "permission_denied",
}
_PARAM = re.compile(r"[a-zA-Z_]\w*(?:\.(?:[a-zA-Z_]\w*|\d+)|\[\d+\])*", re.ASCII)
_MESSAGE_HINTS = {
    "field_required": re.compile(r"field required\b"),
    "extra_forbidden": re.compile(r"extra inputs are not permitted\b"),
    "context_length": re.compile(r"(?:this model['\u2019]s |the )?maximum context length\b"),
    "unsupported_parameter": re.compile(r"(?:unsupported|unrecognized request) parameter\b"),
    "model_not_found": re.compile(r"model not found\b"),
}


def _message_hint(*values: object) -> str:
    for value in values:
        if isinstance(value, str):
            prefix = value[:256].strip().casefold()
            for label, pattern in _MESSAGE_HINTS.items():
                if pattern.match(prefix):
                    return label
    return "unknown"


def safe_request_id(value: object) -> str | None:
    if isinstance(value, str) and len(value) in (32, 36):
        try:
            uid = UUID(value)
        except ValueError:
            return None
        if value.lower() in (uid.hex, str(uid)):
            return str(uid)
    return None


def endpoint_shape(url: object) -> dict[str, Any]:
    try:
        parts = urlsplit(str(url)).path.rstrip("/").split("/")
    except ValueError:
        parts = []
    count = sum(pair == ("chat", "completions") for pair in pairwise(parts))
    return {"kind": "chat/completions" if parts[-2:] == ["chat", "completions"] else "other",
            "chat_completions_count": count}


def _decode(body: object, limit: int) -> tuple[object, str]:
    if isinstance(body, bytes):
        if len(body) > limit:
            return None, "oversized"
        if not body.strip():
            return None, "empty"
        try:
            body = json.loads(body)
        except (ValueError, RecursionError):
            return body[:1024].decode("utf-8", errors="replace"), "text"
        if body is None:
            return None, "null"
    if isinstance(body, dict):
        return body, "object"
    if isinstance(body, list):
        return body, "array"
    if isinstance(body, str):
        return body, "string"
    return body, "unavailable" if body is None else "scalar"


def wire_shape(payload: object) -> dict[str, Any]:
    data, kind = _decode(payload, 8 * 1024 * 1024)
    shape: dict[str, Any] = {"body_type": kind}
    if isinstance(payload, bytes):
        shape["body_bytes"] = len(payload)
    if not isinstance(data, dict):
        return shape
    model = data.get("model")
    shape["model"] = ("absent" if "model" not in data else "empty" if model in (None, "") else
                      "set" if isinstance(model, str) else "invalid")
    for key in ("max_tokens", "max_completion_tokens"):
        if key in data:
            value = data[key]
            shape[key] = value if type(value) is int and 0 <= value < 2**31 else "invalid"
    if "stream" in data:
        shape["stream"] = data["stream"] if type(data["stream"]) is bool else "invalid"
    shape["thinking_present"] = "thinking" in data
    shape["function_choice_present"] = "function_call" in data or "tool_choice" in data
    for key in ("tools", "functions"):
        definitions = data.get(key, [])
        shape[key] = len(definitions) if isinstance(definitions, list) else "invalid"
    messages = data.get("messages")
    if not isinstance(messages, list):
        shape["messages"] = "absent" if "messages" not in data else "invalid"
        return shape
    roles: Counter[str] = Counter()
    calls = states = chars = 0
    for message in messages:
        if not isinstance(message, dict):
            roles["other"] += 1
            continue
        role = message.get("role")
        roles[role if isinstance(role, str) and role in _ROLES else "other"] += 1
        states += int(bool(message.get("functions_state_id") or message.get("tools_state_id")))
        calls += int(isinstance(message.get("function_call"), dict))
        batch = message.get("tool_calls")
        if isinstance(batch, list):
            calls += len(batch)
            states += sum(bool(call.get("tools_state_id")) for call in batch
                          if isinstance(call, dict))
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for block in content:
                value = block.get("text") if isinstance(block, dict) else block
                if isinstance(value, str):
                    chars += len(value)
    shape.update(messages=len(messages), roles=dict(roles), tool_calls=calls,
                 tool_results=roles["tool"] + roles["function"], state_ids=states, text_chars=chars)
    return shape


def _safe_path(raw: object) -> list[str | int]:
    if isinstance(raw, str):
        if len(raw) > 512 or not _PARAM.fullmatch(raw):
            return ["*"]
        raw = re.findall(r"\w+", raw, re.ASCII)
    if not isinstance(raw, (list, tuple)):
        return []
    path: list[str | int] = []
    for part in raw[:12]:
        if path and path[-1] in ("properties", "arguments"):
            path.append("*")
        elif ((isinstance(part, str) and len(part) <= 32 and part in _FIELDS)
              or (type(part) is int and 0 <= part <= 9999)):
            path.append(part)
        elif isinstance(part, str) and re.fullmatch(r"[0-9]{1,4}", part):
            path.append(int(part))
        else:
            path.append("*")
    return path


def validation_details(error: object) -> dict[str, Any]:
    """Report count and bounded locations without API messages, inputs or arbitrary keys."""
    body, kind = _decode(error, 65536)
    details: dict[str, Any] = {"body_type": kind, "error_count": 0,
                               "issues": [], "message_hint": _message_hint(body)}
    if isinstance(error, bytes):
        details["body_bytes"] = len(error)
    if isinstance(body, dict):
        envelope = body.get("error", body)
        details["message_hint"] = _message_hint(envelope, body.get("message"), body.get("detail"))
        if "error" in body:
            details["body_type"] = "error_object"
        if not isinstance(envelope, dict):
            details["body_type"] = "error_" + _decode(envelope, 0)[1]
            return details
        issues = next((envelope[k] for k in ("detail", "details", "errors") if k in envelope), None)
        if issues is not None:
            details["body_type"] = "detail_" + _decode(issues, 0)[1]
        if details["message_hint"] == "unknown":
            details["message_hint"] = _message_hint(envelope.get("message"), issues)
        if not isinstance(issues, (list, dict)):
            structured = "error" in body or {"param", "loc", "code", "type"} & envelope.keys()
            issues = envelope if structured else []
    else:
        issues = body if isinstance(body, list) else []
    if isinstance(issues, dict):
        issues = [issues]
    details["error_count"] = len(issues)
    for issue in issues[:5]:
        if not isinstance(issue, dict):
            details["issues"].append({"type": "other", "path": []})
            continue
        if details["message_hint"] == "unknown":
            details["message_hint"] = _message_hint(issue.get("msg"), issue.get("message"))
        reason = next((val for val in (issue.get("code"), issue.get("type"))
                       if isinstance(val, str) and val in _REASONS), "other")
        location = next((issue[k] for k in ("loc", "param", "field") if k in issue), None)
        details["issues"].append({"type": reason, "path": _safe_path(location)})
    return details


class WireDiagnostics(dict[str, Any]):
    """In-process transport metadata, assembled only from safe structural summaries."""

    def __init__(self, wire: Literal["openai", "adapter-input", "gigachat-v1"],
                 payload: object, error: object, url: object = None) -> None:
        super().__init__(wire=wire, request=wire_shape(payload),
                         validation=validation_details(error))
        if url is not None:
            self["endpoint"] = endpoint_shape(url)
