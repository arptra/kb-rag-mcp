"""Portal model selection, with read-only compatibility for the former Harness."""

# ruff: noqa: RUF001 -- Russian diagnostics.
from __future__ import annotations

import json
import os
import re
import sqlite3
import ssl
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any, Literal
from urllib.parse import urlsplit

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field

from corporate_kb.config import Settings

Provider = Literal["api", "gigacode"]
Adapter = Literal["openai", "gpt2giga"]
_BUNDLE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_TLS_UNAVAILABLE = "Комплект TLS прежнего Harness недоступен. Загрузите сертификаты модели."
_RECONFIGURE = "Настройки подключения повреждены. Укажите адрес API и учётные данные заново."


def _deepseek_flash(name: str) -> bool:
    normalized = re.sub(r"[-_ ]", "", name.rsplit("/", 1)[-1].casefold())
    return normalized in {"deepseekflash", "deepseekv4flash", "deepseekflashv4"}



class ModelSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: Provider
    adapter: Adapter | None = None
    base_url: str | None = Field(default=None, max_length=2000)
    model_name: str | None = Field(default=None, max_length=200)
    ca_pem: str | None = Field(default=None, max_length=65536, repr=False)
    cert_pem: str | None = Field(default=None, max_length=65536, repr=False)
    key_pem: str | None = Field(default=None, max_length=32768, repr=False)
    api_key: str | None = Field(default=None, max_length=8192, repr=False)


def _origin(url: str) -> tuple[str, str, int]:
    try:
        endpoint = urlsplit(url)
        if (not endpoint.hostname or endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment or any(ord(c) < 33 for c in url)
                or endpoint.scheme not in {"http", "https"}):
            raise ValueError
        if endpoint.scheme == "http" and endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError
        port = endpoint.port
        if port is None:
            port = 443 if endpoint.scheme == "https" else 80
        return endpoint.scheme, endpoint.hostname, port
    except ValueError:
        raise ValueError("Укажите HTTPS-адрес API без логина, пароля и параметров.") from None


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: Provider = "gigacode"
    adapter: Adapter = "gpt2giga"
    base_url: str = ""
    model_name: str = ""
    source: Literal["portal", "legacy", "environment", "default"] = "default"
    warning: str = ""
    ca_pem: str = Field(default="", repr=False)
    cert_pem: str = Field(default="", repr=False)
    key_pem: str = Field(default="", repr=False)
    ca_file: Path | None = None
    cert_file: Path | None = None
    key_file: Path | None = None
    api_key: str = Field(default="", repr=False)
    api_key_env: str = ""
    uses_env_api_key: bool = False
    environment_key: str = Field(default="", repr=False, exclude=True)
    disable_thinking: bool = False
    timeout_seconds: int | None = Field(default=None, ge=5, le=7200)
    output_tokens: int | None = Field(default=None, ge=1, le=131072)

    def use_model_defaults(self) -> None:
        self.disable_thinking = _deepseek_flash(self.model_name)
        self.timeout_seconds = 1800 if self.disable_thinking else None
        self.output_tokens = 32768 if self.disable_thinking else None

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "adapter": self.adapter,
            "base_url": self.base_url, "model_name": self.model_name,
            "source": self.source, "warning": self.warning,
            "has_ca": bool(self.ca_file or self.ca_pem),
            "has_cert": bool(self.cert_file or self.cert_pem),
            "has_key": bool(self.key_file or self.key_pem),
            "has_api_key": bool(self.api_key or self.environment_key),
        }

    @contextmanager
    def model_kwargs(self) -> Iterator[dict[str, Any]]:
        if self.provider == "gigacode":
            yield {}
            return
        if self.warning:
            raise ValueError(self.warning)
        scheme, _, _ = _origin(self.base_url)
        api_key = self.api_key or self.environment_key
        if (self.api_key_env or self.uses_env_api_key) and not api_key:
            raise ValueError("На сервере отсутствует настроенный ключ API модели.")
        if scheme != "https" and (api_key or self.cert_pem or self.cert_file):
            raise ValueError("Для отправки сертификата или ключа API требуется HTTPS.")
        kwargs: dict[str, Any] = {
            "base_url": self.base_url.rstrip("/").removesuffix("/chat/completions"),
            "model_name": self.model_name,
            "api_key": api_key or None,
            "adapter": self.adapter,
        }
        if self.disable_thinking and self.adapter == "openai":
            kwargs["disable_thinking"] = True
        if self.timeout_seconds is not None:
            kwargs["timeout_seconds"] = self.timeout_seconds
        if self.output_tokens is not None:
            kwargs["output_tokens"] = self.output_tokens
        with ExitStack() as stack:
            directory = None
            for kind in ("ca", "cert", "key"):
                material = getattr(self, f"{kind}_pem")
                path = getattr(self, f"{kind}_file")
                if material:
                    if directory is None:
                        directory = Path(stack.enter_context(TemporaryDirectory()))
                    path = directory / f"{kind}.pem"
                    with open(path, "x", opener=_private_opener) as stream:
                        stream.write(material)
                kwargs[f"{kind}_file"] = path
            yield kwargs


