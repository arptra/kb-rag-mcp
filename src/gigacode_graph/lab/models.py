"""Versioned machine-readable documents used by graph-lab."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field


class LabModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LabRepository(LabModel):
    source: str
    ref: str | None = None
    name: str | None = None


class CountTargets(LabModel):
    min_services: int = Field(default=0, ge=0)
    min_entrypoints: int = Field(default=0, ge=0)
    min_exitpoints: int = Field(default=0, ge=0)
    min_dependencies: int = Field(default=0, ge=0)
    max_issues: int | None = Field(default=None, ge=0)


class ServiceTarget(LabModel):
    service: str
    min_entrypoints: int = Field(default=0, ge=0)
    min_exitpoints: int = Field(default=0, ge=0)


class EdgeTarget(LabModel):
    source: str
    target: str | None = None
    protocol: str | None = None
    operation_contains: str | None = None
    minimum: int = Field(default=1, ge=1)


class CaseExpectations(LabModel):
    counts: CountTargets = Field(default_factory=CountTargets)
    services: list[ServiceTarget] = Field(default_factory=list)
    required_edges: list[EdgeTarget] = Field(default_factory=list)
    forbidden_edges: list[EdgeTarget] = Field(default_factory=list)


class GraphLabCase(LabModel):
    schema_version: Literal[1] = 1
    id: str
    description: str
    algorithm: str = "static-v2"
    mode: Literal["static", "gigacode"] = "static"
    verify_all: bool = False
    repositories: list[LabRepository] = Field(min_length=1)
    expectations: CaseExpectations = Field(default_factory=CaseExpectations)


class AlgorithmRelease(LabModel):
    id: str
    version: str
    stage: Literal["experimental", "candidate", "production", "retired"]
    implementation: str
    targets: list[str] = Field(default_factory=list)
    evidence_runs: list[str] = Field(default_factory=list)
    notes: str = ""


class AlgorithmRegistryDocument(LabModel):
    schema_version: Literal[1] = 1
    active: str = "static-v2"
    algorithms: list[AlgorithmRelease] = Field(default_factory=list)


def load_yaml_model[T: BaseModel](path: Path, model: type[T]) -> T:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return model.model_validate(payload)


def dump_yaml_model(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        yaml.safe_dump(
            value.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
