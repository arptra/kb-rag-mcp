"""Visible research objectives and progress, never private model reasoning."""

import hashlib
import json
import re
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


def needs_source_check(run: dict[str, Any]) -> bool:
    """Autopilot needs a retained result from a follow-up source check."""
    return run["mode"] == "autonomous" and not any(
        item.get("checked_with") for item in run["evidence"]
    )


def initial_search_query(run: dict[str, Any]) -> str:
    parent = run.get("parent_question")
    question = run["question"]
    return (f"{parent}\nFollow-up: {question}" if parent else question)[:8000]


def lookup_key(name: str, args: dict[str, Any]) -> str:
    # Keep queries out of persistent diagnostic fields. Formatting and result
    # count don't turn a previously attempted lookup into a new lead.
    target = (str(args["document_id"]) if name == "read_document"
              else " ".join(str(args["query"]).casefold().split()))
    return hashlib.sha256(f"{name}\0{args['index_id']}\0{target}".encode()).hexdigest()


def next_source_lookup(
    ranked: list[dict[str, Any]], knowledge_ids: list[str],
    attempted: set[str],
) -> dict[str, Any] | None:
    """Pick an untried read or a literal lead from the same selected index."""
    sources = [item for item in ranked if item["index_id"] in knowledge_ids
               and item.get("kind", "source") == "source"]
    for item in sources:
        args = {"index_id": item["index_id"], "document_id": item.get("document_id") or item["id"]}
        if not item.get("read_document") and lookup_key("read_document", args) not in attempted:
            return {"name": "read_document", "args": args}
    for item in sources:
        # Ticket references and service names make useful literal search terms.
        # Never forward free-form document contents across source boundaries.
        for match in re.finditer(r"\b(?:[A-Z][A-Z0-9]{1,9}-[0-9]{1,10}"
                                 r"|[a-z][a-z0-9]{1,30}(?:-[a-z][a-z0-9]{1,30}){1,3})\b",
                                 item["title"] + "\n" + item["text"]):
            args = {"index_id": item["index_id"], "query": match.group()}
            if lookup_key("search_knowledge", args) not in attempted:
                return {"name": "search_knowledge", "args": args}
    return None


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
    data = [(item["index_id"], item["id"], item.get("text", ""), item.get("revision"))
            for item in run["evidence"]]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
