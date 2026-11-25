import json
import sqlite3
import ssl
import stat

import pytest
from pydantic import SecretStr

from corporate_kb.access.dev_pki import prepare_local_identity
from corporate_kb.config import Settings
from portal_agents.model_settings import ModelSettingsStore, ModelSettingsUpdate


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        agents_state_dir=tmp_path / "agents",
        harness_state_dir=tmp_path / "portal-old",
        legacy_standalone_state_dir=tmp_path / "standalone-old",
    )


def legacy_connection(directory, **values):
    directory.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(directory / "configuration.sqlite3") as db:
        db.execute("CREATE TABLE model_connection (id INTEGER PRIMARY KEY, data TEXT)")
        db.execute("INSERT INTO model_connection VALUES (1, ?)", (json.dumps({
            "provider": "openai", "base_url": "https://gateway/v1/chat/completions",
            "model": "DeepSeek", **values,
        }),))


def legacy_tls(directory, pki):
    bundle = directory / "tls-credentials" / "mtls.json"
    bundle.parent.mkdir()
    bundle.write_text(json.dumps({
        "metadata": {"id": "mtls"}, "ca_certificate": pki.ca_cert.read_text(),
        "client_certificate": pki.client_cert.read_text(), "client_key": pki.client_key.read_text(),
    }))
    return bundle


@pytest.mark.parametrize("directory_field", ["harness_state_dir", "legacy_standalone_state_dir"])
def test_legacy_profile_usable_before_save_and_preserved_after_restart(
    settings, tmp_path, directory_field, monkeypatch,
):
    directory = getattr(settings, directory_field)
    legacy_connection(directory, tls_bundle_id="mtls", timeout_seconds=1800, output_tokens=32768,
                      api_key_env="LEGACY_MODEL_TOKEN")
    pki = prepare_local_identity(tmp_path / "pki")
    bundle = legacy_tls(directory, pki)
    old_bytes = bundle.read_bytes()
    monkeypatch.setenv("LEGACY_MODEL_TOKEN", "only-for-this-test")
    store = ModelSettingsStore(settings)
    profile = store.load()
    assert profile.public() == {
        "provider": "api", "adapter": "gpt2giga", "base_url": "https://gateway/v1/chat/completions",
        "model_name": "DeepSeek", "source": "legacy", "has_ca": True, "has_cert": True,
        "has_key": True, "has_api_key": True, "warning": "",
    }
    assert not store.path.exists()
    with profile.model_kwargs() as kwargs:
        assert kwargs["base_url"] == "https://gateway/v1"
        assert kwargs["api_key"] == "only-for-this-test"
        assert kwargs["timeout_seconds"] == 1800
        assert kwargs["output_tokens"] == 32768
        tls = ssl.create_default_context(cafile=kwargs["ca_file"])
        tls.load_cert_chain(kwargs["cert_file"], kwargs["key_file"])
        temporary_key = kwargs["key_file"]
        assert stat.S_IMODE(temporary_key.stat().st_mode) == 0o600
    assert not temporary_key.exists()
    assert not (directory / "model-settings.json").exists()

    store.save(ModelSettingsUpdate(provider="gigacode"))
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert "only-for-this-test" not in store.path.read_text()
    assert old_bytes == bundle.read_bytes()
    bundle.unlink()
    monkeypatch.setenv("LEGACY_MODEL_TOKEN", "rotated-test-token")
    reopened = ModelSettingsStore(settings)
    with reopened.load().model_kwargs() as kwargs:
        assert kwargs == {}
    activated = reopened.save(ModelSettingsUpdate(provider="api"))
    with activated.model_kwargs() as kwargs:
        assert kwargs["model_name"] == "DeepSeek"
        assert kwargs["api_key"] == "rotated-test-token"
        assert kwargs["key_file"].read_bytes() == pki.client_key.read_bytes()


def test_environment_precedence_and_api_key_reference(settings, tmp_path):
    legacy_connection(settings.harness_state_dir, tls_bundle_id="old")
    pki = prepare_local_identity(tmp_path / "pki")
    settings = settings.model_copy(update={
        "agents_model_base_url": "https://other/v2", "agents_model_name": "other",
        "agents_model_ca_file": pki.ca_cert, "agents_model_cert_file": pki.client_cert,
        "agents_model_key_file": pki.client_key, "agents_model_api_key": SecretStr("test-api-key"),
    })
    store = ModelSettingsStore(settings)
    assert store.load().source == "environment"
    store.save(ModelSettingsUpdate(provider="api", model_name="deployment-b"))
    assert "test-api-key" not in store.path.read_text()
    updated = ModelSettingsStore(settings.model_copy(update={
        "agents_model_api_key": SecretStr("rotated-api-key"),
    })).load()
    with updated.model_kwargs() as kwargs:
        assert kwargs["api_key"] == "rotated-api-key"
        assert kwargs["key_file"] == pki.client_key
        assert kwargs["model_name"] == "deployment-b"


