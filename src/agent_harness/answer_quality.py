"""Checks for substantive findings and the provenance of citations in model answers."""

# ruff: noqa: RUF001  # Russian prose and linguistic patterns.

import json
import re
from decimal import Decimal
from typing import Any

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_harness.contracts import ModelDecisionError
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
_DURATION_QUESTION = re.compile(
    r"\b(?:сколько (?:миллисекунд|секунд|минут)|каков\w* таймаут|какой таймаут|"
    r"how many (?:milli)?seconds|what is the (?:read |write )?timeout)\b", re.I,
)
_DURATION = re.compile(
    r"(?<![\w.,-])(?P<n>\d{1,12}(?:[.,]\d{1,6})?)\s*"
    r"(?P<unit>мс|миллисекунд\w*|ms|milliseconds?|с|сек|секунд\w*|s|sec|seconds?|"
    r"мин|минут\w*|min|minutes?)\b", re.I,
)
_ASKS_JSON = re.compile(
    r"\b(?:в (?:формате|виде)|как|верни|покажи|выведи|пришли|in|as|return|output|show)"
    r"\s+(?:a |an )?json\b|\bjson[- ]формат\w*\b", re.I,
)
_RETRIEVAL_FIELDS = {
    "id", "index_id", "document_id", "source_id", "title", "citation", "citation_number",
    "url", "revision", "text", "kind", "context_truncated", "read_document", "checked_with",
}


def _seconds(value: re.Match[str]) -> Decimal:
    unit = value["unit"].casefold()
    multiplier = (Decimal("0.001") if unit.startswith(("мс", "миллисекунд", "ms", "millisecond"))
                  else Decimal(60) if unit.startswith(("мин", "min"))
                  else Decimal(1))
    return Decimal(value["n"].replace(",", ".")) * multiplier


