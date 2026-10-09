"""Validation and immutable snapshots for the independent skills registry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar
from urllib.parse import urlparse

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gigacode_graph.sources import RepositorySourceManager

MAX_FILES = 256
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PACKAGE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SKILLS = 200
MAX_SCAN_ENTRIES = 20_000
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def safe_relative_path(value: str, *, allow_root: bool = False) -> str:
    """Accept portable, unambiguous paths without resolving symlinks."""
    if allow_root and value in {"", "."}:
        return "."
    if not value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("Path must be a non-empty relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ":" in value
        or any(part in {"", ".", "..", ".git"} for part in value.split("/"))
    ):
        raise ValueError("Path must stay inside the repository and cannot contain .git")
    return path.as_posix()


class SourceConfig(BaseModel):
    """Public source configuration; credentials belong in server Git helpers."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    git_url: str = Field(min_length=1, max_length=2048)
    ref: str = Field(default="HEAD", min_length=1, max_length=256)
    skills_path: str = "skills"
    recursive: bool = True
    enabled: bool = True
    interval_minutes: int = Field(default=30, ge=0, le=525600)
    auto_publish: bool = True

    @field_validator("git_url")
    @classmethod
    def validate_git_url(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("Git URL contains control characters")
        if not RepositorySourceManager._is_git_url(value):
            raise ValueError("Use a Git URL, including file:// for server-local Git repositories")
        value = RepositorySourceManager._validated_url(value)
        parsed = urlparse(value)
        if parsed.scheme == "file":
            if parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/"):
                raise ValueError("file:// must refer to an absolute server-local Git repository")
        elif parsed.scheme and not parsed.hostname:
            raise ValueError("Git URL must include a hostname")
        return value

    @field_validator("skills_path")
    @classmethod
    def validate_skills_path(cls, value: str) -> str:
        return safe_relative_path(value, allow_root=True)

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        if value.startswith("-") or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("Git ref cannot start with '-' or contain whitespace")
        return value


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    content: bytes
    sha256: str
    executable: bool

    def manifest(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": len(self.content),
            "sha256": self.sha256,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class SkillSnapshot:
    relative_path: str
    name: str
    description: str
    metadata: dict[str, Any]
    revision: str
    files: tuple[SnapshotFile, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "name": self.name,
            "description": self.description,
            "revision": self.revision,
            "file_count": len(self.files),
            "total_bytes": sum(len(file.content) for file in self.files),
        }


@dataclass(frozen=True)
class SkillScanIssue:
    relative_path: str
    path: str
    error: str

    def to_dict(self) -> dict[str, str]:
        return {"relative_path": self.relative_path, "path": self.path, "error": self.error}


@dataclass(frozen=True)
class SkillScanResult:
    snapshots: list[SkillSnapshot]
    issues: list[SkillScanIssue]


class _SkillLoader(yaml.SafeLoader):  # type: ignore[misc]
    # Copy the resolver table: changing SafeLoader globally would affect other application YAML.
    yaml_implicit_resolvers: ClassVar[dict[str | None, list[Any]]] = {
        first: [
            resolver
            for resolver in resolvers
            if resolver[0] not in {"tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:bool"}
        ]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        if isinstance(node, yaml.MappingNode):
            keys: set[Any] = set()
            # Inspect explicit keys before flattening YAML merges. A merged default may legally
            # be overridden, but duplicate keys written directly in the same mapping are errors.
            for key_node, _value_node in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in keys
                    keys.add(key)
                except TypeError as exc:
                    raise yaml.constructor.ConstructorError(
                        None, None, "unsupported mapping key", key_node.start_mark
                    ) from exc
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        None, None, "duplicate mapping key", key_node.start_mark
                    )
        return super().construct_mapping(node, deep=deep)

    def construct_timestamp_string(self, node: Any) -> str:
        value: str = self.construct_scalar(node)
        if not self.timestamp_regexp.match(value):
            raise yaml.constructor.ConstructorError(
                None, None, "invalid timestamp", node.start_mark
            )
        # Check explicit timestamps (including calendar values) before preserving their text.
        # PyYAML otherwise dereferences a failed regex match for an invalid !!timestamp scalar.
        yaml.SafeLoader.construct_yaml_timestamp(self, node)
        return value


_SkillLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.I), list("tTfF")
)
_SkillLoader.add_constructor("tag:yaml.org,2002:timestamp", _SkillLoader.construct_timestamp_string)


