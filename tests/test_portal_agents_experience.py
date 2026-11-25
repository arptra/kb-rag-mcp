import sqlite3
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest

from portal_agents import experience
from portal_agents.experience import ExperienceStore, StrategyPreferences, profile_key
from portal_agents.model_settings import ModelProfile

KEY = sha256(b"model connection one").hexdigest()
OTHER = sha256(b"model connection two").hexdigest()


def test_profile_partitions_follow_effective_transport_without_persisting_private_inputs(tmp_path):
    profile = ModelProfile(provider="api", base_url="https://internal.example/v1",
                           model_name="company-model", api_key="PRIVATE-TOKEN",
                           cert_pem="PRIVATE-CERT", key_pem="PRIVATE-KEY")
    key = profile_key(profile)
    alias = profile.model_copy(update={
        "base_url": "https://INTERNAL.EXAMPLE:443/v1/chat/completions/",
        "key_pem": "ROTATED-PRIVATE-KEY", "source": "environment",
    })
    assert key == profile_key(alias)
    assert key != profile_key(profile, config_identity="updated-deployment")
    for change in ({"provider": "gigacode"}, {"adapter": "openai"},
                   {"base_url": "https://internal.example/v2"}, {"model_name": "other"}):
        assert profile_key(profile.model_copy(update=change)) != key
    # API details retained behind an inactive provider are irrelevant to GigaCode.
    assert (profile_key(profile.model_copy(update={"provider": "gigacode"}))
            == profile_key(ModelProfile()))

    path = tmp_path / "new" / "experience.sqlite3"
    store = ExperienceStore(path)
    assert not path.parent.exists()
    assert store.preferences(key) == StrategyPreferences()
    assert path.stat().st_mode & 0o777 == 0o600
    refused = "PRIVATE-USER-QUESTION-OR-TOOL-OUTPUT"
    with pytest.raises(ValueError, match="opaque"):
        store.record(profile.base_url, "read_success")
    with pytest.raises(ValueError, match="Unknown") as error:
        store.record_turn(key, ["read_success", refused])
    assert refused not in str(error.value)
    assert store.summary(key)["turns"] == 0
    store.record(key, "read_success")
    assert ExperienceStore(path).summary(key)["outcomes"]["read_success"] == 1
    data = path.read_bytes()
    for text in (profile.base_url, profile.model_name, profile.api_key,
                 profile.cert_pem, profile.key_pem, refused):
        assert text.encode() not in data


def test_deduped_turns_learn_confirmed_improvements_and_mixed_directions_stay_neutral(tmp_path):
    store = ExperienceStore(tmp_path / "experience.sqlite3")
    store.record_turn(KEY, ["native_schema", "read_success", "native_to_read_success",
                            "native_to_read_success", "long_read_success", "long_read_success"])
    for _ in range(4):
        store.record_turn(KEY, ["native_schema", "native_incomplete", "read_format",
                                "read_length", "native_success"])
    store.record_turn(KEY, ["policy", "native_to_read_success", "long_read_success"])
    assert store.preferences(KEY) == StrategyPreferences()
    first = store.summary(KEY)
    assert first["turns"] == 6 and first["outcomes"]["native_to_read_success"] == 1
    assert first["outcomes"]["long_read_success"] == 1 and first["outcomes"]["policy"] == 1
    store.record_turn(KEY, ["native_to_read_success", "long_read_success"])
    assert store.preferences(KEY) == StrategyPreferences(True, True)
    store.record(KEY, "read_to_native_success")
    assert store.preferences(KEY) == StrategyPreferences(None, True)
    for _ in range(2):
        store.record(OTHER, "read_to_native_success")
    assert store.preferences(OTHER) == StrategyPreferences(False, False)


def test_expired_cases_leave_preferences_and_are_pruned_on_the_next_write(tmp_path, monkeypatch):
    path = tmp_path / "journal.sqlite3"
    store = ExperienceStore(path)
    now = 1_700_000_000
    monkeypatch.setattr(experience.time, "time", lambda: now)
    for _ in range(2):
        store.record(KEY, "native_to_read_success")
    now += 100
    for _ in range(2):
        store.record(KEY, "read_to_native_success")
    assert store.preferences(KEY) == StrategyPreferences()
    now += experience._TTL - 100
    assert store.preferences(KEY) == StrategyPreferences(False, False)
    assert store.summary(KEY)["turns"] == 2
    store.record(OTHER, "policy")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM strategy_turns").fetchone() == (3,)


def test_limits_bound_journal_and_unrelated_noise_cannot_evict_recoveries(tmp_path, monkeypatch):
    monkeypatch.setattr(experience, "_PROFILE_LIMIT", 2)
    monkeypatch.setattr(experience, "_GLOBAL_LIMIT", 3)
    path = tmp_path / "journal.sqlite3"
    store = ExperienceStore(path)
    for _ in range(2):
        store.record(KEY, "native_to_read_success")
    for _ in range(8):
        store.record(KEY, "policy")
    assert store.preferences(KEY) == StrategyPreferences(True, False)
    assert store.summary(KEY)["turns"] == 4
    for n in range(10):
        key = sha256(str(n).encode()).hexdigest()
        store.record(key, "read_success")
        store.record(key, "long_read_success")
    with sqlite3.connect(path) as db:
        sizes = db.execute("SELECT confirmed, count(*) FROM strategy_turns GROUP BY confirmed")
        assert dict(sizes) == {0: 3, 1: 3}


def test_concurrent_workers_preserve_every_turn_through_cold_start(tmp_path):
    path = tmp_path / "new" / "shared.sqlite3"

    def save(_):
        ExperienceStore(path).record_turn(KEY, ["native_schema", "native_to_read_success",
                                               "native_schema"])

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(save, range(48)))
    stats = ExperienceStore(path).summary(KEY)
    assert stats["turns"] == 48
    assert stats["outcomes"]["native_to_read_success"] == stats["outcomes"]["native_schema"] == 48
    assert stats["prefer_chat_reads"] is True and stats["larger_read_answer"] is False
