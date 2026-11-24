"""Aggregate retained run telemetry without exposing conversation or source contents."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_harness.store import ACTIVE, RunStore

COUNTERS = (
    "model_calls",
    "decision_calls",
    "source_calls",
    "tool_calls",
    "summary_calls",
    "extensions",
    "compactions",
    "input_tokens_estimated",
    "output_tokens_estimated",
)
REPORTED = ("input_tokens_reported", "output_tokens_reported")
# Project only telemetry in SQLite: retained answers, documents and checkpoints
# may each be large and must never be loaded into the admin statistics process.
_METRIC_FIELDS = (*COUNTERS, *REPORTED, "duration_ms")
_METRIC_SQL = ", ".join(
    f"'{name}', CASE WHEN json_type(data, '$.metrics.{name}') IN ('integer','real') "
    f"THEN json_extract(data, '$.metrics.{name}') ELSE NULL END"
    for name in _METRIC_FIELDS
)
_RUN_PROJECTION = (
    "json_object('id',id,'created_at',created,'status',status,"
    "'mode',json_extract(data,'$.mode'),'effort',json_extract(data,'$.effort'),"
    "'stop_reason',json_extract(data,'$.stop_reason'),"
    f"'metrics',json_object({_METRIC_SQL}),"
    "'budget',json_object('extensions',json_extract(data,'$.budget.extensions'),"
    "'stop_reason',json_extract(data,'$.budget.stop_reason')),"
    "'context_summary',json_object('compactions',json_extract(data,'$.context_summary.compactions')))"
)
STOP_LABELS = {
    "completed": "Ответ завершён",
    "time_limit": "Исчерпано время",
    "cancelled": "Остановлено пользователем",
    "failed": "Ошибка выполнения",
    "interrupted": "Прервано перезапуском",
    "budget_ceiling": "Достигнут потолок бюджета",
    "context_limit": "Подготовка контекста беседы остановлена",
    "no_new_evidence": "Новых материалов нет",
    "partial": "Ответ содержит ограничения",
    "active": "Выполняется",
}


def _number(value: Any) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return 0
    return max(0, int(value))


def _stop_reason(run: dict[str, Any]) -> str:
    status = run.get("status", "failed")
    if status in ACTIVE:
        return "active"
    if status == "timed_out":
        return "time_limit"
    if status in {"completed", "cancelled", "failed", "interrupted"}:
        return str(status)
    # Prefer stable, server-owned codes. Never return model-written reasons verbatim.
    reason = run.get("stop_reason") or run.get("budget", {}).get("stop_reason")
    if reason in STOP_LABELS:
        return str(reason)
    return "partial"


def _safe_run(run: dict[str, Any]) -> dict[str, Any]:
    metrics = run.get("metrics") or {}
    budget = run.get("budget") or {}
    context = run.get("context_summary") or {}
    result = {
        "id": str(run.get("id", "")),
        "created_at": str(run.get("created_at", "")),
        "mode": run.get("mode")
        if run.get("mode") in {"quick", "investigate", "autonomous"}
        else "unknown",
        "effort": run.get("effort")
        if run.get("effort") in {"low", "medium", "high"}
        else "unknown",
        "status": run.get("status")
        if run.get("status")
        in ACTIVE | {"completed", "partial", "failed", "timed_out", "cancelled", "interrupted"}
        else "unknown",
        "duration_ms": _number(metrics.get("duration_ms")),
        "stop_reason": _stop_reason(run),
        **{name: _number(metrics.get(name)) for name in COUNTERS},
        **{
            name: _number(metrics[name]) if metrics.get(name) is not None else None
            for name in REPORTED
        },
    }
    result["extensions"] = _number(
        budget.get("extensions")
        if budget.get("extensions") is not None
        else metrics.get("extensions")
    )
    result["compactions"] = _number(
        metrics.get("compactions")
        if metrics.get("compactions") is not None
        else context.get("compactions")
    )
    return result


def _aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    durations = sorted(run["duration_ms"] for run in runs if run["status"] not in ACTIVE)
    total_duration = sum(durations)
    result: dict[str, Any] = {
        "runs": len(runs),
        "completed": sum(run["status"] == "completed" for run in runs),
        "partial": sum(run["status"] == "partial" for run in runs),
        "failed": sum(run["status"] in {"failed", "timed_out", "interrupted"} for run in runs),
        "cancelled": sum(run["status"] == "cancelled" for run in runs),
        "active": sum(run["status"] in ACTIVE for run in runs),
        "duration_ms": total_duration,
        "duration_mean_ms": round(total_duration / len(durations)) if durations else 0,
        "duration_p95_ms": durations[max(0, math.ceil(len(durations) * 0.95) - 1)]
        if durations
        else 0,
        **{name: sum(run[name] for run in runs) for name in COUNTERS},
        **{
            name: sum(run[name] for run in runs if run[name] is not None)
            if any(run[name] is not None for run in runs)
            else None
            for name in REPORTED
        },
        "reported_usage_runs": sum(any(run[name] is not None for name in REPORTED) for run in runs),
    }
    return result


def collect_metrics(
    store: RunStore, *, days: int = 7, now: datetime | None = None
) -> dict[str, Any]:
    if not 1 <= days <= 90:
        raise ValueError("Период должен составлять от 1 до 90 дней.")
    now = now or datetime.now(UTC)
    end = now.astimezone(UTC)
    begin = end.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    with store._connect() as db:
        runs = [
            _safe_run(json.loads(row[0]))
            for row in db.execute(
                f"SELECT {_RUN_PROJECTION} FROM runs "
                "WHERE created>=? AND created<=? ORDER BY created,id",
                (begin.isoformat(), end.isoformat()),
            )
        ]
        active_runs = [
            _safe_run(json.loads(row[0]))
            for row in db.execute(
                f"SELECT {_RUN_PROJECTION} FROM runs "
                "WHERE status IN ('queued','running','cancelling') ORDER BY created,id",
            )
        ]
    by_day: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        by_day.setdefault(run["created_at"][:10], []).append(run)
    series = []
    for offset in range(days):
        day = (begin + timedelta(days=offset)).date().isoformat()
        series.append({"date": day, **_aggregate(by_day.get(day, []))})
    reasons: dict[str, int] = {}
    for run in runs:
        reasons[run["stop_reason"]] = reasons.get(run["stop_reason"], 0) + 1
    return {
        "range": {
            "days": days,
            "from": begin.isoformat(),
            "to": end.isoformat(),
            "timezone": "UTC",
        },
        "summary": {**_aggregate(runs), "active_now": len(active_runs)},
        "series": series,
        "modes": [
            {"mode": mode, **_aggregate([run for run in runs if run["mode"] == mode])}
            for mode in ("quick", "investigate", "autonomous")
        ],
        "stop_reasons": [
            {"reason": key, "label": STOP_LABELS[key], "count": value}
            for key, value in sorted(reasons.items(), key=lambda item: -item[1])
        ],
        "recent_runs": list(reversed(runs[-50:])),
        "active_runs": active_runs,
        "notes": {
            "retained_only": True,
            "duration_ms": "Сумма длительности завершённых запросов; mean/p95 — отдельно",
            "tokens_estimated": "Приблизительная оценка, не фактическое списание провайдера",
            "tokens_reported": "Данные провайдера; null означает отсутствие таких данных",
            "privacy": "Тексты бесед, источники и идентификаторы пользователей исключены",
        },
    }
