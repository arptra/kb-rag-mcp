"""Operator-owned ceilings and auditable resource requests for autonomous runs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_harness.research import evidence_signature
from agent_harness.store import timestamp


class BudgetChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model_calls: int | None = Field(default=None, ge=1)
    summary_calls: int | None = Field(default=None, ge=0)
    source_calls: int | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0)
    parallelism: int | None = Field(default=None, ge=1)
    document_reads: int | None = Field(default=None, ge=0)
    evidence_per_index: int | None = Field(default=None, ge=1)
    evidence_chars: int | None = Field(default=None, ge=100)
    document_chars: int | None = Field(default=None, ge=100)
    timeout_seconds: float | None = Field(default=None, gt=0)
    context_tokens: int | None = Field(default=None, ge=1024)


class BudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reason: str = Field(min_length=10, max_length=1000)
    next_action: str = Field(min_length=5, max_length=500)
    changes: BudgetChanges


BUDGET_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "request_budget",
        "description": (
            "Autopilot only. Request absolute resource limits needed to finish concrete remaining "
            "checks. Explain what the additional resources will verify and the next action. "
            "Inspect resource_budget; request only needed increases within operator ceilings. "
            "You may lower unused limits. This never grants access, changes permissions, enlarges "
            "the model's physical context window, or changes global settings. A denial is final "
            "until new evidence or an operator change; do not repeat an unchanged request."
        ),
        "parameters": BudgetRequest.model_json_schema(),
    },
}


def apply_request(run: dict[str, Any], args: Any, *, elapsed: float) -> dict[str, Any]:
    """Grant only the intersection of a model request and a persisted operator policy."""
    request = BudgetRequest.model_validate(args)
    requested = request.changes.model_dump(exclude_none=True)
    budget = run["budget"]
    current, ceiling = budget["current"], budget["ceiling"]
    policy = run["_control"]["autonomy"]
    fingerprint = evidence_signature(run)
    increases = any(value > current[key] for key, value in requested.items())
    denial = ""
    if run["mode"] != "autonomous" or not policy["enabled"]:
        denial = "Изменение бюджета доступно только в режиме «Автопилот»."
    elif not requested:
        denial = "Запрос не содержит изменений бюджета."
    elif increases and budget["extensions"] >= policy["max_extensions"]:
        denial = "Достигнуто число расширений, заданное администратором."
    elif (increases and policy["minimum_progress"]
          and run.get("_budget_signature") == fingerprint):
        denial = "После предыдущего расширения не появились новые подтверждения."
    metrics = run["metrics"]
    floors = {
        "model_calls": metrics.get("decision_calls", metrics["model_calls"]) + 1,
        "summary_calls": metrics.get("summary_calls", 0),
        "source_calls": max(0, metrics["source_calls"] - metrics.get("bootstrap_calls", 0)),
        "tool_calls": metrics["tool_calls"],
        "document_reads": metrics.get("document_reads", 0),
        "timeout_seconds": elapsed,
    }
    granted = {
        key: min(ceiling[key], max(value, floors.get(key, 0)))
        for key, value in requested.items()
    } if not denial else {}
    changed = {key: value for key, value in granted.items() if value != current[key]}
    if not denial and not changed:
        denial = "Текущий бюджет достаточен либо достигнут административный потолок."
    accepted = not denial
    event = {
        "timestamp": timestamp(), "reason": request.reason,
        "next_action": request.next_action, "requested": requested,
        "granted": changed if accepted else {}, "accepted": accepted,
        "message": denial or "Бюджет изменён в пределах разрешённых значений.",
    }
    budget["events"] = [*budget.get("events", []), event][-100:]
    if accepted:
        if any(value > current[key] for key, value in changed.items()):
            budget["extensions"] += 1
            run["_budget_signature"] = fingerprint
        current.update(changed)
    return event
