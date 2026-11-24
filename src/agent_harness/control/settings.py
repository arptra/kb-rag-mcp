"""Validated operator budgets; an agent can spend grants but cannot change this policy."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_harness.config import HarnessSettings
from agent_harness.model_connection import ModelConnectionStore
from agent_harness.model_profiles import DEEPSEEK_CONTEXT_WINDOW_TOKENS, DEEPSEEK_OUTPUT_TOKENS
from agent_harness.policy import BUDGETS, QUICK_BUDGETS

MODES = ("quick", "investigate", "autonomous")
EFFORTS = ("low", "medium", "high")
RESTART_FIELDS = ("quick_workers", "investigate_workers", "queue_max")


class StrictSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RunLimits(StrictSettings):
    model_calls: int = Field(ge=1, le=1000, description="Основные вызовы модели, без сжатия беседы")
    summary_calls: int = Field(default=8, ge=0, le=1000, description="Вызовы для сжатия беседы")
    source_calls: int = Field(ge=0, le=5000, description="Повторные поиски и чтения источников")
    tool_calls: int = Field(ge=0, le=5000, description="Вызовы инструментов на запрос")
    parallelism: int = Field(ge=1, le=64, description="Одновременные обращения к источникам")
    document_reads: int = Field(ge=0, le=1000, description="Автоматическое чтение документов")
    evidence_per_index: int = Field(ge=1, le=100, description="Материалы из одного индекса")
    evidence_chars: int = Field(ge=100, le=100_000, description="Размер поисковой выдержки")
    document_chars: int = Field(ge=100, le=1_000_000, description="Размер читаемого документа")
    timeout_seconds: float = Field(gt=0, le=86_400, description="Рабочее время запроса, секунды")
    context_tokens: int = Field(
        ge=256, le=1_000_000, description="Входной контекст, оценка токенов"
    )


class SearchLimits(RunLimits):
    model_calls: Literal[0] = 0


class AutonomySettings(StrictSettings):
    enabled: bool = True
    max_extensions: int = Field(default=4, ge=0, le=100)
    minimum_progress: bool = Field(
        default=True,
        description="Расширение разрешается при наблюдаемом продвижении исследования",
    )
    caps: RunLimits


class ContextSettings(StrictSettings):
    enabled: bool = Field(
        default=True, description="Смысловое сжатие истории библиотекой LangChain"
    )
    window_tokens: int = Field(
        default=65_536,
        ge=2048,
        le=2_000_000,
        description="Окно вашей модели, заданное оператором; это не автоматическое определение",
    )
    response_reserve_tokens: int = Field(default=4096, ge=128, le=200_000)
    history_fraction: float = Field(default=0.35, gt=0, le=0.8)
    trigger_fraction: float = Field(default=0.8, gt=0, le=1)
    keep_fraction: float = Field(default=0.35, gt=0, le=0.9)
    summary_output_tokens: int = Field(default=1024, ge=128, le=32_000)
    chars_per_token: float = Field(
        default=3.0,
        ge=1,
        le=8,
        description="Коэффициент приблизительного подсчёта; точный токенизатор модели неизвестен",
    )
    summary_input_fraction: float = Field(default=0.8, gt=0, le=1)

    @model_validator(mode="after")
    def valid_reserve(self) -> Self:
        if self.response_reserve_tokens >= self.window_tokens:
            raise ValueError("Резерв ответа должен быть меньше окна модели.")
        if self.window_tokens - self.response_reserve_tokens < 256:
            raise ValueError("После резерва ответа должно остаться хотя бы 256 входных токенов.")
        if self.keep_fraction >= self.trigger_fraction:
            raise ValueError("Хвост истории должен быть меньше порога сжатия.")
        return self


class RuntimeLimits(StrictSettings):
    quick_workers: int = Field(default=2, ge=1, le=16)
    investigate_workers: int = Field(default=1, ge=1, le=16)
    queue_max: int = Field(default=32, ge=1, le=1000)
    authentication_timeout_seconds: float = Field(default=600, gt=0, le=7200)
    cleanup_timeout_seconds: float = Field(default=15, gt=0, le=120)
    source_timeout_seconds: float = Field(default=15, gt=0, le=120)
    external_tool_timeout_seconds: float = Field(default=600, gt=0, le=7200)
    final_reserve_seconds: float = Field(default=120, ge=0, le=3600)
    max_indexes: int = Field(default=1000, ge=1, le=1000)
    results_per_index: int = Field(default=5, ge=1, le=100)
    evidence_chars: int = Field(default=4000, ge=100, le=100_000)
    max_evidence: int = Field(default=1000, ge=1, le=5000)
    answer_chars: int = Field(default=50_000, ge=1000, le=1_000_000)
    retention_days: int = Field(default=30, ge=1, le=3650)
    max_runs_per_owner: int = Field(default=100, ge=1, le=100_000)
    max_total_runs: int = Field(default=10_000, ge=1, le=1_000_000)
    graph_recursion_limit: int = Field(default=2048, ge=32, le=100_000)
    max_stalled_rounds: int = Field(default=3, ge=1, le=100)
    max_answer_retries: int = Field(default=1, ge=0, le=10)
    max_decision_errors: int = Field(default=3, ge=1, le=20)
    max_resume_attempts: int = Field(default=3, ge=0, le=100)
    gateway_wait_seconds: float = Field(
        default=300, ge=0, le=86400,
        description="Ожидание отложенных повторов Автопилота в секундах",
    )
    memory_max_records: int = Field(default=10_000, ge=1, le=1_000_000)
    memory_retrieval_count: int = Field(default=3, ge=0, le=100)
    memory_context_tokens: int = Field(default=2000, ge=0, le=32_000)


class ControlSettings(StrictSettings):
    schema_version: Literal[2] = 2
    revision: int = Field(default=0, ge=0)
    model_profile: Literal["generic", "deepseek_v4_flash"] = "generic"
    profiles: dict[str, dict[str, RunLimits]]
    autonomy: AutonomySettings
    context: ContextSettings = Field(default_factory=ContextSettings)
    runtime: RuntimeLimits = Field(default_factory=RuntimeLimits)

    def search_limits(self, effort: str) -> SearchLimits:
        # Use the fast queue's I/O limits and the operator's direct retrieval
        # limits. Older control databases need no additional editable profile.
        return SearchLimits.model_validate({
            **self.profiles["quick"][effort].model_dump(),
            "model_calls": 0, "summary_calls": 0, "document_reads": 0,
            "evidence_per_index": self.runtime.results_per_index,
            "evidence_chars": self.runtime.evidence_chars,
        })

    @model_validator(mode="after")
    def complete_profiles(self) -> Self:
        if set(self.profiles) != set(MODES):
            raise ValueError("Требуются профили всех трёх режимов.")
        for mode, profiles in self.profiles.items():
            if set(profiles) != set(EFFORTS):
                raise ValueError("Для каждого режима требуются три уровня усилия.")
            for profile in profiles.values():
                if mode == "autonomous":
                    for field in RunLimits.model_fields:
                        if getattr(profile, field) > getattr(self.autonomy.caps, field):
                            raise ValueError("Начальный бюджет превышает потолок автопилота.")
        return self


def default_settings(
    settings: HarnessSettings, model_profile: str = "generic",
) -> ControlSettings:
    profiles: dict[str, dict[str, RunLimits]] = {}
    for mode in MODES:
        source_mode = "quick" if mode == "quick" else "investigate"
        catalog = QUICK_BUDGETS if mode == "quick" else BUDGETS
        profiles[mode] = {
            effort: RunLimits(
                model_calls=profile.model_calls,
                source_calls=profile.source_calls,
                tool_calls=profile.tool_calls,
                parallelism=profile.parallelism,
                document_reads=profile.document_reads,
                evidence_per_index=profile.evidence_per_index,
                evidence_chars=profile.evidence_chars,
                document_chars=profile.document_chars,
                timeout_seconds=getattr(settings, source_mode + "_timeout_seconds")
                * profile.time_ratio,
                context_tokens=max(
                    256,
                    int(
                        getattr(settings, source_mode + "_context_chars")
                        * profile.context_ratio
                        / 3
                    ),
                ),
            )
            for effort, profile in catalog.items()
        }
    ceiling = RunLimits(
        model_calls=128,
        summary_calls=32,
        source_calls=512,
        tool_calls=512,
        parallelism=32,
        document_reads=128,
        evidence_per_index=20,
        evidence_chars=20_000,
        document_chars=100_000,
        timeout_seconds=7200.0,
        context_tokens=60_000,
    )
    # Respect explicitly configured legacy settings even when higher than new defaults.
    ceiling = RunLimits.model_validate(
        {
            name: max(
                getattr(ceiling, name), *(getattr(p, name) for p in profiles["autonomous"].values())
            )
            for name in RunLimits.model_fields
        }
    )
    runtime_fields = set(RuntimeLimits.model_fields) & set(type(settings).model_fields)
    result = ControlSettings(
        profiles=profiles,
        autonomy=AutonomySettings(caps=ceiling),
        runtime=RuntimeLimits.model_validate(
            {field: getattr(settings, field) for field in runtime_fields}
        ),
    )
    if model_profile != "deepseek_v4_flash":
        return result
    result.model_profile = "deepseek_v4_flash"
    result.context.window_tokens = DEEPSEEK_CONTEXT_WINDOW_TOKENS
    result.context.response_reserve_tokens = DEEPSEEK_OUTPUT_TOKENS
    result.context.summary_output_tokens = 4096
    # Approximate counting needs headroom for provider framing and tokenizer
    # differences. The 256K window includes both the prompt and the response.
    input_tokens = DEEPSEEK_CONTEXT_WINDOW_TOKENS - DEEPSEEK_OUTPUT_TOKENS - 8192
    for mode, mode_profiles in result.profiles.items():
        legacy_mode = "quick" if mode == "quick" else "investigate"
        for position, profile in enumerate(mode_profiles.values()):
            if legacy_mode + "_context_chars" not in settings.model_fields_set:
                profile.context_tokens = input_tokens
            if legacy_mode + "_timeout_seconds" not in settings.model_fields_set:
                profile.timeout_seconds = 3900.0 if mode == "quick" else (
                    14_400.0, 21_600.0, 28_800.0
                )[position]
            if mode != "quick":
                profile.model_calls = (12, 24, 48)[position]
                profile.source_calls = (96, 192, 384)[position]
                profile.tool_calls = (48, 96, 192)[position]
                profile.document_reads = (4, 8, 12)[position]
                profile.document_chars = 120_000
                profile.evidence_chars = 12_000
    if "final_reserve_seconds" not in settings.model_fields_set:
        result.runtime.final_reserve_seconds = 3600.0
    if "answer_chars" not in settings.model_fields_set:
        result.runtime.answer_chars = 200_000
    if "evidence_chars" not in settings.model_fields_set:
        result.runtime.evidence_chars = 12_000
    result.autonomy.caps.timeout_seconds = 86_400.0
    result.autonomy.caps.context_tokens = input_tokens
    result.autonomy.caps.document_chars = 120_000
    return ControlSettings.model_validate(result.model_dump())


def _migrate_profile(
    current: ControlSettings, previous: ControlSettings, selected: ControlSettings,
) -> ControlSettings:
    """Replace only untouched defaults when the connected model profile changes."""
    def merge(value: Any, old: Any, new: Any) -> Any:
        if isinstance(value, dict) and isinstance(old, dict) and isinstance(new, dict):
            return {key: merge(item, old.get(key), new.get(key))
                    for key, item in value.items()}
        return new if value == old else value

    data = merge(current.model_dump(), previous.model_dump(), selected.model_dump())
    data["revision"] = current.revision
    data["model_profile"] = selected.model_profile
    # Explicit operator ceilings take precedence over a larger provider preset.
    for profile in data["profiles"]["autonomous"].values():
        for field in RunLimits.model_fields:
            profile[field] = min(profile[field], data["autonomy"]["caps"][field])
    if data["context"]["response_reserve_tokens"] >= data["context"]["window_tokens"]:
        data["context"]["response_reserve_tokens"] = max(
            128, data["context"]["window_tokens"] // 4,
        )
    return ControlSettings.model_validate(data)


class RevisionConflict(ValueError):
    """The operator edited an older revision than the persisted one."""


class ControlStore:
    """One atomic policy revision shared by UI and run snapshots; lazy on first write."""

    def __init__(self, settings: HarnessSettings) -> None:
        self.path: Path = settings.state_dir / "control.sqlite3"
        self.settings = settings
        self._model_store: ModelConnectionStore | None = None
        self._defaults: dict[str, ControlSettings] = {"generic": default_settings(settings)}

    @property
    def defaults(self) -> ControlSettings:
        # Resolve at each load so saving a model connection applies to new runs
        # immediately, including stores created before the connection was edited.
        profile = "generic"
        if (self.settings.state_dir / "configuration.sqlite3").exists():
            if self._model_store is None:
                self._model_store = ModelConnectionStore(self.settings.state_dir)
            connection = self._model_store.load()
            if connection.provider == "openai":
                profile = connection.resolved_profile
        if profile not in self._defaults:
            self._defaults[profile] = default_settings(self.settings, profile)
        return self._defaults[profile].model_copy(deep=True)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY, data TEXT)")
            with db:
                yield db
        finally:
            db.close()

    def load(self) -> ControlSettings:
        defaults = self.defaults
        if not self.path.exists():
            return defaults
        with self._connect() as db:
            row = db.execute("SELECT data FROM control WHERE id=1").fetchone()
        if not row:
            return defaults
        data = json.loads(row[0])
        if "schema_version" not in data:
            # The old validator mandated zero follow-up and tool calls for
            # quick. Preserve every other operator setting and future opt-outs.
            for effort, limits in data["profiles"]["quick"].items():
                if limits["source_calls"] == limits["tool_calls"] == 0:
                    profile = defaults.profiles["quick"][effort]
                    limits.update(source_calls=profile.source_calls, tool_calls=profile.tool_calls)
                    if limits["model_calls"] == 1:
                        limits["model_calls"] = profile.model_calls
        current = ControlSettings.model_validate(data)
        if current.model_profile != defaults.model_profile:
            current = _migrate_profile(
                current, default_settings(self.settings, current.model_profile), defaults,
            )
        return current

    def save(self, candidate: ControlSettings, *, expected_revision: int) -> ControlSettings:
        # Validate even callers using model_copy/model_construct bypassed Pydantic initially.
        candidate = ControlSettings.model_validate(candidate.model_dump())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM control WHERE id=1").fetchone()
            current = ControlSettings.model_validate_json(row[0]) if row else self.defaults
            if expected_revision != current.revision:
                raise RevisionConflict(
                    "Настройки уже изменились. Обновите страницу перед сохранением."
                )
            saved = candidate.model_copy(update={"revision": current.revision + 1}, deep=True)
            db.execute(
                "INSERT INTO control VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (saved.model_dump_json(),),
            )
        return saved


class SettingsUpdate(StrictSettings):
    revision: Annotated[int, Field(ge=0)]
    settings: dict[str, Any]
