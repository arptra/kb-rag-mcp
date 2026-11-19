"""Optional harness configuration independent of the portal."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class HarnessSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HARNESS_", env_file=".env", extra="ignore")

    enabled: bool = False
    state_dir: Path = Path("var/agent-harness")
    quick_workers: int = Field(default=2, ge=1, le=16)
    investigate_workers: int = Field(default=1, ge=1, le=16)
    queue_max: int = Field(default=32, ge=1, le=1000)
    quick_timeout_seconds: float = Field(default=20, gt=0, le=3600)
    investigate_timeout_seconds: float = Field(default=300, gt=0, le=7200)
    authentication_timeout_seconds: float = Field(default=600, gt=0, le=7200)
    cleanup_timeout_seconds: float = Field(default=15, gt=0, le=120)
    source_timeout_seconds: float = Field(default=15, gt=0, le=120)
    final_reserve_seconds: float = Field(default=30, ge=0, le=300)
    quick_context_chars: int = Field(default=24_000, ge=12_000, le=200_000)
    investigate_context_chars: int = Field(default=48_000, ge=12_000, le=400_000)
    max_indexes: int = Field(default=1000, ge=1, le=1000)
    results_per_index: int = Field(default=3, ge=1, le=10)
    evidence_chars: int = Field(default=2000, ge=100, le=10_000)
    max_evidence: int = Field(default=24, ge=1, le=100)
    answer_chars: int = Field(default=12_000, ge=1000, le=50_000)
    retention_days: int = Field(default=30, ge=1, le=3650)
    max_runs_per_owner: int = Field(default=100, ge=1, le=10_000)
    max_total_runs: int = Field(default=10_000, ge=1, le=1_000_000)
