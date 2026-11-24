"""Model-specific defaults shared by transport and Harness execution policy."""

from __future__ import annotations

from typing import Literal

ModelProfile = Literal["auto", "generic", "deepseek_v4_flash"]
ResolvedModelProfile = Literal["generic", "deepseek_v4_flash"]

# This is the corporate deployment's limit, not the public provider's advertised
# window. Input, output and the context safety margin must fit inside this cap.
DEEPSEEK_CONTEXT_WINDOW_TOKENS = 256_000
DEEPSEEK_OUTPUT_TOKENS = 32_768
DEEPSEEK_TIMEOUT_SECONDS = 1800


def resolve_model_profile(profile: str = "auto", model: str = "") -> ResolvedModelProfile:
    if profile == "deepseek_v4_flash":
        return "deepseek_v4_flash"
    normalized = (
        model.rsplit("/", 1)[-1].casefold().replace("-", "").replace("_", "").replace(" ", "")
    )
    # Older R1/V3 deployments can have different windows and reject the V4
    # thinking switch. An unfamiliar corporate alias requires explicit selection.
    if profile == "auto" and normalized in {
        "deepseekflash", "deepseekv4flash", "deepseekflashv4",
    }:
        return "deepseek_v4_flash"
    return "generic"


def model_profile_defaults(profile: ResolvedModelProfile) -> dict[str, int]:
    if profile == "deepseek_v4_flash":
        return {
            "timeout_seconds": DEEPSEEK_TIMEOUT_SECONDS,
            "output_tokens": DEEPSEEK_OUTPUT_TOKENS,
            "model_window_tokens": DEEPSEEK_CONTEXT_WINDOW_TOKENS,
        }
    return {"timeout_seconds": 60, "output_tokens": 4096, "model_window_tokens": 65_536}


def model_profile_options() -> list[dict[str, object]]:
    return [
        {"id": "auto", "label": "Автоматически по названию модели"},
        {
            "id": "deepseek_v4_flash",
            "label": "DeepSeek Flash V4 · 256K",
            "defaults": model_profile_defaults("deepseek_v4_flash"),
        },
        {
            "id": "generic",
            "label": "Совместимый API",
            "defaults": model_profile_defaults("generic"),
        },
    ]
