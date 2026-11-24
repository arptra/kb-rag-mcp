"""Checks for substantive findings and the provenance of citations in model answers."""

# ruff: noqa: RUF001  # Russian prose and linguistic patterns.

import json
import re
from collections import Counter
from decimal import Decimal
from typing import Any, Self

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent_harness.contracts import ModelDecisionError
from agent_harness.mcp_catalog import question_record_id
from agent_harness.model_connection import ModelPolicyStop, check_model_policy

_MARKDOWN = MarkdownIt("commonmark", {"html": False})
_REFERENCE = re.compile(r"\[([^\[\]\n]+)\]")
# An unexpected word from a third writing system in otherwise Russian prose can
# indicate decoder corruption. Latin identifiers, Greek formulas, and code are common.
_OTHER_WRITING = re.compile(
    r"[\u0590-\u05ff\u0600-\u06ff\u0900-\u097f\u0e00-\u0e7f"
    r"\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]{2,}|"
    r"\b[A-Za-zА-Яа-яЁё]*(?:[а-яё][a-z]|[a-z][а-яё])[A-Za-zА-Яа-яЁё]*\b"
)
_LANGUAGE_TASK = re.compile(
    r"\b(?:перевед\w*|перевод\w*|транслит\w*|translate|английск\w*|корейск\w*|"
    r"китайск\w*|арабск\w*|японск\w*|иврит\w*|хинди|тайск\w*)\b", re.I,
)
_ASKS_WHY_TOO = re.compile(r"\b(?:и (?:почему|зачем)|and why)\b", re.I)
_REASON = re.compile(
    r"\b(?:потому что|поскольку|так как|чтобы|из-за|because|so that|in order to)\b", re.I,
)
MISSING_REASON = "Указан результат, но осталось объяснить причину, о которой спросил пользователь."
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
    if (item.get("retrieval_relevance") == "irrelevant" or item.get("kind", "source") != "source"
            or item.get("tool_outcome") in {"failed", "empty"}
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
    r"\b(?:(?:как называется|как(?:ой|ое|ая))\s+(?:(?:этот|это|эта)\s+)?"
    r"(?:событие|топик|поле|сервис|метод|класс|таблица|эндпоинт|параметр|функция)|"
    r"(?:what|which|what is the name of (?:the|this))\s+"
    r"(?:event|topic|field|service|method|class|table|endpoint|parameter|function))\b", re.I,
)
_CODE_NAME = re.compile(r"(?<!`)`([a-zA-Z_][\w./:-]{1,179})`(?!`)")
_SERVICE_NAME = re.compile(r"(?<![a-zA-Z0-9_-])(?:[a-z][a-z0-9]*-)+service(?![a-zA-Z0-9_-])")
_DESIGN_REQUEST = re.compile(
    r"\b(?:придум\w*|предлож\w*|спроектир\w*|созда\w*|напиш\w*|пример\w*|например|"
    r"design|propose|implement|example|invent|create)\b", re.I,
)
_FIELD_QUESTION = re.compile(
    r"(?:как(?:ой|ое|ая)|каков[оа]?|назови(?:те)?|покажи(?:те)?|узнай(?:те)?|уточни(?:те)?|"
    r"what(?:'s| is)|show|get)\s+(?:(?:the|current|сейчас|текущ\w+|актуальн\w+)\s+)*"
    r"(?:значение (?:у )?поля\s+)?`?([\w-]+)`?", re.I,
)
_FIELD_ALIASES = {
    "статус": {"статус", "состояние", "status", "state"},
    "версия": {"версия", "version"},
    "приоритет": {"приоритет", "priority"},
    "регион": {"регион", "region"},
}
_SCALAR = re.compile(r"[\w][\w ./+-]{0,64}")


