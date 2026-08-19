"""Materialize local paths and Git URLs into managed, reproducible checkouts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from gigacode_graph.config import GraphSettings
from gigacode_graph.models import GraphSnapshot, IngestionManifest, IngestionRecord

_MANAGED_MARKER = ".gigacode-graph-source.json"
_URL_PATTERN = re.compile(r"^(?:https?|ssh|git|file)://", re.I)
_SCP_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:.+")


@dataclass(frozen=True)
class RepositorySpec:
    source: str
    ref: str | None = None
    base_directory: Path | None = None


class RepositorySourceManager:
    """Clone/update remote repositories without modifying user-owned local checkouts."""

    def __init__(self, settings: GraphSettings) -> None:
        self.settings = settings

    def materialize(
        self,
        specs: list[RepositorySpec],
        *,
        refresh: bool = True,
    ) -> tuple[list[Path], list[IngestionRecord]]:
        if not specs:
            raise ValueError("At least one repository path or Git URL is required")
        paths: list[Path] = []
        records: list[IngestionRecord] = []
        seen: set[Path] = set()
        for spec in specs:
            path, record = self._materialize_one(spec, refresh=refresh)
            if path in seen:
                continue
            seen.add(path)
            paths.append(path)
            records.append(record)
        return paths, records

    def save_manifest(
        self,
        records: list[IngestionRecord],
        snapshot: GraphSnapshot,
    ) -> IngestionManifest:
        manifest = IngestionManifest(
            generated_at=datetime.now(UTC),
            graph_generated_at=snapshot.generated_at,
            graph_path=str(self.settings.store_path),
            repositories=records,
        )
        path = self.settings.ingestion_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)
        return manifest

    def _materialize_one(
        self,
        spec: RepositorySpec,
        *,
        refresh: bool,
    ) -> tuple[Path, IngestionRecord]:
        raw = spec.source.strip()
        if not raw:
            raise ValueError("Repository source must not be empty")
        if not self._is_git_url(raw):
            base = (spec.base_directory or Path.cwd()).resolve()
            candidate = Path(raw).expanduser()
            path = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
            if not path.is_dir():
                raise ValueError(f"Repository directory does not exist: {path}")
            commit = self._local_commit(path)
            return path, IngestionRecord(
                source_type="local",
                source=str(path),
                requested_ref=spec.ref,
                checkout_path=str(path),
                commit=commit,
                action="local",
            )

        source = self._validated_url(raw)
        destination = self._checkout_path(source, spec.ref)
        action: str
        if destination.exists():
            self._validate_managed_checkout(destination, source, spec.ref)
            if refresh:
                self._fetch_and_checkout(destination, spec.ref)
                action = "updated"
            else:
                action = "reused"
        else:
            self._clone(destination, source, spec.ref)
            action = "cloned"
        self._write_marker(destination, source, spec.ref)
        commit = self._git_output(["rev-parse", "HEAD"], cwd=destination, source=source)
        return destination, IngestionRecord(
            source_type="git",
            source=source,
            requested_ref=spec.ref,
            checkout_path=str(destination),
            commit=commit,
            action=action,
        )

    def _clone(self, destination: Path, source: str, ref: str | None) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}-",
                dir=destination.parent,
            )
        )
        try:
            self._git(["init", "--quiet"], cwd=temporary, source=source)
            self._git(["remote", "add", "origin", source], cwd=temporary, source=source)
            self._fetch_and_checkout(temporary, ref, source=source)
            temporary.replace(destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _fetch_and_checkout(
        self,
        checkout: Path,
        ref: str | None,
        *,
        source: str | None = None,
    ) -> None:
        safe_source = source or self._marker_source(checkout)
        target = ref or "HEAD"
        self._git(
            ["fetch", "--depth", "1", "--no-tags", "origin", target],
            cwd=checkout,
            source=safe_source,
        )
        self._git(
            ["checkout", "--detach", "--force", "FETCH_HEAD"],
            cwd=checkout,
            source=safe_source,
        )

    def _validate_managed_checkout(
        self,
        checkout: Path,
        source: str,
        ref: str | None,
    ) -> None:
        marker = checkout / _MANAGED_MARKER
        if not (checkout / ".git").is_dir() or not marker.is_file():
            raise RuntimeError(
                f"Refusing to update unmanaged directory in repository cache: {checkout}"
            )
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("source") != source or payload.get("ref") != ref:
            raise RuntimeError(f"Managed checkout identity mismatch: {checkout}")

    def _write_marker(self, checkout: Path, source: str, ref: str | None) -> None:
        marker = checkout / _MANAGED_MARKER
        marker.write_text(
            json.dumps(
                {
                    "managed": True,
                    "source": source,
                    "ref": ref,
                    "repository_name": self._remote_identity(source)[1],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _marker_source(checkout: Path) -> str:
        marker = checkout / _MANAGED_MARKER
        if not marker.is_file():
            return "managed git remote"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        return str(payload.get("source") or "managed git remote")

    def _checkout_path(self, source: str, ref: str | None) -> Path:
        host, name = self._remote_identity(source)
        slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{host}-{name}").strip("-.")
        digest = hashlib.sha256(f"{source}\x1f{ref or ''}".encode()).hexdigest()[:10]
        return (self.settings.repository_cache_path / f"{slug}-{digest}").resolve()

    @staticmethod
    def _remote_identity(source: str) -> tuple[str, str]:
        parsed = urlparse(source)
        if parsed.scheme:
            host = parsed.hostname or "local"
            remote_path = parsed.path
        else:
            host_part, _, remote_path = source.partition(":")
            host = host_part.rsplit("@", 1)[-1]
        name = Path(remote_path.rstrip("/")).name.removesuffix(".git") or "repository"
        return host, name

    @staticmethod
    def _is_git_url(value: str) -> bool:
        return bool(_URL_PATTERN.match(value) or _SCP_PATTERN.match(value))

    @staticmethod
    def _validated_url(value: str) -> str:
        if _SCP_PATTERN.match(value):
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https", "ssh", "git", "file"}:
            raise ValueError(f"Unsupported Git URL scheme: {parsed.scheme}")
        if parsed.password is not None:
            raise ValueError("Credentials must not be embedded in a Git URL")
        if parsed.scheme in {"http", "https"} and parsed.username is not None:
            raise ValueError(
                "Credentials/userinfo must not be embedded in an HTTP Git URL; "
                "use a Git credential helper"
            )
        if parsed.query or parsed.fragment:
            raise ValueError("Git URL query and fragment are not allowed; pass --ref separately")
        return value

    @staticmethod
    def _local_commit(path: Path) -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=path,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None

    def _git_output(self, arguments: list[str], *, cwd: Path, source: str) -> str:
        result = self._git(arguments, cwd=cwd, source=source)
        return result.stdout.strip()

    def _git(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        source: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.settings.git_timeout_seconds,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("git executable is required for Git URL ingestion") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Git operation timed out for {source}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "git command failed").strip()
            message = message.replace(source, "<repository-url>")
            raise RuntimeError(f"Git operation failed for {source}: {message}")
        return result
