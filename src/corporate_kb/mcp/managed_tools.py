"""Persistent, declarative MCP search tools managed through the admin dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Lock
from typing import Any

from fastmcp.tools import Tool, ToolResult
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from corporate_kb.mcp.tools import KnowledgeTools

logger = logging.getLogger(__name__)
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ALLOWED_ARGUMENTS = {
    "query",
    "top_k",
    "min_score",
    "service",
    "domain",
    "document_type",
    "status",
    "authority",
    "source_type",
}
_RESERVED_NAMES = {
    "kb_search",
    "kb_get_document",
    "kb_get_chunk",
    "kb_run_context_benchmark",
    "kb_list_documents",
    "kb_stats",
}
_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "result_count": {"type": "integer"},
        "retrieved_candidate_count": {"type": "integer"},
        "context_token_count": {"type": "integer"},
        "results": {"type": "array", "items": {"type": "object"}},
    },
    "required": [
        "query",
        "result_count",
        "retrieved_candidate_count",
        "context_token_count",
        "results",
    ],
}


class ManagedToolDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_k: int = Field(default=3, ge=1, le=20)
    min_score: float | None = None
    service: str | None = None
    domain: str | None = None
    document_type: str | None = None
    status: str | None = "current"
    authority: str | None = None
    source_type: str | None = None


class ManagedToolDefinition(BaseModel):
    """A safe search-tool definition; it cannot execute arbitrary server code."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = Field(min_length=10, max_length=2000)
    input_schema: dict[str, Any]
    defaults: ManagedToolDefaults = Field(default_factory=ManagedToolDefaults)
    index_ids: list[str] = Field(default_factory=lambda: ["default"], max_length=20)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _TOOL_NAME.fullmatch(value):
            raise ValueError("Tool name must contain only A-Z, a-z, 0-9, _, . or -")
        if not value.startswith("kb_"):
            raise ValueError("Managed tool name must start with kb_")
        if value in _RESERVED_NAMES:
            raise ValueError("Tool name is reserved by the built-in MCP server")
        return value

    @field_validator("input_schema")
    @classmethod
    def validate_input_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        if schema.get("type") != "object":
            raise ValueError("input_schema.type must be object")
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise ValueError("input_schema.properties must be an object")
        unknown = set(properties) - _ALLOWED_ARGUMENTS
        if unknown:
            raise ValueError(f"Unsupported schema properties: {', '.join(sorted(unknown))}")
        query = properties.get("query")
        if not isinstance(query, dict) or query.get("type") != "string":
            raise ValueError("input_schema must declare query as a string")
        expected_types = {
            "query": "string",
            "top_k": "integer",
            "min_score": "number",
            "service": "string",
            "domain": "string",
            "document_type": "string",
            "status": "string",
            "authority": "string",
            "source_type": "string",
        }
        for name, property_schema in properties.items():
            if not isinstance(property_schema, dict):
                raise ValueError(f"Schema property {name} must be an object")
            if property_schema.get("type") != expected_types[name]:
                raise ValueError(f"Schema property {name} must have type {expected_types[name]}")
        required = schema.get("required", [])
        if not isinstance(required, list) or "query" not in required:
            raise ValueError("input_schema.required must include query")
        if any(item not in properties for item in required):
            raise ValueError("input_schema.required contains an undeclared property")
        if schema.get("additionalProperties", False) is not False:
            raise ValueError("input_schema.additionalProperties must be false")
        return schema

    @field_validator("index_ids")
    @classmethod
    def validate_index_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("index_ids must not contain duplicates")
        for value in values:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
                raise ValueError("index_ids contains an invalid index id")
        return values


