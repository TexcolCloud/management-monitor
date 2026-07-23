"""Compatibility exports for the pure diagnostic redaction rules."""

from safety_monitor.domain.diagnostics import (
    MAX_DIAGNOSTIC_LENGTH,
    REDACTED,
    diagnostic_error,
    redact_text,
)


__all__ = [
    "MAX_DIAGNOSTIC_LENGTH",
    "REDACTED",
    "diagnostic_error",
    "redact_text",
]
