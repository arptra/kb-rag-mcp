"""Credential storage outside chat history, integration settings and shared memory."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, field_validator


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
        self.personal = PersonalCredentials(state_dir)
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


class _PersonalUpload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    repository_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=160)


class PersonalSSHUpload(_PersonalUpload):
    kind: Literal["ssh"]
    private_key: SecretStr = Field(min_length=1, max_length=24000)
    known_hosts: SecretStr = Field(min_length=1, max_length=24000)


class GradleUpload(_PersonalUpload):
    kind: Literal["gradle"]
    registry_url: str = Field(min_length=1, max_length=2000)
    username: SecretStr = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=4096)

    @field_validator("registry_url")
    @classmethod
    def validate_registry(cls, value: str) -> str:
        parts = urlsplit(value)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or any(c.isspace() or ord(c) < 32 for c in value)
                or not re.fullmatch(r"[a-zA-Z0-9/_.~+-]*", parts.path)
                or any(p in {".", ".."} for p in parts.path.split("/"))
                or not re.fullmatch(r"[a-zA-Z0-9.\[\]:-]+", parts.netloc)):
            raise ValueError("Укажите HTTPS-адрес Maven-репозитория.")
        _ = parts.port
        return value.rstrip("/")

    @field_validator("username")
    @classmethod
    def validate_username(cls, username: SecretStr) -> SecretStr:
        if ":" in username.get_secret_value():
            raise ValueError("Имя пользователя не может содержать двоеточие.")
        return username


PersonalUpload: TypeAdapter[PersonalSSHUpload | GradleUpload] = TypeAdapter(
    Annotated[PersonalSSHUpload | GradleUpload, Field(discriminator="kind")]
)


class PersonalCredentials:
    """A private file shared by the installation; every query binds the authenticated owner."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "personal-credentials" / "credentials.sqlite3"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        folder = self.path.parent
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        if folder.is_symlink():
            raise ValueError("Некорректный каталог личных доступов.")
        folder.chmod(0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("""
                    CREATE TABLE IF NOT EXISTS credentials(
                        owner TEXT NOT NULL, id TEXT NOT NULL, repository_id TEXT NOT NULL,
                        repository_url TEXT NOT NULL, kind TEXT NOT NULL, metadata TEXT NOT NULL,
                        secret BLOB NOT NULL, PRIMARY KEY(owner, id),
                        UNIQUE(owner, repository_id, repository_url, kind)
                    )
                """)
                yield db
        finally:
            db.close()

    def _cipher(self, *, initialize: bool = False) -> Fernet:
        key_path = self.path.with_name("encryption.key")
        if initialize and not key_path.exists():
            # Publish a fully written key without overwriting a concurrent first writer.
            staging = self.path.with_name("." + uuid4().hex + ".key")
            try:
                fd = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as keyfile:
                    keyfile.write(Fernet.generate_key())
                    keyfile.flush()
                    os.fsync(keyfile.fileno())
                with suppress(FileExistsError):
                    os.link(staging, key_path)
            finally:
                staging.unlink(missing_ok=True)
        try:
            fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as keyfile:
                os.fchmod(keyfile.fileno(), 0o600)
                return Fernet(keyfile.read(45))
        except (OSError, ValueError):
            raise ValueError("Восстановите файл шифрования личных доступов.") from None

    def save(
        self, owner: str, url: str, upload: PersonalSSHUpload | GradleUpload,
    ) -> dict[str, str]:
        metadata = {"name": upload.name, "repository_id": upload.repository_id,
                    "repository_url": url, "kind": upload.kind}
        if isinstance(upload, PersonalSSHUpload):
            if url.startswith("https://"):
                raise ValueError("Выберите SSH-репозиторий.")
            key = _validate(SSHKeyUpload(id="key", name=upload.name,
                                         private_key=upload.private_key.get_secret_value(),
                                         known_hosts=upload.known_hosts.get_secret_value()))
            metadata["fingerprint"] = key.metadata["fingerprint"]
            secret = key.clone_input().decode()
        else:
            metadata["registry_url"] = upload.registry_url
            secret = json.dumps({"url": upload.registry_url,
                                 "username": upload.username.get_secret_value(),
                                 "password": upload.password.get_secret_value()})
        with self._connect() as db:
            empty = db.execute("SELECT 1 FROM credentials LIMIT 1").fetchone() is None
            cipher = self._cipher(initialize=empty)
            binding = [owner, upload.repository_id, url, upload.kind]
            encrypted = cipher.encrypt(json.dumps([binding, secret]).encode())
            row = db.execute("""
                INSERT INTO credentials(owner,id,repository_id,repository_url,kind,metadata,secret)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner,repository_id,repository_url,kind)
                DO UPDATE SET metadata=excluded.metadata,secret=excluded.secret RETURNING id
            """, (owner, uuid4().hex, upload.repository_id, url, upload.kind,
                  json.dumps(metadata), encrypted)).fetchone()
        return {**metadata, "id": row[0]}

    def list(self, owner: str) -> list[dict[str, str]]:
        with self._connect() as db:
            return [{**json.loads(raw), "id": key_id} for key_id, raw in db.execute(
                "SELECT id,metadata FROM credentials WHERE owner=? ORDER BY repository_id,kind",
                (owner,),
            )]

    def delete(self, owner: str, key_id: str) -> bool:
        with self._connect() as db:
            return bool(db.execute("DELETE FROM credentials WHERE owner=? AND id=?",
                                   (owner, key_id)).rowcount)

    def _lookup(
        self, owner: str, repository_id: str, url: str, kind: str,
    ) -> str | None:
        if not self.path.exists():
            return None
        with self._connect() as db:
            row = db.execute("""
                SELECT secret FROM credentials WHERE owner=? AND repository_id=?
                AND repository_url=? AND kind=?
            """, (owner, repository_id, url, kind)).fetchone()
        if not row:
            return None
        try:
            binding, secret = json.loads(self._cipher().decrypt(row[0]))
            if binding != [owner, repository_id, url, kind] or not isinstance(secret, str):
                raise InvalidToken
            return secret
        except (InvalidToken, TypeError, ValueError):
            raise ValueError("Проверьте файл шифрования личных доступов.") from None

    def clone_input(self, owner: str, repository_id: str, url: str) -> bytes | None:
        secret = self._lookup(owner, repository_id, url, "ssh")
        return secret.encode() if secret else None

    def gradle_access(
        self, owner: str, repository_id: str, url: str,
    ) -> tuple[str, SecretStr, SecretStr] | None:
        raw = self._lookup(owner, repository_id, url, "gradle")
        if not raw:
            return None
        secret = json.loads(raw)
        return (secret["url"], SecretStr(secret["username"]),
                SecretStr(secret["password"]))
