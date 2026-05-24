"""Canonical Eä exit codes per ``docs/reference/exit-codes.md``.

Every CLI handler uses these constants when raising :class:`typer.Exit` so the
exit-code surface is stable across runtimes. The
:class:`eawf.surfaces.cli.errors.CliError` taxonomy maps one exception class per
non-zero code.

The v0.3 surface compresses the legacy 0..9 surface into the new 0..5
taxonomy (``OK``, ``USER_ERROR``, ``VALIDATION_ERROR``, ``STATE_CONFLICT``,
``DAEMON_UNREACHABLE``, ``INTERNAL_ERROR``). Legacy names remain exposed as
aliases mapped per the bucket table so downstream callsites continue to
compile until they migrate to the new five-class surface in subsequent waves.

Legacy → new bucket map:

* ``GENERIC_ERROR (1)`` → ``INTERNAL_ERROR (5)``
* ``NOT_FOUND (2)`` → ``USER_ERROR (1)``
* ``INVALID_INPUT (3)`` → ``USER_ERROR (1)``
* ``VALIDATION_FAILED (4)`` → ``VALIDATION_ERROR (2)``
* ``LOCK_CONFLICT (5)`` → ``STATE_CONFLICT (3)``
* ``INSTRUMENT_MISSING (6)`` → ``USER_ERROR (1)``
* ``USER_DECLINED (7)`` → ``USER_ERROR (1)``
* ``INTEGRITY_VIOLATION (8)`` → ``STATE_CONFLICT (3)``
* ``HOOK_BLOCKED (9)`` → ``STATE_CONFLICT (3)``
"""

from __future__ import annotations

# --- New 0..5 surface ------------------------------------------------------

OK: int = 0
USER_ERROR: int = 1
VALIDATION_ERROR: int = 2
STATE_CONFLICT: int = 3
DAEMON_UNREACHABLE: int = 4
INTERNAL_ERROR: int = 5

# --- Legacy 0..9 aliases mapped onto the new buckets (deprecated) ----------
# Downstream waves (W05+) retire each callsite; once empty, drop these
# aliases. Existing tests/callsites continue to import these names without
# behavioural change beyond the new numeric values.

GENERIC_ERROR: int = INTERNAL_ERROR
NOT_FOUND: int = USER_ERROR
INVALID_INPUT: int = USER_ERROR
VALIDATION_FAILED: int = VALIDATION_ERROR
LOCK_CONFLICT: int = STATE_CONFLICT
INSTRUMENT_MISSING: int = USER_ERROR
USER_DECLINED: int = USER_ERROR
INTEGRITY_VIOLATION: int = STATE_CONFLICT
HOOK_BLOCKED: int = STATE_CONFLICT

# --- Name lookup -----------------------------------------------------------
# Only the canonical 0..5 surface is reachable via ``name_for`` — legacy
# names are deliberately not addressable here so error envelopes always
# emit the new canonical name.

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
