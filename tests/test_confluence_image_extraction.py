from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from corporate_kb.confluence.images import GigaCodeImageExtractor
from corporate_kb.confluence.models import ConfluenceError


def extractor_with_results(tmp_path, statuses, expected_name="attachment.png"):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        assert kwargs["allow_edits"] is False
        assert (kwargs["checkout"] / expected_name).is_file()
        return SimpleNamespace(
            payload={"status": statuses[len(calls) - 1], "analyzed_files": [expected_name]}
        )

    extractor = GigaCodeImageExtractor(SimpleNamespace(run_json=run), tmp_path / "cache")
    return extractor, calls


def cache_path(extractor, data):
    digest = hashlib.sha256(data).hexdigest()
    key = hashlib.sha256(f"{digest}:{extractor.identity}".encode()).hexdigest()
    return extractor.cache_dir / f"{key}.json"


@pytest.mark.parametrize("status", ["partial", "unsupported", "unreadable"])
def test_incomplete_image_result_is_retried_and_success_is_cached(tmp_path, status):
    path = tmp_path / "source.png"
    path.write_bytes(b"fixture")
    extractor, calls = extractor_with_results(tmp_path, [status, "ok"])

    assert extractor.extract(path).status == status
    assert not list(extractor.cache_dir.glob("*.json"))
    assert extractor.extract(path).status == "ok"
    assert extractor.extract(path).status == "ok"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "cached",
    [
        '{"status":"unsupported","analyzed_files":["attachment.png"]}',
        '{"status":"partial","analyzed_files":["attachment.png"]}',
        '{"status":"unreadable","analyzed_files":["attachment.png"]}',
        '{"status":"ok","analyzed_files":["wrong.png"]}',
        '{"status":"ok","analyzed_files":[]}',
        "not json",
        "[]",
    ],
)
def test_legacy_incomplete_or_invalid_cache_does_not_block_recovery(tmp_path, cached):
    data = b"fixture"
    path = tmp_path / "source.png"
    path.write_bytes(data)
    extractor, calls = extractor_with_results(tmp_path, ["ok"])
    existing = cache_path(extractor, data)
    existing.write_text(cached)
    progress = []

    assert extractor.extract(path, progress=progress.append).status == "ok"
    assert extractor.extract(path).status == "ok"
    assert len(calls) == 1
    assert json.loads(existing.read_text())["status"] == "ok"
    assert progress[0].startswith("image cache ignored:")


def test_existing_successful_cache_keeps_its_identity_and_needs_no_model_call(tmp_path):
    data = b"fixture"
    path = tmp_path / "source.png"
    path.write_bytes(data)
    extractor, calls = extractor_with_results(tmp_path, [])
    cache_path(extractor, data).write_text(
        '{"status":"ok","analyzed_files":["attachment.png"],"visible_text":"existing"}'
    )

    assert extractor.extract(path).visible_text == "existing"
    assert calls == []


@pytest.mark.parametrize(
    ("filename", "data", "expected_name"),
    [
        ("source.bin", b"\x89PNG\r\n\x1a\nfixture", "attachment.png"),
        ("source.png", b"\xff\xd8\xfffixture", "attachment.jpg"),
        ("source.jpeg", b"\xff\xd8\xfffixture", "attachment.jpeg"),
        ("source.bin", b"GIF89afixture", "attachment.gif"),
        ("source.bin", b"BMfixture", "attachment.bmp"),
        ("source.bin", b"RIFF0000WEBPfixture", "attachment.webp"),
        ("source.bin", b'\xef\xbb\xbf<?xml version="1.0"?>\n<!--x--><svg/>', "attachment.svg"),
        ("source.SVG", b"<svg/>", "attachment.svg"),
    ],
)
def test_image_handoff_uses_identified_format_and_keeps_exact_input_bytes(
    tmp_path, filename, data, expected_name
):
    path = tmp_path / filename
    path.write_bytes(data)
    observed = []

    def run(**kwargs):
        image = kwargs["checkout"] / expected_name
        observed.append(image.read_bytes())
        assert f"Read the image file {expected_name}" in kwargs["prompt"]
        return SimpleNamespace(payload={"status": "ok", "analyzed_files": [expected_name]})

    extractor = GigaCodeImageExtractor(SimpleNamespace(run_json=run), tmp_path / "cache")
    assert extractor.extract(path).status == "ok"
    assert observed == [data]


@pytest.mark.parametrize(
    ("filename", "data", "code"),
    [
        ("source.png", b"", "image_input_empty"),
        ("source.png", b" \n\t", "image_input_empty"),
        ("source.svg", b"<!DOCTYPE html><html>private login</html>", "image_input_html"),
        ("source.jpg", b"\n<HTML><body>private login</body></HTML>", "image_input_html"),
        ("source.png", b'<?xml version="1.0"?><!--x--><html>private login', "image_input_html"),
        ("source.bin", b"unknown private payload", "image_input_format"),
    ],
)
def test_bad_archived_image_is_rejected_before_cli_or_cache(tmp_path, filename, data, code):
    path = tmp_path / filename
    path.write_bytes(data)
    extractor, calls = extractor_with_results(tmp_path, [])
    # Even an old successful cache cannot hide invalid downloaded bytes.
    cache_path(extractor, data).write_text(
        '{"status":"ok","analyzed_files":["attachment.png"]}'
    )

    with pytest.raises(ConfluenceError) as raised:
        extractor.extract(path)

    assert raised.value.error_code == code
    assert "private" not in str(raised.value)
    assert calls == []


def test_relative_cache_directory_keeps_image_in_the_requested_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "source.png"
    path.write_bytes(b"fixture")
    calls = []

    def run(**kwargs):
        root = kwargs["checkout"].resolve()
        assert root.parent == (tmp_path / "relative-cache").resolve()
        assert (root / "attachment.png").read_bytes() == b"fixture"
        calls.append(root)
        return SimpleNamespace(payload={"status": "ok", "analyzed_files": ["attachment.png"]})

    extractor = GigaCodeImageExtractor(
        SimpleNamespace(run_json=run), path.parent.relative_to(tmp_path) / "relative-cache"
    )
    assert extractor.extract(path).status == "ok"
    assert len(calls) == 1
