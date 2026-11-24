from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from test_confluence import config, registry

from corporate_kb.confluence.client import ConfluenceClient
from corporate_kb.confluence.images import GigaCodeImageExtractor, ImageExtraction
from corporate_kb.confluence.models import Attachment, ConfluenceError
from corporate_kb.gigacode_runner import GigaCodeTimedOut
from corporate_kb.loaders.filesystem import FileSystemDocumentLoader

PRIVATE_OUTPUT = "confidential-image-text https://private.example/?token=secret-link pat-never-log"


def _single_page(instance, client, monkeypatch, *, retries=0):
    client.page_ids = lambda root: iter([root])
    monkeypatch.setattr(instance, "RETRY_ROUNDS", retries)
    monkeypatch.setattr(instance, "RETRY_DELAY_SECONDS", 0)


@pytest.mark.parametrize(
    ("exception_type", "error_code"),
    [
        (GigaCodeTimedOut, "image_timeout"),
        (ValueError, "image_invalid_result"),
        (RuntimeError, "image_runner_error"),
        (FileNotFoundError, "image_file_missing"),
        (PermissionError, "image_file_permission"),
    ],
)
def test_image_failure_identifies_stage_without_logging_raw_cli_error(
    tmp_path, monkeypatch, caplog, exception_type, error_code
):
    def extract(path, **kwargs):
        raise exception_type(PRIVATE_OUTPUT)

    extractor = SimpleNamespace(identity="diagnostic-test", extract=extract)
    instance, client, published, _, _ = registry(tmp_path, image_extractor=extractor)
    _single_page(instance, client, monkeypatch)
    source = instance.upsert_source(config(analyze_images=True).model_dump(), "pat-never-log")
    assert instance.run_job(instance.queue_sync(source["id"])["id"])["status"] == "completed"
    with caplog.at_level(logging.INFO):
        finished = instance.run_job(instance.queue_image_analysis(source["id"])["id"])
    assert finished["status"] == "completed_with_warnings"
    assert finished["result"]["failed_pages"] == 1
    assert published == ["target", "target"]
    journal = instance.job_log(finished["id"])
    events = [
        event for event in journal["items"] if event["details"].get("error_code") == error_code
    ]
    assert len(events) == 1
    assert events[0]["details"]["attachment_id"] == "asset-1"
    assert events[0]["details"]["page_id"] == "1"
    assert events[0]["level"] == "warning"
    assert error_code in caplog.text
    for secret in ("confidential-image-text", "private.example", "secret-link", "pat-never-log"):
        assert secret not in caplog.text
        assert secret not in json.dumps(journal)
    with instance._db() as db:
        asset = db.execute("SELECT path,error FROM assets").fetchone()
    assert (instance.root / asset["path"]).read_bytes() == b"image-version-1"
    assert asset["error"] == exception_type.__name__


@pytest.mark.parametrize("status", ["partial", "unreadable", "unsupported"])
def test_incomplete_image_result_logs_status_without_uncertainties(
    tmp_path, monkeypatch, caplog, status
):
    extractor = SimpleNamespace(
        identity="diagnostic-test",
        extract=lambda path, **kwargs: ImageExtraction(
            status=status, analyzed_files=[path.name], uncertainties=[PRIVATE_OUTPUT]
        ),
    )
    instance, client, _, _, _ = registry(tmp_path, image_extractor=extractor)
    _single_page(instance, client, monkeypatch)
    source = instance.upsert_source(config(analyze_images=True).model_dump(), "pat-never-log")
    assert instance.run_job(instance.queue_sync(source["id"])["id"])["status"] == "completed"
    with caplog.at_level(logging.INFO):
        finished = instance.run_job(instance.queue_image_analysis(source["id"])["id"])
    assert finished["status"] == "completed_with_warnings"
    journal = instance.job_log(finished["id"])
    events = [event for event in journal["items"] if event["details"].get("image_status") == status]
    assert len(events) == 1
    assert events[0]["level"] == "warning"
    assert events[0]["details"]["error_code"] == f"image_{status}"
    assert f'"image_status": "{status}"' in caplog.text
    for secret in ("confidential-image-text", "private.example", "secret-link", "pat-never-log"):
        assert secret not in caplog.text
        assert secret not in json.dumps(journal)


