"""Private TLS client identities and CA trust, separate from public model settings."""

from __future__ import annotations

import json
import os
import re
import ssl
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from pydantic import BaseModel, ConfigDict, Field


class TLSCredentialUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=160)
    ca_certificate: str = Field(default="", max_length=24000, repr=False)
    client_certificate: str = Field(default="", max_length=24000, repr=False)
    client_key: str = Field(default="", max_length=16000, repr=False)


def _certificates(value: str, *, ca: bool) -> list[x509.Certificate]:
    try:
        certificates = x509.load_pem_x509_certificates(value.encode())
        if not certificates:
            raise ValueError
        now = datetime.now(UTC)
        for certificate in certificates:
            if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
                raise ValueError("Сертификат ещё не действует или уже истёк.")
            if ca:
                basic = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
                if not basic.value.ca:
                    raise ValueError
        return certificates
    except (ValueError, x509.ExtensionNotFound, UnsupportedAlgorithm):
        kind = "CA" if ca else "клиентский"
        raise ValueError(f"Добавьте действующий {kind} сертификат в формате PEM.") from None


def _validate(upload: TLSCredentialUpload) -> tuple[dict[str, Any], dict[str, str]]:
    if not upload.ca_certificate.strip() and not upload.client_certificate.strip():
        raise ValueError("Добавьте CA-сертификат или пару: клиентский сертификат и приватный ключ.")
    if bool(upload.client_certificate.strip()) != bool(upload.client_key.strip()):
        raise ValueError("Клиентский сертификат и приватный ключ нужны вместе.")
    authorities = (
        _certificates(upload.ca_certificate, ca=True) if upload.ca_certificate.strip() else []
    )
    clients = (
        _certificates(upload.client_certificate, ca=False)
        if upload.client_certificate.strip()
        else []
    )
    private = ""
    if clients:
        try:
            key = serialization.load_pem_private_key(upload.client_key.encode(), password=None)
            public = key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            expected = (
                clients[0]
                .public_key()
                .public_bytes(
                    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
                )
            )
            if public != expected:
                raise ValueError("Ключ не соответствует клиентскому сертификату.")
            private = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode()
        except TypeError:
            raise ValueError("Нужен приватный ключ клиента без пароля в формате PEM.") from None
        except (ValueError, UnsupportedAlgorithm):
            raise ValueError("Приватный ключ не соответствует клиентскому сертификату.") from None
    metadata = {
        "id": upload.id,
        "name": upload.name,
        "has_ca_certificate": bool(authorities),
        "has_client_certificate": bool(clients),
        "expires_at": min(c.not_valid_after_utc for c in authorities + clients).isoformat(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    secret = {
        "ca_certificate": "".join(
            c.public_bytes(serialization.Encoding.PEM).decode() for c in authorities
        ),
        "client_certificate": "".join(
            c.public_bytes(serialization.Encoding.PEM).decode() for c in clients
        ),
        "client_key": private,
    }
    return metadata, secret


class TLSCredentialStore:
    def __init__(self, state_dir: Path) -> None:
        self.directory = state_dir / "tls-credentials"
        self._lock = RLock()

    def _path(self, key_id: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", key_id):
            raise ValueError("Invalid TLS credential ID")
        if self.directory.is_symlink():
            raise ValueError("TLS credential directory must not be a symbolic link")
        path = self.directory / (key_id + ".json")
        if path.is_symlink():
            raise ValueError("TLS credential must not be a symbolic link")
        return path

    def _load(self, key_id: str) -> dict[str, Any]:
        path = self._path(key_id)
        try:
            with path.open("rb") as stream:
                raw = stream.read(131073)
            if len(raw) > 131072:
                raise ValueError
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict):
                raise ValueError
            return data
        except FileNotFoundError:
            raise KeyError("TLS credential not found") from None
        except (ValueError, TypeError):
            raise ValueError("TLS credential could not be read") from None

    @staticmethod
    def _metadata(data: dict[str, Any]) -> dict[str, Any]:
        return {
            key: data["metadata"][key]
            for key in (
                "id",
                "name",
                "has_ca_certificate",
                "has_client_certificate",
                "expires_at",
                "created_at",
            )
        }

    def get(self, key_id: str) -> dict[str, Any]:
        with self._lock:
            return self._metadata(self._load(key_id))

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.get(path.stem) for path in sorted(self.directory.glob("*.json"))]

    def save(self, upload: TLSCredentialUpload) -> dict[str, Any]:
        metadata, secret = _validate(upload)
        with self._lock:
            path = self._path(upload.id)
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            temporary = self.directory / ("." + uuid4().hex + ".tmp")
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    json.dump({"metadata": metadata, **secret}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return metadata

    def ssl_context(self, key_id: str) -> ssl.SSLContext:
        with self._lock:
            data = self._load(key_id)
            # A supplied private CA supplements system trust; verification stays enabled.
            context = ssl.create_default_context()
            try:
                if data["ca_certificate"]:
                    context.load_verify_locations(cadata=data["ca_certificate"])
                if data["client_certificate"]:
                    with tempfile.TemporaryDirectory(
                        prefix=".tls-", dir=self.directory
                    ) as directory:
                        cert_path, key_path = (
                            Path(directory) / "chain.pem",
                            Path(directory) / "key.pem",
                        )
                        for target, value in (
                            (cert_path, data["client_certificate"]),
                            (key_path, data["client_key"]),
                        ):
                            descriptor = os.open(
                                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                            )
                            with os.fdopen(descriptor, "w") as stream:
                                stream.write(value)
                        context.load_cert_chain(cert_path, key_path)
            except (ssl.SSLError, KeyError, TypeError, ValueError):
                raise ValueError("Ошибка загрузки TLS-сертификатов. Проверьте настройки.") from None
            return context

    def delete(self, key_id: str) -> None:
        with self._lock:
            try:
                self._path(key_id).unlink()
            except FileNotFoundError:
                raise KeyError("TLS credential not found") from None
