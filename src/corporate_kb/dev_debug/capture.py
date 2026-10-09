"""Opt-in diagnostic hooks for development recordings, with no recovery actions."""

from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any

logger = logging.getLogger(__name__)
_CAPTURED_ATTRIBUTE = "_kb_dev_debug_failure_captured"
_MAX_DIAGNOSTIC_CHARS = 64_000


def emit_failure(component: str, phase: str, exc: Exception, **fields: Any) -> bool:
    """Log a sanitized failure once when explicitly enabled; never raise to the caller.

    The dev command's collector owns persistence. Do not pass ``exc_info`` to logging:
    handlers would otherwise format the original, potentially secret-bearing exception.
    """
    if os.environ.get("KB_DEV_DEBUG_CAPTURE") != "1":
        return False
    try:
        if getattr(exc, _CAPTURED_ATTRIBUTE, False):
            return False
        # Keep normal server imports and startup independent of recording support.
        from corporate_kb.dev_debug.recording import redact_text

        diagnostic = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        # Redact before truncation: cutting a private-key block first can hide its end marker.
        diagnostic = redact_text(diagnostic)
        if len(diagnostic) > _MAX_DIAGNOSTIC_CHARS:
            diagnostic = diagnostic[:_MAX_DIAGNOSTIC_CHARS] + "\n[diagnostic truncated]"
        context = redact_text(
            json.dumps(
                {"component": component, "phase": phase, **fields},
                ensure_ascii=False,
                default=str,
            )
        )[:8_000]
        logger.error("DEV_DEBUG_FAILURE %s\n%s", context, diagnostic)
        setattr(exc, _CAPTURED_ATTRIBUTE, True)
        return True
    except Exception:
        # A missing recorder, redaction error, or failing log handler cannot mask the failure.
        return False