def test_verify_draft_then_save_upload_and_reject_broken_replacement(settings, tmp_path):
    store = ModelSettingsStore(settings)
    pki = prepare_local_identity(tmp_path / "pki")
    body = ModelSettingsUpdate(
        provider="api", base_url="https://gateway/v1", model_name="deepseek",
        ca_pem=pki.ca_cert.read_text(), cert_pem=pki.client_cert.read_text(),
        key_pem=pki.client_key.read_text(),
    )
    assert store.preview(body).public()["has_ca"]
    assert not store.path.exists()
    store.save(body)
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="PEM"):
        store.save(ModelSettingsUpdate(provider="api", key_pem=pki.server_key.read_text()))
    assert store.path.read_bytes() == before
    assert not any("PRIVATE KEY" in str(v) for v in store.load().public().values())


def test_cross_origin_cannot_reuse_secrets_even_via_gigacode(settings):
    store = ModelSettingsStore(settings)
    store.save(ModelSettingsUpdate(provider="api", base_url="https://api.test:443/v1",
                                   api_key="secret"))
    store.preview(ModelSettingsUpdate(provider="api", base_url="https://api.test/v2"))
    store.save(ModelSettingsUpdate(provider="gigacode"))
    with pytest.raises(ValueError, match="другого адреса"):
        store.preview(ModelSettingsUpdate(provider="api", base_url="https://different.test/api"))
    with pytest.raises(ValueError, match="другого адреса"):
        store.save(ModelSettingsUpdate(provider="gigacode", base_url=""))
    changed = store.save(ModelSettingsUpdate(provider="api", base_url="https://other.test",
                                             api_key=""))
    assert not changed.public()["has_api_key"]


@pytest.mark.parametrize("bundle", ["absent", "../untrusted"])
def test_broken_legacy_tls_keeps_model_visible_and_cannot_run_plain(settings, bundle):
    legacy_connection(settings.harness_state_dir, tls_bundle_id=bundle)
    store = ModelSettingsStore(settings)
    profile = store.load()
    assert profile.public()["model_name"] == "DeepSeek"
    with pytest.raises(ValueError, match="TLS"), profile.model_kwargs():
        pytest.fail()
    assert store.preview(ModelSettingsUpdate(provider="api", ca_pem="", cert_pem="", key_pem=""))


def test_default_does_not_create_files(settings):
    store = ModelSettingsStore(settings)
    profile = store.load()
    assert profile.provider == "gigacode"
    assert profile.source == "default"
    assert not settings.agents_state_dir.exists()
    assert not settings.harness_state_dir.exists()


def test_legacy_deepseek_dialect_and_early_small_defaults(settings):
    legacy_connection(settings.harness_state_dir, model="DeepSeek-V4-Flash",
                      timeout_seconds=60, output_tokens=4096)
    with ModelSettingsStore(settings).load().model_kwargs() as kwargs:
        assert kwargs["adapter"] == "gpt2giga" and "disable_thinking" not in kwargs
        assert (kwargs["timeout_seconds"], kwargs["output_tokens"]) == (1800, 32768)


@pytest.mark.parametrize("broken_state", ["database", "json", "url", "new-json"])
def test_damaged_connection_can_be_replaced_without_exposing_url_or_using_gigacode(
    settings, broken_state,
):
    store = ModelSettingsStore(settings)
    legacy_connection(settings.harness_state_dir, base_url="https://username:secret@gateway/v1")
    database = settings.harness_state_dir / "configuration.sqlite3"
    if broken_state == "database":
        database.write_bytes(b"not sqlite")
    elif broken_state == "json":
        with sqlite3.connect(database) as db:
            db.execute("UPDATE model_connection SET data='{'")
    elif broken_state == "new-json":
        store.path.parent.mkdir()
        store.path.write_text("{")
    old_bytes = database.read_bytes()
    profile = store.load()
    assert profile.provider == "api" and profile.warning and not profile.public()["base_url"]
    assert "secret" not in str(profile.public())
    with pytest.raises(ValueError), profile.model_kwargs():
        pytest.fail()
    saved = store.save(ModelSettingsUpdate(provider="api", base_url="https://valid.example/v1",
                                          model_name="new"))
    assert not saved.warning and saved.provider == "api"
    assert database.read_bytes() == old_bytes


def test_switching_model_resets_legacy_dialect_and_applies_deepseek_defaults(settings):
    legacy_connection(settings.harness_state_dir, model="corporate-alias",
                      profile="deepseek_v4_flash", timeout_seconds=1200, output_tokens=8192)
    store = ModelSettingsStore(settings)
    with store.save(ModelSettingsUpdate(provider="api", model_name="corporate-alias",
                                        adapter="openai")).model_kwargs() as kwargs:
        assert kwargs["disable_thinking"] and kwargs["output_tokens"] == 8192
    generic = store.save(ModelSettingsUpdate(provider="api", model_name="other-model"))
    with generic.model_kwargs() as kwargs:
        assert {"disable_thinking", "output_tokens", "timeout_seconds"}.isdisjoint(kwargs)
    deepseek = store.save(ModelSettingsUpdate(provider="api", model_name="corp/DeepSeek-V4-Flash"))
    with deepseek.model_kwargs() as kwargs:
        assert kwargs["disable_thinking"]
        assert (kwargs["timeout_seconds"], kwargs["output_tokens"]) == (1800, 32768)


