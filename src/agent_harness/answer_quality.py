"""Checks for empty findings and the provenance of citations in Autopilot drafts."""

# ruff: noqa: RUF001  # Russian prose and linguistic patterns.

import json
import re
from typing import Any

from markdown_it import MarkdownIt
from pydantic import BaseModel, Field, ValidationError

from agent_harness.model_connection import ModelPolicyStop, check_model_policy

_MARKDOWN = MarkdownIt("commonmark", {"html": False})
_REFERENCE = re.compile(r"\[([^\[\]\n]+)\]")
_NAMED_SOURCE = re.compile(
    r"""(?:\b(?:в|из|по|согласно|from|in|according to)\s+(?:(?:the|a)\s+)?"""
    r"""(?:карточк[еи]|документ[еау]|файл[еау]|стать[еи]|инструкции|"""
    r"""источнике|document|file|article|source)\s+|"""
    r"""^\s*(?:источник|source|скачано из|получено из)\s*:\s*)"""
    r"""(?:«([^«»\n]+)»|“([^“”\n]+)”|"([^"\n]+)"|'([^'\n]+)'|`([^`\n]+)`)""",
    re.I | re.M,
)
_CLAUSES = re.compile(r"\n+|(?<=[.!?;])\s+|,\s*(?:но|однако|при этом|but|however)\s+", re.I)
_NO_FINDING = "В черновике нет результата проверки. Нужен конкретный вывод по вопросу."
_NOTICE_ROOTS = ("модел", "мнен", "позиц", "точк", "информацию", "не могу", "model", "opinion")

# These patterns classify individual clauses. A caveat or next step alongside a
# concrete finding is valid, even in a short answer. Never infer a policy block here.
_PREFACE = re.compile(r"^(?:(?:к сожалению|увы|иногда|unfortunately|sometimes)[,\s]+)+", re.I)
_BOILERPLATE = re.compile(
    r"(?:(?:генеративные|языковые|ии)[- ]модели\s+(?:иногда\s+)?(?:ошибаются|"
    r"(?:могут|способны) (?:ошибаться|.{0,50}(?:ошибочн|ошибки|некорректн|неточн|недостоверн)))|"
    r"(?:как|будучи) (?:языковая модель|модель ии)|"
    r"(?:я |у меня )?(?:не (?:имею|бывает)|нет) (?:своего |личного |собственного )?мнения|"
    r"(?:(?:данн\w*|этот|это|сгенерированн\w*) )?"
    r"(?:ответ|результат генерации|текст|информация|мнени\w+|позиция)\b.{0,80}"
    r"не (?:отража\w+|выража\w+|представля\w+|совпада\w+ с|явля\w+(?: выражением)?) "
    r"(?:официальн\w* |личн\w* )?(?:мнен|позици|точк\w* зрения)|"
    r"(?:рекомендую |пожалуйста,? )?(?:перепроверьте|проверяйте|уточняйте|проверьте) "
    r"(?:эту |полученную )?информацию(?: самостоятельно| в источниках)?$|"
    r"(?:generative|language|ai) models? (?:sometimes )?(?:can|may) "
    r"(?:make mistakes|.{0,45}(?:errors|incorrect|inaccurate))|as an ai (?:language )?model)",
    re.I,
)
_PLAN = re.compile(
    r"(?:(?:вот |мой )?план(?: (?:действий|проверки|исследования|расследования|работы))?"
    r"(?=$|:)|(?:следующие|дальнейшие) шаги(?=$|:)|"
    r"(?:для начала |затем |далее |сначала |сейчас )?(?:я |мы )?"
    r"(?:проверю|проверим|найду|найдём|изучу|изучим|проанализирую|посмотрю|прочитаю|поищу|"
    r"запрошу|соберу|сверю|сопоставлю|уточню|подготовлю|приступлю|планирую|собираюсь)\b|"
    r"(?:my |the |research )?plan(?=$|:)|(?:next,? |first,? |then )?"
    r"(?:i(?:'ll| will)|we(?:'ll| will)|i plan to)\s+)",
    re.I,
)
_PLAN_TASK = re.compile(
    r"(?:проверить|найти|собрать|изучить|сверить|сопоставить|прочитать|проанализировать|"
    r"запросить|подготовить|уточнить|провести|поиск|проверка|изучение|сбор|анализ|"
    r"сопоставление|подготовка|check|search|read|collect|verify)\b", re.I,
)
_LABEL = re.compile(
    r"(?:источники|подтверждения|ответ|результат|выводы?|итог|sources|answer|result|findings)"
    r"\s*:?$", re.I,
)


