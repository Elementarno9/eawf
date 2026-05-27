"""Canonical Eä exit codes per ``docs/reference/exit-codes.md``.

Every CLI handler uses these constants when raising :class:`typer.Exit` so the
exit-code surface is stable across runtimes. The
:class:`eawf.surfaces.cli.errors.CliError` taxonomy maps one exception class per
non-zero code.

The v0.3 surface (``OK``, ``USER_ERROR``, ``VALIDATION_ERROR``,
``STATE_CONFLICT``, ``DAEMON_UNREACHABLE``, ``INTERNAL_ERROR``) is the
sole public contract. The legacy 0..9 alias block
(``GENERIC_ERROR`` / ``NOT_FOUND`` / ``INVALID_INPUT`` / ``VALIDATION_FAILED``
/ ``LOCK_CONFLICT`` / ``INSTRUMENT_MISSING`` / ``USER_DECLINED`` /
``INTEGRITY_VIOLATION`` / ``HOOK_BLOCKED``) was deleted in P28-I02-W21
after every downstream callsite migrated to the canonical names. The
historical bucket mapping (legacy name → canonical bucket) is recorded in
``docs/reference/exit-codes.md`` for archival purposes.
"""

from __future__ import annotations

# --- Canonical 0..5 surface ------------------------------------------------

OK: int = 0
USER_ERROR: int = 1
VALIDATION_ERROR: int = 2
STATE_CONFLICT: int = 3
DAEMON_UNREACHABLE: int = 4
INTERNAL_ERROR: int = 5

# --- Name lookup -----------------------------------------------------------
# Only the canonical 0..5 surface is reachable via ``name_for`` so error
# envelopes always emit the canonical name.

_NAMES: dict[int, str] = {
    OK: "OK",
    USER_ERROR: "USER_ERROR",
    VALIDATION_ERROR: "VALIDATION_ERROR",
    STATE_CONFLICT: "STATE_CONFLICT",
    DAEMON_UNREACHABLE: "DAEMON_UNREACHABLE",
    INTERNAL_ERROR: "INTERNAL_ERROR",
}


def name_for(code: int) -> str:
    """Return the canonical name for *code*.

    Raises:
        KeyError: When *code* falls outside the canonical 0..5 range.
    """
    return _NAMES[code]
