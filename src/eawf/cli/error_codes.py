"""Cause-level :class:`ErrorCode` enum layered over the five exit buckets.

The five-bucket exit-code surface (:mod:`eawf.cli.exit_codes`) answers the
shell-scripting question *"how did this fail, broadly?"* — one of
``USER_ERROR (1)``, ``VALIDATION_ERROR (2)``, ``STATE_CONFLICT (3)``,
``DAEMON_UNREACHABLE (4)``, ``INTERNAL_ERROR (5)``. That granularity is
deliberately coarse so the exit-code contract stays stable.

:class:`ErrorCode` adds the missing precision *without* growing the
exit-code surface: each cause-level member names one specific failure mode
(``WAVE_DEPS_NOT_SATISFIED``, ``WORKTREE_DIRTY``, ``RUNTIME_AUTH_EXPIRED``,
...) and folds onto exactly one of the five buckets via
:func:`ErrorCode.exit_code`. Operators pivot CI on the precise member while
the process still exits with the stable bucket code.

Each member has exactly one doc anchor in ``docs/reference/error-codes.md``;
:func:`tests.unit.test_error_codes` enforces full anchor coverage.
"""

from __future__ import annotations

from enum import StrEnum

from eawf.cli import exit_codes


class ErrorCode(StrEnum):
    """Closed cause-level error vocabulary layered over the exit buckets.

    The string value of each member is its own name (PEP 663 ``StrEnum``),
    so ``ErrorCode.WORKTREE_DIRTY == "WORKTREE_DIRTY"`` holds for JSON
    serialisation and ``See <code>`` rendering. Members are grouped by
    failure domain in declaration order; the grouping is documentation
    only — the bucket mapping lives in :func:`exit_code`.
    """

    # --- Schema / state --------------------------------------------------
    STATE_VALIDATION_FAILED = "STATE_VALIDATION_FAILED"
    STATE_VERSION_MISMATCH = "STATE_VERSION_MISMATCH"
    BACKUP_WRITE_FAILED = "BACKUP_WRITE_FAILED"
    MIGRATION_STEP_FAILED = "MIGRATION_STEP_FAILED"
    MIGRATION_POSTCONDITION_FAILED = "MIGRATION_POSTCONDITION_FAILED"
    MIGRATION_TARGET_UNKNOWN = "MIGRATION_TARGET_UNKNOWN"

    # --- Daemon / IPC ----------------------------------------------------
    DAEMON_PROTOCOL_MAJOR_SKEW = "DAEMON_PROTOCOL_MAJOR_SKEW"
    DAEMON_PROTOCOL_MINOR_SKEW = "DAEMON_PROTOCOL_MINOR_SKEW"
    DAEMON_SPAWN_FAILED = "DAEMON_SPAWN_FAILED"
    DAEMON_LOCK_HELD = "DAEMON_LOCK_HELD"
    DAEMON_SOCKET_UNREACHABLE = "DAEMON_SOCKET_UNREACHABLE"

    # --- Scope / lifecycle -----------------------------------------------
    SCOPE_CONFLICT = "SCOPE_CONFLICT"
    WAVE_DEPS_NOT_SATISFIED = "WAVE_DEPS_NOT_SATISFIED"
    PHASE_NOT_ACTIVE = "PHASE_NOT_ACTIVE"
    ITER_NOT_ACTIVE = "ITER_NOT_ACTIVE"
    WAVE_OUT_OF_ORDER_REJECTED = "WAVE_OUT_OF_ORDER_REJECTED"

    # --- Worktree / git --------------------------------------------------
    WORKTREE_DIRTY = "WORKTREE_DIRTY"
    WORKTREE_BRANCH_STALE = "WORKTREE_BRANCH_STALE"
    CHERRY_PICK_CONFLICT = "CHERRY_PICK_CONFLICT"

    # --- Runtime / dispatch ----------------------------------------------
    RUNTIME_AUTH_EXPIRED = "RUNTIME_AUTH_EXPIRED"
    RUNTIME_RATE_LIMIT = "RUNTIME_RATE_LIMIT"
    RUNTIME_SERVER_ERROR = "RUNTIME_SERVER_ERROR"
    DISPATCH_BUDGET_EXCEEDED = "DISPATCH_BUDGET_EXCEEDED"
    SESSION_LOG_MISSING = "SESSION_LOG_MISSING"

    # --- Plugin / sync ---------------------------------------------------
    PLUGIN_MANIFEST_INVALID = "PLUGIN_MANIFEST_INVALID"
    PLUGIN_DRIFT_DETECTED = "PLUGIN_DRIFT_DETECTED"

    # --- Config / profile ------------------------------------------------
    PROFILE_CONFLICT_UNDECLARED = "PROFILE_CONFLICT_UNDECLARED"
    CONFIG_LAYER_NOT_WRITABLE = "CONFIG_LAYER_NOT_WRITABLE"
    CONFIG_FIELD_UNKNOWN = "CONFIG_FIELD_UNKNOWN"

    # --- User input ------------------------------------------------------
    INVALID_INPUT = "INVALID_INPUT"
    MISSING_REQUIRED_ANSWER = "MISSING_REQUIRED_ANSWER"

    # --- External --------------------------------------------------------
    EXTERNAL_API_FAILURE = "EXTERNAL_API_FAILURE"

    # --- Fallback --------------------------------------------------------
    UNKNOWN = "UNKNOWN"

    @property
    def exit_code(self) -> int:
        """Return the five-bucket exit code this cause folds onto.

        The mapping is total over the enum — every member resolves to one
        of the :mod:`eawf.cli.exit_codes` 1..5 values. ``UNKNOWN`` and any
        otherwise-unmapped member fold to ``INTERNAL_ERROR`` so an uncaught
        cause never escapes as exit 0.

        Returns:
            One of ``exit_codes.USER_ERROR``, ``VALIDATION_ERROR``,
            ``STATE_CONFLICT``, ``DAEMON_UNREACHABLE``, ``INTERNAL_ERROR``.
        """
        return _EXIT_CODE_FOR.get(self, exit_codes.INTERNAL_ERROR)