def _field_values(text: str, aliases: set[str]) -> list[str]:
    def matches(key: str) -> bool:
        return re.sub(r"^(?:current|текущ\w+)\s+", "", key.strip().casefold()) in aliases

    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        line = re.fullmatch(r"\s*([\w -]+?)\s*[:=]\s*[\"']?([\w][\w ./+-]{0,64}?)[\"']?\s*", text)
        return [line[2].strip()] if line and matches(line[1]) else []
    # Traverse object fields. Arrays describe collections, not a unique entity.
    pending = [data] if isinstance(data, dict) else []
    values = []
    while pending:
        for key, value in pending.pop().items():
            if matches(key):
                value = value.get("name") if isinstance(value, dict) else value
                if isinstance(value, str) and _SCALAR.fullmatch(value):
                    values.append(value)
            elif isinstance(value, dict):
                pending.append(value)
    return values


def requested_field_label(question: str) -> str | None:
    question = question.strip()
    lead, _, tail = question.partition("?")
    request = _FIELD_QUESTION.match(lead)
    tail = tail.strip()
    if (tail and ("?" in tail or not re.match(r"(?:проверь|сверь|verify|check)\b", tail, re.I))):
        return None
    if (not request or requested_json(question) or
            re.search(r"[,;]|\b(?:и|или|and|or|почему|why)\b", lead, re.I)):
        return None
    return request[1].casefold()


def requested_field(
    run: dict[str, Any], sources: list[dict[str, Any]],
) -> tuple[str, str, str] | None:
    """Return the requested field, value and source only for a unique live MCP result."""
    field = requested_field_label(run.get("question", ""))
    if not field:
        return None
    label, aliases = next(((k, v) for k, v in _FIELD_ALIASES.items() if field in v),
                          (field, {field}))
    candidates: list[tuple[str, str]] = []
    for item in sources:
        if item.get("checked_with") == "mcp_call" and is_answer_source(item):
            candidates.extend((value, item["id"]) for value in _field_values(item["text"], aliases))
    if len(candidates) != 1:
        return None
    value, ref = candidates[0]
    if (value.isdecimal() or re.search(r"[\[\]\r\n]", ref)
            or sum(item["id"] == ref for item in sources) != 1):
        return None
    return label, value, ref


def _reported_field(run: dict[str, Any], content: str, sources: list[dict[str, Any]]) -> str | None:
    """Bind a model-selected value or source without crediting ungrounded extra prose."""
    result = requested_field(run, sources)
    if not result or re.search(
        r"\b(?:не|нет|not|возможно|maybe|perhaps|possibly)\b", content, re.I,
    ):
        return None
    label, value, ref = result
    if question_record_id(run["question"]) and ref not in run.get("_question_bound_mcp_refs", []):
        return None
    if "[" in content:
        # A model can copy the evidence number correctly but corrupt the value
        # itself. A single selected, current field supplies its canonical value.
        aliases_by_ref = _source_references(run["evidence"])
        _, _, refs, metadata = _inspect_markdown(content, set(aliases_by_ref))
        if metadata or refs - aliases_by_ref.keys() or ref not in {aliases_by_ref[r] for r in refs}:
            return None
    elif not re.search(r"(?<![\w.-])" + re.escape(value) + r"(?![\w-]|\.\w)", content, re.I):
        return None
    return f"{label[:1].upper()}{label[1:]} — {value} [{ref}]."


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
    number: int | None = None

    @model_validator(mode="after")
    def redundant_number_matches(self) -> Self:
        if "number" in self.model_fields_set and self.sources != [self.number]:
            raise ValueError("Redundant source number does not match selected source")
        return self


class InvalidWriterAnswer(ModelDecisionError):
    """A final answer could not be tied to the sources shown in its particular request."""


def decode_writer_answer(
    content: str, visible: dict[int, str], *, visible_ids: set[str] | None = None,
) -> str:
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
        offered = set(visible.values()) | (visible_ids or set())
        _, _, refs, _ = _inspect_markdown(raw, offered)
        if refs - offered:
            raise InvalidWriterAnswer("В ответе есть ссылка за пределами переданных материалов.")
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