def test_missing_image_extractor_is_visible_in_operation_journal(tmp_path, monkeypatch, caplog):
    instance, client, _, _, _ = registry(
        tmp_path, image_extractor=SimpleNamespace(identity="configured-at-queue-time")
    )
    _single_page(instance, client, monkeypatch)
    source = instance.upsert_source(config().model_dump(), "secret")
    assert instance.run_job(instance.queue_sync(source["id"])["id"])["status"] == "completed"
    queued = instance.queue_image_analysis(source["id"])
    instance.image_extractor = None
    with caplog.at_level(logging.INFO):
        finished = instance.run_job(queued["id"])
    assert finished["status"] == "completed_with_warnings"
    journal = instance.job_log(finished["id"])
    assert any(
        event["details"].get("error_code") == "image_extractor_unconfigured"
        for event in journal["items"]
    )
    assert "image_extractor_unconfigured" in caplog.text


@pytest.mark.parametrize("initial_status", ["partial", "unreadable", "unsupported"])
def test_real_extractor_retries_incomplete_result_and_keeps_successful_cache(
    tmp_path, monkeypatch, initial_status
):
    calls = []

    def run(**kwargs):
        calls.append(kwargs["checkout"])
        assert (kwargs["checkout"] / "attachment.png").read_bytes() == b"image-version-1"
        assert kwargs["allow_edits"] is False
        assert "pat-never-log" not in kwargs["prompt"]
        return SimpleNamespace(
            payload={
                "status": initial_status if len(calls) == 1 else "ok",
                "visible_text": "recovered diagram evidence" if len(calls) > 1 else "",
                "analyzed_files": ["attachment.png"],
            }
        )

    extractor = GigaCodeImageExtractor(SimpleNamespace(run_json=run), tmp_path / "images")
    instance, client, published, _, _ = registry(tmp_path, image_extractor=extractor)
    _single_page(instance, client, monkeypatch, retries=2)
    # Some Confluence installations label image downloads as generic binary data.
    client.attachments = lambda page_id: [
        Attachment(
            id=f"asset-{page_id}",
            title="diagram.PNG",
            media_type="application/octet-stream",
            download_url="https://docs.example.com/download",
        )
    ]
    source = instance.upsert_source(config().model_dump(), "pat-never-log")
    assert instance.run_job(instance.queue_sync(source["id"])["id"])["status"] == "completed"
    finished = instance.run_job(instance.queue_image_analysis(source["id"])["id"])
    assert finished["status"] == "completed"
    assert finished["result"]["recovered_pages"] == 1
    assert finished["result"]["failed_pages"] == 0
    assert len(calls) == 2
    assert published == ["target", "target"]
    loaded = FileSystemDocumentLoader().load_directory(tmp_path / "knowledge" / "target")
    assert len(loaded) == 1
    assert "recovered diagram evidence" in loaded[0].content
    cached = list((tmp_path / "images").glob("*.json"))
    assert len(cached) == 1
    assert json.loads(cached[0].read_text())["status"] == "ok"
    with instance._db() as db:
        asset = db.execute("SELECT path,extraction_json,error FROM assets").fetchone()
    assert json.loads(asset["extraction_json"])["result"]["status"] == "ok"
    assert asset["error"] is None
    # Exercise both the extractor's successful disk cache and the page checkpoint.
    assert extractor.extract(instance.root / asset["path"]).status == "ok"
    unchanged = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert unchanged["result"]["changed"] == 0
    assert len(calls) == 2


