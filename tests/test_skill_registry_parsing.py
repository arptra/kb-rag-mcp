"""Package-local parse failures must not hide valid siblings or weaken source limits."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from skill_registry import models


def write_package(root: Path, relative: str, *, content: bytes | None = None) -> Path:
    directory = root / "skills" / relative
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_bytes(
        content
        if content is not None
        else b"---\nname: example\ndescription: Valid task\n---\nBody\n"
    )
    return directory


def test_invalid_package_does_not_block_valid_sibling(tmp_path: Path) -> None:
    write_package(tmp_path, "bad", content=b"No frontmatter\n")
    write_package(tmp_path, "good")

    result = models.scan_skills(tmp_path, "skills", True)

    assert [item.relative_path for item in result.snapshots] == ["good"]
    assert len(result.issues) == 1
    assert result.issues[0].to_dict() == {
        "relative_path": "bad",
        "path": "bad/SKILL.md",
        "error": "SKILL.md requires YAML frontmatter with name and description",
    }


def test_invalid_root_package_has_portable_issue_path(tmp_path: Path) -> None:
    write_package(tmp_path, ".", content=b"---\nname: root\n---\n")
    result = models.scan_skills(tmp_path, "skills", True)
    assert result.snapshots == []
    assert result.issues[0].relative_path == "."
    assert result.issues[0].path == "SKILL.md"
    assert "description" in result.issues[0].error


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf", b"\n\n", b"\xef\xbb\xbf\r\n \r\n"])
@pytest.mark.parametrize("terminator", [b"---", b"..."])
def test_compatible_frontmatter_preserves_raw_bytes_and_revision(
    tmp_path: Path,
    prefix: bytes,
    terminator: bytes,
) -> None:
    raw = prefix + b"---\nname: example\ndescription: Valid task\n" + terminator + b"\nBody\n"
    package = write_package(tmp_path, "example", content=raw)
    result = models.scan_skills(tmp_path, "skills", True)
    assert result.issues == []
    snapshot = result.snapshots[0]
    assert snapshot.files[0].content == raw
    assert (package / "SKILL.md").read_bytes() == raw
    expected = hashlib.sha256(
        json.dumps(
            [
                {
                    "path": "SKILL.md",
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "executable": False,
                }
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert snapshot.revision == expected


def test_timestamps_and_yaml12_booleans_are_isolated_from_global_loader(tmp_path: Path) -> None:
    write_package(
        tmp_path,
        "on",
        content=(
            b"---\nname: on\ndescription: Off\ncreated: 2026-10-09\n"
            b"changed: 2026-10-09T12:30:00Z\nexplicit: !!timestamp 2026-10-09\nmetadata:\n"
            b"  aliases: [on, off, yes, no]\n  enabled: true\n  disabled: FALSE\n---\n"
        ),
    )
    result = models.scan_skills(tmp_path, "skills", True)
    assert result.issues == []
    metadata = result.snapshots[0].metadata
    assert metadata["name"] == "on" and metadata["description"] == "Off"
    assert metadata["created"] == "2026-10-09"
    assert metadata["changed"] == "2026-10-09T12:30:00Z"
    assert metadata["explicit"] == "2026-10-09"
    assert metadata["metadata"] == {
        "aliases": ["on", "off", "yes", "no"],
        "enabled": True,
        "disabled": False,
    }
    json.dumps(metadata, allow_nan=False)
    assert yaml.safe_load("value: 2026-10-09")["value"] == date(2026, 10, 9)
    assert yaml.safe_load("value: on")["value"] is True


@pytest.mark.parametrize(
    "header",
    [
        "description: Task",
        "name: example",
        "name: true\ndescription: Task",
        "name: example\ndescription: false",
        "name: example\ndescription: ''",
        "name: Uppercase\ndescription: Task",
    ],
)
def test_required_metadata_is_not_fabricated_or_repaired(tmp_path: Path, header: str) -> None:
    write_package(tmp_path, "bad", content=f"---\n{header}\n---\n".encode())
    result = models.scan_skills(tmp_path, "skills", True)
    assert result.snapshots == []
    assert len(result.issues) == 1


def test_yaml_errors_include_location_without_source_excerpt(tmp_path: Path) -> None:
    write_package(
        tmp_path,
        "bad",
        content=(b"\n---\nname: bad\ndescription: task\ncredentials: [do-not-expose-this\n---\n"),
    )
    issue = models.scan_skills(tmp_path, "skills", True).issues[0]
    assert "line" in issue.error and "column" in issue.error
    assert "do-not-expose-this" not in issue.error
    assert "credentials" not in issue.error
    assert str(tmp_path) not in issue.error


def test_duplicate_keys_are_rejected_with_original_document_line_number(tmp_path: Path) -> None:
    write_package(
        tmp_path,
        "bad",
        content=(b"\xef\xbb\xbf\n\n---\nname: example\nname: other\ndescription: Task\n---\n"),
    )
    issue = models.scan_skills(tmp_path, "skills", True).issues[0]
    assert issue.error == "Invalid SKILL.md frontmatter: duplicate mapping key at line 5, column 1"


def test_yaml_merge_defaults_allow_explicit_overrides(tmp_path: Path) -> None:
    write_package(
        tmp_path,
        "example",
        content=(
            b"---\nname: example\ndescription: Task\ndefaults: &defaults\n  color: blue\n"
            b"metadata:\n  <<: *defaults\n  color: green\n---\n"
        ),
    )
    result = models.scan_skills(tmp_path, "skills", True)
    assert result.issues == []
    assert result.snapshots[0].metadata["metadata"] == {"color": "green"}


@pytest.mark.parametrize("scalar", ["|", ">"])
def test_indented_document_markers_inside_description_are_content(
    tmp_path: Path,
    scalar: str,
) -> None:
    write_package(
        tmp_path,
        "example",
        content=(
            "---\nname: example\ndescription: " + scalar + "\n"
            "  Explain separators:\n  ---\n  ...\n  Preserve their contents.\n---\nBody\n"
        ).encode(),
    )
    result = models.scan_skills(tmp_path, "skills", True)
    assert result.issues == []
    assert "---" in result.snapshots[0].description
    assert "..." in result.snapshots[0].description


@pytest.mark.parametrize("tag", ["!!bool", "!!float", "!!int", "!!timestamp"])
def test_bad_explicit_yaml_values_do_not_leak_or_block_siblings(tmp_path: Path, tag: str) -> None:
    write_package(
        tmp_path,
        "bad",
        content=(
            f"---\nname: bad\ndescription: Task\nmetadata: {tag} sensitive-invalid-value\n---\n"
        ).encode(),
    )
    write_package(tmp_path, "good")
    result = models.scan_skills(tmp_path, "skills", True)
    assert [snapshot.relative_path for snapshot in result.snapshots] == ["good"]
    assert len(result.issues) == 1
    assert "sensitive-invalid-value" not in result.issues[0].error
    assert "frontmatter" in result.issues[0].error


@pytest.mark.parametrize(
    "extra",
    [
        "metadata: &loop [*loop]",
        "metadata: .nan",
        "metadata: !!binary c2VjcmV0",
        "metadata: " + "[" * 14 + "0" + "]" * 14,
        "metadata: [" + ",".join("0" for _ in range(2001)) + "]",
    ],
)
def test_unsupported_or_excessive_yaml_structures_remain_rejected(
    tmp_path: Path, extra: str
) -> None:
    write_package(
        tmp_path, "bad", content=f"---\nname: bad\ndescription: Task\n{extra}\n---\n".encode()
    )
    result = models.scan_skills(tmp_path, "skills", True)
    assert result.snapshots == []
    assert len(result.issues) == 1
    assert "frontmatter" in result.issues[0].error


@pytest.mark.parametrize("link_path", ["escape", "references/escape", "SKILL.md"])
def test_package_symlink_skips_only_its_package(tmp_path: Path, link_path: str) -> None:
    package = write_package(tmp_path, "bad")
    write_package(tmp_path, "good")
    target = tmp_path / "outside-secret.txt"
    target.write_text("Never import this")
    link = package / link_path
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(target)

    result = models.scan_skills(tmp_path, "skills", True)

    assert [snapshot.relative_path for snapshot in result.snapshots] == ["good"]
    assert len(result.issues) == 1
    assert result.issues[0].relative_path == "bad"
    assert result.issues[0].path == "bad/" + link_path
    assert "Symlink" in result.issues[0].error
    assert "Never import" not in json.dumps(result.issues[0].to_dict())


def test_nested_skill_file_is_support_data_not_an_independent_package(tmp_path: Path) -> None:
    write_package(tmp_path, "outer")
    write_package(tmp_path, "outer/references/nested", content=b"No frontmatter\n")
    result = models.scan_skills(tmp_path, "skills", True)
    assert result.issues == []
    assert [snapshot.relative_path for snapshot in result.snapshots] == ["outer"]
    assert [file.path for file in result.snapshots[0].files] == [
        "SKILL.md",
        "references/nested/SKILL.md",
    ]


@pytest.mark.parametrize(
    "configured_path", ["../outside", "/outside", "skills/../outside", "skills\\bad"]
)
def test_unsafe_source_root_is_fatal(tmp_path: Path, configured_path: str) -> None:
    with pytest.raises(ValueError):
        models.scan_skills(tmp_path, configured_path, True)


def test_missing_source_or_source_symlink_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        models.scan_skills(tmp_path, "missing", True)
    write_package(tmp_path, "good")
    (tmp_path / "skills" / "unsafe").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        models.scan_skills(tmp_path, "skills", True)


def test_source_enumeration_failure_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_package(tmp_path, "good")

    def fail_walk(_path: Path, **options: Any) -> Any:
        options["onerror"](PermissionError("Source directory cannot be enumerated"))
        return iter(())

    monkeypatch.setattr(models.os, "walk", fail_walk)
    with pytest.raises(OSError):
        models.scan_skills(tmp_path, "skills", True)


def test_package_enumeration_failure_is_reported_and_siblings_survive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = write_package(tmp_path, "bad")
    write_package(tmp_path, "good")
    original_walk = models.os.walk

    def fail_package(path: Path, **options: Any) -> Any:
        if path == bad:
            options["onerror"](PermissionError("private filesystem details"))
            return iter(())
        return original_walk(path, **options)

    monkeypatch.setattr(models.os, "walk", fail_package)
    result = models.scan_skills(tmp_path, "skills", True)
    assert [snapshot.relative_path for snapshot in result.snapshots] == ["good"]
    assert result.issues[0].error == "Cannot read skill package files"
    assert "private filesystem" not in json.dumps(result.issues[0].to_dict())


def test_bytes_read_before_skipped_package_failure_count_against_source_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = write_package(tmp_path, "a-bad")
    good = write_package(tmp_path, "b-good")
    (bad / "z-unreadable.txt").write_text("Cannot read")
    monkeypatch.setattr(
        models,
        "MAX_SOURCE_BYTES",
        sum((package / "SKILL.md").stat().st_size for package in (bad, good)) - 1,
    )
    original_open = models.os.open

    def fail_one_file(path: Path, *args: Any, **options: Any) -> int:
        if Path(path).name == "z-unreadable.txt":
            raise PermissionError("Do not expose filesystem contents")
        return original_open(path, *args, **options)

    monkeypatch.setattr(models.os, "open", fail_one_file)
    with pytest.raises(ValueError, match="Source exceeds"):
        models.scan_skills(tmp_path, "skills", True)


def test_bad_metadata_bytes_still_count_against_source_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = write_package(tmp_path, "a-bad", content=b"No metadata" * 10)
    good = write_package(tmp_path, "b-good")
    monkeypatch.setattr(
        models,
        "MAX_SOURCE_BYTES",
        sum((package / "SKILL.md").stat().st_size for package in (bad, good)) - 1,
    )
    with pytest.raises(ValueError, match="Source exceeds"):
        models.scan_skills(tmp_path, "skills", True)


def test_package_file_limit_does_not_block_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = write_package(tmp_path, "bad")
    (bad / "z-extra.txt").write_text("Too many files")
    write_package(tmp_path, "good")
    monkeypatch.setattr(models, "MAX_FILES", 1)
    result = models.scan_skills(tmp_path, "skills", True)
    assert [snapshot.relative_path for snapshot in result.snapshots] == ["good"]
    assert "files" in result.issues[0].error


@pytest.mark.parametrize("limit", ["MAX_SKILLS", "MAX_SCAN_ENTRIES"])
def test_global_enumeration_limits_remain_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
) -> None:
    write_package(tmp_path, "a")
    write_package(tmp_path, "b")
    monkeypatch.setattr(models, limit, 1)
    with pytest.raises(ValueError, match="Source exceeds"):
        models.scan_skills(tmp_path, "skills", True)


def test_nonrecursive_discovery_preserves_existing_depth_behavior(tmp_path: Path) -> None:
    write_package(tmp_path, "direct")
    write_package(tmp_path, "group/nested")
    result = models.scan_skills(tmp_path, "skills", False)
    assert [snapshot.relative_path for snapshot in result.snapshots] == ["direct"]
    recursive = models.scan_skills(tmp_path, "skills", True)
    assert [snapshot.relative_path for snapshot in recursive.snapshots] == [
        "direct",
        "group/nested",
    ]
