"""Transport diagnostics stay scoped and never publish attachment credentials."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from corporate_kb.confluence.client import ConfluenceClient
from corporate_kb.confluence.diagnostics import safe_http_logging
from corporate_kb.confluence.models import Attachment, ConfluenceError, SourceConfig

SIGNED_TOKEN = "secret-signed-download-token"
COOKIE = "secret-response-session-cookie"
PRIVATE_FILENAME = "private-document-name.png"
TRANSPORT_LOGGERS = (
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)


def cloud_source() -> SourceConfig:
    return SourceConfig(
        name="Logging test",
        index_id="default",
        base_url="https://team.atlassian.net/wiki",
        root_page_url="1",
        username="reader@example.com",
    )


def ordinary_http_request() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client:
        client.get("https://other-integration.example/health")


class DiagnosticStream(httpx.SyncByteStream):
    def __iter__(self):
        logging.getLogger("httpcore.http11").debug("response headers: Set-Cookie=%s", COOKIE)
        yield b"image-bytes"

    def close(self):
        logging.getLogger("httpcore.connection").debug("response closed: cookie=%s", COOKIE)


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize("status", [200, 401])
def test_cdn_response_logs_omit_signed_urls_headers_and_filenames(caplog, level, status):
    def handle(request):
        for name in TRANSPORT_LOGGERS:
            logging.getLogger(name).debug("transport trace: token=%s", SIGNED_TOKEN)
        if request.url.host == "team.atlassian.net":
            return httpx.Response(
                302,
                headers={
                    "Location": "https://api.media.atlassian.com/file/1"
                    f"?token={SIGNED_TOKEN}"
                },
            )
        assert "Authorization" not in request.headers
        return httpx.Response(status, stream=DiagnosticStream())

    with caplog.at_level(level):
        client = ConfluenceClient(
            cloud_source(), "private-pat", transport=httpx.MockTransport(handle), retries=0
        )
        try:
            attachment = Attachment(
                id="1",
                title=PRIVATE_FILENAME,
                download_url=f"/download/attachments/1/{PRIVATE_FILENAME}",
            )
            if status == 200:
                assert client.download(attachment) == b"image-bytes"
                assert "attachment download completed" in caplog.text
            else:
                with pytest.raises(ConfluenceError, match="Attachment HTTP 401"):
                    client.download(attachment)
            assert f"status={status}" in caplog.text
            assert "Confluence HTTP request started" in caplog.text
            assert "attachment redirect accepted" in caplog.text
            for secret in (SIGNED_TOKEN, COOKIE, PRIVATE_FILENAME, "private-pat"):
                assert secret not in caplog.text
        finally:
            client.close()
        ordinary_http_request()
        assert "HTTP Request: GET https://other-integration.example/health" in caplog.text


def test_api_request_queries_do_not_leak_from_httpx(caplog):
    with caplog.at_level(logging.INFO):
        client = ConfluenceClient(
            cloud_source(),
            "private-pat",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        )
        try:
            client._request(client.api + "/pages", params={"cursor": SIGNED_TOKEN})
            assert "Confluence HTTP response received" in caplog.text
            assert SIGNED_TOKEN not in caplog.text
        finally:
            client.close()


def test_scope_resets_after_exception_and_preserves_other_http_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError, match="test exception"), safe_http_logging():
            ordinary_http_request()
            logging.getLogger("httpcore.proxy").debug("hidden proxy details")
            raise ValueError("test exception")
        assert not caplog.records
        ordinary_http_request()
        logging.getLogger("httpcore.proxy").debug("other integration proxy details")
    assert "HTTP Request: GET https://other-integration.example/health" in caplog.text
    assert "other integration proxy details" in caplog.text


def test_nested_scope_and_other_thread_are_independent(caplog):
    with caplog.at_level(logging.INFO), safe_http_logging():
        with safe_http_logging():
            ordinary_http_request()
        ordinary_http_request()
        assert not caplog.records
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(ordinary_http_request).result()
    assert len(caplog.records) == 1
    assert "HTTP Request: GET https://other-integration.example/health" in caplog.text