def _reference_pairs(evidence: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    # Ordinals match the numbered cards in this run's UI. A real ID, even that
    # of an unavailable source, always takes precedence over an ordinal.
    counts = Counter(item.get("id") for item in evidence)
    refs = {}
    for number, item in enumerate(evidence, start=1):
        if item.get("id") and is_answer_source(item):
            ref = str(item["id"])
            pair = (str(item.get("index_id", "")), ref)
            if counts[ref] == 1:
                refs[ref] = pair
            if str(number) not in counts:
                refs[str(number)] = pair
    return refs


def _source_references(evidence: list[dict[str, Any]]) -> dict[str, str]:
    return {alias: pair[1] for alias, pair in _reference_pairs(evidence).items()}


def reviewed_excerpt(run: dict[str, Any]) -> str | None:
    """Bind the reviewer's literal finding to the same retained and citable source."""
    support = run.get("_retrieval_support") or {}
    pending = ("_retrieval_gap", "_pending_source_checks", "_uncertain_tool_call",
               "_uncertain_calls", "_inflight_action", "sandbox_profile_id")
    if (not support or any(run.get(key) for key in pending)
            or run.get("research_plan", {}).get("gaps") or requested_json(run["question"])):
        return None
    pair = (support.get("index_id"), support.get("id"))
    quote = support.get("quote")
    if not isinstance(quote, str) or not 8 <= len(quote) <= 800 or pair[0] not in run["index_ids"]:
        return None
    evidence = run["evidence"]
    if not any((e["index_id"], e["id"]) == pair and is_answer_source(e)
               and e.get("revision") == support.get("revision") and quote in e["text"]
               and (run["mode"] != "autonomous" or e.get("checked_with")) for e in evidence):
        return None
    refs = _reference_pairs(evidence)
    ref = next((key for key, target in refs.items() if target == pair
                and not re.search(r"[\r\n\[\]]", key)), None)
    return f"{quote} [{ref}]" if ref else None


def answer_quality_issue(run: dict[str, Any], content: str) -> str | None:
    """A finished source-backed answer must contain a finding and cite current evidence."""
    sources = None
    if run.get("mode") == "autonomous" or run.get("evidence"):
        sources = set(_source_references(run.get("evidence", [])))
    issue = _draft_issue(
        content, sources, require_citation=bool(sources) or run.get("mode") == "autonomous",
        requested_json=requested_json(run.get("question", "")),
    )
    if not issue and _unexpected_writing(run, content):
        return ("В русском объяснении появились посторонние слова. Ответьте кратко и простыми "
                "предложениями. Точные имена компонентов скопируйте из источников.")
    def cited_passages() -> list[dict[str, Any]]:
        refs = cited_evidence_refs(content, run["evidence"], question=run.get("question", ""))
        return [item for item in run["evidence"]
                if (str(item.get("index_id", "")), item.get("id")) in refs]

    if (not issue and sources and _SERVICE_NAME.search(content)
            and not _DESIGN_REQUEST.search(run.get("question", ""))):
        names = set(_SERVICE_NAME.findall(_REFERENCE.sub("", content)))
        confirmed = {name for item in cited_passages() for key in ("title", "text", "source_path")
                     for name in _SERVICE_NAME.findall(item.get(key) or "")}
        if confirmed and names - confirmed:
            return "Название сервиса расходится с приведёнными источниками. Скопируйте его точно."
    if not issue and question_record_id(run.get("question", "")):
        field = requested_field(run, run.get("evidence", []))
        if field and field[2] not in run.get("_question_bound_mcp_refs", []):
            return "Вызов источника не подтвердил, что значение относится к указанной записи."
    if not issue and run.get("mode") in {"quick", "investigate"} and sources:
        options = named_values(run.get("question", ""), run["evidence"])
        if options:
            cited_options = named_values(run.get("question", ""), cited_passages())
            body = _REFERENCE.sub("", content)
            if not any(re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-]|\.\w)", body)
                       for name in cited_options):
                return "В ответе отсутствует точное название из указанного источника."
    if issue or not sources or not _DURATION_QUESTION.search(run.get("question", "")):
        return issue
    findings = {_seconds(m) for m in _DURATION.finditer(_REFERENCE.sub("", content))}
    if len(findings) == 1:
        measured = {_seconds(m) for item in cited_passages()
                    for m in _DURATION.finditer(item["text"])}
        if len(measured) == 1 and findings != measured:
            return "Указанное время расходится со значением в приведённом источнике."
    return None