def test_existing_profile_defaults_to_gpt2giga_without_reupload(settings, tmp_path):
    pki = prepare_local_identity(tmp_path / "pki")
    store = ModelSettingsStore(settings)
    store.save(ModelSettingsUpdate(
        provider="api", base_url="https://gateway/v1", model_name="DeepSeek-V4-Flash",
        ca_pem=pki.ca_cert.read_text(), cert_pem=pki.client_cert.read_text(),
        key_pem=pki.client_key.read_text(),
    ))
    # Simulate a portal profile saved before the adapter selector existed.
    old = json.loads(store.path.read_text())
    del old["adapter"]
    store.path.write_text(json.dumps(old))
    assert store.load().adapter == "gpt2giga"
    draft = ModelSettingsUpdate(provider="api")
    with store.preview(draft).model_kwargs() as args:
        assert args["adapter"] == "gpt2giga" and args["model_name"] == "DeepSeek-V4-Flash"
        assert "disable_thinking" not in args
        assert (args["timeout_seconds"], args["output_tokens"]) == (1800, 32768)
        assert args["key_file"].read_bytes().strip() == pki.client_key.read_bytes().strip()
    store.save(draft)
    reopened = ModelSettingsStore(settings)
    assert reopened.load().public()["adapter"] == "gpt2giga"
    assert reopened.load().public()["has_key"]
    # The admin can switch to GigaCode temporarily, then restore the same API connection.
    with reopened.save(ModelSettingsUpdate(provider="gigacode")).model_kwargs() as args:
        assert args == {}
    with reopened.save(ModelSettingsUpdate(provider="api")).model_kwargs() as args:
        assert args["adapter"] == "gpt2giga"
    with pytest.raises(ValueError, match="другого адреса"):
        reopened.preview(ModelSettingsUpdate(provider="api", base_url="https://other/v1"))


@pytest.mark.parametrize("adapter", [None, "openai"])
def test_gateway_environment_selector(settings, monkeypatch, adapter):
    if adapter:
        monkeypatch.setenv("KB_AGENTS_MODEL_ADAPTER", adapter)
    else:
        monkeypatch.delenv("KB_AGENTS_MODEL_ADAPTER", raising=False)
    monkeypatch.setenv("KB_AGENTS_MODEL_BASE_URL", "https://native.test/api/v1")
    monkeypatch.setenv("KB_AGENTS_MODEL_NAME", "DeepSeek-V4-Flash")
    configured = Settings(_env_file=None, agents_state_dir=settings.agents_state_dir)
    store = ModelSettingsStore(configured)
    expected = adapter or "gpt2giga"
    assert store.load().public()["adapter"] == expected
    with store.load().model_kwargs() as args:
        assert args["adapter"] == expected and args["base_url"] == "https://native.test/api/v1"
        assert (args["timeout_seconds"], args["output_tokens"]) == (1800, 32768)
        assert ("disable_thinking" in args) == (expected == "openai")


def test_saved_explicit_adapter_wins_over_the_environment_default(settings):
    legacy_connection(settings.harness_state_dir)
    direct_default = settings.model_copy(update={"agents_model_adapter": "openai"})
    assert ModelSettingsStore(direct_default).load().adapter == "openai"
    store = ModelSettingsStore(settings)
    store.save(ModelSettingsUpdate(provider="api", adapter="openai"))
    assert ModelSettingsStore(settings).load().adapter == "openai"
    store.save(ModelSettingsUpdate(provider="api", adapter="gpt2giga"))
    assert ModelSettingsStore(direct_default).load().adapter == "gpt2giga"


def test_switching_protocol_retains_operator_limits_and_openai_dialect(settings):
    legacy_connection(settings.harness_state_dir, model="custom-model-alias",
                      profile="deepseek_v4_flash", timeout_seconds=1200, output_tokens=8192)
    store = ModelSettingsStore(settings)
    store.save(ModelSettingsUpdate(provider="api", adapter="gpt2giga"))
    with ModelSettingsStore(settings).load().model_kwargs() as args:
        assert (args["timeout_seconds"], args["output_tokens"]) == (1200, 8192)
        assert "disable_thinking" not in args
    with store.save(ModelSettingsUpdate(provider="api", adapter="openai")).model_kwargs() as args:
        assert (args["timeout_seconds"], args["output_tokens"]) == (1200, 8192)
        assert args["disable_thinking"]
