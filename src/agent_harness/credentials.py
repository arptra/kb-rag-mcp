"""Administrator-owned SSH secrets, never included in integration settings or tool data."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from pydantic import BaseModel, ConfigDict, Field


class SSHKeyUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=160)
    private_key: str = Field(min_length=1, max_length=24000, repr=False)
    known_hosts: str = Field(min_length=1, max_length=24000, repr=False)


@dataclass(repr=False, frozen=True)
class SSHCredential:
    metadata: dict[str, Any]
    private_key: str = field(repr=False)
    known_hosts: str = field(repr=False)

    def clone_input(self) -> bytes:
        return json.dumps(
            {"private_key": self.private_key, "known_hosts": self.known_hosts}
        ).encode()


def _validate(upload: SSHKeyUpload) -> SSHCredential:
    raw = upload.private_key.strip().encode() + b"\n"
    try:
        loader = (
            serialization.load_ssh_private_key
            if raw.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
            else serialization.load_pem_private_key
        )
        key = loader(raw, password=None)
        # Serializing to OpenSSH rejects unsuitable key types and drops PEM extras.
        canonical = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()
        public = key.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
    except TypeError:
        raise ValueError("Ключ защищён паролем. Нужен отдельный SSH-ключ без пароля.") from None
    except (ValueError, AttributeError, NotImplementedError, UnsupportedAlgorithm):
        raise ValueError("Нужен корректный приватный SSH-ключ в формате OpenSSH или PEM.") from None
    hosts: list[str] = []
    host_count = 0
    for line in upload.known_hosts.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if (
            len(fields) < 3
            or not re.fullmatch(r"[a-zA-Z0-9.,:\[\]|/+=_-]+", fields[0])
            or any(ord(char) < 32 for char in line)
        ):
            raise ValueError("known_hosts должен содержать проверенные ключи SSH-серверов.")
        try:
            serialization.load_ssh_public_key((fields[1] + " " + fields[2]).encode())
        except (ValueError, TypeError, NotImplementedError, UnsupportedAlgorithm):
            raise ValueError("known_hosts содержит некорректный публичный ключ сервера.") from None
        hosts.append(" ".join(fields[:3]))
        host_count += 1
    if not hosts:
        raise ValueError("Добавьте проверенный ключ сервера в known_hosts.")
    public_blob = base64.b64decode(public.split()[1])
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(public_blob).digest()
    ).decode().rstrip("=")
    return SSHCredential(
        metadata={
            "id": upload.id,
            "name": upload.name,
            "fingerprint": fingerprint,
            "key_type": public.split()[0].decode(),
            "created_at": datetime.now(UTC).isoformat(),
            "host_count": host_count,
        },
        private_key=canonical,
        known_hosts="\n".join(hosts) + "\n",
    )


class CredentialStore:
    """Local single-process store. Files are private; APIs only expose metadata."""

    def __init__(self, state_dir: Path) -> None:
        self.directory = state_dir / "ssh-credentials"
        self._lock = RLock()

    def _path(self, key_id: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", key_id):
            raise ValueError("Invalid SSH key ID")
        if self.directory.is_symlink():
            raise ValueError("SSH credential directory must not be a symbolic link")
        path = self.directory / (key_id + ".json")
        if path.is_symlink():
            raise ValueError("SSH credential must not be a symbolic link")
        return path

    def save(self, upload: SSHKeyUpload) -> dict[str, Any]:
        secret = _validate(upload)
        with self._lock:
            path = self._path(upload.id)
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            temporary = self.directory / ("." + uuid4().hex + ".tmp")
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(
                        {
                            "metadata": secret.metadata,
                            "private_key": secret.private_key,
                            "known_hosts": secret.known_hosts,
                        },
                        stream,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return dict(secret.metadata)

    def load(self, key_id: str) -> SSHCredential:
        with self._lock:
            path = self._path(key_id)
            try:
                with path.open("rb") as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError("SSH credential is too large")
                data = json.loads(raw)
                metadata = data["metadata"]
                # Never expose arbitrary persisted fields via list metadata.
                public = {
                    key: metadata[key]
                    for key in ("id", "name", "fingerprint", "key_type", "created_at", "host_count")
                }
                return SSHCredential(public, data["private_key"], data["known_hosts"])
            except FileNotFoundError:
                raise KeyError("SSH key not found") from None
            except (ValueError, KeyError, TypeError):
                raise ValueError("SSH credential could not be read") from None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(self.load(path.stem).metadata)
                for path in sorted(self.directory.glob("*.json"))
            ]

    def delete(self, key_id: str) -> None:
        with self._lock:
            try:
                self._path(key_id).unlink()
            except FileNotFoundError:
                raise KeyError("SSH key not found") from None
