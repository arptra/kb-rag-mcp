"""Credential-free explanations of outbound transport failures."""

from __future__ import annotations

import logging
import socket
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

from corporate_kb.confluence.models import ConfluenceError

_confluence_http_scope: ContextVar[bool] = ContextVar("confluence_http_scope", default=False)


class _ConfluenceHttpLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX logs full URLs at INFO; httpcore DEBUG also includes response
        # headers and raw errors. The client provides safe request diagnostics.
        return not _confluence_http_scope.get()


_http_log_filter = _ConfluenceHttpLogFilter()
for _logger_name in (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
):
    # Logger filters do not propagate to child loggers. Install on each concrete
    # transport logger, but leave records from all other operations untouched.
    logging.getLogger(_logger_name).addFilter(_http_log_filter)


@contextmanager
def safe_http_logging() -> Iterator[None]:
    """Use the client's safe diagnostics for this context's Confluence request."""
    token = _confluence_http_scope.set(True)
    try:
        yield
    finally:
        _confluence_http_scope.reset(token)


def network_error(exc: httpx.HTTPError, *, attachment: bool = False) -> ConfluenceError:
    # httpx/httpcore wrap socket and TLS exceptions. Never expose their raw text:
    # it can contain Authorization, proxy passwords or a signed attachment URL.
    causes: list[BaseException] = []
    pending: list[BaseException] = [exc]
    while pending and len(causes) < 16:
        cause = pending.pop()
        if any(cause is seen for seen in causes):
            continue
        causes.append(cause)
        for nested in (cause.__cause__, cause.__context__):
            if nested is not None:
                pending.append(nested)

    if any(isinstance(cause, ssl.SSLCertVerificationError) for cause in causes) or any(
        "CERTIFICATE_VERIFY_FAILED" in str(cause) for cause in causes
    ):
        code, hint = (
            "tls_certificate",
            "HTTPS certificate verification failed. Check the server certificate chain and "
            "export SSL_CERT_FILE with the trusted corporate CA bundle for the backend process. "
            "curl -k skips this check and does not confirm certificate trust.",
        )
    elif any(isinstance(cause, ssl.SSLError) for cause in causes):
        code, hint = "tls_handshake", "TLS handshake failed. Check server TLS and proxy settings."
    elif isinstance(exc, httpx.ProxyError):
        code, hint = "proxy", "Proxy connection failed. Check the backend proxy environment."
    elif any(isinstance(cause, socket.gaierror) for cause in causes):
        code, hint = "dns", "DNS resolution failed on the backend host. Check DNS, VPN and proxy."
    elif isinstance(exc, httpx.ConnectTimeout):
        code, hint = "connect_timeout", "Connection timed out. Check backend network access."
    elif isinstance(exc, httpx.ReadTimeout):
        code, hint = "read_timeout", "Timed out waiting for data from Confluence or its proxy."
    elif isinstance(exc, httpx.WriteTimeout):
        code, hint = "write_timeout", "Timed out sending the request to Confluence or its proxy."
    elif isinstance(exc, httpx.PoolTimeout):
        code, hint = "pool_timeout", "Timed out waiting for a free HTTP connection."
    elif any(isinstance(cause, ConnectionRefusedError) for cause in causes):
        code, hint = "connection_refused", "Connection refused. Check the backend route and port."
    elif isinstance(exc, httpx.LocalProtocolError):
        code, hint = (
            "local_protocol",
            "Invalid outgoing HTTP request. Check that the token contains no line breaks "
            "or control characters.",
        )
    elif isinstance(exc, httpx.RemoteProtocolError):
        code, hint = "remote_protocol", "Invalid HTTP response from Confluence or its proxy."
    elif isinstance(exc, httpx.ConnectError):
        code, hint = "connection", "Connection failed. Check backend DNS, VPN, firewall and proxy."
    else:
        code, hint = "network", "Network transfer failed. Check connectivity from the backend host."
    operation = "Attachment" if attachment else "Confluence"
    return ConfluenceError(f"{operation} network request failed [{code}]: {hint}", error_code=code)
