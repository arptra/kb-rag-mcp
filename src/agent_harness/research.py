"""Visible research objectives and progress, never private model reasoning."""

# ruff: noqa: RUF001  # Russian UI and linguistic patterns.

import hashlib
import json
import re
from collections.abc import Iterator
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
    if run["mode"] != "autonomous":
        return False
    candidates = [item for item in run["evidence"]
                  if item.get("retrieval_relevance") != "irrelevant"]
    if service_inventory_question(run["question"]):
        return not any(inventory_source_score(item) >= 2
                       and (item.get("checked_with") or item.get("read_document"))
                       for item in candidates)
    return not any(item.get("checked_with") for item in candidates)


_SERVICE = re.compile(r"\b(?:микро)?сервис\w*|\b(?:micro)?services?\b", re.I)
_INVENTORY_INTENT = re.compile(
    r"(?:сколько|количество|число|численность|перечень|список|какие|перечисли\w*)"
    r"\s+(?:(?:всего|сейчас|разных|активных|есть|у нас|в (?:ас|системе))\s+){0,3}"
    r"(?:микро)?сервис|(?:how many|number of|list(?: of)?|count(?: of)?)\s+"
    r"(?:active\s+)?(?:micro)?services?|\bservice inventory\b", re.I,
)
_INVENTORY = re.compile(r"реестр|каталог|перечень|список|инвентар|inventory|catalog|registry", re.I)
_SERVICE_TOTAL = re.compile(r"\b\d+\s+(?:активн\w*\s+)?(?:микро)?сервис\w*|"
                            r"\b\d+\s+(?:active\s+)?(?:micro)?services?\b|"
                            r"(?:сервис\w*|services?)\s*[:=—–-]\s*\d+\b", re.I)


def service_inventory_question(question: str) -> bool:
    return bool(_SERVICE.search(question) and _INVENTORY_INTENT.search(question))


def simple_service_count_question(question: str) -> bool:
    """Only an unqualified total. Compound, filtered and deployment questions need investigation."""
    return re.fullmatch(
        r"(?:сколько (?:всего )?(?:микро)?сервисов(?: (?:есть|зарегистрировано))?"
        r"(?: (?:в (?:этой |нашей )?(?:ас|системе|графе)|у нас))?"
        r"|how many (?:micro)?services(?: are there)?(?: in the (?:system|graph))?)[?.!]*",
        " ".join(question.split()), re.I,
    ) is not None


def inventory_source_score(item: dict[str, Any]) -> int:
    """Prioritize inventories and explicit totals over incidental service mentions."""
    if item.get("kind", "source") != "source" or item.get("tool_outcome"):
        return 0
    title, body = item["title"], item["text"]
    if _SERVICE_TOTAL.search(body) or (_SERVICE.search(title) and _INVENTORY.search(title)):
        return 3
    if re.search(r'"(?:services|service_count|total_services)"\s*:\s*(?:\[|\d)', body, re.I):
        return 3
    if re.search(r'"(?:kind|type|label)"\s*:\s*"(?:micro)?service"', body, re.I):
        return 2
    if _SERVICE.search(body) and _INVENTORY.search(body):
        return 2
    return int(bool(_SERVICE.search(title)))


