"""Bounded LLM generation of local service SSOT documents."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from corporate_kb.config import Settings

_SOURCE_SUFFIXES = {".java", ".kt", ".kts", ".proto", ".properties", ".yaml", ".yml"}
_IGNORED_DIRECTORIES = {
    ".git",
    ".gradle",
    ".idea",
    ".mvn",
    "build",
    "dist",
    "generated",
    "node_modules",
    "out",
    "target",
    "vendor",
}


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class SsotLlmClient(Protocol):
    @property
    def model_name(self) -> str: ...

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        cancel: CancellationSignal | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class GeneratedSsot:
    service_id: str
    content: str
    used_llm: bool
    model: str
    source_files: tuple[str, ...]
    error: str | None = None


class OpenAICompatibleSsotClient:
    """Small OpenAI-compatible chat client suitable for local or hosted models."""

    def __init__(self, settings: Settings) -> None:
        if not settings.ssot_llm_base_url or not settings.ssot_llm_model:
            raise RuntimeError(
                "SSOT LLM is not configured; set KB_SSOT_LLM_BASE_URL and KB_SSOT_LLM_MODEL"
            )
        self._url = f"{settings.ssot_llm_base_url}/chat/completions"
        self._model = settings.ssot_llm_model
        self._api_key = (
            settings.ssot_llm_api_key.get_secret_value()
            if settings.ssot_llm_api_key is not None
            else ""
        )
        self._timeout = settings.ssot_llm_timeout_seconds
        self._max_tokens = settings.ssot_llm_max_tokens
        self._temperature = settings.ssot_llm_temperature

    @property
    def model_name(self) -> str:
        return self._model

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        cancel: CancellationSignal | None = None,
    ) -> str:
        if cancel is not None and cancel.is_set():
            raise RuntimeError("SSOT generation was cancelled")
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    self._url,
                    headers=headers,
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": self._temperature,
                        "max_tokens": self._max_tokens,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"SSOT LLM returned HTTP {exc.response.status_code}") from None
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"SSOT LLM request failed: {exc}") from None
        if cancel is not None and cancel.is_set():
            raise RuntimeError("SSOT generation was cancelled")
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(
                "SSOT LLM response does not contain choices[0].message.content"
            ) from None
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("SSOT LLM returned empty content")
        return content.strip()


class ServiceSsotGenerator:
    """Build one evidence-backed SSOT from fresh analysis plus bounded source excerpts."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: SsotLlmClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        if self._client is None and self.configured:
            self._client = OpenAICompatibleSsotClient(settings)
        self._system_prompt = self._load_system_prompt()

    @property
    def configured(self) -> bool:
        return bool(self.settings.ssot_llm_base_url and self.settings.ssot_llm_model)

    @property
    def model_name(self) -> str | None:
        if self._client is not None:
            return self._client.model_name
        return self.settings.ssot_llm_model

    def status(self) -> dict[str, Any]:
        return {
            "configured": self._client is not None,
            "provider": "openai-compatible",
            "model": self.model_name,
            "workers": self.settings.ssot_generation_workers,
            "output_pattern": "<index.knowledge_dir>/ssot/generated/<service-id>.md",
            "required_settings": (
                []
                if self._client is not None
                else ["KB_SSOT_LLM_BASE_URL", "KB_SSOT_LLM_MODEL"]
            ),
        }

    def generate(
        self,
        payload: dict[str, Any],
        *,
        checkout: Path,
        existing_ssot: str | None = None,
        cancel: CancellationSignal | None = None,
    ) -> GeneratedSsot:
        service = payload["service"]
        service_id = str(service["id"])
        excerpts = self._source_excerpts(service, checkout, cancel=cancel)
        source_files = tuple(path for path, _text in excerpts)
        if self._client is None:
            raise RuntimeError(
                "SSOT LLM is not configured; set KB_SSOT_LLM_BASE_URL and KB_SSOT_LLM_MODEL"
            )
        prompt = self._user_prompt(payload, excerpts, existing_ssot)
        body = self._client.generate(
            system_prompt=self._system_prompt,
            user_prompt=prompt,
            cancel=cancel,
        )
        return GeneratedSsot(
            service_id=service_id,
            content=self._document(payload, self._clean_body(body), model=self._client.model_name),
            used_llm=True,
            model=self._client.model_name,
            source_files=source_files,
        )

    def fallback(
        self,
        payload: dict[str, Any],
        *,
        checkout: Path,
        error: str,
        cancel: CancellationSignal | None = None,
    ) -> GeneratedSsot:
        service = payload["service"]
        excerpts = self._source_excerpts(service, checkout, cancel=cancel)
        body = self._fallback_body(payload)
        return GeneratedSsot(
            service_id=str(service["id"]),
            content=self._document(payload, body, model="source-analysis-fallback"),
            used_llm=False,
            model="source-analysis-fallback",
            source_files=tuple(path for path, _text in excerpts),
            error=error,
        )

    def _load_system_prompt(self) -> str:
        root = self.settings.ssot_skill_path
        parts = [
            "Generate a concise service SSOT from static source analysis. Return Markdown body "
            "only, without YAML frontmatter or code fences. Never invent unsupported facts.",
        ]
        for relative in (
            Path("SKILL.md"),
            Path("references/analysis-contract.md"),
            Path("assets/ssot-template.md"),
        ):
            path = root / relative
            if path.is_file():
                try:
                    parts.append(path.read_text(encoding="utf-8")[:20_000])
                except (OSError, UnicodeDecodeError):
                    continue
        return "\n\n".join(parts)

    def _source_excerpts(
        self,
        service: dict[str, Any],
        checkout: Path,
        *,
        cancel: CancellationSignal | None,
    ) -> list[tuple[str, str]]:
        checkout = checkout.resolve()
        module_path = Path(str(service.get("module_path") or "."))
        module = (checkout / module_path).resolve()
        if not module.is_relative_to(checkout) or not module.is_dir():
            module = checkout
        candidates: list[Path] = []
        for current, directories, files in os.walk(module, followlinks=False):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("SSOT generation was cancelled")
            root = Path(current)
            directories[:] = [
                name
                for name in directories
                if name not in _IGNORED_DIRECTORIES
                and not name.startswith(".")
                and not (root / name).is_symlink()
            ]
            candidates.extend(
                root / name
                for name in files
                if (root / name).suffix.lower() in _SOURCE_SUFFIXES
                and not (root / name).is_symlink()
            )
        candidates.sort(key=lambda path: (self._source_priority(path), path.as_posix()))
        remaining = self.settings.ssot_generation_source_chars
        excerpts: list[tuple[str, str]] = []
        for path in candidates[: self.settings.ssot_generation_max_source_files]:
            if remaining <= 0:
                break
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            excerpt = text[: min(6000, remaining)]
            remaining -= len(excerpt)
            excerpts.append((path.relative_to(checkout).as_posix(), excerpt))
        return excerpts

    @staticmethod
    def _source_priority(path: Path) -> int:
        name = path.name.lower()
        priorities = (
            ("controller", 0),
            ("endpoint", 1),
            ("api", 2),
            ("service", 3),
            ("handler", 4),
            ("listener", 5),
            ("client", 6),
            ("application", 7),
        )
        return next((priority for marker, priority in priorities if marker in name), 20)

    def _user_prompt(
        self,
        payload: dict[str, Any],
        excerpts: list[tuple[str, str]],
        existing_ssot: str | None,
    ) -> str:
        service = payload["service"]
        source = "\n\n".join(
            f"### `{path}`\n```\n{text}\n```" for path, text in excerpts
        )
        previous = (
            f"\n\n## Existing SSOT to revise\n{existing_ssot[:12_000]}"
            if existing_ssot
            else ""
        )
        return (
            f"Create a minimal draft SSOT for service `{service['id']}`. Focus on observed API, "
            "events, scheduled jobs, outbound calls, and likely code-level functionality. Label "
            "inference and unknowns explicitly. Keep identifiers and evidence IDs exact.\n\n"
            "## Static analysis JSON\n"
            f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n"
            f"## Bounded source excerpts\n{source or 'No readable source files found.'}"
            f"{previous}"
        )

    @staticmethod
    def _clean_body(content: str) -> str:
        cleaned = content.strip()
        if cleaned.startswith("```") and cleaned.endswith("```"):
            cleaned = re.sub(r"^```(?:markdown|md)?\s*", "", cleaned, count=1)
            cleaned = re.sub(r"\s*```$", "", cleaned, count=1)
        if cleaned.startswith("---"):
            parts = cleaned.split("---", 2)
            if len(parts) == 3:
                cleaned = parts[2].lstrip()
        return cleaned.strip()

    @staticmethod
    def _document(payload: dict[str, Any], body: str, *, model: str) -> str:
        service = payload["service"]
        generated_at = datetime.now(UTC).isoformat()
        frontmatter = {
            "document_type": "ssot",
            "service": service["id"],
            "repository": service["repository"],
            "module": service.get("module_path") or ".",
            "status": "current",
            "review_status": "draft",
            "authority": "source-analysis-draft",
            "source_type": "generated",
            "generated_by": "kb_generate_system_ssot",
            "model": model,
            "commit": service.get("commit") or "unknown",
            "generated_at": generated_at,
        }
        yaml = "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in frontmatter.items()
        )
        return f"---\n{yaml}\n---\n\n{body.strip()}\n"

    @staticmethod
    def _fallback_body(payload: dict[str, Any]) -> str:
        service = payload["service"]
        lines = [
            f"# {service['name']}",
            "",
            "> Draft generated from static source analysis because the LLM call failed. "
            "Validate all inferred behavior with the service owner.",
            "",
            "## Observed functionality",
            "",
        ]
        entrypoints = service.get("entrypoints", [])
        if entrypoints:
            lines.extend(
                f"- `{item['kind']}` `{item['operation']}` — {item['description']}"
                for item in entrypoints
            )
        else:
            lines.append("- No API or event entrypoints were observed.")
        lines.extend(["", "## Outbound integrations", ""])
        outbound = service.get("outbound_interfaces", [])
        if outbound:
            lines.extend(
                f"- `{item['kind']}` `{item['operation']}` → "
                f"`{item.get('target_hint') or 'unknown'}`"
                for item in outbound
            )
        else:
            lines.append("- No outbound interfaces were observed.")
        lines.extend(["", "## Known dependencies", ""])
        dependencies = payload.get("dependencies", [])
        if dependencies:
            lines.extend(
                f"- `{item['protocol']}` `{item['operation']}`: "
                f"`{item['source_service_id']}` → "
                f"`{item.get('target_service_id') or item['target_hint']}`"
                for item in dependencies
            )
        else:
            lines.append("- No service dependencies were resolved.")
        lines.extend(
            [
                "",
                "## Unknowns",
                "",
                "- Business purpose, ownership, SLAs, security guarantees, and runtime behavior "
                "require human confirmation.",
            ]
        )
        return "\n".join(lines)
