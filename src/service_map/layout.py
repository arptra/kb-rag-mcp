"""Fast repository layout discovery for Maven/Gradle monorepositories.

The layout pass never executes repository code or a build tool. It reads build descriptors,
application configuration and the optional graph manifest, then produces isolated scan targets.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree

BuildSystem = Literal["maven", "gradle", "unknown"]
ModuleState = Literal["active", "empty", "unsupported"]
ModuleRole = Literal["service", "component", "container"]

_IGNORED_DIRECTORIES = {
    ".git",
    ".gradle",
    ".idea",
    ".mvn",
    ".settings",
    "build",
    "dist",
    "generated",
    "node_modules",
    "out",
    "target",
    "vendor",
}
_APPLICATION_NAMES = ("application", "bootstrap")


@dataclass(frozen=True, slots=True)
class ModuleLayout:
    root: Path
    relative_path: str
    service_id: str
    display_name: str
    owner: str | None
    aliases: tuple[str, ...]
    build_system: BuildSystem
    state: ModuleState
    role: ModuleRole
    declared: bool
    source_roots: tuple[Path, ...]
    resource_roots: tuple[Path, ...]
    openspec_roots: tuple[Path, ...]
    source_files: tuple[Path, ...] = ()
    resource_files: tuple[Path, ...] = ()
    component_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LayoutIssue:
    file: str | None
    message: str


@dataclass(frozen=True, slots=True)
class RepositoryLayout:
    root: Path
    modules: tuple[ModuleLayout, ...]
    openspec_roots: tuple[Path, ...]
    issues: tuple[LayoutIssue, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class _Candidate:
    root: Path
    build_system: BuildSystem = "unknown"
    declared: bool = False


@dataclass(frozen=True, slots=True)
class _RepositoryInventory:
    """Files collected by the only recursive walk performed during layout discovery."""

    descriptors: tuple[Path, ...]
    source_files: tuple[Path, ...]
    resource_files: tuple[Path, ...]
    openspec_roots: tuple[Path, ...]


class RepositoryLayoutAnalyzer:
    """Discover independently scannable modules without invoking Maven or Gradle."""

    def discover(
        self,
        repository: Path,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> RepositoryLayout:
        root = repository.resolve()
        if not root.is_dir():
            raise ValueError(f"Repository directory does not exist: {root}")

        issues: list[LayoutIssue] = []
        manifest = self._manifest(root, issues)
        manifest_modules = self._manifest_modules(root, manifest, issues)
        candidates: dict[Path, _Candidate] = {
            path: _Candidate(path, declared=True) for path in manifest_modules
        }

        inventory = self._inventory(root, progress=progress)
        descriptors = inventory.descriptors
        maven_modules: set[Path] = set()
        gradle_modules: set[Path] = set()
        for descriptor in descriptors:
            module_root = descriptor.parent.resolve()
            if descriptor.name == "pom.xml":
                self._merge_candidate(candidates, module_root, "maven", declared=False)
                maven_modules.update(self._maven_modules(root, descriptor, issues))
            elif descriptor.name in {"build.gradle", "build.gradle.kts"}:
                self._merge_candidate(candidates, module_root, "gradle", declared=False)
            elif descriptor.name in {"settings.gradle", "settings.gradle.kts"}:
                gradle_modules.update(self._gradle_modules(root, descriptor, issues))

        for module_root in maven_modules:
            self._merge_candidate(candidates, module_root, "maven", declared=True)
        for module_root in gradle_modules:
            self._merge_candidate(candidates, module_root, "gradle", declared=True)

        if not candidates:
            candidates[root] = _Candidate(root)

        if progress is not None:
            progress(
                f"Layout descriptors resolved: repository={root.name}; "
                f"descriptors={len(descriptors)}; candidates={len(candidates)}"
            )

        candidate_roots = set(candidates)
        owned_sources = self._owned_files(inventory.source_files, candidate_roots, root)
        owned_resources = self._owned_files(inventory.resource_files, candidate_roots, root)
        owned_openspec = self._owned_files(inventory.openspec_roots, candidate_roots, root)
        modules: list[ModuleLayout] = []
        ordered_candidates = sorted(
            candidates.values(), key=lambda item: self._relative(root, item.root)
        )
        for position, candidate in enumerate(ordered_candidates, start=1):
            relative_path = self._relative(root, candidate.root)
            if progress is not None:
                progress(
                    f"Layout module [{position}/{len(ordered_candidates)}] inspect: "
                    f"{relative_path}; build={candidate.build_system}"
                )
            explicit = self._module_manifest(root, candidate.root, manifest, manifest_modules)
            source_roots = self._source_roots(candidate.root, candidate.build_system)
            resource_roots = self._resource_roots(candidate.root, candidate.build_system)
            source_files = tuple(
                path
                for path in owned_sources.get(candidate.root, ())
                if any(path.is_relative_to(source_root) for source_root in source_roots)
            )
            resource_files = tuple(
                path
                for path in owned_resources.get(candidate.root, ())
                if any(path.is_relative_to(resource_root) for resource_root in resource_roots)
            )
            has_sources = bool(source_files)
            spring_name = self._spring_application_name_files(resource_files)
            explicit_service = isinstance(explicit, dict) and isinstance(
                explicit.get("service"), dict
            )
            service_marker = bool(spring_name) or self._has_service_marker(
                source_files,
                module_path=relative_path,
                progress=progress,
            )
            role: ModuleRole = (
                "service"
                if explicit_service or service_marker or (candidate.declared and not has_sources)
                else "component"
                if has_sources
                else "container"
            )

            service = explicit.get("service", {}) if isinstance(explicit, dict) else {}
            service = service if isinstance(service, dict) else {}
            artifact = self._maven_artifact(candidate.root / "pom.xml", issues, root)
            gradle_name = self._gradle_project_name(candidate.root)
            fallback = root.name if relative_path == "." else candidate.root.name
            service_id = (
                str(service.get("id", "")).strip()
                or spring_name
                or artifact
                or gradle_name
                or fallback
            )
            display_name = str(service.get("displayName", "")).strip() or service_id
            owner = str(service.get("owner", "")).strip() or None
            raw_aliases = service.get("aliases", [])
            aliases = {
                service_id,
                fallback,
                *(str(item).strip() for item in raw_aliases if isinstance(raw_aliases, list)),
            }
            aliases.discard("")
            state: ModuleState = "active" if has_sources else "empty"
            modules.append(
                ModuleLayout(
                    root=candidate.root,
                    relative_path=relative_path,
                    service_id=service_id,
                    display_name=display_name,
                    owner=owner,
                    aliases=tuple(sorted(aliases)),
                    build_system=candidate.build_system,
                    state=state,
                    role=role,
                    declared=candidate.declared or candidate.root in manifest_modules,
                    source_roots=source_roots,
                    resource_roots=resource_roots,
                    openspec_roots=owned_openspec.get(candidate.root, ()),
                    source_files=source_files,
                    resource_files=resource_files,
                )
            )
            if progress is not None:
                progress(
                    f"Layout module [{position}/{len(ordered_candidates)}] ready: "
                    f"{relative_path}; role={role}; state={state}; "
                    f"sources={len(source_files)}; resources={len(resource_files)}"
                )

        if not modules:
            modules.append(self._fallback_module(root, manifest, issues))
        modules = self._attach_components(root, modules, issues)
        return RepositoryLayout(
            root=root,
            modules=tuple(modules),
            openspec_roots=inventory.openspec_roots,
            issues=tuple(issues),
        )

    def _attach_components(
        self,
        repository: Path,
        modules: list[ModuleLayout],
        issues: list[LayoutIssue],
    ) -> list[ModuleLayout]:
        """Attach library modules to a real service boundary without inventing services."""
        service_indexes = [index for index, item in enumerate(modules) if item.role == "service"]
        components = [item for item in modules if item.role == "component"]
        if not service_indexes and components:
            # There is no deployable marker to trust. Preserve useful analysis by treating only
            # source-bearing modules as provisional boundaries and report the uncertainty.
            promoted_paths = {item.relative_path for item in components}
            modules = [
                replace(item, role="service") if item.relative_path in promoted_paths else item
                for item in modules
            ]
            issues.append(
                LayoutIssue(
                    None,
                    "No deployable service markers were found; source modules were promoted to "
                    "provisional services. Define gigacode-graph.json for exact boundaries.",
                )
            )
            return modules
        if not service_indexes:
            # An empty repository still gets one visible placeholder, not one service per empty
            # Maven/Gradle module.
            preferred = next((item for item in modules if item.root == repository), modules[0])
            return [replace(preferred, role="service")]

        service_modules = [modules[index] for index in service_indexes]
        attachments: dict[str, list[ModuleLayout]] = {
            item.relative_path: [] for item in service_modules
        }
        for component in components:
            ancestors = [
                service
                for service in service_modules
                if component.root.is_relative_to(service.root)
            ]
            owner = max(ancestors, key=lambda item: len(item.root.parts)) if ancestors else None
            if owner is None and len(service_modules) == 1:
                owner = service_modules[0]
            if owner is None:
                issues.append(
                    LayoutIssue(
                        component.relative_path,
                        "Library/component module is not attached because several service "
                        "boundaries are possible; declare its service in gigacode-graph.json.",
                    )
                )
                continue
            attachments[owner.relative_path].append(component)

        result: list[ModuleLayout] = []
        for module in modules:
            if module.role != "service":
                continue
            attached = attachments.get(module.relative_path, [])
            result.append(
                replace(
                    module,
                    source_roots=tuple(
                        sorted(
                            {
                                *module.source_roots,
                                *(root for item in attached for root in item.source_roots),
                            }
                        )
                    ),
                    resource_roots=tuple(
                        sorted(
                            {
                                *module.resource_roots,
                                *(root for item in attached for root in item.resource_roots),
                            }
                        )
                    ),
                    openspec_roots=tuple(
                        sorted(
                            {
                                *module.openspec_roots,
                                *(root for item in attached for root in item.openspec_roots),
                            }
                        )
                    ),
                    source_files=tuple(
                        sorted(
                            {
                                *module.source_files,
                                *(path for item in attached for path in item.source_files),
                            }
                        )
                    ),
                    resource_files=tuple(
                        sorted(
                            {
                                *module.resource_files,
                                *(path for item in attached for path in item.resource_files),
                            }
                        )
                    ),
                    component_paths=tuple(sorted(item.relative_path for item in attached)),
                )
            )
        return result

    @staticmethod
    def _merge_candidate(
        candidates: dict[Path, _Candidate],
        root: Path,
        build_system: BuildSystem,
        *,
        declared: bool,
    ) -> None:
        current = candidates.get(root)
        if current is None:
            candidates[root] = _Candidate(root, build_system, declared)
            return
        if current.build_system == "unknown":
            current.build_system = build_system
        current.declared = current.declared or declared

    @staticmethod
    def _relative(repository: Path, path: Path) -> str:
        relative = path.relative_to(repository).as_posix()
        return relative or "."

    def _fallback_module(
        self,
        root: Path,
        manifest: dict[str, Any],
        issues: list[LayoutIssue],
    ) -> ModuleLayout:
        service = manifest.get("service", {}) if isinstance(manifest, dict) else {}
        service = service if isinstance(service, dict) else {}
        build_system: BuildSystem = "maven" if (root / "pom.xml").is_file() else "unknown"
        source_roots = self._source_roots(root, build_system)
        resource_roots = self._resource_roots(root, build_system)
        spring_name = self._spring_application_name(resource_roots)
        artifact = self._maven_artifact(root / "pom.xml", issues, root)
        service_id = str(service.get("id", "")).strip() or spring_name or artifact or root.name
        raw_aliases = service.get("aliases", [])
        aliases = {service_id, root.name}
        if isinstance(raw_aliases, list):
            aliases.update(str(item).strip() for item in raw_aliases if str(item).strip())
        return ModuleLayout(
            root=root,
            relative_path=".",
            service_id=service_id,
            display_name=str(service.get("displayName", "")).strip() or service_id,
            owner=str(service.get("owner", "")).strip() or None,
            aliases=tuple(sorted(aliases)),
            build_system=build_system,
            state=(
                "active"
                if any(
                    self._contains_suffix(path, suffix, set())
                    for path in source_roots
                    for suffix in (".java", ".kt")
                )
                else "empty"
            ),
            role="service",
            declared=True,
            source_roots=source_roots,
            resource_roots=resource_roots,
            openspec_roots=self._openspec_roots(root, set()),
            source_files=tuple(
                sorted(
                    path
                    for source_root in source_roots
                    for path in self._files(source_root, {".java", ".kt"})
                )
            ),
            resource_files=tuple(
                sorted(
                    path
                    for resource_root in resource_roots
                    for path in self._files(
                        resource_root, {".properties", ".yaml", ".yml", ".xml", ".sql"}
                    )
                )
            ),
        )

    def _inventory(
        self,
        root: Path,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> _RepositoryInventory:
        descriptor_names = {
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
        }
        descriptors: list[Path] = []
        source_files: list[Path] = []
        resource_files: list[Path] = []
        openspec_roots: list[Path] = []
        resource_suffixes = {".properties", ".yaml", ".yml", ".xml", ".sql"}
        visited_directories = 0
        visited_files = 0
        started_at = time.monotonic()
        last_report_at = started_at
        if progress is not None:
            progress(f"Layout inventory start: repository={root.name}; path={root}")
        for current, directories, files in os.walk(root, followlinks=False):
            directory = Path(current)
            visited_directories += 1
            visited_files += len(files)
            directories[:] = [
                name
                for name in directories
                if name not in _IGNORED_DIRECTORIES
                and not name.startswith(".")
                and not (directory / name).is_symlink()
            ]
            for name in tuple(directories):
                if name.lower() == "openspec":
                    openspec_roots.append((directory / name).resolve())
            for name in files:
                path = (directory / name).resolve()
                if path.is_symlink():
                    continue
                if name in descriptor_names:
                    descriptors.append(path)
                suffix = path.suffix.lower()
                if suffix in {".java", ".kt"}:
                    source_files.append(path)
                elif suffix in resource_suffixes:
                    resource_files.append(path)
            now = time.monotonic()
            if progress is not None and (
                visited_files % 1000 < len(files) or now - last_report_at >= 2
            ):
                progress(
                    f"Layout inventory running: repository={root.name}; "
                    f"directories={visited_directories}; files={visited_files}; "
                    f"java_kotlin={len(source_files)}; current={directory}; "
                    f"elapsed={now - started_at:.1f}s"
                )
                last_report_at = now
        if progress is not None:
            progress(
                f"Layout inventory ready: repository={root.name}; "
                f"directories={visited_directories}; files={visited_files}; "
                f"java_kotlin={len(source_files)}; resources={len(resource_files)}; "
                f"openspec={len(openspec_roots)}; elapsed={time.monotonic() - started_at:.3f}s"
            )
        return _RepositoryInventory(
            descriptors=tuple(sorted(set(descriptors))),
            source_files=tuple(sorted(set(source_files))),
            resource_files=tuple(sorted(set(resource_files))),
            openspec_roots=tuple(sorted(set(openspec_roots))),
        )

    @staticmethod
    def _owned_files(
        files: tuple[Path, ...],
        candidate_roots: set[Path],
        repository: Path,
    ) -> dict[Path, tuple[Path, ...]]:
        owned: dict[Path, list[Path]] = {path: [] for path in candidate_roots}
        for path in files:
            current = path if path.is_dir() else path.parent
            while current.is_relative_to(repository):
                if current in candidate_roots:
                    owned[current].append(path)
                    break
                if current == repository:
                    break
                current = current.parent
        return {key: tuple(value) for key, value in owned.items()}

    @staticmethod
    def _has_service_marker(
        files: tuple[Path, ...],
        *,
        module_path: str,
        progress: Callable[[str], None] | None = None,
    ) -> bool:
        markers = (
            "@SpringBootApplication",
            "@SpringBootConfiguration",
            "@RestController",
            "@Controller",
            "@KafkaListener",
            "@GrpcService",
            "@Scheduled",
        )
        started_at = time.monotonic()
        last_report_at = started_at
        if progress is not None and files:
            progress(f"Layout service markers start: module={module_path}; files={len(files)}")
        for position, path in enumerate(files, start=1):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(marker in text for marker in markers):
                if progress is not None:
                    progress(
                        f"Layout service marker found: module={module_path}; "
                        f"file={path.name}; checked={position}/{len(files)}"
                    )
                return True
            if path.suffix.lower() == ".kt" and re.search(r"(?m)^\s*fun\s+main\s*\(", text):
                if progress is not None:
                    progress(
                        f"Layout Kotlin main found: module={module_path}; "
                        f"file={path.name}; checked={position}/{len(files)}"
                    )
                return True
            if path.suffix.lower() == ".java" and re.search(r"\bstatic\s+void\s+main\s*\(", text):
                if progress is not None:
                    progress(
                        f"Layout Java main found: module={module_path}; "
                        f"file={path.name}; checked={position}/{len(files)}"
                    )
                return True
            now = time.monotonic()
            if progress is not None and (position % 500 == 0 or now - last_report_at >= 2):
                progress(
                    f"Layout service markers running: module={module_path}; "
                    f"checked={position}/{len(files)}; current={path.name}; "
                    f"elapsed={now - started_at:.1f}s"
                )
                last_report_at = now
        if progress is not None and files:
            progress(
                f"Layout service markers ready: module={module_path}; found=false; "
                f"checked={len(files)}; elapsed={time.monotonic() - started_at:.3f}s"
            )
        return False

    @staticmethod
    def _spring_application_name_files(files: tuple[Path, ...]) -> str | None:
        candidates = [
            path
            for path in files
            if path.suffix.lower() in {".properties", ".yaml", ".yml"}
            and path.name.startswith(_APPLICATION_NAMES)
        ]
        return RepositoryLayoutAnalyzer._application_name_from_paths(candidates)

    @staticmethod
    def _files(root: Path, suffixes: set[str]) -> tuple[Path, ...]:
        if not root.is_dir():
            return ()
        found: list[Path] = []
        for current, directories, files in os.walk(root, followlinks=False):
            directory = Path(current)
            directories[:] = [
                name
                for name in directories
                if name not in _IGNORED_DIRECTORIES and not (directory / name).is_symlink()
            ]
            found.extend(
                directory / name for name in files if (directory / name).suffix.lower() in suffixes
            )
        return tuple(found)

    def _descriptor_files(self, root: Path) -> tuple[Path, ...]:
        names = {
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
        }
        found: list[Path] = []
        for current, directories, files in os.walk(root, followlinks=False):
            directory = Path(current)
            directories[:] = [
                name
                for name in directories
                if name not in _IGNORED_DIRECTORIES
                and not name.startswith(".")
                and not (directory / name).is_symlink()
            ]
            found.extend(directory / name for name in files if name in names)
        return tuple(sorted(found))

    def _maven_modules(
        self,
        repository: Path,
        pom: Path,
        issues: list[LayoutIssue],
    ) -> set[Path]:
        try:
            root = ElementTree.fromstring(pom.read_text(encoding="utf-8"))
        except (ElementTree.ParseError, OSError, UnicodeDecodeError) as exc:
            issues.append(
                LayoutIssue(self._relative(repository, pom), f"Cannot read Maven POM: {exc}")
            )
            return set()
        modules: set[Path] = set()
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] != "module" or not element.text:
                continue
            candidate = (pom.parent / element.text.strip()).resolve()
            if candidate.is_relative_to(repository) and candidate.is_dir():
                modules.add(candidate)
            else:
                module_name = element.text.strip()
                issues.append(
                    LayoutIssue(
                        self._relative(repository, pom),
                        f"Declared Maven module is missing or outside repository: {module_name}",
                    )
                )
        return modules

    def _gradle_modules(
        self,
        repository: Path,
        settings: Path,
        issues: list[LayoutIssue],
    ) -> set[Path]:
        try:
            text = settings.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            issues.append(
                LayoutIssue(
                    self._relative(repository, settings), f"Cannot read Gradle settings: {exc}"
                )
            )
            return set()
        modules: set[Path] = set()
        for statement in re.finditer(r"(?m)^\s*include\s*(?:\((.*?)\)|(.*))$", text):
            arguments = statement.group(1) or statement.group(2) or ""
            for match in re.finditer(r"[\"'](:?[^\"']+)[\"']", arguments):
                notation = match.group(1).strip().lstrip(":")
                if not notation:
                    continue
                candidate = (settings.parent / notation.replace(":", "/")).resolve()
                if candidate.is_relative_to(repository) and candidate.is_dir():
                    modules.add(candidate)
                else:
                    module_name = match.group(1)
                    issues.append(
                        LayoutIssue(
                            self._relative(repository, settings),
                            f"Missing/outside Gradle module: {module_name}",
                        )
                    )
        return modules

    @staticmethod
    def _manifest(repository: Path, issues: list[LayoutIssue]) -> dict[str, Any]:
        path = repository / "gigacode-graph.json"
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            issues.append(LayoutIssue("gigacode-graph.json", f"Cannot read graph manifest: {exc}"))
            return {}
        if not isinstance(payload, dict):
            issues.append(
                LayoutIssue("gigacode-graph.json", "Graph manifest must be a JSON object")
            )
            return {}
        return payload

    def _manifest_modules(
        self,
        repository: Path,
        manifest: dict[str, Any],
        issues: list[LayoutIssue],
    ) -> set[Path]:
        raw = manifest.get("modules", [])
        if not isinstance(raw, list):
            issues.append(LayoutIssue("gigacode-graph.json", "Manifest modules must be an array"))
            return set()
        modules: set[Path] = set()
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                issues.append(
                    LayoutIssue("gigacode-graph.json", "Each manifest module needs a path")
                )
                continue
            candidate = (repository / item["path"]).resolve()
            if candidate.is_relative_to(repository) and candidate.is_dir():
                modules.add(candidate)
            else:
                issues.append(
                    LayoutIssue(
                        "gigacode-graph.json",
                        f"Manifest module is missing or outside repository: {item['path']}",
                    )
                )
        return modules

    @staticmethod
    def _module_manifest(
        repository: Path,
        module: Path,
        manifest: dict[str, Any],
        manifest_modules: set[Path],
    ) -> dict[str, Any]:
        if module == repository and module not in manifest_modules:
            return manifest
        raw = manifest.get("modules", [])
        if not isinstance(raw, list):
            return {}
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            if (repository / item["path"]).resolve() == module:
                return item
        return {}

    @staticmethod
    def _source_roots(module: Path, build_system: BuildSystem) -> tuple[Path, ...]:
        conventional = [
            module / "src" / "main" / "java",
            module / "src" / "main" / "kotlin",
        ]
        existing = tuple(path.resolve() for path in conventional if path.is_dir())
        if existing:
            return existing
        if build_system != "unknown":
            return ()
        source = module / "src"
        return (source.resolve(),) if source.is_dir() else (module.resolve(),)

    @staticmethod
    def _resource_roots(module: Path, build_system: BuildSystem) -> tuple[Path, ...]:
        conventional = module / "src" / "main" / "resources"
        if conventional.is_dir():
            return (conventional.resolve(),)
        return () if build_system != "unknown" else (module.resolve(),)

    def _all_openspec_roots(self, repository: Path) -> tuple[Path, ...]:
        return self._openspec_roots(repository, set())

    @staticmethod
    def _openspec_roots(root: Path, excluded: set[Path]) -> tuple[Path, ...]:
        found: list[Path] = []
        for current, directories, _files in os.walk(root, followlinks=False):
            directory = Path(current)
            if any(directory == item or directory.is_relative_to(item) for item in excluded):
                directories[:] = []
                continue
            directories[:] = [
                name
                for name in directories
                if name not in _IGNORED_DIRECTORIES
                and not name.startswith(".")
                and not (directory / name).is_symlink()
            ]
            for name in tuple(directories):
                if name.lower() == "openspec":
                    found.append((directory / name).resolve())
                    directories.remove(name)
        return tuple(sorted(set(found)))

    @staticmethod
    def _contains_suffix(root: Path, suffix: str, excluded: set[Path]) -> bool:
        if not root.is_dir():
            return False
        for current, directories, files in os.walk(root, followlinks=False):
            directory = Path(current)
            if any(directory == item or directory.is_relative_to(item) for item in excluded):
                directories[:] = []
                continue
            directories[:] = [
                name
                for name in directories
                if name not in _IGNORED_DIRECTORIES and not (directory / name).is_symlink()
            ]
            if any(name.lower().endswith(suffix) for name in files):
                return True
        return False

    @staticmethod
    def _spring_application_name(
        resource_roots: tuple[Path, ...],
        excluded: set[Path] | None = None,
    ) -> str | None:
        excluded = excluded or set()
        candidates: list[Path] = []
        for root in resource_roots:
            if not root.is_dir():
                continue
            for current, directories, files in os.walk(root, followlinks=False):
                directory = Path(current)
                if any(directory == item or directory.is_relative_to(item) for item in excluded):
                    directories[:] = []
                    continue
                directories[:] = [
                    name
                    for name in directories
                    if name not in _IGNORED_DIRECTORIES and not (directory / name).is_symlink()
                ]
                for name in files:
                    path = directory / name
                    if path.suffix.lower() in {".properties", ".yaml", ".yml"} and name.startswith(
                        _APPLICATION_NAMES
                    ):
                        candidates.append(path)
        return RepositoryLayoutAnalyzer._application_name_from_paths(candidates)

    @staticmethod
    def _application_name_from_paths(candidates: list[Path]) -> str | None:
        for path in sorted(candidates):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            match = re.search(r"(?m)^\s*spring\.application\.name\s*[:=]\s*([^\s#]+)", text)
            if match:
                return match.group(1).strip().strip("\"'")
            yaml = re.search(
                r"(?ms)^spring\s*:\s*\n(?:(?:[ \t]+.*)?\n)*?"
                r"[ \t]+application\s*:\s*\n(?:(?:[ \t]+.*)?\n)*?"
                r"[ \t]+name\s*:\s*([^\s#]+)",
                text,
            )
            if yaml:
                return yaml.group(1).strip().strip("\"'")
        return None

    def _maven_artifact(
        self,
        pom: Path,
        issues: list[LayoutIssue],
        repository: Path,
    ) -> str | None:
        if not pom.is_file():
            return None
        try:
            root = ElementTree.fromstring(pom.read_text(encoding="utf-8"))
        except (ElementTree.ParseError, OSError, UnicodeDecodeError) as exc:
            relative = (
                self._relative(repository, pom) if pom.is_relative_to(repository) else str(pom)
            )
            issues.append(LayoutIssue(relative, f"Cannot read Maven artifactId: {exc}"))
            return None
        for child in root:
            if child.tag.rsplit("}", 1)[-1] == "artifactId" and child.text:
                return child.text.strip()
        return None

    @staticmethod
    def _gradle_project_name(module: Path) -> str | None:
        for name in ("settings.gradle", "settings.gradle.kts"):
            path = module / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            match = re.search(r"\brootProject\.name\s*=\s*[\"']([^\"']+)[\"']", text)
            if match:
                return match.group(1)
        return None