def is_answer_source(item: dict[str, Any]) -> bool:
    """Exclude tool descriptions, empty excerpts and repository checkout receipts."""
    if (item.get("kind", "source") != "source" or item.get("tool_outcome") in {"failed", "empty"}
            or not item.get("text", "").strip()):
        return False
    # Retained runs predate the distinction between a checkout receipt and file content.
    index = item.get("index_id", "")
    if index.startswith("repo:"):
        prefix = index.removeprefix("repo:") + "/"
        doc_id = str(item["id"])
        if doc_id.startswith(prefix):
            _revision, _, path = doc_id[len(prefix):].partition("/")
            if path == "checkout":
                return False
    return str(item.get("checked_with", "")) != "clone_repository"


def answer_quality_issue(run: dict[str, Any], content: str) -> str | None:
    """Identify a standalone disclaimer, or a missing finding/citation in Autopilot."""
    sources = None
    if run.get("mode") == "autonomous":
        sources = {item["id"] for item in run.get("evidence", [])
                   if item.get("id") and is_answer_source(item)}
    return _draft_issue(content, sources)


def format_source_reference(run: dict[str, Any], content: str) -> str:
    """Resolve a writer's explicit quoted source name or a verbatim sourced answer."""
    if (run.get("mode") != "autonomous" or "[" in content or not content.strip()
            or content.lstrip().startswith(("{", "```"))):
        return content
    sources = [item for item in run.get("evidence", []) if is_answer_source(item)
               and (item.get("checked_with") or item.get("read_document"))
               and item.get("id") and not re.search(r"[\[\]\r\n]", item["id"])]

    def resolve(match: re.Match[str]) -> str:
        name = next(group for group in match.groups() if group is not None)
        matches = [item for item in sources if name in (item["id"], item.get("title"))]
        if len(matches) == 1:
            return f"{match[0]} [{matches[0]['id']}]"
        return match[0]

    result = _NAMED_SOURCE.sub(resolve, content)
    if result != content:
        return result
    # An exact complete source sentence needs no inference about which fact it supports.
    sentence = " ".join(content.strip().split())
    if len(sentence) >= 16:
        matches = [item for item in sources if sentence in (
            " ".join(part.split()) for part in re.split(r"(?<=[.!?])\s+|\n+", item["text"])
        )]
        if len(matches) == 1:
            return f"{content} [{matches[0]['id']}]"
    return content


class SourceQuote(BaseModel):
    source_id: str = Field(min_length=1, max_length=512)
    quote: str = Field(min_length=8, max_length=2000)


class QuotedFindings(BaseModel):
    findings: list[SourceQuote] = Field(min_length=1, max_length=8)


def render_quoted_findings(run: dict[str, Any], content: str) -> str | None:
    """Publish only verbatim excerpts found in the unique, verified current source."""
    body = content.strip()
    if body.startswith("```") and body.endswith("```"):
        body = body.partition("\n")[2].removesuffix("```").strip()
    try:
        report = QuotedFindings.model_validate_json(body)
    except ValidationError:
        return None
    parts = []
    for item in report.findings:
        ref = item.source_id
        if re.search(r"[\r\n\[\]]", ref):
            return None
        sources = [source for source in run["evidence"] if source["id"] == ref
                   and is_answer_source(source)
                   and (source.get("read_document") or source.get("checked_with"))]
        quote = " ".join(item.quote.split())
        if len(sources) != 1 or len(quote) < 8 or quote not in " ".join(sources[0]["text"].split()):
            return None
        parts.append(f"{quote} [{ref}]")
    return "\n\n".join(parts)


