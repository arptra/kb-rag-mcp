"""Direct TLS transport for explicitly enabled certificate-presentation mode.

OpenSSL still verifies CertificateVerify (possession of the peer's private key),
but CA-chain trust is deliberately not an enrollment requirement in this mode.
The certificate is only a source of CN: expiry and purpose are not checked here.
The application normalizes CN and applies the database account-status policy.
Certificate identity never crosses an HTTP header or a second listening socket.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from OpenSSL import SSL
from uvicorn.config import Config
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.server import ServerState

from corporate_kb.access.models import CertificateIdentity
from corporate_kb.access.tls import _CertificateScopeApp, identity_from_der

_CHUNK = 16 * 1024
_MAX_INPUT = 1024 * 1024
# A single non-streaming ASGI response must fit this budget. Larger responses
# should use streaming chunks; the cap also bounds a slow peer's pending data.
_MAX_OUTPUT = 16 * 1024 * 1024
_HANDSHAKE_TIMEOUT = 10.0
_CLOSE_TIMEOUT = 5.0
_LOGGER = logging.getLogger("uvicorn.error")


def _presented_identity(certificate: x509.Certificate | None) -> CertificateIdentity | None:
    """Read a CN-source certificate after TLS proof; application code validates CN.

    Certificate validity, issuer and purpose do not authorize this identity.
    Parse extensions only to reject malformed/duplicate ASN.1 structures, not
    to impose an EKU, KeyUsage, or other certificate-policy requirement.
    """
    if certificate is None:
        return None
    _ = certificate.extensions
    return identity_from_der(certificate.public_bytes(serialization.Encoding.DER))


class _PlaintextTransport(asyncio.Transport):
    """H11's transport; the underlying socket only ever receives TLS records."""

    def __init__(self, owner: _CertificateProtocol) -> None:
        super().__init__()
        self._owner = owner

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "sslcontext":
            return self._owner._context
        if name == "ssl_object":
            return self._owner._tls
        raw = self._owner._raw
        return default if raw is None else raw.get_extra_info(name, default)

    def write(self, data: bytes | bytearray | memoryview) -> None:
        self._owner._write(bytes(data))

    def writelines(self, list_of_data: Iterable[bytes | bytearray | memoryview]) -> None:
        for data in list_of_data:
            self.write(data)

    def close(self) -> None:
        self._owner._close()

    def abort(self) -> None:
        self._owner._abort()

    def is_closing(self) -> bool:
        return self._owner._closing or self._owner._closed

    def pause_reading(self) -> None:
        self._owner._read_paused = True
        self._owner._sync_reading()

    def resume_reading(self) -> None:
        self._owner._read_paused = False
        self._owner._sync_reading()
        self._owner._schedule_pump()

    def is_reading(self) -> bool:
        return not self.is_closing() and not self._owner._read_paused

    def get_write_buffer_size(self) -> int:
        raw = self._owner._raw
        return self._owner._output_size + (raw.get_write_buffer_size() if raw else 0)

    def get_write_buffer_limits(self) -> tuple[int, int]:
        return self._owner._low_water, self._owner._high_water

    def set_write_buffer_limits(self, high: int | None = None, low: int | None = None) -> None:
        high = 64 * 1024 if high is None else high
        low = high // 4 if low is None else low
        if not 0 <= low <= high < _MAX_OUTPUT:
            raise ValueError("Invalid TLS write buffer limits")
        self._owner._low_water, self._owner._high_water = low, high
        self._owner._update_flow()

    def can_write_eof(self) -> bool:
        return False

    def write_eof(self) -> None:
        raise NotImplementedError("TLS does not support half-close")

    def get_protocol(self) -> asyncio.Protocol:
        return self._owner._http

    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        # This adapter intentionally supports HTTP/1.1 only, without upgrades.
        raise NotImplementedError("Protocol upgrades are disabled for certificate TLS")