@pytest.mark.parametrize(
    "payload,error_code",
    [
        (b"", "image_input_empty"),
        (b"<!doctype html><html><form>SSO login</form></html>", "image_input_html"),
    ],
)
def test_invalid_downloaded_image_is_diagnosed_before_running_gigacode(
    tmp_path, monkeypatch, caplog, payload, error_code
):
    def run(**kwargs):
        pytest.fail("Invalid image input must not reach the GigaCode runner")

    extractor = GigaCodeImageExtractor(SimpleNamespace(run_json=run), tmp_path / "images")
    instance, client, _, _, _ = registry(tmp_path, image_extractor=extractor)
    _single_page(instance, client, monkeypatch)
    client.download = lambda attachment: payload
    source = instance.upsert_source(config(analyze_images=True).model_dump(), "secret")
    assert instance.run_job(instance.queue_sync(source["id"])["id"])["status"] == "completed"
    with caplog.at_level(logging.INFO):
        finished = instance.run_job(instance.queue_image_analysis(source["id"])["id"])
    assert finished["status"] == "completed_with_warnings"
    assert error_code in caplog.text
    assert any(
        event["details"].get("error_code") == error_code
        for event in instance.job_log(finished["id"])["items"]
    )


@pytest.mark.parametrize(
    "title,media_type",
    [
        ("diagram.png", "application/octet-stream"),
        ("diagram", "image/png"),
    ],
)
@pytest.mark.parametrize("response_type", ["text/html; charset=utf-8", "application/xhtml+xml"])
def test_client_rejects_html_login_response_for_expected_image(title, media_type, response_type):
    payload = b"<html><form>confidential-login-page</form></html>"
    client = ConfluenceClient(
        config(),
        "secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=payload, headers={"Content-Type": response_type}
            )
        ),
    )
    try:
        with pytest.raises(ConfluenceError) as failure:
            client.download(
                Attachment(
                    id="asset-1",
                    title=title,
                    media_type=media_type,
                    download_url="https://docs.example.com/download",
                )
            )
        assert failure.value.error_code == "attachment_html_response"
        assert "confidential-login-page" not in str(failure.value)
    finally:
        client.close()


def test_html_attachment_remains_valid_nonimage_download():
    payload = b"<html>attached documentation</html>"
    client = ConfluenceClient(
        config(),
        "secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=payload, headers={"Content-Type": "text/html"}
            )
        ),
    )
    try:
        assert (
            client.download(
                Attachment(
                    id="asset-1",
                    title="documentation.html",
                    media_type="text/html",
                    download_url="https://docs.example.com/download",
                )
            )
            == payload
        )
    finally:
        client.close()


def test_nonimage_attachment_is_archived_without_calling_image_runner(tmp_path, monkeypatch):
    def extract(path, **kwargs):
        pytest.fail("Non-image attachments must not be sent to GigaCode image extraction")

    extractor = SimpleNamespace(identity="diagnostic-test", extract=extract)
    instance, client, _, _, _ = registry(tmp_path, image_extractor=extractor)
    _single_page(instance, client, monkeypatch)
    client.attachments = lambda page_id: [
        Attachment(
            id=f"asset-{page_id}",
            title="specification.pdf",
            media_type="application/pdf",
            download_url="https://docs.example.com/download",
        )
    ]
    source = instance.upsert_source(config().model_dump(), "secret")
    assert instance.run_job(instance.queue_sync(source["id"])["id"])["status"] == "completed"
    client.version = 2
    finished = instance.run_job(instance.queue_image_analysis(source["id"])["id"])
    assert finished["status"] == "completed"
    journal = instance.job_log(finished["id"])
    assert any("non-image file" in event["message"] for event in journal["items"])
    with instance._db() as db:
        asset = db.execute("SELECT path,extraction_json,error FROM assets").fetchone()
    assert (instance.root / asset["path"]).is_file()
    assert asset["extraction_json"] is None
    assert asset["error"] is None