def explanation_gap(run: dict[str, Any], content: str) -> str | None:
    if not _ASKS_WHY_TOO.search(run.get("question", "")) or _REASON.search(content):
        return None
    # A paraphrase can explain the cause without an explicit conjunction.
    def words(text: str) -> set[str]:
        return set(re.findall(r"[\w-]{4,}", text.casefold()))

    pattern = _REASON.pattern + r"([^.!?]{8,400})"
    reasons = [match[1] for item in run.get("evidence", []) if is_answer_source(item)
               for match in re.finditer(pattern, " ".join(item["text"].split()), re.I)]
    if reasons and not any(len(words(reason) & words(content)) >= 2 for reason in reasons):
        return MISSING_REASON
    return None


def _unexpected_writing(run: dict[str, Any], content: str) -> bool:
    question = run.get("question", "")
    if (not _OTHER_WRITING.search(content) or not re.search(r"[а-яё]{3}", question, re.I)
            or _LANGUAGE_TASK.search(question)
            or len(re.findall(r"[а-яё]{2,}", content, re.I)) < 6):
        return False
    # Only inspect visible prose. Quoted names or log text present in the sources,
    # Markdown code, links and their visible labels are legitimate verbatim content.
    known = [question, *(text for item in run.get("evidence", []) if is_answer_source(item)
                         for text in (item["text"], item.get("title", "")))]
    for block in _MARKDOWN.parse(content):
        link = False
        for token in block.children or []:
            if token.type in {"link_open", "link_close"}:
                link = token.type == "link_open"
            if token.type == "text" and not link:
                for word in _OTHER_WRITING.findall(token.content):
                    if word.isalpha() and all(word not in text for text in known):
                        return True
    return False


def format_source_reference(run: dict[str, Any], content: str) -> str:
    """Resolve a writer's explicit quoted source name or a verbatim sourced answer."""
    if (run.get("mode") not in {"quick", "investigate", "autonomous"}
            or not content.strip()
            or ("[" in content and not _FIELD_QUESTION.match(run.get("question", "").strip()))
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

    if not requested_json(run.get("question", "")) and (field := _reported_field(
        run, content, sources,
    )):
        return field
    if "[" in content:
        return content
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


class WrittenFindings(QuotedFindings):
    answer: str = Field(min_length=1)


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
                   and (run.get("mode") in {"quick", "investigate"}
                        or source.get("read_document") or source.get("checked_with"))]
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
    choices: list[tuple[tuple[bool, int, bool], SourceQuote]] = []
    wants_reason = bool(_ASKS_WHY_TOO.search(question))
    russian = bool(re.search(r"[а-яё]{3}", question, re.I))
    seen = set()
    for item in evidence[:8]:
        if not 0 < len(item["id"]) <= 512 or re.search(r"[\[\]\r\n]", item["id"]):
            continue
        ranked = []
        for paragraph in re.split(r"\n\s*\n", item["text"]):
            for chunk in split.split_text(paragraph):
                key = " ".join(chunk.split())
                if len(key) < 8 or key.startswith("#") or key in seen:
                    continue
                matches = len(terms & set(re.findall(r"[\w-]{3,}", chunk.casefold())))
                score = (bool(matches and wants_reason and _REASON.search(chunk)), matches,
                         russian and bool(re.search(r"[а-яё]{3,}", chunk, re.I)))
                ranked.append((score, SourceQuote(source_id=item["id"], quote=chunk)))
                seen.add(key)
        choices.extend(sorted(ranked, key=lambda pair: pair[0], reverse=True)[:2])
    choices.sort(key=lambda pair: pair[0], reverse=True)
    return {str(n): choice for n, (_, choice) in enumerate(choices, 1)}


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