class ManagedSearchTool(Tool):
    """FastMCP Tool with a persisted runtime JSON Schema."""

    _execute: Callable[[str, dict[str, Any]], dict[str, Any]] = PrivateAttr()

    def __init__(
        self,
        definition: ManagedToolDefinition,
        execute: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> None:
        super().__init__(
            name=definition.name,
            description=definition.description,
            parameters=definition.input_schema,
            output_schema=_OUTPUT_SCHEMA,
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        self._execute = execute

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        result = await asyncio.to_thread(self._execute, self.name, arguments)
        return self.convert_result(result)


class ManagedToolRegistry:
    """Persist and execute declarative search tools."""

    def __init__(
        self,
        path: Path,
        tools: KnowledgeTools,
        *,
        index_tools: Callable[[str], KnowledgeTools] | None = None,
        index_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self._path = path.resolve()
        self._tools = tools
        self._index_tools = index_tools or self._default_index_tools
        self._index_exists = index_exists or (lambda index_id: index_id == "default")
        self._lock = Lock()
        self._definitions = self._load()

    def list(self) -> list[ManagedToolDefinition]:
        with self._lock:
            return [self._definitions[name] for name in sorted(self._definitions)]

    def payload(self) -> dict[str, Any]:
        definitions = self.list()
        return {
            "tool_count": len(definitions),
            "tools": [definition.model_dump(mode="json") for definition in definitions],
        }

    def create_tool(self, definition: ManagedToolDefinition) -> ManagedSearchTool:
        return ManagedSearchTool(definition, self.execute)

    def upsert(self, definition: ManagedToolDefinition) -> ManagedToolDefinition:
        unknown = [
            index_id for index_id in definition.index_ids if not self._index_exists(index_id)
        ]
        if unknown:
            raise ValueError(f"Unknown RAG indexes: {', '.join(unknown)}")
        with self._lock:
            if definition.name not in self._definitions and len(self._definitions) >= 50:
                raise ValueError("Managed tool limit reached (50)")
            self._definitions[definition.name] = definition
            self._save_locked()
        return definition

    def delete(self, name: str) -> None:
        with self._lock:
            if name not in self._definitions:
                raise KeyError(f"Unknown managed tool: {name}")
            del self._definitions[name]
            self._save_locked()

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            definition = self._definitions.get(name)
        if definition is None:
            raise KeyError(f"Unknown managed tool: {name}")
        values = self._validated_arguments(definition, arguments)
        defaults = definition.defaults
        if not definition.index_ids:
            raise RuntimeError(f"Managed tool {name} is not bound to a RAG index")
        top_k = values.get("top_k", defaults.top_k)
        payloads: list[tuple[str, dict[str, Any]]] = []
        for index_id in definition.index_ids:
            payloads.append(
                (
                    index_id,
                    self._index_tools(index_id).search(
                        query=values["query"],
                        top_k=top_k,
                        min_score=values.get("min_score", defaults.min_score),
                        service=values.get("service", defaults.service),
                        domain=values.get("domain", defaults.domain),
                        document_type=values.get("document_type", defaults.document_type),
                        status=values.get("status", defaults.status),
                        authority=values.get("authority", defaults.authority),
                        source_type=values.get("source_type", defaults.source_type),
                    ),
                )
            )
        payload = self._merge_payloads(values["query"], top_k, payloads)
        self._tools.usage.record(name)
        return payload

    def _default_index_tools(self, index_id: str) -> KnowledgeTools:
        if index_id != "default":
            raise KeyError(f"Unknown RAG index: {index_id}")
        return self._tools

    @staticmethod
    def _merge_payloads(
        query: str,
        top_k: int,
        payloads: Sequence[tuple[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        candidate_count = 0
        for index_id, payload in payloads:
            candidate_count += int(payload.get("retrieved_candidate_count", 0))
            for raw in payload.get("results", []):
                item = dict(raw)
                item["index_id"] = index_id
                results.append(item)
        results.sort(key=lambda item: (-float(item.get("score", 0.0)), str(item.get("chunk_id"))))
        selected = results[:top_k]
        for rank, item in enumerate(selected, start=1):
            item["rank"] = rank
        return {
            "query": query,
            "result_count": len(selected),
            "retrieved_candidate_count": candidate_count,
            "context_token_count": sum(int(item.get("excerpt_tokens", 0)) for item in selected),
            "index_ids": [index_id for index_id, _payload in payloads],
            "results": selected,
        }

    @staticmethod
    def _validated_arguments(
        definition: ManagedToolDefinition,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = set(definition.input_schema["properties"])
        unknown = set(arguments) - allowed
        if unknown:
            raise ValueError(f"Unknown tool arguments: {', '.join(sorted(unknown))}")
        for required in definition.input_schema.get("required", []):
            if required not in arguments:
                raise ValueError(f"Missing required argument: {required}")
        values = dict(arguments)
        query = values.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if "top_k" in values:
            top_k = values["top_k"]
            if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
                raise ValueError("top_k must be an integer between 1 and 20")
        if "min_score" in values and not isinstance(values["min_score"], (int, float)):
            raise ValueError("min_score must be a number")
        for key in allowed - {"query", "top_k", "min_score"}:
            if key in values and values[key] is not None and not isinstance(values[key], str):
                raise ValueError(f"{key} must be a string")
        return values

    def _load(self) -> dict[str, ManagedToolDefinition]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("managed_tools.json must contain an array")
            definitions = [ManagedToolDefinition.model_validate(item) for item in raw]
            return {definition.name: definition for definition in definitions}
        except Exception as exc:
            logger.warning("Managed MCP tools could not be loaded: %s", exc)
            return {}

    def _save_locked(self) -> None:
        payload = json.dumps(
            [self._definitions[name].model_dump(mode="json") for name in sorted(self._definitions)],
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self._path.parent, delete=False) as handle:
                temporary_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