# Cause-level member -> five-bucket exit code. Kept as a module-level table
# (not a per-member attribute) so the enum stays a plain ``StrEnum`` whose
# values serialise as their own names.
_EXIT_CODE_FOR: dict[ErrorCode, int] = {
    # USER_ERROR (1) — operator-fixable input / environment / declined gate.
    ErrorCode.MIGRATION_TARGET_UNKNOWN: exit_codes.USER_ERROR,
    ErrorCode.SCOPE_CONFLICT: exit_codes.USER_ERROR,
    ErrorCode.WAVE_DEPS_NOT_SATISFIED: exit_codes.USER_ERROR,
    ErrorCode.PHASE_NOT_ACTIVE: exit_codes.USER_ERROR,
    ErrorCode.ITER_NOT_ACTIVE: exit_codes.USER_ERROR,
    ErrorCode.WAVE_OUT_OF_ORDER_REJECTED: exit_codes.USER_ERROR,
    ErrorCode.WORKTREE_DIRTY: exit_codes.USER_ERROR,
    ErrorCode.WORKTREE_BRANCH_STALE: exit_codes.USER_ERROR,
    ErrorCode.RUNTIME_AUTH_EXPIRED: exit_codes.USER_ERROR,
    ErrorCode.PROFILE_CONFLICT_UNDECLARED: exit_codes.USER_ERROR,
    ErrorCode.CONFIG_LAYER_NOT_WRITABLE: exit_codes.USER_ERROR,
    ErrorCode.CONFIG_FIELD_UNKNOWN: exit_codes.USER_ERROR,
    ErrorCode.INVALID_INPUT: exit_codes.USER_ERROR,
    ErrorCode.MISSING_REQUIRED_ANSWER: exit_codes.USER_ERROR,
    # VALIDATION_ERROR (2) — strict schema / invariant rejection.
    ErrorCode.STATE_VALIDATION_FAILED: exit_codes.VALIDATION_ERROR,
    ErrorCode.MIGRATION_POSTCONDITION_FAILED: exit_codes.VALIDATION_ERROR,
    ErrorCode.PLUGIN_MANIFEST_INVALID: exit_codes.VALIDATION_ERROR,
    # STATE_CONFLICT (3) — lock / integrity / drift / cherry-pick collision.
    ErrorCode.STATE_VERSION_MISMATCH: exit_codes.STATE_CONFLICT,
    ErrorCode.DAEMON_LOCK_HELD: exit_codes.STATE_CONFLICT,
    ErrorCode.CHERRY_PICK_CONFLICT: exit_codes.STATE_CONFLICT,
    ErrorCode.PLUGIN_DRIFT_DETECTED: exit_codes.STATE_CONFLICT,
    # DAEMON_UNREACHABLE (4) — daemon down / unspawnable / protocol skew.
    ErrorCode.DAEMON_PROTOCOL_MAJOR_SKEW: exit_codes.DAEMON_UNREACHABLE,
    ErrorCode.DAEMON_PROTOCOL_MINOR_SKEW: exit_codes.DAEMON_UNREACHABLE,
    ErrorCode.DAEMON_SPAWN_FAILED: exit_codes.DAEMON_UNREACHABLE,
    ErrorCode.DAEMON_SOCKET_UNREACHABLE: exit_codes.DAEMON_UNREACHABLE,
    # INTERNAL_ERROR (5) — backup write / migration step / runtime / external.
    ErrorCode.BACKUP_WRITE_FAILED: exit_codes.INTERNAL_ERROR,
    ErrorCode.MIGRATION_STEP_FAILED: exit_codes.INTERNAL_ERROR,
    ErrorCode.RUNTIME_RATE_LIMIT: exit_codes.INTERNAL_ERROR,
    ErrorCode.RUNTIME_SERVER_ERROR: exit_codes.INTERNAL_ERROR,
    ErrorCode.DISPATCH_BUDGET_EXCEEDED: exit_codes.INTERNAL_ERROR,
    ErrorCode.SESSION_LOG_MISSING: exit_codes.INTERNAL_ERROR,
    ErrorCode.EXTERNAL_API_FAILURE: exit_codes.INTERNAL_ERROR,
    ErrorCode.UNKNOWN: exit_codes.INTERNAL_ERROR,
}