@pytest.mark.parametrize("bad_payload", [b"", b"<html><form>SSO login</form></html>"])
def test_bad_image_response_is_redownloaded_on_retry_then_analyzed(
    tmp_path, monkeypatch, bad_payload
):
    downloads, analyses = [], []
    valid_payload = b"\x89PNG\r\n\x1a\nimage-fixture"

    def run(**kwargs):
        analyses.append((kwargs["checkout"] / "attachment.png").read_bytes())
        return SimpleNamespace(payload={"status": "ok", "analyzed_files": ["attachment.png"]})

    def download(attachment):
        downloads.append(attachment.id)
        return bad_payload if len(downloads) == 1 else valid_payload

    extractor = GigaCodeImageExtractor(SimpleNamespace(run_json=run), tmp_path / "images")
    instance, client, _, _, _ = registry(tmp_path, image_extractor=extractor)
    _single_page(instance, client, monkeypatch, retries=2)
    client.download = lambda attachment: bad_payload
    source = instance.upsert_source(config().model_dump(), "secret")
    assert instance.run_job(instance.queue_sync(source["id"])["id"])["status"] == "completed"
    client.download = download
    finished = instance.run_job(instance.queue_image_analysis(source["id"])["id"])
    assert finished["status"] == "completed"
    assert finished["result"]["recovered_pages"] == 1
    assert downloads == ["asset-1", "asset-1"]
    assert analyses == [valid_payload]
    with instance._db() as db:
        assets = db.execute("SELECT path,extraction_json FROM assets").fetchall()
    assert len(assets) == 1
    assert (instance.root / assets[0]["path"]).read_bytes() == valid_payload
    assert json.loads(assets[0]["extraction_json"])["result"]["status"] == "ok"


def test_legacy_html_image_archive_is_repaired_without_reusing_old_extraction(
    tmp_path, monkeypatch
):
    analyses = []
    good_payload = b"\x89PNG\r\n\x1a\nrepaired-image-fixture"

    def run(**kwargs):
        analyses.append((kwargs["checkout"] / "attachment.png").read_bytes())
        return SimpleNamespace(
            payload={
                "status": "ok",
                "analyzed_files": ["attachment.png"],
                "visible_text": "new diagram evidence",
            }
        )

    extractor = GigaCodeImageExtractor(SimpleNamespace(run_json=run), tmp_path / "images")
    instance, client, _, _, _ = registry(tmp_path, image_extractor=extractor)
    _single_page(instance, client, monkeypatch)
    client.download = lambda attachment: b"<html><form>SSO login</form></html>"
    source = instance.upsert_source(config(analyze_images=False).model_dump(), "secret")
    first = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert first["status"] == "completed"
    assert analyses == []
    with instance._db() as db:
        old_asset = dict(db.execute("SELECT * FROM assets").fetchone())
        db.execute(
            "UPDATE assets SET extraction_json=?,error='OldFailure' WHERE id=?",
            (
                json.dumps(
                    {
                        "extractor": extractor.identity,
                        "result": {"status": "ok", "analyzed_files": ["attachment.png"]},
                        "markdown": "stale wrong-image evidence",
                    }
                ),
                old_asset["id"],
            ),
        )
    downloads = []

    def download(attachment):
        downloads.append(attachment.id)
        return good_payload

    client.download = download
    finished = instance.run_job(instance.queue_image_analysis(source["id"])["id"])
    assert finished["status"] == "completed"
    assert downloads == ["asset-1"]
    assert analyses == [good_payload]
    with instance._db() as db:
        assets = db.execute("SELECT * FROM assets").fetchall()
    assert len(assets) == 1
    repaired = dict(assets[0])
    assert repaired["id"] == old_asset["id"]
    assert repaired["created_at"] == old_asset["created_at"]
    assert repaired["sha256"] != old_asset["sha256"]
    assert repaired["error"] is None
    assert (instance.root / repaired["path"]).read_bytes() == good_payload
    assert "new diagram evidence" in repaired["extraction_json"]
    assert "stale wrong-image evidence" not in repaired["extraction_json"]
    loaded = FileSystemDocumentLoader().load_directory(tmp_path / "knowledge" / "target")
    assert "new diagram evidence" in loaded[0].content
    assert "stale wrong-image evidence" not in loaded[0].content