def inventory_citation_issue(run: dict[str, Any], content: str) -> str | None:
    from agent_harness.answer_quality import cited_evidence_refs

    if not service_inventory_question(run["question"]):
        return None
    citations = cited_evidence_refs(content, run["evidence"], question=run["question"])
    relevant = [item for item in run["evidence"]
                if (str(item.get("index_id", "")), item["id"]) in citations
                and (run["mode"] != "autonomous" or item.get("checked_with")
                     or item.get("read_document"))
                and inventory_source_score(item) >= 2]
    if not relevant:
        return "Для ответа о составе сервисов нужен проверенный реестр, список или итог."
    if not simple_service_count_question(run["question"]):
        return None
    totals: set[int] = set()
    for item in relevant:
        try:
            payload = json.loads(item["text"])
        except (ValueError, RecursionError):
            continue
        if isinstance(payload, dict):
            for key in ("service_count", "total_services"):
                value = payload.get(key)
                if type(value) is int and value >= 0:
                    totals.add(value)
    if len(totals) > 1:
        return "В реестрах расходится число сервисов. Уточните область и актуальность подсчёта."
    if not totals:
        return None
    # A date, ticket or count inside a citation is not a claim about service count.
    plain = re.sub(r"\[[^\]\n]*\]|[*_`]", "", content.casefold())
    claims = _SERVICE_TOTAL.findall(plain)
    if re.search(r"(?:не уверен\w*|предположительно|возможно|maybe|perhaps|possibly)"
                 r"[,\s]+(?:что\s+)?\d+\b", plain):
        return "Сверьте итог с указанным в реестре числом сервисов."
    numbers = re.findall(r"(?<![\w-])\d+\b", " ".join(claims) if claims else plain)
    if not numbers:
        small = {"ноль": 0, "один": 1, "два": 2, "три": 3, "четыре": 4,
                 "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10}
        numbers = [str(small[word]) for word in re.findall(
            r"\b(" + "|".join(small) + r")\s+(?:микро)?сервис\w*\b", plain)]
    if set(numbers) != {str(next(iter(totals)))}:
        return "Приведите число сервисов из проверенного реестра и ссылку на него."
    return None


def initial_search_query(run: dict[str, Any]) -> str:
    parent = run.get("parent_question")
    question = run["question"]
    return (f"{parent}\nFollow-up: {question}" if parent else question)[:8000]


def lookup_key(name: str, args: dict[str, Any]) -> str:
    # Keep queries out of persistent diagnostic fields. Formatting and result
    # count don't turn a previously attempted lookup into a new lead.
    if name in {"search_knowledge", "read_document"} and "index_id" in args:
        target = (str(args.get("document_id", "")) if name == "read_document"
                  else " ".join(str(args.get("query", "")).casefold().split()))
        value = f"{name}\0{args['index_id']}\0{target}"
    else:
        values = dict(args)
        if name in {"mcp_list_tools", "mcp_tool_schema"}:
            values.pop("refresh", None)
        if name == "mcp_call":
            values.pop("schema_revision", None)
        if name == "mcp_list_tools":
            values.setdefault("query", "")
        value = json.dumps([name, values], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(value.encode()).hexdigest()


def next_source_lookup(
    ranked: list[dict[str, Any]], knowledge_ids: list[str],
    attempted: set[str], question: str = "",
) -> dict[str, Any] | None:
    """Pick an untried read or a literal lead from the same selected index."""
    sources = [item for item in ranked if item["index_id"] in knowledge_ids
               and item.get("kind", "source") == "source"]
    for item in sources:
        args = {"index_id": item["index_id"], "document_id": item.get("document_id") or item["id"]}
        if not item.get("read_document") and lookup_key("read_document", args) not in attempted:
            return {"name": "read_document", "args": args}
    for item in sources:
        # A named card or parameter is a search lead, not an invented document ID.
        # Search only the source that actually supplied the text.
        for query in _search_leads(item, question):
            args = {"index_id": item["index_id"], "query": query}
            if lookup_key("search_knowledge", args) not in attempted:
                return {"name": "search_knowledge", "args": args}
    return None


_REFERENCE_TERM = re.compile(
    r"(?:\b(?:карточк[аеиу]|документ[еау]?|раздел[еа]?|страниц[аеуы]|"
    r"см\.|see|card|page|document|section|refer to)\s*[:№#]?\s+"
    r"|\[\[|`)(?:[«“\"])?([\w][\w.-]{1,79})", re.I,
)
_WORDS = re.compile(r"[^\W\d_][\w-]{2,59}", re.UNICODE)
_COMMON_WORDS = {
    "как", "какой", "какая", "какое", "где", "почему", "что", "это", "для", "или", "при",
    "указан", "указана", "обзор", "the", "this", "that", "from", "with", "how", "what", "where",
    "why", "overview",
    "read", "source", "entry", "explain", "investigate", "describe", "tell",
}


def _search_leads(item: dict[str, Any], question: str) -> Iterator[str]:
    body = item["title"] + "\n" + item["text"]
    references = [name.rstrip(".") for name in _REFERENCE_TERM.findall(body)]
    links = re.findall(r"\[([^\[\]\n]{2,80})\]\([^)\n]+\)", body)
    identifiers = _CODE_IDENTIFIER.findall(body)
    explicit = [*references, *links, *identifiers]
    if explicit:
        yield from dict.fromkeys(explicit)
        return
    if not question:
        return
    terms = [word for word in _WORDS.findall(question) if word.casefold() not in _COMMON_WORDS]
    original = {term.casefold() for term in terms}
    # Also handle natural-language documents without a ticket, code symbol or link.
    # Pair a new term from the source with a term from the user's goal.
    if terms:
        excluded = original | _COMMON_WORDS
        words = _WORDS.findall(item["text"])
        words.extend(_WORDS.findall(item["title"]))
        for word in dict.fromkeys(words):
            if word.casefold() not in excluded:
                yield f"{word} {terms[-1]}"


_CODE_IDENTIFIER = re.compile(
    r"\b(?:[A-Z][A-Z0-9]{1,9}-\d{1,10}|[a-z][\w]*[A-Z][A-Za-z0-9]+|"
    r"[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]+)+|[a-z][a-z0-9]+(?:[-_][a-z0-9]+)+)\b",
    re.ASCII,
)
_SOURCE_LINE = re.compile(r"^([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*):(\d{1,6}):")


def next_external_lookup(
    run: dict[str, Any], catalog: list[dict[str, Any]], attempted: set[str],
) -> dict[str, Any] | None:
    """Derive one read-only operation from the selected schema and same-source leads."""
    definitions = {item["function"]["name"]: item["function"] for item in catalog}
    for index in run["index_ids"]:
        prefix, _, resource = index.partition(":")
        candidates: list[tuple[str, dict[str, Any]]] = []
        if prefix == "repo" and resource:
            for item in run["evidence"]:
                if item["index_id"] != index or item.get("checked_with") != "search_repository":
                    continue
                for line in item["text"].splitlines():
                    match = _SOURCE_LINE.match(line)
                    if match and ".." not in match[1].split("/"):
                        lineno = int(match[2])
                        candidates.append(("read_repository", {
                            "repository_id": resource, "path": match[1],
                            "start_line": max(1, lineno - 12), "end_line": lineno + 24,
                        }))
                        break
            for term in dict.fromkeys(_CODE_IDENTIFIER.findall(run["question"])):
                candidates.append(("search_repository", {"repository_id": resource, "query": term}))
            candidates.append(("read_repository", {"repository_id": resource, "path": "README.md"}))
        elif prefix == "connection" and resource:
            schema = definitions.get("mcp_list_tools", {}).get("parameters", {})
            properties = schema.get("properties", {})
            allowed = properties.get("connection_id", {}).get("enum", [])
            if resource not in allowed:
                continue
            for item in run["evidence"]:
                if item["index_id"] != index or item.get("kind") != "tool_metadata":
                    continue
                try:
                    data = json.loads(item["text"])
                except ValueError:
                    continue
                if not isinstance(data, dict) or data.get("kind") != (
                    "tool_catalog_not_system_evidence"
                ) or not isinstance(data.get("tools"), list):
                    continue
                for tool in data.get("tools", []):
                    if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                        candidates.append(("mcp_tool_schema", {"connection_id": resource,
                                                               "tool": tool["name"]}))
            candidates.append(("mcp_list_tools", {"connection_id": resource}))
        for name, args in candidates:
            if name in definitions and lookup_key(name, args) not in attempted:
                return {"name": name, "args": args}
    return None


def branch_question(call: dict[str, Any] | None, plan: dict[str, Any]) -> tuple[str, str]:
    if not call:
        return (next(iter(plan.get("gaps", [])), plan.get("goal", ""))[:500], "")
    args = call["args"]
    target = args.get("index_id") or args.get("repository_id") or args.get("connection_id", "")
    question = {
        "read_document": f"Что подтверждает документ {args.get('document_id', '')[:100]}?",
        "search_knowledge": f"Какие материалы уточняют {args.get('query', '')[:100]}?",
        "search_repository": f"Где в коде используется {args.get('query', '')[:100]}?",
        "read_repository": f"Что показывает файл {args.get('path', '')[:150]}?",
        "mcp_list_tools": "Какие доступные инструменты помогут проверить текущий вопрос?",
        "mcp_tool_schema": f"Какие параметры принимает {args.get('tool', '')[:100]}?",
    }[call["name"]]
    return question, target


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
        **existing, **plan.model_dump(), "round": existing.get("round", 0),
        "model_updated": True, "completion_reason": "",
    }
    if run.get("mode") == "autonomous":
        run["research_plan"]["goal"] = run["question"][:500]


def evidence_signature(run: dict[str, Any]) -> str:
    """A content fingerprint catches new detail even when document IDs stay unchanged."""
    data = [(item["index_id"], item["id"], item.get("text", ""), item.get("revision"),
             item.get("checked_with")) for item in run["evidence"] if not item.get("tool_outcome")]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
