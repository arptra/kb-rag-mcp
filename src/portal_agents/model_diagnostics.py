"""Correlate sanitized provider validation failures across retries of one portal request."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4

from portal_agents.experience import profile_key
from portal_agents.model_errors import ModelGatewayError
from portal_agents.model_settings import ModelProfile

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Request:
    trace: str
    operation: str
    provider: str
    adapter: str
    connection: str


_CURRENT: ContextVar[_Request | None] = ContextVar("portal_model_diagnostics", default=None)


@contextmanager
def model_request_scope(profile: ModelProfile, operation: str) -> Iterator[None]:
    request = _Request(uuid4().hex[:16], operation, profile.provider,
                       profile.adapter if profile.provider == "api" else "-", profile_key(profile))
    token = _CURRENT.set(request)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def log_model_422(error: ModelGatewayError, *, recovery: str = "stop") -> str:
    if error.diagnostic_reference:
        return error.diagnostic_reference
    request = _CURRENT.get()
    ref = uuid4().hex[:16]
    error.diagnostic_reference = ref
    _LOG.warning(
        "model_422 ref=%s trace=%s operation=%s provider=%s adapter=%s connection=%s "
        "category=%s status=422 attempts=%d stage=%s tools=%d validation_field=%s "
        "upstream_request_id=%s retry_forbidden=%s recovery=%s diagnostics=%s",
        ref, request.trace if request else ref, request.operation if request else "agent",
        request.provider if request else "-", request.adapter if request else "-",
        request.connection[:16] if request else "-", error.category, error.attempts, error.stage,
        error.tool_count, error.validation_field or "-", error.request_id or "-",
        error.retry_forbidden, recovery, json.dumps(error.diagnostics, separators=(",", ":")),
    )
    return ref