def _private_opener(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def _read_bundle(directory: Path, bundle_id: str) -> dict[str, str]:
    if not _BUNDLE_ID.fullmatch(bundle_id):
        return {"warning": _TLS_UNAVAILABLE}
    folder = directory / "tls-credentials"
    path = folder / (bundle_id + ".json")
    try:
        if folder.is_symlink() or path.is_symlink():
            raise ValueError
        with path.open("rb") as stream:
            raw = stream.read(131073)
        if len(raw) > 131072:
            raise ValueError
        data = json.loads(raw)
        if data["metadata"]["id"] != bundle_id:
            raise ValueError
        parts = {key: data[name] for key, name in (
            ("ca_pem", "ca_certificate"), ("cert_pem", "client_certificate"),
            ("key_pem", "client_key"),
        )}
        if not all(isinstance(v, str) for v in parts.values()) or not any(parts.values()):
            raise ValueError
        if bool(parts["cert_pem"]) != bool(parts["key_pem"]):
            raise ValueError
        return parts
    except (AttributeError, KeyError, ValueError, TypeError, OSError):
        return {"warning": _TLS_UNAVAILABLE}


def _legacy_profile(directory: Path) -> ModelProfile | None:
    path = directory / "configuration.sqlite3"
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_connection'"
            ).fetchone()
            if not table:
                return None
            row = db.execute("SELECT data FROM model_connection WHERE id=1").fetchone()
        if not row:
            return None
        old = json.loads(row[0])
        provider = {"openai": "api", "gigacode": "gigacode"}[old.get("provider", "gigacode")]
        bundle = old.get("tls_bundle_id") or ""
        key_env = old.get("api_key_env") or ""
        if key_env and not _ENV_NAME.fullmatch(key_env):
            raise ValueError
        name = old.get("model", "")
        deepseek = old.get("profile") == "deepseek_v4_flash" or (
            old.get("profile", "auto") == "auto" and _deepseek_flash(name)
        )
        timeout = old.get("timeout_seconds", 1800 if deepseek else 60)
        output = old.get("output_tokens", 32768 if deepseek else 4096)
        if deepseek and "profile" not in old:
            timeout = 1800 if timeout == 60 else timeout
            output = 32768 if output == 4096 else output
        return ModelProfile(
            provider=provider, source="legacy", base_url=old.get("base_url", ""),
            model_name=old.get("model", ""), api_key_env=key_env,
            timeout_seconds=timeout, output_tokens=output, disable_thinking=deepseek,
            **(_read_bundle(directory, bundle) if bundle else {}),
        )
    except (AttributeError, ValueError, KeyError, OSError, TypeError, sqlite3.Error):
        return ModelProfile(provider="api", source="legacy", warning=_RECONFIGURE)


class ModelSettingsStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.agents_state_dir / "model-settings.json"

    def load(self) -> ModelProfile:
        if self.path.exists():
            try:
                profile = ModelProfile.model_validate_json(self.path.read_bytes())
            except (OSError, ValueError):
                profile = ModelProfile(provider="api", source="portal", warning=_RECONFIGURE)
        elif self.settings.agents_model_base_url:
            cfg = self.settings
            profile = ModelProfile(
                source="environment", provider="api", base_url=cfg.agents_model_base_url or "",
                adapter=cfg.agents_model_adapter,
                model_name=cfg.agents_model_name or "", ca_file=cfg.agents_model_ca_file,
                cert_file=cfg.agents_model_cert_file, key_file=cfg.agents_model_key_file,
                uses_env_api_key=bool(cfg.agents_model_api_key),
            )
            profile.use_model_defaults()
        else:
            profile = (
                _legacy_profile(self.settings.harness_state_dir)
                or _legacy_profile(self.settings.legacy_standalone_state_dir)
                or ModelProfile()
            )
        if "adapter" not in profile.model_fields_set:
            profile.adapter = self.settings.agents_model_adapter
        if profile.base_url or profile.provider == "api":
            try:
                _origin(profile.base_url)
            except ValueError:
                # A malformed URL can itself contain credentials. Keep it out of public metadata.
                profile.base_url = ""
                profile.warning = profile.warning or _RECONFIGURE
        if profile.api_key_env:
            profile.environment_key = os.environ.get(profile.api_key_env, "")
        elif profile.uses_env_api_key and self.settings.agents_model_api_key:
            profile.environment_key = self.settings.agents_model_api_key.get_secret_value()
        return profile

    def preview(self, update: ModelSettingsUpdate) -> ModelProfile:
        profile = self.load()
        url = update.base_url.strip() if update.base_url is not None else profile.base_url
        previous_origin = _origin(profile.base_url) if profile.base_url else None
        new_origin = _origin(url) if url else None
        retains_credentials = any((
            bool(profile.cert_file or profile.cert_pem) and update.cert_pem is None,
            bool(profile.key_file or profile.key_pem) and update.key_pem is None,
            bool(profile.api_key or profile.uses_env_api_key or profile.api_key_env)
            and update.api_key is None,
        ))
        if previous_origin != new_origin and retains_credentials:
            raise ValueError(
                "Для другого адреса API заново укажите или явно удалите клиентские "
                "сертификаты и ключи."
            )
        name = update.model_name.strip() if update.model_name is not None else profile.model_name
        changed_endpoint = (url.rstrip("/").removesuffix("/chat/completions")
                            != profile.base_url.rstrip("/").removesuffix("/chat/completions"))
        if name != profile.model_name or changed_endpoint:
            profile.model_name = name
            profile.use_model_defaults()
        if update.adapter is not None:
            profile.adapter = update.adapter
        profile.base_url = url
        profile.provider = update.provider
        if profile.warning == _RECONFIGURE and new_origin and update.base_url is not None:
            profile.warning = ""
        for kind in ("ca", "cert", "key"):
            value = getattr(update, f"{kind}_pem")
            if value is not None:
                setattr(profile, f"{kind}_file", None)
                setattr(profile, f"{kind}_pem", value.strip())
        if update.api_key is not None:
            profile.api_key = update.api_key.strip()
            profile.uses_env_api_key = False
            profile.api_key_env = ""
            profile.environment_key = ""
        changes_tls = any(x is not None for x in (update.ca_pem, update.cert_pem, update.key_pem))
        if profile.warning == _TLS_UNAVAILABLE and changes_tls:
            profile.warning = ""
        if profile.provider == "api":
            with profile.model_kwargs() as kwargs:
                try:
                    tls = ssl.create_default_context(cafile=kwargs["ca_file"])
                    if kwargs["key_file"] and not kwargs["cert_file"]:
                        raise ValueError
                    if profile.cert_pem and not kwargs["key_file"]:
                        raise ValueError
                    if kwargs["cert_file"]:
                        tls.load_cert_chain(kwargs["cert_file"], kwargs["key_file"], password=b"")
                except (OSError, ValueError):
                    raise ValueError(
                        "Проверьте CA и пару клиентского сертификата и ключа в формате PEM."
                    ) from None
        return profile

    def save(self, update: ModelSettingsUpdate) -> ModelProfile:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(self.path.with_suffix(".lock"), timeout=5, mode=0o600):
            profile = self.preview(update)
            profile.source = "portal"
            with NamedTemporaryFile("w", dir=self.path.parent, delete=False) as stream:
                temp = Path(stream.name)
                try:
                    stream.write(profile.model_dump_json())
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.replace(temp, self.path)
                finally:
                    temp.unlink(missing_ok=True)
        return profile