def passage_arguments(content: str) -> dict[str, Any] | None:
    """Read the one offered, local passage selector when a model writes its call as text."""
    raw = content.strip()
    if raw.startswith("```json\n") and raw.endswith("```"):
        raw = raw.partition("\n")[2].removesuffix("```").strip()
    if not raw.startswith("{") or len(raw) > 16_000 or '"select_passages"' not in raw:
        return None
    # Some models double-escape quotes around decimal array entries. Restrict
    # this repair to numbered items, never arbitrary strings or tool arguments.
    raw = re.sub(r'(?<=[\[,\s])\\"([0-9]{1,3})\\"(?=\s*[,\]])', r'"\1"', raw)
    try:
        item = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(item, dict) or item.keys() != {"name", "parameters"}:
        return None
    args = item["parameters"]
    if (item["name"] == "select_passages" and isinstance(args, dict)
            and "numbers" in args and args.keys() <= {"numbers", "answer"}):
        return args
    return None


def answer_from_selected_quotes(
    run: dict[str, Any], answer: Any, report: QuotedFindings,
) -> str | None:
    """Keep the writer's explanation and attach only references to checked passages."""
    if (not isinstance(answer, str) or not answer.strip()
            or not render_quoted_findings(run, report.model_dump_json())):
        return None
    check_model_policy({"content": answer}, None)
    ids = list(dict.fromkeys(item.source_id for item in report.findings))
    _, _, refs, metadata = _inspect_markdown(answer, set(ids))
    if metadata or refs - set(ids):
        return None
    citations = " ".join(f"[{ref}]" for ref in ids if ref not in refs)
    return f"{answer.strip()} {citations}".rstrip()


def supporting_reason(
    run: dict[str, Any], answer: str, choices: dict[str, SourceQuote],
) -> str | None:
    """Complete a compound answer only with one explicit cause for its named component."""
    entities = set(_SERVICE_NAME.findall(_REFERENCE.sub("", answer)))
    subject = set(re.findall(r"[\w-]{4,}", run["question"].casefold())) - {
        "почему", "какой", "какие", "назови", "какая", "который", "этому", "сервису",
        "этого", "сервис", "система", "what", "service", "does", "this", "which",
    }
    if not entities or not subject:
        return None
    candidates = {}
    for passage in choices.values():
        excerpt = " ".join(passage.quote.split())
        if (not entities.intersection(_SERVICE_NAME.findall(excerpt))
                or not subject.intersection(re.findall(r"[\w-]{4,}", excerpt.casefold()))):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", excerpt):
            if (_REASON.search(sentence) and 8 <= len(sentence) <= 600
                    and (_REASON.match(sentence)
                         or entities.intersection(_SERVICE_NAME.findall(sentence)))):
                fact = SourceQuote(source_id=passage.source_id, quote=sentence)
                candidates[fact.quote.casefold()] = fact
    if len(candidates) == 1:
        report = QuotedFindings(findings=list(candidates.values()))
        return render_quoted_findings(run, report.model_dump_json())
    return None


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
    return {ref for _, ref in cited_evidence_refs(content, evidence, question=question)}


def cited_evidence_refs(
    content: str, evidence: list[dict[str, Any]], *, question: str = "",
) -> set[tuple[str, str]]:
    """Resolve each Markdown/JSON citation to its unambiguous source and evidence ID."""
    if "[" not in content:
        return set()
    aliases = _reference_pairs(evidence)
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