def _metadata(content: bytes) -> dict[str, Any]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("SKILL.md must be UTF-8") from exc
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip()), None)
    if start is None or lines[start].rstrip() != "---":
        raise ValueError("SKILL.md requires YAML frontmatter with name and description")
    end = next(
        (
            i
            for i, line in enumerate(lines[start + 1 :], start + 1)
            if line.rstrip() in {"---", "..."}
        ),
        None,
    )
    if end is None:
        raise ValueError("SKILL.md frontmatter is not terminated")
    header = "\n".join(lines[start + 1 : end])
    if len(header.encode()) > 64 * 1024:
        raise ValueError("SKILL.md frontmatter exceeds 64 KiB")
    try:
        metadata = yaml.load(header, Loader=_SkillLoader)
    except yaml.YAMLError as exc:
        reason = (
            "duplicate mapping key"
            if getattr(exc, "problem", None) == "duplicate mapping key"
            else "invalid YAML"
        )
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + start + 2}, column {mark.column + 1}" if mark else ""
        # PyYAML's str(exc) includes raw source lines, which may contain secrets or instructions.
        raise ValueError(f"Invalid SKILL.md frontmatter: {reason}{location}") from exc
    except RecursionError as exc:
        raise ValueError("SKILL.md frontmatter structure is too complex") from exc
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        # Explicit standard YAML tags can raise constructor errors containing raw scalar data.
        raise ValueError("Invalid SKILL.md frontmatter: invalid YAML value") from exc
    try:
        # Bound structure before JSON encoding: YAML permits aliases and recursive values.
        _check_metadata(metadata, seen=set(), budget=[2000])
        serialized = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(
            "SKILL.md frontmatter must contain bounded, JSON-compatible, non-recursive values"
        ) from exc
    if not isinstance(metadata, dict) or len(serialized.encode()) > 64 * 1024:
        raise ValueError("SKILL.md frontmatter must be a small mapping")
    name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(name, str) or len(name) > 64 or not _NAME.fullmatch(name):
        raise ValueError("Skill name must use lowercase letters, numbers and single hyphens (1-64)")
    if not isinstance(description, str) or not description.strip() or len(description) > 8192:
        raise ValueError("Skill description must be non-empty text (at most 8192 characters)")
    return dict(metadata)


def _check_metadata(value: Any, *, seen: set[int], budget: list[int], depth: int = 0) -> None:
    budget[0] -= 1
    if budget[0] < 0 or depth > 12:
        raise ValueError("Frontmatter structure is too complex")
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if not isinstance(value, (dict, list)) or id(value) in seen:
        raise ValueError("Frontmatter must contain JSON-compatible, non-recursive values")
    seen.add(id(value))
    children: Iterable[Any]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Frontmatter mapping keys must be strings")
        children = value.values()
    else:
        children = value
    for child in children:
        _check_metadata(child, seen=seen, budget=budget, depth=depth + 1)
    seen.remove(id(value))


class _SourceLimitError(ValueError):
    """Source-wide exhaustion must never be mistaken for one invalid package."""


class _PackageReadError(ValueError):
    def __init__(self, message: str, path: str = ".") -> None:
        super().__init__(message)
        self.path = path


@dataclass
class _ScanBudget:
    bytes_read: int = 0

    def charge(self, count: int) -> None:
        self.bytes_read += count
        if self.bytes_read > MAX_SOURCE_BYTES:
            raise _SourceLimitError("Source exceeds 64 MiB of skill content")


def _walk_error(error: OSError) -> None:
    raise error


