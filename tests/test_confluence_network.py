"""Actionable transport failures without leaking credentials or signed URLs."""

from __future__ import annotations

import errno
import logging
import socket
import ssl
import threading

import httpx
import pytest

from corporate_kb.confluence.client import ConfluenceClient
from corporate_kb.confluence.diagnostics import network_error
from corporate_kb.confluence.models import Attachment, ConfluenceError, SourceConfig, SyncCancelled

TOKEN = "pat-secret-never-log"
SIGNED_TOKEN = "signed-token-never-log"
PROXY_PASSWORD = "proxy-password-never-log"
UNSAFE_MESSAGE = (
    f"Authorization: Bearer {TOKEN}; "
    f"https://proxy-user:{PROXY_PASSWORD}@proxy.example/; "
    f"https://api.media.atlassian.com/file?id=1&token={SIGNED_TOKEN}"
)


def source(*, cloud: bool = False) -> SourceConfig:
    return SourceConfig(
        name="Transport test",
        index_id="default",
        base_url="https://team.atlassian.net/wiki"
        if cloud
        else "https://docs.example.com/confluence",
        root_page_url="1",
        username="reader@example.com" if cloud else None,
    )


def assert_no_secrets(text: str) -> None:
    for secret in (TOKEN, SIGNED_TOKEN, PROXY_PASSWORD, "Authorization:", "token="):
        assert secret not in text


def test_bad_ca_file_returns_safe_configuration_error(tmp_path, monkeypatch, caplog):
    private_path = tmp_path / "private-ca-path-do-not-log.pem"
    monkeypatch.setenv("SSL_CERT_FILE", str(private_path))
    with caplog.at_level(logging.INFO), pytest.raises(ConfluenceError) as caught:
        ConfluenceClient(source(), TOKEN)
    assert caught.value.error_code == "client_configuration"
    assert "SSL_CERT_FILE" in str(caught.value)
    assert str(private_path) not in str(caught.value) + caplog.text
    assert_no_secrets(str(caught.value) + caplog.text)


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (httpx.ProxyError, "proxy"),
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.WriteTimeout, "write_timeout"),
        (httpx.PoolTimeout, "pool_timeout"),
        (httpx.LocalProtocolError, "local_protocol"),
        (httpx.RemoteProtocolError, "remote_protocol"),
        (httpx.ConnectError, "connection"),
        (httpx.ReadError, "network"),
        (httpx.HTTPError, "network"),
    ],
)
def test_transport_errors_have_safe_actionable_categories(error_type, code):
    result = network_error(error_type(UNSAFE_MESSAGE))
    assert result.error_code == code
    assert result.status_code is None
    assert str(result).startswith(f"Confluence network request failed [{code}]:")
    assert_no_secrets(str(result))


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
@pytest.mark.parametrize(
    ("cause", "code"),
    [
        (ssl.SSLCertVerificationError(1, UNSAFE_MESSAGE), "tls_certificate"),
        (ssl.SSLError(1, UNSAFE_MESSAGE), "tls_handshake"),
        (socket.gaierror(socket.EAI_NONAME, UNSAFE_MESSAGE), "dns"),
        (ConnectionRefusedError(errno.ECONNREFUSED, UNSAFE_MESSAGE), "connection_refused"),
    ],
)
def test_nested_transport_causes_are_classified_without_printing_them(link, cause, code):
    intermediate = RuntimeError(UNSAFE_MESSAGE)
    setattr(intermediate, link, cause)
    error = httpx.ConnectError(UNSAFE_MESSAGE)
    setattr(error, link, intermediate)
    result = network_error(error)
    assert result.error_code == code
    assert_no_secrets(str(result))


def test_certificate_marker_fallback_is_safe_without_a_nested_ssl_exception():
    result = network_error(httpx.ConnectError(f"CERTIFICATE_VERIFY_FAILED: {UNSAFE_MESSAGE}"))
    assert result.error_code == "tls_certificate"
    assert_no_secrets(str(result))


def test_exception_chain_cycle_does_not_loop():
    error = httpx.ConnectError(UNSAFE_MESSAGE)
    error.__cause__ = error
    assert network_error(error).error_code == "connection"


