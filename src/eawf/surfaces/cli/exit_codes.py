"""Canonical Eä exit codes per ``docs/reference/exit-codes.md``.

Every CLI handler uses these constants when raising :class:`typer.Exit` so the
exit-code surface is stable across runtimes. The
:class:`eawf.surfaces.cli.errors.CliError` taxonomy maps one exception class per
non-zero code.

The v0.3 surface (``OK``, ``USER_ERROR``, ``VALIDATION_ERROR``,
``STATE_CONFLICT``, ``DAEMON_UNREACHABLE``, ``INTERNAL_ERROR``) plus
``NEEDS_OPERATOR`` is the sole public contract. The legacy 0..9 alias block
(``GENERIC_ERROR`` / ``NOT_FOUND`` / ``INVALID_INPUT`` / ``VALIDATION_FAILED``
/ ``LOCK_CONFLICT`` / ``INSTRUMENT_MISSING`` / ``USER_DECLINED`` /
``INTEGRITY_VIOLATION`` / ``HOOK_BLOCKED``) was deleted in P28-I02-W21
after every downstream callsite migrated to the canonical names. The
historical bucket mapping (legacy name → canonical bucket) is recorded in
``docs/reference/exit-codes.md`` for archival purposes.

This table is the only place an exit value is defined. Every outcome a
harness branches on has its own code: a refused mutation, a request that
stopped because it needs an operator's answer, and a failure to attach to
the daemon or to a tree are three different outcomes and exit three
different ways, so a caller never has to parse prose to tell them apart.
"""

from __future__ import annotations

# --- Canonical surface -------------------------------------------------------

OK: int = 0
USER_ERROR: int = 1
VALIDATION_ERROR: int = 2
STATE_CONFLICT: int = 3
DAEMON_UNREACHABLE: int = 4
INTERNAL_ERROR: int = 5
#: The request stopped at a needs-operator envelope: it cannot proceed until
#: an operator answers or approves, which no retry by the caller can supply.
NEEDS_OPERATOR: int = 6

#: The attach-failure outcome: the process could not attach to what it
#: addresses -- the daemon, or a tree whose console entry state is terminal
#: (resolution failed, migration required, schema unsupported). An
#: unreachable daemon is the CLI's attach failure, so the two share one code
#: rather than being two outcomes on one value.
ATTACH_FAILURE: int = DAEMON_UNREACHABLE

#: Every code the CLI exits with, in value order. The generated reference page
#: and the exit-code contract test both walk this tuple.
SURFACE: tuple[int, ...] = (
    OK,
    USER_ERROR,
    VALIDATION_ERROR,
    STATE_CONFLICT,
    DAEMON_UNREACHABLE,
    INTERNAL_ERROR,
    NEEDS_OPERATOR,
)

# --- Name lookup -----------------------------------------------------------
# Only the canonical surface is reachable via ``name_for`` so error
# envelopes always emit the canonical name.

_NAMES: dict[int, str] = {
    OK: "OK",
    USER_ERROR: "USER_ERROR",
    VALIDATION_ERROR: "VALIDATION_ERROR",
    STATE_CONFLICT: "STATE_CONFLICT",
    DAEMON_UNREACHABLE: "DAEMON_UNREACHABLE",
    INTERNAL_ERROR: "INTERNAL_ERROR",
    NEEDS_OPERATOR: "NEEDS_OPERATOR",
}


def name_for(code: int) -> str:
    """Return the canonical name for *code*.

    Raises:
        KeyError: When *code* is not on the canonical surface.
    """
    return _NAMES[code]