# Search receipts can accompany a finding but cannot stand in for the finding.
# Match a whole clause: “нашёл документы, где указан таймаут” contains a real claim.
_SEARCH_RECEIPT = re.compile(
    r"(?:(?:я |мы )?(?:наш[её]л|нашла|нашли|проверил|посмотрел|изучил)\s+|"
    r"(?:найден[оы]?|собран[оы]?|подобран[оы]?|получен[оы]?)\s+)"
    r"(?:(?:\d+|несколько|следующие|релевантные|подходящие|нужные)\s+)*"
    r"(?:материал\w*|источник\w*|документ\w*|фрагмент\w*)"
    r"(?:\s+по\s+(?:(?:вашему|этому|данному)\s+)?(?:вопросу|запросу|теме))?"
    r"|(?:подробности|подробнее|см\.|смотрите|смотри(?:те)? подробности)\s+"
    r"(?:в (?:источниках|материалах|документах)|ниже|справа)"
    r"|(?:I |we )?(?:found|retrieved) (?:\d+ |some |relevant )*(?:sources|documents|materials)"
    r"(?: for (?:the |your )?(?:question|query))?", re.I,
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


def requested_json(question: str) -> bool:
    return bool(_ASKS_JSON.search(question))


_TECHNICAL_NAME = re.compile(
    r"\b(?:(?:как называется|како(?:й|е|ая))\s+(?:(?:этот|это|эта)\s+)?"
    r"(?:событие|топик|поле|сервис|метод|класс|таблица|эндпоинт|параметр|функция)|"
    r"(?:what|which|what is the name of (?:the|this))\s+"
    r"(?:event|topic|field|service|method|class|table|endpoint|parameter|function))\b", re.I,
)
_CODE_NAME = re.compile(r"(?<!`)`([a-zA-Z_][\w./:-]{1,179})`(?!`)")


def named_values(question: str, evidence: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Literal identifiers mentioned by the documents for a technical name lookup."""
    if not _TECHNICAL_NAME.search(question) or requested_json(question):
        return {}
    result: dict[str, list[str]] = {}
    for item in evidence:
        if not is_answer_source(item):
            continue
        for name in dict.fromkeys(_CODE_NAME.findall(item["text"])):
            if name.casefold() not in question.casefold():
                result.setdefault(name, []).append(item["id"])
    return result


class WriterAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer: str = Field(min_length=1)
    sources: list[int] = Field(min_length=1)


class InvalidWriterAnswer(ModelDecisionError):
    """A final answer could not be tied to the sources shown in its particular request."""


def decode_writer_answer(content: str, visible: dict[int, str]) -> str:
    """Read a text-only answer envelope and attach only its selected, offered citations."""
    raw = content.strip()
    if raw.startswith(("```json\n", "```\n")):
        fenced = raw.partition("\n")[2].strip()
        if '"answer"' not in fenced and '"sources"' not in fenced:
            return content  # Ordinary code remains available in a Markdown answer.
        raw = fenced.removesuffix("```").strip()
    if not raw.startswith("{"):
        if re.search(r"""(?:^|\n)\s*(?:[^\n{}]*:\s*)?\{\s*"(?:answer|sources)"\s*:""", raw):
            raise InvalidWriterAnswer("Модель добавила текст вне итогового JSON-объекта.")
        return content
    try:
        writer = WriterAnswer.model_validate_json(raw)
    except ValidationError:
        raise InvalidWriterAnswer("Модель не передала текст и номера подтверждающих источников.") \
            from None
    if any(n not in visible for n in writer.sources):
        raise InvalidWriterAnswer("В ответе указан номер источника вне найденных фрагментов.")
    text = writer.answer.strip()
    check_model_policy({"content": text}, None)
    selected = list(dict.fromkeys(visible[n] for n in writer.sources))
    _, _, refs, metadata = _inspect_markdown(text, set(selected))
    if metadata or refs - set(selected):
        raise InvalidWriterAnswer("Ссылки в тексте расходятся с выбранными источниками.")
    suffix = " ".join(f"[{ref}]" for ref in selected if ref not in refs)
    return f"{text} {suffix}".strip()


def _source_references(evidence: list[dict[str, Any]]) -> dict[str, str]:
    # Ordinals match the numbered cards in this run's UI. A real ID, even that
    # of an unavailable source, always takes precedence over an ordinal.
    ids = {item.get("id") for item in evidence}
    refs = {}
    for number, item in enumerate(evidence, start=1):
        if item.get("id") and is_answer_source(item):
            ref = str(item["id"])
            refs[ref] = ref
            if str(number) not in ids:
                refs[str(number)] = ref
    return refs


def answer_quality_issue(run: dict[str, Any], content: str) -> str | None:
    """A finished source-backed answer must contain a finding and cite current evidence."""
    sources = None
    if run.get("mode") == "autonomous" or run.get("evidence"):
        sources = set(_source_references(run.get("evidence", [])))
    issue = _draft_issue(
        content, sources, require_citation=bool(sources) or run.get("mode") == "autonomous",
        requested_json=requested_json(run.get("question", "")),
    )
    if not issue and run.get("mode") in {"quick", "investigate"} and sources:
        options = named_values(run.get("question", ""), run["evidence"])
        if options:
            cited = cited_evidence_ids(content, run["evidence"])
            body = _REFERENCE.sub("", content)
            if not any(re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-]|\.\w)", body)
                       and cited.intersection(refs) for name, refs in options.items()):
                return "В ответе отсутствует точное название из указанного источника."
    if issue or not sources or not _DURATION_QUESTION.search(run.get("question", "")):
        return issue
    findings = {_seconds(m) for m in _DURATION.finditer(_REFERENCE.sub("", content))}
    if len(findings) == 1:
        cited = cited_evidence_ids(content, run["evidence"], question=run.get("question", ""))
        measured = {_seconds(m) for item in run["evidence"]
                    if item["id"] in cited and is_answer_source(item)
                    for m in _DURATION.finditer(item["text"])}
        if len(measured) == 1 and findings != measured:
            return "Указанное время расходится со значением в приведённом источнике."
    return None


def format_source_reference(run: dict[str, Any], content: str) -> str:
    """Resolve a writer's explicit quoted source name or a verbatim sourced answer."""
    if (run.get("mode") not in {"quick", "investigate", "autonomous"}
            or "[" in content or not content.strip()
            or content.lstrip().startswith(("{", "```"))):
        return content
    sources = [item for item in run.get("evidence", []) if is_answer_source(item)
               and (run["mode"] != "autonomous" or item.get("checked_with")
                    or item.get("read_document"))
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
    duration = _DURATION.fullmatch(sentence.rstrip("."))
    if duration and _DURATION_QUESTION.search(run.get("question", "")):
        measured = [(item["id"], {_seconds(m) for m in _DURATION.finditer(item["text"])})
                    for item in sources if _DURATION.search(item["text"])]
        if len(measured) == 1 and measured[0][1] == {_seconds(duration)}:
            return f"{content} [{measured[0][0]}]"
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


def _draft_issue(
    content: str, sources: set[str] | None, *, require_citation: bool = True,
    requested_json: bool = False,
) -> str | None:
    if sources is None and not _BOILERPLATE.search(content):
        return None
    has_finding, saw_boilerplate, references, metadata = _inspect_markdown(
        content, sources, requested_json=requested_json,
    )
    if metadata:
        return "Вместо полей источника напишите вывод и поставьте ссылку рядом с ним."
    if not has_finding and (sources is not None or saw_boilerplate):
        return _NO_FINDING
    if sources is None:
        return None
    if references - sources:
        return "В ответе есть ссылка без подтверждающего материала в этом исследовании."
    if not references and require_citation:
        return "Укажите точный идентификатор проверенного материала в квадратных скобках."
    return None


def cited_evidence_ids(
    content: str, evidence: list[dict[str, Any]], *, question: str = "",
) -> set[str]:
    """Credit the same literal source references the answer validator recognizes."""
    if "[" not in content:
        return set()
    aliases = _source_references(evidence)
    _, _, references, _ = _inspect_markdown(
        content, set(aliases), requested_json=requested_json(question),
    )
    return {aliases[ref] for ref in references if ref in aliases}


def _source_record(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")) or not (
        '"citation' in stripped or '"sources"' in stripped
    ):
        return False
    try:
        # A weak writer may copy a source object and append an extra brace or
        # unsupported prose. Examine the decoded prefix even in that case.
        value, _end = json.JSONDecoder().raw_decode(stripped)
    except (ValueError, RecursionError):
        return False
    rows = value if isinstance(value, list) else [value]
    return bool(rows) and all(isinstance(row, dict) and (
        row.keys() == {"answer", "sources"} or (
            row.keys() <= _RETRIEVAL_FIELDS
            and bool(row.keys() & {"id", "source_id", "document_id", "index_id"})
            and bool(row.keys() & {"citation", "citation_number"})
        )
    ) for row in rows)


def _inspect_markdown(
    content: str, sources: set[str] | None, *, requested_json: bool = False,
) -> tuple[bool, bool, set[str], bool]:
    references: set[str] = set()

    def collect(match: re.Match[str]) -> str:
        ref = match[1].strip()
        if sources is not None and ref in sources:
            references.add(ref)
        elif ref and not (ref in {"x", "X"} and match.start() == 0
                          and match.string.startswith(" ", match.end())):
            references.update(part.strip() for part in re.split(r"[,;]\s*", ref))
        return " "

    def collect_json(payload: str) -> bool:
        try:
            pending = [json.loads(payload)]
        except (ValueError, RecursionError):
            return False
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, str):
                _REFERENCE.sub(collect, value)
        return True

    if requested_json and content.lstrip().startswith(("{", "[")) and collect_json(content):
        return True, False, references, False
    has_finding = saw_boilerplate = in_plan = heading = metadata = False
    for block in _MARKDOWN.parse(content):
        if block.type in {"heading_open", "heading_close"}:
            heading = block.type == "heading_open"
        if block.type in {"fence", "code_block"} and block.content.strip():
            has_finding = True  # Code can answer a question. Brackets inside are literal.
            if requested_json and block.info.strip().casefold() == "json":
                # Literals in ordinary code do not credit a citation. A requested
                # JSON result can return its real source reference inside the payload.
                collect_json(block.content)
        if block.type != "inline":
            continue
        if _source_record(block.content):
            metadata = True
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
            if _BOILERPLATE.match(clause) or _SEARCH_RECEIPT.fullmatch(clause):
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

    return has_finding, saw_boilerplate, references, metadata