def test_request_retry_exhaustion_reports_safe_category_in_error_and_logs(caplog, monkeypatch):
    calls, waits = [], []

    def handle(request):
        calls.append(request)
        raise httpx.ConnectTimeout(UNSAFE_MESSAGE, request=request)

    client = ConfluenceClient(source(), TOKEN, transport=httpx.MockTransport(handle), retries=2)
    monkeypatch.setattr(client, "_wait", waits.append)
    try:
        with caplog.at_level(logging.INFO), pytest.raises(ConfluenceError) as caught:
            client.page("1", body=False)
        assert caught.value.error_code == "connect_timeout"
        assert len(calls) == 3
        assert waits == [1, 2]
        failure_logs = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and "connect_timeout" in record.message
        ]
        assert len(failure_logs) == 3
        assert all(record.exc_info is None for record in failure_logs)
        assert_no_secrets(str(caught.value) + caplog.text)
    finally:
        client.close()


def test_transient_transport_failure_can_still_recover(monkeypatch):
    calls, waits = [], []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout(UNSAFE_MESSAGE, request=request)
        return httpx.Response(200, json={"id": "1", "title": "Root", "version": {"number": 7}})

    client = ConfluenceClient(source(), TOKEN, transport=httpx.MockTransport(handle), retries=1)
    monkeypatch.setattr(client, "_wait", waits.append)
    try:
        assert client.page("1", body=False).version == 7
        assert len(calls) == 2 and waits == [1]
    finally:
        client.close()


def test_cancellation_during_failed_request_stops_before_another_attempt():
    calls, cancel = [], threading.Event()

    def handle(request):
        calls.append(request)
        cancel.set()
        raise httpx.ConnectError(UNSAFE_MESSAGE, request=request)

    client = ConfluenceClient(
        source(), TOKEN, transport=httpx.MockTransport(handle), cancel=cancel, retries=3
    )
    try:
        with pytest.raises(SyncCancelled):
            client.page("1", body=False)
        assert len(calls) == 1
    finally:
        client.close()


def test_http_401_remains_an_authentication_failure_without_transport_retries(monkeypatch):
    calls, waits = [], []

    def handle(request):
        calls.append(request)
        return httpx.Response(401, text=UNSAFE_MESSAGE)

    client = ConfluenceClient(source(), TOKEN, transport=httpx.MockTransport(handle), retries=3)
    monkeypatch.setattr(client, "_wait", waits.append)
    try:
        with pytest.raises(ConfluenceError, match="Authentication failed") as caught:
            client.page("1", body=False)
        assert caught.value.status_code == 401
        assert caught.value.error_code is None
        assert len(calls) == 1 and waits == []
        assert_no_secrets(str(caught.value))
    finally:
        client.close()


def test_cdn_attachment_failure_keeps_signed_url_out_of_error_and_logs(caplog, monkeypatch):
    calls, waits = [], []

    def handle(request):
        calls.append(request)
        if request.url.host == "team.atlassian.net":
            return httpx.Response(
                302,
                headers={
                    "Location": f"https://api.media.atlassian.com/file/1?token={SIGNED_TOKEN}"
                },
            )
        assert "Authorization" not in request.headers
        raise httpx.ReadTimeout(UNSAFE_MESSAGE, request=request)

    client = ConfluenceClient(
        source(cloud=True), TOKEN, transport=httpx.MockTransport(handle), retries=1
    )
    monkeypatch.setattr(client, "_wait", waits.append)
    try:
        with caplog.at_level(logging.INFO), pytest.raises(ConfluenceError) as caught:
            client.download(
                Attachment(id="1", title="diagram.png", download_url="/download/attachments/1")
            )
        assert caught.value.error_code == "read_timeout"
        assert str(caught.value).startswith("Attachment network request failed [read_timeout]:")
        assert len(calls) == 3 and waits == [1]
        assert "read_timeout" in caplog.text
        assert_no_secrets(str(caught.value) + caplog.text)
    finally:
        client.close()
