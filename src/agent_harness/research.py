"""Visible research objectives and progress, never private model reasoning."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    goal: str = Field(min_length=1, max_length=500)
    questions: list[str] = Field(default_factory=list, max_length=8)
    completed: list[str] = Field(default_factory=list, max_length=8)
    gaps: list[str] = Field(default_factory=list, max_length=8)
    next_action: str = Field(default="", max_length=500)
    ready_to_answer: bool = False

    @field_validator("questions", "completed", "gaps")
    @classmethod
    def bounded_items(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 500 for item in values):
            raise ValueError("Research plan items must be concise and nonempty")
        return list(dict.fromkeys(item.strip() for item in values))


PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "update_research_plan",
        "description": (
            "Maintain the visible investigation checklist. First split the user's goal into "
            "concrete checks. After new findings update completed checks and unresolved gaps. "
            "Describe observations and next actions only, not private reasoning. This tool "
            "does not perform a search: also call evidence tools to verify each claim. "
            "Mark ready_to_answer only when checks are supported or missing access is explicit."
        ),
        "parameters": ResearchPlan.model_json_schema(),
    },
}


def initialize_plan(run: dict[str, Any]) -> None:
    if run.get("research_plan"):
        return
    run["research_plan"] = {
        **ResearchPlan(
            goal=run["question"][:500], questions=[run["question"][:500]],
            next_action="Найти исходные материалы по выбранным источникам.",
        ).model_dump(),
        "round": 0, "model_updated": False, "completion_reason": "",
    }


def update_plan(run: dict[str, Any], args: Any) -> None:
    plan = ResearchPlan.model_validate(args)
    existing = run.get("research_plan", {})
    run["research_plan"] = {
        **plan.model_dump(), "round": existing.get("round", 0),
        "model_updated": True, "completion_reason": "",
    }


def evidence_signature(run: dict[str, Any]) -> str:
    """A content fingerprint catches new detail even when document IDs stay unchanged."""
    import hashlib
    import json

    data = [(item["index_id"], item["id"], item.get("text", ""), item.get("revision"))
            for item in run["evidence"]]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
