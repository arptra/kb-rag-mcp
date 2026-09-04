"""Isolated GigaCode repair and guarded algorithm promotion workflows."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gigacode_graph.algorithms import get_graph_algorithm
from gigacode_graph.lab.models import (
    AlgorithmRegistryDocument,
    AlgorithmRelease,
    dump_yaml_model,
    load_yaml_model,
)
from gigacode_graph.lab.validation import compare_graphs
from gigacode_graph.store import JsonGraphStore

_ALLOWED_REPAIR_PATHS = (
    "src/gigacode_graph/",
    "src/service_map/",
    "src/corporate_kb/graph_verifier.py",
    "src/corporate_kb/gigacode_runner.py",
    "tests/",
    "graph-lab/",
    "README.gigacode-graph.md",
    "pyproject.toml",
    "uv.lock",
)


def prepare_repair(run_directory: Path, lab_root: Path) -> Path:
    run = run_directory.resolve()
    manifest = _read_json(run / "run.json")
    validation = _read_json(run / "validation.json")
    task_id = f"{manifest['run_id']}-{os.urandom(3).hex()}"
    task = (lab_root.resolve() / "tasks" / task_id)
    task.mkdir(parents=True, exist_ok=False)
    task_payload = {
        "schema_version": 1,
        "task_id": task_id,
        "created_at": datetime.now(UTC).isoformat(),
        "source_run": str(run),
        "algorithm": manifest["algorithm"],
        "status": "prepared",
        "failure_count": validation["failure_count"],
        "allowed_paths": list(_ALLOWED_REPAIR_PATHS),
        "experiment_path": f"graph-lab/experiments/{task_id}",
        "rules": {
            "isolated_worktree": True,
            "automatic_apply": False,
            "automatic_commit": False,
            "automatic_push": False,
        },
    }
    _write_json(task / "task.json", task_payload)
    _write_json(task / "failures.json", validation)
    (task / "TASK.md").write_text(_repair_task_markdown(task_payload, validation), encoding="utf-8")
    (task / "HYPOTHESIS.md").write_text(
        "# Hypothesis\n\nDescribe the scanner blind spot before editing code.\n",
        encoding="utf-8",
    )
    return task


def run_repair(
    task_directory: Path,
    project_root: Path,
    *,
    command: str = "gigacode",
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    task = task_directory.resolve()
    root = project_root.resolve()
    task_payload = _read_json(task / "task.json")
    executable = shutil.which(command)
    if executable is None:
        raise RuntimeError(f"GigaCode executable not found: {command}")
    worktree = root / ".graph-debug" / "worktrees" / str(task_payload["task_id"])
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        raise RuntimeError(f"Repair worktree already exists: {worktree}")
    _checked(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], cwd=root)
    iteration = task / "iterations" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    iteration.mkdir(parents=True, exist_ok=False)
    prompt = _repair_prompt(task, task_payload, worktree)
    (iteration / "prompt.txt").write_text(prompt, encoding="utf-8")
    command_line = [
        executable,
        "--output-format",
        "stream-json",
        "--exclude-tools",
        "agent,web_fetch,web_search",
        "--max-session-turns",
        "50",
    ]
    started_at = time.monotonic()
    completed = subprocess.run(
        command_line,
        cwd=worktree,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    (iteration / "stdout.jsonl").write_text(completed.stdout, encoding="utf-8")
    (iteration / "stderr.log").write_text(completed.stderr, encoding="utf-8")
    experiment = worktree / str(task_payload["experiment_path"])
    required_experiment_documents = [
        experiment / "PLAN.md",
        experiment / "HYPOTHESIS.md",
        experiment / "CHANGESET.md",
    ]
    missing_experiment_documents = [
        str(path.relative_to(worktree))
        for path in required_experiment_documents
        if not path.is_file()
    ]
    changed = _changed_paths(worktree)
    disallowed = [path for path in changed if not _allowed_path(path)]
    validation: dict[str, Any] = {
        "gigacode_exit_code": completed.returncode,
        "changed_paths": changed,
        "disallowed_paths": disallowed,
        "missing_experiment_documents": missing_experiment_documents,
        "commands": [],
    }
    if (
        completed.returncode == 0
        and changed
        and not disallowed
        and not missing_experiment_documents
    ):
        validation["commands"] = _validate_worktree(
            worktree,
            root,
            Path(str(task_payload["source_run"])),
            iteration,
        )
    candidate_descriptor = _descriptor_in_worktree(
        worktree,
        root,
        str(task_payload["algorithm"]["id"]),
    )
    validation["candidate_algorithm"] = candidate_descriptor
    validation["version_bumped"] = (
        candidate_descriptor.get("version") != task_payload["algorithm"].get("version")
    )
    validation["cache_namespace_changed"] = (
        candidate_descriptor.get("cache_namespace")
        != task_payload["algorithm"].get("cache_namespace")
    )
    _write_experiment_results(
        experiment,
        validation,
        Path(str(task_payload["source_run"])),
        iteration,
    )
    changed = _changed_paths(worktree)
    untracked = [
        path
        for path in changed
        if _checked(
            ["git", "ls-files", "--error-unmatch", "--", path],
            cwd=worktree,
            check=False,
        ).returncode
        != 0
    ]
    if untracked and not disallowed:
        _checked(["git", "add", "-N", "--", *untracked], cwd=worktree)
    patch = _checked(
        ["git", "diff", "HEAD", "--binary", "--no-ext-diff"],
        cwd=worktree,
        check=False,
    ).stdout
    patch_path = iteration / "changes.patch"
    patch_path.write_text(patch, encoding="utf-8")
    passed = (
        completed.returncode == 0
        and bool(changed)
        and not disallowed
        and not missing_experiment_documents
        and validation["version_bumped"]
        and validation["cache_namespace_changed"]
        and all(item["exit_code"] == 0 for item in validation["commands"])
    )
    result = {
        "schema_version": 1,
        "status": "passed" if passed else "failed",
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "worktree": str(worktree),
        "patch": str(patch_path),
        "validation": validation,
        "apply_command": f"gigacode-graph debug apply-repair {patch_path}",
    }
    _write_json(iteration / "result.json", result)
    task_payload.update(
        {
            "status": "repair-passed" if passed else "repair-failed",
            "latest_iteration": str(iteration),
        }
    )
    _write_json(task / "task.json", task_payload)
    return result


def apply_repair(patch_path: Path, project_root: Path) -> None:
    patch = patch_path.resolve()
    root = project_root.resolve()
    if not patch.is_file() or not patch.read_text(encoding="utf-8", errors="replace").strip():
        raise ValueError(f"Repair patch is empty or missing: {patch}")
    _checked(["git", "apply", "--check", str(patch)], cwd=root)
    _checked(["git", "apply", str(patch)], cwd=root)


def promote_algorithm(
    registry_path: Path,
    *,
    algorithm_id: str,
    version: str,
    stage: str,
    evidence_run: Path,
) -> AlgorithmRegistryDocument:
    descriptor = get_graph_algorithm(algorithm_id).descriptor
    if descriptor.version != version:
        raise ValueError(
            f"Installed {algorithm_id} version is {descriptor.version}, not {version}"
        )
    run = _read_json(evidence_run.resolve() / "run.json")
    if run.get("status") != "passed":
        raise ValueError("Only a passed graph-lab run can be promotion evidence")
    validation = run.get("validation", {})
    if validation.get("failure_count") != 0:
        raise ValueError("Promotion evidence contains validation failures")
    path = registry_path.resolve()
    document = (
        load_yaml_model(path, AlgorithmRegistryDocument)
        if path.is_file()
        else AlgorithmRegistryDocument()
    )
    releases = list(document.algorithms)
    current = next(
        (item for item in releases if item.id == algorithm_id and item.version == version),
        None,
    )
    run_ref = str(evidence_run.resolve())
    if current is None:
        current = AlgorithmRelease(
            id=algorithm_id,
            version=version,
            stage=stage,
            implementation=f"entrypoint:corporate_kb.graph_algorithms:{algorithm_id}",
            evidence_runs=[run_ref],
        )
        releases.append(current)
    else:
        current = current.model_copy(
            update={
                "stage": stage,
                "evidence_runs": list(dict.fromkeys([*current.evidence_runs, run_ref])),
            }
        )
        releases = [
            current if item.id == algorithm_id and item.version == version else item
            for item in releases
        ]
    active = algorithm_id if stage == "production" else document.active
    updated = document.model_copy(update={"active": active, "algorithms": releases})
    dump_yaml_model(path, updated)
    return updated


def _repair_task_markdown(task: dict[str, Any], validation: dict[str, Any]) -> str:
    failures = validation.get("failures", [])
    lines = [
        f"# Repair task {task['task_id']}",
        "",
        f"Source run: `{task['source_run']}`",
        f"Algorithm: `{task['algorithm']['id']}@{task['algorithm']['version']}`",
        "",
        "## Objective",
        "",
        "Fix the smallest general scanner defect that explains these reproducible failures.",
        "Do not hard-code repository names, service ids, paths or expected edge ids.",
        "",
        "## Failures",
        "",
    ]
    lines.extend(f"- `{item['code']}` {item['message']}" for item in failures)
    lines.extend(
        [
            "",
            "## Definition of done",
            "",
            "- Add a focused regression test that fails before the fix.",
            "- Keep the graph model and evidence contract valid.",
            "- Pass pytest, Ruff, mypy and graph replay.",
            "- Write the hypothesis and observed limitation in the experiment artifacts.",
        ]
    )
    return "\n".join(lines) + "\n"


def _repair_prompt(task: Path, payload: dict[str, Any], worktree: Path) -> str:
    return (
        "You are repairing a source-derived graph analyzer in an isolated Git worktree. "
        "Read TASK.md, failures.json, the source run artifacts and the relevant implementation. "
        "First identify the general root cause, then add a focused regression test, then make the "
        "smallest general fix. Never hard-code case-specific service names, repository paths, edge "
        "ids or expected counts. Do not commit, push, access the network, or edit outside the "
        "allowed paths. Leave all changes in the worktree.\n\n"
        f"TASK DIRECTORY: {task}\n"
        f"SOURCE RUN: {payload['source_run']}\n"
        f"WORKTREE: {worktree}\n"
        f"ALLOWED PATHS: {json.dumps(payload['allowed_paths'], ensure_ascii=False)}\n"
        f"EXPERIMENT DIRECTORY TO CREATE: {payload['experiment_path']}\n"
        "Create PLAN.md, HYPOTHESIS.md and CHANGESET.md in that directory before editing. "
        "For every behavior change bump the algorithm descriptor patch version and change its "
        "cache_namespace, then update the algorithm CHANGELOG and KNOWN-LIMITATIONS if relevant. "
        "The supervisor will add VALIDATION.md, COMPARISON.json and CONCLUSION.md after tests.\n"
    )


def _validate_worktree(
    worktree: Path,
    project_root: Path,
    source_run: Path,
    iteration: Path,
) -> list[dict[str, Any]]:
    python = project_root / ".venv" / "bin" / "python"
    commands = [
        [str(python), "-m", "pytest", "-q"],
        [str(python), "-m", "ruff", "check", "."],
        [str(python), "-m", "mypy"],
        [
            str(python),
            "-m",
            "gigacode_graph.cli",
            "debug",
            "replay",
            str(source_run.resolve()),
            "--lab-root",
            str(iteration / "replay-lab"),
        ],
    ]
    results = []
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(worktree / "src")
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=worktree,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1200,
            check=False,
        )
        results.append(
            {
                "command": command,
                "exit_code": completed.returncode,
                "stdout": completed.stdout[-20_000:],
                "stderr": completed.stderr[-20_000:],
            }
        )
        if completed.returncode != 0:
            break
    return results


def _changed_paths(worktree: Path) -> list[str]:
    status = _checked(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=worktree,
    ).stdout
    paths = []
    fields = status.split("\0")
    position = 0
    while position < len(fields):
        field = fields[position]
        position += 1
        if not field:
            continue
        code = field[:2]
        path = field[3:]
        if ("R" in code or "C" in code) and position < len(fields) and fields[position]:
            path = fields[position]
            position += 1
        paths.append(path)
    return sorted(set(paths))


def _descriptor_in_worktree(
    worktree: Path,
    project_root: Path,
    algorithm_id: str,
) -> dict[str, Any]:
    python = project_root / ".venv" / "bin" / "python"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(worktree / "src")
    command = [
        str(python),
        "-c",
        (
            "import json; from gigacode_graph.algorithms import get_graph_algorithm; "
            f"print(json.dumps(get_graph_algorithm({algorithm_id!r}).descriptor.as_dict()))"
        ),
    ]
    completed = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        return {"error": completed.stderr[-4000:], "exit_code": completed.returncode}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"Invalid descriptor JSON: {exc}"}
    return payload if isinstance(payload, dict) else {"error": "Descriptor is not an object"}


def _write_experiment_results(
    experiment: Path,
    validation: dict[str, Any],
    source_run: Path,
    iteration: Path,
) -> None:
    experiment.mkdir(parents=True, exist_ok=True)
    commands = validation.get("commands", [])
    lines = ["# Validation", ""]
    for item in commands:
        command = " ".join(str(value) for value in item.get("command", []))
        lines.append(f"- `{command}` → exit `{item.get('exit_code')}`")
    lines.extend(
        [
            f"- Version bumped: `{validation.get('version_bumped')}`",
            f"- Cache namespace changed: `{validation.get('cache_namespace_changed')}`",
            "",
        ]
    )
    (experiment / "VALIDATION.md").write_text("\n".join(lines), encoding="utf-8")
    replay_runs = sorted((iteration / "replay-lab" / "runs").glob("*"))
    comparison: dict[str, Any] = {"status": "unavailable"}
    if replay_runs:
        replay_graph = replay_runs[-1] / "static-graph.json"
        baseline_graph = source_run.resolve() / "static-graph.json"
        if replay_graph.is_file() and baseline_graph.is_file():
            comparison = compare_graphs(
                JsonGraphStore(baseline_graph).load(),
                JsonGraphStore(replay_graph).load(),
            )
            comparison["status"] = "compared"
    _write_json(experiment / "COMPARISON.json", comparison)
    passed = (
        bool(commands)
        and all(item.get("exit_code") == 0 for item in commands)
        and not validation.get("missing_experiment_documents")
        and validation.get("version_bumped") is True
        and validation.get("cache_namespace_changed") is True
    )
    (experiment / "CONCLUSION.md").write_text(
        "# Conclusion\n\n"
        + (
            "Candidate passed automated gates; review the patch and comparison before promotion.\n"
            if passed
            else "Candidate failed one or more gates; do not apply or promote it.\n"
        ),
        encoding="utf-8",
    )


def _allowed_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return any(
        normalized == allowed.rstrip("/") or normalized.startswith(allowed)
        for allowed in _ALLOWED_REPAIR_PATHS
    )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _checked(
    command: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=check,
    )