def quote_options(evidence: list[dict[str, Any]], question: str) -> dict[str, SourceQuote]:
    """Offer bounded, contiguous source passages for an extractive final answer."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    split = RecursiveCharacterTextSplitter(chunk_size=540, chunk_overlap=60)
    terms = set(re.findall(r"[\w-]{3,}", question.casefold()))
    choices: dict[str, SourceQuote] = {}
    for item in evidence[:8]:
        if not 0 < len(item["id"]) <= 512 or re.search(r"[\[\]\r\n]", item["id"]):
            continue
        chunks = split.split_text(item["text"])
        chunks.sort(key=lambda s: sum(term in s.casefold() for term in terms), reverse=True)
        for chunk in chunks[:2]:
            if len(chunk) >= 8:
                choices[str(len(choices) + 1)] = SourceQuote(source_id=item["id"], quote=chunk)
    return choices


def selected_quotes(numbers: Any, choices: dict[str, SourceQuote]) -> QuotedFindings | None:
    # Some models serialize an array argument as a string even through native
    # tool calling. Only decimal IDs from the offered set are meaningful here.
    if isinstance(numbers, str) and re.fullmatch(r"""[\[\]\d\s,'"]{1,128}""", numbers):
        try:
            numbers = json.loads(numbers.replace("'", '"'))
        except ValueError:
            return None
    if type(numbers) in (str, int):
        numbers = [numbers]
    if (not isinstance(numbers, list) or not 0 < len(numbers) <= 8
            or any(type(n) not in (int, str) or str(n) not in choices for n in numbers)):
        return None
    return QuotedFindings(findings=[choices[n] for n in dict.fromkeys(str(n) for n in numbers)])


def answer_for_history(content: str | None) -> str:
    """Project stored assistant output for reuse without modifying the source record."""
    if not content:
        return ""
    # Retrieval may rank thousands of ordinary old answers in one request. Only
    # notices need sentence parsing and a policy check.
    folded = content.casefold()
    if not any(root in folded for root in _NOTICE_ROOTS):
        return content
    try:
        check_model_policy({"content": content}, None)
    except ModelPolicyStop:
        return ""
    return "" if _draft_issue(content, None) else content


def _draft_issue(content: str, sources: set[str] | None) -> str | None:
    if sources is None and not _BOILERPLATE.search(content):
        return None
    has_finding, saw_boilerplate, references = _inspect_markdown(content, sources)
    if not has_finding and (sources is not None or saw_boilerplate):
        return _NO_FINDING
    if sources is None:
        return None
    if references - sources:
        return "В ответе есть ссылка без подтверждающего материала в этом исследовании."
    if not references:
        return "Укажите точный идентификатор проверенного материала в квадратных скобках."
    return None


def cited_evidence_ids(content: str, evidence: list[dict[str, Any]]) -> set[str]:
    """Credit the same literal source references the answer validator recognizes."""
    sources = {item["id"] for item in evidence if item.get("id") and is_answer_source(item)}
    if not sources or "[" not in content:
        return set()
    _, _, references = _inspect_markdown(content, sources)
    return references & sources


def _inspect_markdown(
    content: str, sources: set[str] | None,
) -> tuple[bool, bool, set[str]]:
    references: set[str] = set()

    def collect(match: re.Match[str]) -> str:
        ref = match[1].strip()
        if sources is not None and ref in sources:
            references.add(ref)
        elif ref and not (ref in {"x", "X"} and match.start() == 0
                          and match.string.startswith(" ", match.end())):
            references.update(part.strip() for part in re.split(r"[,;]\s*", ref))
        return " "

    has_finding = saw_boilerplate = in_plan = heading = False
    for block in _MARKDOWN.parse(content):
        if block.type in {"heading_open", "heading_close"}:
            heading = block.type == "heading_open"
        if block.type in {"fence", "code_block"} and block.content.strip():
            has_finding = True  # Code can answer a question. Brackets inside are literal.
        if block.type != "inline":
            continue
        chunks: list[str] = []
        in_link = False
        for token in block.children or []:
            if token.type in {"link_open", "link_close"}:
                in_link = token.type == "link_open"
            elif token.type == "text":
                chunks.append(token.content if in_link else _REFERENCE.sub(collect, token.content))
            elif token.type == "code_inline":
                chunks.append(token.content)
            elif token.type in {"softbreak", "hardbreak"}:
                chunks.append("\n")
        for part in _CLAUSES.split("".join(chunks)):
            clause = _PREFACE.sub("", part.strip(" \t.:!?;—–-"))
            if not re.search(r"\w", clause):
                continue
            if _BOILERPLATE.match(clause):
                saw_boilerplate = True
            elif _PLAN.match(clause):
                in_plan = True
            elif heading:
                in_plan = False
                # A single sourced heading can itself be the complete answer.
                if _REFERENCE.search(block.content) and not _LABEL.fullmatch(clause):
                    has_finding = True
            elif not (_LABEL.fullmatch(clause) or (in_plan and _PLAN_TASK.match(clause))):
                has_finding = True

    return has_finding, saw_boilerplate, references
