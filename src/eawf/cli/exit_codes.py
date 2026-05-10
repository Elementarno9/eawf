"""Canonical Eä exit codes per ``docs/reference/exit-codes.md``.

Every CLI handler uses these constants when raising :class:`typer.Exit` so the
exit-code surface is stable across runtimes. The :class:`eawf.cli.errors.CliError`
taxonomy maps one exception class per non-zero code.
"""

from __future__ import annotations

OK: int = 0
GENERIC_ERROR: int = 1
NOT_FOUND: int = 2
INVALID_INPUT: int = 3
VALIDATION_FAILED: int = 4
LOCK_CONFLICT: int = 5
INSTRUMENT_MISSING: int = 6
USER_DECLINED: int = 7
INTEGRITY_VIOLATION: int = 8
HOOK_BLOCKED: int = 9

_NAMES: dict[int, str] = {
    OK: "OK",
    GENERIC_ERROR: "GENERIC_ERROR",
    NOT_FOUND: "NOT_FOUND",
    INVALID_INPUT: "INVALID_INPUT",
    VALIDATION_FAILED: "VALIDATION_FAILED",
    LOCK_CONFLICT: "LOCK_CONFLICT",
    INSTRUMENT_MISSING: "INSTRUMENT_MISSING",
    USER_DECLINED: "USER_DECLINED",
    INTEGRITY_VIOLATION: "INTEGRITY_VIOLATION",
    HOOK_BLOCKED: "HOOK_BLOCKED",
}


def name_for(code: int) -> str:
    """Return the canonical name for *code*.

    Raises :class:`KeyError` for codes outside the canonical 0-9 range.
    """
    return _NAMES[code]
