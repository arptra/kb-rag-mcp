"""Immutable analysis runs and portable SSOT input bundles."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gigacode_graph.models import GraphSnapshot
from service_map.models import ServiceMapSnapshot


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_name(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "-_" else "-" for character in value
    )
    return normalized.strip("-")[:100] or "service"


class AnalysisArchive:
    """Persist every successful source analysis and package it for an LLM."""

    def __init__(self, root: Path, skill_path: Path) -> None:
        self.root = root
        self.skill_path = skill_path

    def record(
        self,
        service_map: ServiceMapSnapshot,
        graph: GraphSnapshot,
        *,
        job_id: str | None,
        repository_count: int,
    ) -> dict[str, Any]:
        created_at = _now()
        run_id = f"{created_at:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        runs = self.root / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}-", dir=runs))
        destination = runs / run_id
        try:
            analysis = {
                "schema_version": 1,
                "run_id": run_id,
                "job_id": job_id,
                "created_at": created_at.isoformat(),
                "repository_count": repository_count,
                "service_map": service_map.model_dump(mode="json"),
                "graph": graph.model_dump(mode="json"),
            }
            (temporary / "services").mkdir()
            (temporary / "analysis.json").write_text(
                json.dumps(analysis, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            for service in service_map.services:
                service_payload = self._service_payload(service_map, graph, service.id)
                stem = _safe_name(service.id)
                (temporary / "services" / f"{stem}.json").write_text(
                    json.dumps(service_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                (temporary / "services" / f"{stem}.md").write_text(
                    self._service_markdown(service_payload),
                    encoding="utf-8",
                )
            temporary.replace(destination)
        except Exception:
            import shutil

            shutil.rmtree(temporary, ignore_errors=True)
            raise

        manifest = {
            "run_id": run_id,
            "job_id": job_id,
            "created_at": created_at.isoformat(),
            "path": str(destination),
            "repository_count": repository_count,
            "service_count": len(service_map.services),
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            "issue_count": len(service_map.issues) + len(graph.issues),
        }
        self._atomic_json(self.root / "latest.json", manifest)
        return manifest

    def overview(self) -> dict[str, Any]:
        path = self.root / "latest.json"
        if not path.is_file():
            return {"available": False, "path": str(self.root)}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {"available": True, **payload}

    def create_bundle(self, service_id: str) -> dict[str, str]:
        manifest = self.overview()
        if not manifest.get("available"):
            raise RuntimeError("No completed analysis is available; run service analysis first")
        run_path = Path(str(manifest["path"])).resolve()
        if not run_path.is_relative_to((self.root / "runs").resolve()):
            raise RuntimeError("Latest analysis manifest points outside the archive")
        analysis_path = run_path / "analysis.json"
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        service_payload = self._service_payload_from_analysis(analysis, service_id)
        if not (self.skill_path / "SKILL.md").is_file():
            raise RuntimeError(f"SSOT skill is missing: {self.skill_path}")

        bundle_id = f"{_safe_name(service_id)}-{uuid.uuid4().hex[:10]}"
        bundles = self.root / "bundles"
        bundles.mkdir(parents=True, exist_ok=True)
        temporary = bundles / f".{bundle_id}.tmp"
        destination = bundles / f"{bundle_id}.zip"
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(analysis_path, "analysis/full-analysis.json")
                archive.writestr(
                    "analysis/service-analysis.json",
                    json.dumps(service_payload, ensure_ascii=False, indent=2),
                )
                archive.writestr("PROMPT.md", self._bundle_prompt(service_id))
                for path in sorted(self.skill_path.rglob("*")):
                    if path.is_file():
                        archive.write(path, Path("skill") / path.relative_to(self.skill_path))
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "bundle_id": bundle_id,
            "service_id": service_id,
            "run_id": str(manifest["run_id"]),
            "path": str(destination),
            "download_url": f"/admin/api/analysis/bundles/download?bundle_id={bundle_id}",
        }

    def bundle_path(self, bundle_id: str) -> Path:
        if not bundle_id or _safe_name(bundle_id) != bundle_id:
            raise ValueError("Invalid bundle_id")
        candidate = (self.root / "bundles" / f"{bundle_id}.zip").resolve()
        if (
            not candidate.is_relative_to((self.root / "bundles").resolve())
            or not candidate.is_file()
        ):
            raise KeyError(f"Unknown SSOT bundle: {bundle_id}")
        return candidate

    @staticmethod
    def _service_payload(
        service_map: ServiceMapSnapshot,
        graph: GraphSnapshot,
        service_id: str,
    ) -> dict[str, Any]:
        return AnalysisArchive._service_payload_from_analysis(
            {
                "service_map": service_map.model_dump(mode="json"),
                "graph": graph.model_dump(mode="json"),
            },
            service_id,
        )

    @staticmethod
    def _service_payload_from_analysis(analysis: dict[str, Any], service_id: str) -> dict[str, Any]:
        service_map = analysis["service_map"]
        service = next(
            (item for item in service_map["services"] if item["id"] == service_id),
            None,
        )
        if service is None:
            raise KeyError(f"Unknown service in latest analysis: {service_id}")
        dependencies = [
            item
            for item in service_map["dependencies"]
            if item["source_service_id"] == service_id
            or item.get("target_service_id") == service_id
        ]
        evidence_ids = {
            evidence_id
            for interface in [*service["entrypoints"], *service["outbound_interfaces"]]
            for evidence_id in interface["evidence_ids"]
        }
        evidence_ids.update(
            evidence_id for dependency in dependencies for evidence_id in dependency["evidence_ids"]
        )
        graph = analysis["graph"]
        graph_nodes = [item for item in graph["nodes"] if item.get("service_id") == service_id]
        node_ids = {item["id"] for item in graph_nodes}
        graph_edges = [
            item
            for item in graph["edges"]
            if item["source"] in node_ids or item["target"] in node_ids
        ]
        return {
            "schema_version": 1,
            "service": service,
            "dependencies": dependencies,
            "evidence": [item for item in service_map["evidence"] if item["id"] in evidence_ids],
            "issues": [
                item
                for item in service_map["issues"]
                if item["repository"] == service["repository"]
            ],
            "graph_nodes": graph_nodes,
            "graph_edges": graph_edges,
        }

    @staticmethod
    def _service_markdown(payload: dict[str, Any]) -> str:
        service = payload["service"]
        lines = [
            f"# Source analysis: {service['name']}",
            "",
            f"- Service ID: `{service['id']}`",
            f"- Repository: `{service['repository']}`",
            f"- Module: `{service['module_path']}`",
            f"- Build system: `{service['build_system']}`",
            f"- State: `{service['module_state']}`",
            f"- Commit: `{service.get('commit') or 'unknown'}`",
            "",
            "## Observed entrypoints",
            "",
        ]
        lines.extend(
            f"- `{item['kind']}` `{item['operation']}` — {item['description']}"
            for item in service["entrypoints"]
        )
        if not service["entrypoints"]:
            lines.append("- None observed")
        lines.extend(["", "## Observed outbound interfaces", ""])
        lines.extend(
            f"- `{item['kind']}` `{item['operation']}` → `{item.get('target_hint') or 'unknown'}`"
            for item in service["outbound_interfaces"]
        )
        if not service["outbound_interfaces"]:
            lines.append("- None observed")
        lines.extend(["", "## Evidence", ""])
        lines.extend(
            (
                f"- `{item['id']}` — `{item['file']}:{item['line']}` "
                f"({item['confidence']}): {item['snippet']}"
            )
            for item in payload["evidence"]
        )
        if not payload["evidence"]:
            lines.append("- No direct evidence extracted")
        lines.extend(["", "## Analysis issues", ""])
        lines.extend(f"- {item['message']}" for item in payload["issues"])
        if not payload["issues"]:
            lines.append("- None")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _bundle_prompt(service_id: str) -> str:
        return (
            "# Task\n\n"
            f"Use `skill/SKILL.md` to turn `analysis/service-analysis.json` into an SSOT "
            f"document for `{service_id}`. Use `analysis/full-analysis.json` only for system-level "
            "context. Save the result as `OUTPUT/ssot.md`. Never invent missing facts; preserve "
            "evidence references and list unknowns explicitly.\n"
        )

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