class _CertificateProtocol(asyncio.Protocol):
    """Memory-BIO TLS pump around Uvicorn's normal HTTP/1.1 implementation."""

    _context: SSL.Context

    def __init__(
        self,
        config: Config,
        server_state: ServerState,
        app_state: dict[str, Any],
        _loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._loop = _loop or asyncio.get_event_loop()
        self._server_state = server_state
        self._http = H11Protocol(config, server_state, app_state, self._loop)
        self._http.ws_protocol_class = None
        self._tls = SSL.Connection(self._context, None)
        self._tls.set_accept_state()
        self._plain = _PlaintextTransport(self)
        self._raw: asyncio.Transport | None = None
        self._input: deque[bytes] = deque()
        self._output: deque[bytes] = deque()
        self._input_size = 0
        self._output_size = 0
        self._handshake_bytes = 0
        self._handshaken = False
        self._read_paused = False
        self._raw_write_paused = False
        self._app_write_paused = False
        self._read_wants_write = False
        self._closing = False
        self._closed = False
        self._aborted = False
        self._pumping = False
        self._shutdown_started = False
        self._high_water = 64 * 1024
        self._low_water = 16 * 1024
        self._handshake_timer: asyncio.TimerHandle | None = None
        self._close_timer: asyncio.TimerHandle | None = None
        self._pump_handle: asyncio.Handle | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if not isinstance(transport, asyncio.Transport):
            transport.close()
            return
        self._raw = transport
        transport.set_write_buffer_limits(high=self._high_water, low=self._low_water)
        # Include incomplete handshakes in Uvicorn's graceful-shutdown accounting.
        self._server_state.connections.add(self)  # type: ignore[arg-type]
        self._handshake_timer = self._loop.call_later(_HANDSHAKE_TIMEOUT, self._abort)

    def data_received(self, data: bytes) -> None:
        if self._closed or self._aborted:
            return
        if not self._handshaken:
            self._handshake_bytes += len(data)
        if self._input_size + len(data) > _MAX_INPUT or self._handshake_bytes > _MAX_INPUT:
            self._abort()
            return
        self._input.extend(data[start : start + _CHUNK] for start in range(0, len(data), _CHUNK))
        self._input_size += len(data)
        self._pump()

    def eof_received(self) -> None:
        # An unannounced TCP half-close cannot carry another authenticated record.
        self._abort()

    def connection_lost(self, exc: Exception | None) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_callbacks()
        self._server_state.connections.discard(self)
        self._input.clear()
        self._output.clear()
        self._input_size = self._output_size = 0
        if self._handshaken:
            self._http.connection_lost(exc)
        self._raw = None

    def pause_writing(self) -> None:
        self._raw_write_paused = True
        self._sync_reading()
        self._update_flow()

    def resume_writing(self) -> None:
        self._raw_write_paused = False
        self._sync_reading()
        self._update_flow()
        self._schedule_pump()

    def shutdown(self) -> None:
        if self._handshaken:
            self._http.shutdown()
        else:
            self._abort()

    def _sync_reading(self) -> None:
        if self._raw is None or self._closed:
            return
        if self._read_paused or self._raw_write_paused or self._closing:
            self._raw.pause_reading()
        else:
            self._raw.resume_reading()

    def _update_flow(self) -> None:
        if not self._handshaken or self._closed:
            return
        size = self._plain.get_write_buffer_size()
        if not self._app_write_paused and (self._raw_write_paused or size > self._high_water):
            self._app_write_paused = True
            self._http.pause_writing()
        elif self._app_write_paused and not self._raw_write_paused and size <= self._low_water:
            self._app_write_paused = False
            self._http.resume_writing()

    def _write(self, data: bytes) -> None:
        if self._closing or self._closed or not data:
            return
        if self._plain.get_write_buffer_size() + len(data) > _MAX_OUTPUT:
            self._abort()
            return
        self._output.extend(data[start : start + _CHUNK] for start in range(0, len(data), _CHUNK))
        self._output_size += len(data)
        self._update_flow()
        self._schedule_pump()

    def _schedule_pump(self) -> None:
        if self._pump_handle is None and not self._closed and not self._aborted:
            self._pump_handle = self._loop.call_soon(self._scheduled_pump)

    def _scheduled_pump(self) -> None:
        self._pump_handle = None
        self._pump()

    def _feed_input(self) -> bool:
        if not self._input:
            return False
        data = self._input[0]
        count = self._tls.bio_write(data)
        if not count:
            return False
        self._input_size -= count
        if count == len(data):
            self._input.popleft()
        else:
            self._input[0] = data[count:]
        return True

    def _flush_ciphertext(self) -> bool:
        assert self._raw is not None
        while not self._raw_write_paused:
            try:
                data = self._tls.bio_read(_CHUNK)
            except SSL.WantReadError:
                return True
            if not data:
                return True
            if self._raw.get_write_buffer_size() + len(data) > _MAX_OUTPUT:
                self._abort()
                return False
            self._raw.write(data)
        return False

    def _finish_handshake(self) -> None:
        try:
            identity = _presented_identity(self._tls.get_peer_certificate(as_cryptography=True))
        except (ValueError, x509.DuplicateExtension, x509.UnsupportedGeneralNameType):
            identity = None
        self._http.app = _CertificateScopeApp(self._http.app, identity)
        self._http.connection_made(self._plain)
        # Track the TLS owner, not two connections; shutdown must reach TLS as well.
        self._server_state.connections.discard(self._http)
        self._handshaken = True
        if self._handshake_timer is not None:
            self._handshake_timer.cancel()
            self._handshake_timer = None

    def _pump(self) -> None:
        if self._pumping or self._closed or self._aborted or self._raw is None:
            return
        self._pumping = True
        try:
            # Bound work per event-loop tick, including pipelined HTTP requests.
            for _ in range(128):
                if self._aborted or not self._flush_ciphertext():
                    return
                progressed = False
                if not self._handshaken:
                    try:
                        self._tls.do_handshake()
                    except SSL.WantReadError:
                        progressed = self._feed_input()
                    except SSL.WantWriteError:
                        progressed = True
                    else:
                        self._finish_handshake()
                        progressed = True
                else:
                    if self._output and not self._read_wants_write:
                        retry_write = False
                        try:
                            count = self._tls.send(self._output[0])
                        except SSL.WantReadError:
                            progressed = self._feed_input()
                            retry_write = True
                        except SSL.WantWriteError:
                            progressed = True
                            retry_write = True
                        else:
                            self._output_size -= count
                            data = self._output.popleft()
                            if count < len(data):
                                self._output.appendleft(data[count:])
                            progressed = bool(count)
                        if not self._flush_ciphertext():
                            return
                        if retry_write:
                            # Retry the identical SSL_write before another TLS operation.
                            if not progressed:
                                return
                            continue
                    if self._closing and not self._output and not self._read_wants_write:
                        self._finish_close()
                        return
                    if self._read_wants_write or (not self._read_paused and not self._closing):
                        self._read_wants_write = False
                        try:
                            data = self._tls.recv(_CHUNK)
                        except SSL.WantReadError:
                            progressed = self._feed_input() or progressed
                        except SSL.WantWriteError:
                            # Retry SSL_read after draining its control records,
                            # before starting any pending application SSL_write.
                            self._read_wants_write = True
                            progressed = True
                        except SSL.ZeroReturnError:
                            self._close()
                            progressed = True
                        else:
                            if data and not self._closing:
                                self._http.data_received(data)
                            else:
                                self._close()
                            progressed = True
                if not self._flush_ciphertext() or not progressed:
                    return
            self._schedule_pump()
        except (SSL.Error, OSError):
            # OpenSSL exception strings can contain peer-controlled details.
            _LOGGER.debug("Certificate TLS connection ended during record processing")
            self._abort()
        finally:
            self._pumping = False
            self._update_flow()

    def _close(self) -> None:
        if self._closing or self._closed:
            return
        if not self._handshaken:
            self._abort()
            return
        self._closing = True
        self._sync_reading()
        self._close_timer = self._loop.call_later(_CLOSE_TIMEOUT, self._abort)
        self._schedule_pump()

    def _finish_close(self) -> None:
        if not self._shutdown_started:
            try:
                self._tls.shutdown()
            except SSL.WantWriteError:
                if self._flush_ciphertext():
                    self._schedule_pump()
                return
            except (SSL.WantReadError, SSL.ZeroReturnError):
                pass  # Our close_notify is queued; there is no need to wait for the peer's.
            self._shutdown_started = True
        if self._flush_ciphertext() and self._raw is not None:
            # asyncio flushes its queued ciphertext before closing the socket.
            self._raw.close()

    def _cancel_callbacks(self) -> None:
        for handle in (self._handshake_timer, self._close_timer, self._pump_handle):
            if handle is not None:
                handle.cancel()
        self._handshake_timer = self._close_timer = self._pump_handle = None

    def _abort(self) -> None:
        if self._closed or self._aborted:
            return
        self._aborted = self._closing = True
        self._cancel_callbacks()
        self._input.clear()
        self._output.clear()
        self._input_size = self._output_size = 0
        if self._raw is not None:
            self._raw.abort()


def certificate_http_protocol(certfile: Path, keyfile: Path) -> type[asyncio.Protocol]:
    """Build an HTTP protocol class; do not combine it with Uvicorn's SSL options."""
    context = SSL.Context(SSL.TLS_SERVER_METHOD)
    context.set_min_proto_version(SSL.TLS1_2_VERSION)
    options = SSL.OP_NO_COMPRESSION | SSL.OP_NO_TICKET
    options |= getattr(SSL, "OP_NO_RENEGOTIATION", 0)
    context.set_options(options)
    context.set_session_cache_mode(SSL.SESS_CACHE_OFF)
    context.set_cipher_list(b"HIGH:!aNULL:!eNULL:!MD5:!RC4:!3DES")
    context.use_certificate_chain_file(str(certfile))
    context.use_privatekey_file(str(keyfile))
    context.check_privatekey()
    # A new Context has an empty advertised client-CA list. Deliberately do not
    # populate it: browsers may offer personal certificates from any issuer.
    context.set_verify(SSL.VERIFY_PEER, lambda *_args: True)
    context.set_alpn_select_callback(
        lambda _connection, offered: (
            b"http/1.1" if b"http/1.1" in offered else SSL.NO_OVERLAPPING_PROTOCOLS
        )
    )

    class CertificateHTTPProtocol(_CertificateProtocol):
        _context = context

    return CertificateHTTPProtocol
