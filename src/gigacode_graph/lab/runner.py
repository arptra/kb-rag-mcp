"""End-to-end graph-lab runs with replayable inputs and complete artifacts."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from corporate_kb.config import Settings
from corporate_kb.gigacode_runner import GigaCodeRunner
from corporate_kb.graph_verifier import GraphGigaCodeVerifier
from gigacode_graph.algorithms import get_graph_algorithm
from gigacode_graph.config import GraphSettings
from gigacode_graph.lab.artifacts import LabArtifacts
from gigacode_graph.lab.models import GraphLabCase, LabRepository, dump_yaml_model, load_yaml_model
from gigacode_graph.lab.validation import compare_graphs, validate_graph
from gigacode_graph.models import GraphSnapshot, IngestionRecord
from gigacode_graph.sources import RepositorySourceManager, RepositorySpec
from gigacode_graph.store import JsonGraphStore
from service_map import RepositoryInput, ServiceMapBuilder, finalize_snapshot


class GraphLabRunner:
    """Materialize inputs, execute an algorithm and leave enough state to reproduce it."""

    def __init__(self, lab_root: Path, *, project_root: Path | None = None) -> None:
        self.lab_root = lab_root.resolve()
        self.project_root = (project_root or Path.cwd()).resolve()

    def run(
        self,
        case_path: Path,
        *,
        mode: Literal["static", "gigacode"] | None = None,
        algorithm: str | None = None,
        cleanup: bool = True,
    ) -> Path:
        case_file = case_path.resolve()
        case = load_yaml_model(case_file, GraphLabCase)
        selected_algorithm = algorithm or case.algorithm
        descriptor = get_graph_algorithm(selected_algorithm).descriptor
        selected_mode = mode or case.mode
        run_id = LabArtifacts.new_run_id(case.id)
        artifacts = LabArtifacts(self.lab_root / "runs" / run_id)
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        records: list[IngestionRecord] = []
        artifacts.event(
            "run",
            "started",
            run_id=run_id,
            case_id=case.id,
            algorithm=selected_algorithm,
            mode=selected_mode,
        )
        settings = self._settings(artifacts.directory, selected_algorithm)
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "status": "running",
            "started_at": started_at.isoformat(),
            "case_file": str(case_file),
            "case_id": case.id,
            "algorithm": descriptor.as_dict(),
            "mode": selected_mode,
            "verify_all": case.verify_all,
            "environment": self._environment(),
            "limits": self._limits(settings),
            "repositories": [],
        }
        artifacts.json("run.json", manifest)
        dump_yaml_model(artifacts.directory / "case.yaml", case)
        try:
            specs = [
                RepositorySpec(
                    source=item.source,
                    ref=item.ref,
                    base_directory=case_file.parent,
                )
                for item in case.repositories
            ]
            source_manager = RepositorySourceManager(settings)
            paths, records = source_manager.materialize(specs, refresh=True)
            inputs = self._repository_inputs(case.repositories, records)
            roots = {item.name: item.path for item in inputs}
            manifest["repositories"] = [item.model_dump(mode="json") for item in records]
            artifacts.json("repositories.json", manifest["repositories"])
            artifacts.event("materialize", "repositories-ready", count=len(paths))

            def progress(message: str) -> None:
                artifacts.event("analysis", message)

            result = ServiceMapBuilder(settings).build(
                inputs,
                progress=progress,
                force_all=True,
            )
            static_result = finalize_snapshot(result, mode="static")
            JsonGraphStore(artifacts.directory / "static-graph.json").save(
                static_result.graph
            )
            artifacts.json(
                "candidates.json",
                {
                    "schema_version": 1,
                    "dependencies": [
                        item.model_dump(mode="json")
                        for item in static_result.service_map.dependencies
                    ],
                    "gigacode_candidates": [
                        item.model_dump(mode="json")
                        for item in static_result.service_map.dependencies
                        if case.verify_all
                        or not item.resolved
                        or item.confidence in {"LOW", "UNRESOLVED"}
                    ],
                },
            )
            artifacts.json(
                "static-validation.json",
                validate_graph(static_result.graph, case, repository_roots=roots),
            )
            final_result = static_result
            verification: dict[str, Any] = {}
            if selected_mode == "gigacode":
                kb_settings = Settings().resolved(self.project_root)
                verifier = GraphGigaCodeVerifier(
                    GigaCodeRunner(kb_settings),
                    settings,
                    artifacts.directory,
                )
                verified, verification = verifier.verify(
                    result,
                    inputs,
                    verify_all=case.verify_all,
                    progress=progress,
                    debug_root=artifacts.directory / "gigacode",
                )
                final_result = finalize_snapshot(
                    verified,
                    mode=(
                        "static"
                        if verification.get("fallback") == "static-graph"
                        else "static+gigacode"
                    ),
                    verification=verification,
                )
                artifacts.json("verification.json", verification)
            JsonGraphStore(artifacts.directory / "final-graph.json").save(
                final_result.graph
            )
            validation = validate_graph(
                final_result.graph,
                case,
                repository_roots=roots,
            )
            artifacts.json("validation.json", validation)
            artifacts.text(
                "report.md",
                self._report(case, manifest, final_result.graph, validation, verification),
            )
            manifest.update(
                {
                    "status": (
                        "passed" if validation["status"] == "passed" else "failed"
                    ),
                    "finished_at": datetime.now(UTC).isoformat(),
                    "elapsed_seconds": round(time.monotonic() - started_monotonic, 6),
                    "peak_process_memory_mb": self._peak_process_memory_mb(),
                    "snapshot_id": final_result.graph.snapshot_id,
                    "stats": final_result.graph.stats(),
                    "validation": {
                        "status": validation["status"],
                        "failure_count": validation["failure_count"],
                        "warning_count": validation["warning_count"],
                    },
                }
            )
            artifacts.event("run", "finished", status=manifest["status"])
            self._write_replay_case(artifacts, case, records)
            artifacts.json("run.json", manifest)
            return artifacts.directory
        except BaseException as exc:
            manifest.update(
                {
                    "status": "error",
                    "finished_at": datetime.now(UTC).isoformat(),
                    "elapsed_seconds": round(time.monotonic() - started_monotonic, 6),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            artifacts.event("run", "error", error=manifest["error"])
            artifacts.json("run.json", manifest)
            raise
        finally:
            if cleanup:
                try:
                    removed = self._cleanup_managed(
                        records,
                        settings.repository_cache_path,
                    )
                    manifest["cleanup"] = {
                        "status": "completed",
                        "managed_checkouts_removed": removed,
                    }
                    artifacts.event("cleanup", "managed-checkouts-removed", count=removed)
                except OSError as exc:
                    manifest["cleanup"] = {
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    artifacts.event("cleanup", "failed", error=str(exc))
            else:
                manifest["cleanup"] = {"status": "skipped", "reason": "keep-checkouts"}
            artifacts.json("run.json", manifest)

    def replay(self, run_directory: Path, *, cleanup: bool = True) -> Path:
        source = run_directory.resolve()
        replay_case = source / "replay-case.yaml"
        before_path = source / "static-graph.json"
        if not replay_case.is_file() or not before_path.is_file():
            raise ValueError("Run does not contain replay-case.yaml and static-graph.json")
        replayed = self.run(replay_case, mode="static", cleanup=cleanup)
        comparison = compare_graphs(
            JsonGraphStore(before_path).load(),
            JsonGraphStore(replayed / "static-graph.json").load(),
        )
        (replayed / "replay-comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return replayed

    def _settings(self, run_directory: Path, algorithm: str) -> GraphSettings:
        state = self.lab_root / ".state"
        return GraphSettings(
            store_path=run_directory / "working-graph.json",
            ingestion_path=run_directory / "ingestion.json",
            repository_cache_path=state / "repositories",
            module_cache_path=state / "module-analysis",
            builder_algorithm=algorithm,
        ).resolved(self.project_root)

    @staticmethod
    def _repository_inputs(
        configured: list[LabRepository],
        records: list[IngestionRecord],
    ) -> list[RepositoryInput]:
        if len(configured) != len(records):
            raise RuntimeError("Materialized repository list does not match case inputs")
        result = []
        for position, (item, record) in enumerate(zip(configured, records, strict=True), start=1):
            checkout = Path(record.checkout_path).resolve()
            result.append(
                RepositoryInput(
                    path=checkout,
                    name=item.name or checkout.name or f"repository-{position}",
                    source_url=record.source if record.source_type == "git" else None,
                    commit=record.commit,
                )
            )
        return result

    @staticmethod
    def _write_replay_case(
        artifacts: LabArtifacts,
        case: GraphLabCase,
        records: list[IngestionRecord],
    ) -> None:
        repositories = []
        for configured, record in zip(case.repositories, records, strict=True):
            repositories.append(
                configured.model_copy(
                    update={
                        "source": record.source,
                        "ref": record.commit if record.source_type == "git" else None,
                    }
                )
            )
        dump_yaml_model(
            artifacts.directory / "replay-case.yaml",
            case.model_copy(update={"mode": "static", "repositories": repositories}),
        )

    @staticmethod
    def _cleanup_managed(records: list[IngestionRecord], cache_root: Path) -> int:
        removed = 0
        root = cache_root.resolve()
        for record in records:
            if record.source_type != "git":
                continue
            checkout = Path(record.checkout_path).resolve()
            try:
                checkout.relative_to(root)
            except ValueError:
                continue
            if not (checkout / ".gigacode-graph-source.json").is_file():
                continue
            shutil.rmtree(checkout)
            removed += 1
        return removed

    @staticmethod
    def _limits(settings: GraphSettings) -> dict[str, Any]:
        return {
            "call_depth": settings.call_depth,
            "max_service_seed_methods": settings.max_service_seed_methods,
            "max_traced_methods_per_service": settings.max_traced_methods_per_service,
            "max_call_edges_per_service": settings.max_call_edges_per_service,
            "max_weak_outbound_per_service": settings.max_weak_outbound_per_service,
            "gigacode_max_candidates_per_repository": (
                settings.gigacode_max_candidates_per_repository
            ),
        }

    def _environment(self) -> dict[str, Any]:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        return {
            "python": sys.version,
            "platform": platform.platform(),
            "project_root": str(self.project_root),
            "implementation_commit": revision.stdout.strip() or None,
        }

    @staticmethod
    def _peak_process_memory_mb() -> float | None:
        try:
            import resource

            maximum = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        except (ImportError, OSError, ValueError):
            return None
        divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
        return round(maximum / divisor, 3)

    @staticmethod
    def _report(
        case: GraphLabCase,
        manifest: dict[str, Any],
        graph: GraphSnapshot,
        validation: dict[str, Any],
        verification: dict[str, Any],
    ) -> str:
        lines = [
            f"# Graph run: {case.id}",
            "",
            case.description,
            "",
            f"- Algorithm: `{manifest['algorithm']['id']}@{manifest['algorithm']['version']}`",
            f"- Mode: `{manifest['mode']}`",
            f"- Snapshot: `{graph.snapshot_id}`",
            f"- Nodes / edges / evidence: {len(graph.nodes)} / {len(graph.edges)} / "
            f"{len(graph.evidence)}",
            f"- Validation: **{validation['status']}** "
            f"({validation['failure_count']} failures, {validation['warning_count']} warnings)",
        ]
        if verification:
            lines.append(
                "- GigaCode: "
                f"processed={verification.get('processed', 0)}, "
                f"discovered={verification.get('discovered', 0)}, "
                f"failed={verification.get('failed', 0)}"
            )
        if validation["failures"]:
            lines.extend(["", "## Failures", ""])
            lines.extend(f"- `{item['code']}` {item['message']}" for item in validation["failures"])
        return "\n".join(lines) + "\n"