def scan_skills(checkout: Path, skills_path: str, recursive: bool) -> SkillScanResult:
    """Read complete skill packages. Never execute repository code or follow links."""
    skills_path = safe_relative_path(skills_path, allow_root=True)
    root = checkout
    if root.is_symlink():
        raise ValueError("Skills checkout must not be a symlink")
    for part in PurePosixPath(skills_path).parts:
        root = root / part
        if root.is_symlink():
            raise ValueError(f"Skills path contains a symlink: {skills_path}")
    if not root.is_dir():
        raise ValueError(f"Skills directory does not exist: {skills_path}")
    roots: list[Path] = []
    entries = 0
    # A package's descendants belong to that package, including nested SKILL.md files.
    for directory, directories, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
        path = Path(directory)
        directories[:] = sorted(name for name in directories if name != ".git")
        entries += len(directories) + len(filenames)
        if entries > MAX_SCAN_ENTRIES:
            raise ValueError(f"Source exceeds {MAX_SCAN_ENTRIES} scanned entries")
        if "SKILL.md" in filenames:
            roots.append(path)
            # Once found, all descendants belong to this package. Their errors must reject only
            # this package, and a nested SKILL.md must not become a second independent skill.
            directories.clear()
            if len(roots) > MAX_SKILLS:
                raise ValueError(f"Source exceeds {MAX_SKILLS} skills")
            continue
        for name in [*directories, *filenames]:
            if (path / name).is_symlink():
                raise ValueError("Symlink is not allowed during skills source discovery")
        if not recursive and path != root:
            directories.clear()
    snapshots: list[SkillSnapshot] = []
    issues: list[SkillScanIssue] = []
    budget = _ScanBudget()
    for package in sorted(roots):
        relative = package.relative_to(root).as_posix()
        try:
            safe_relative_path(relative, allow_root=True)
            files = _read_package(package, source_budget=budget)
        except _SourceLimitError:
            raise
        except (ValueError, OSError) as exc:
            error_path = exc.path if isinstance(exc, _PackageReadError) else "."
            issue_path = (PurePosixPath(relative) / error_path).as_posix()
            error = "Cannot read skill package files" if isinstance(exc, OSError) else str(exc)
            issues.append(SkillScanIssue(relative, issue_path, error))
            continue
        try:
            content = next((file.content for file in files if file.path == "SKILL.md"), None)
            if content is None:
                raise ValueError("Skill package no longer contains SKILL.md")
            metadata = _metadata(content)
        except ValueError as exc:
            issues.append(
                SkillScanIssue(
                    relative, (PurePosixPath(relative) / "SKILL.md").as_posix(), str(exc)
                )
            )
            continue
        manifest = [file.manifest() for file in files]
        revision = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        snapshots.append(
            SkillSnapshot(
                relative_path=relative,
                name=metadata["name"],
                description=metadata["description"],
                metadata=metadata,
                revision=revision,
                files=tuple(files),
            )
        )
    return SkillScanResult(snapshots, issues)


def _read_package(package: Path, *, source_budget: _ScanBudget) -> list[SnapshotFile]:
    files: list[SnapshotFile] = []
    total_bytes = 0
    entries = 0
    for directory, directories, filenames in os.walk(
        package, followlinks=False, onerror=_walk_error
    ):
        directories[:] = sorted(name for name in directories if name != ".git")
        path = Path(directory)
        for name in [*directories, *filenames]:
            entries += 1
            if entries > MAX_SCAN_ENTRIES:
                raise _PackageReadError(f"Package exceeds {MAX_SCAN_ENTRIES} scanned entries")
            if (path / name).is_symlink():
                relative = (path / name).relative_to(package).as_posix()
                raise _PackageReadError("Symlink is not allowed in a skill package", relative)
        for name in sorted(filenames):
            if name == ".gigacode-graph-source.json":
                continue
            file = path / name
            relative = safe_relative_path(file.relative_to(package).as_posix())
            info = file.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise _PackageReadError(
                    "Only regular files are allowed in a skill package", relative
                )
            if len(files) >= MAX_FILES or info.st_size > MAX_FILE_BYTES:
                raise _PackageReadError(
                    f"Skill exceeds {MAX_FILES} files or 2 MiB per file", relative
                )
            descriptor = os.open(
                file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            )
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or (info.st_dev, info.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    raise _PackageReadError("Skill file changed during reading", relative)
                content = stream.read(MAX_FILE_BYTES + 1)
            source_budget.charge(len(content))
            total_bytes += len(content)
            if len(content) > MAX_FILE_BYTES or total_bytes > MAX_PACKAGE_BYTES:
                raise _PackageReadError(
                    "Skill package exceeds size limit (16 MiB total, 2 MiB per file)", relative
                )
            files.append(
                SnapshotFile(
                    path=relative,
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                    executable=bool(info.st_mode & 0o111),
                )
            )
    return sorted(files, key=lambda file: file.path)
