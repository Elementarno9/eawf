"""Lifecycle rejection types and the disabled-waiver policy guard.

Kept outside the workflow layer so epoch-2 kernel and daemon code can raise
and catch a lifecycle rejection without importing the transition modules
that first defined it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from eawf.kernel.spec.common import CriterionSpec


class LifecycleError(Exception):
    """Raised by lifecycle transitions when a guard rejects the change.

    The CLI layer catches this and remaps to the appropriate exit code.
    """


ClaimSessionGuardCode = Literal[
    "claim_session_not_found",
    "claim_session_not_active",
    "claim_session_scope_mismatch",
    "claim_session_role_mismatch",
]

WaiverGuardCode = Literal["waiver_mode_disabled"]

AuditAcceptanceGuardCode = Literal[
    "audit_not_found",
    "audit_scope_mismatch",
    "audit_kind_invalid",
    "audit_not_complete",
    "audit_verdict_rejected",
    "audit_evidence_missing",
]

LifecycleGuardCode = Literal[
    "claim_session_not_found",
    "claim_session_not_active",
    "claim_session_scope_mismatch",
    "claim_session_role_mismatch",
    "claim_parent_iter_missing",
    "claim_parent_phase_missing",
    "claim_parent_phase_not_active",
    "claim_parent_iter_terminal",
    "claim_active_iter_conflict",
    "claim_criteria_empty",
    "claim_parallel_limit_reached",
    "claim_budget_ceiling_reached",
    "spawn_wave_not_claimed",
    "waiver_mode_disabled",
    "audit_not_found",
    "audit_scope_mismatch",
    "audit_kind_invalid",
    "audit_not_complete",
    "audit_verdict_rejected",
    "audit_evidence_missing",
]

WAIVER_MODE_DISABLED: WaiverGuardCode = "waiver_mode_disabled"


class LifecycleGuardError(LifecycleError):
    """A lifecycle rejection carrying a stable machine-readable guard code.

    Plain :class:`LifecycleError` messages are operator prose: they move
    whenever the wording is improved, so no caller can key off them. A guard
    that a downstream consumer must be able to PROVE it hit (the H02
    claim-session guards, whose whole point is that a stale or wrong-scope
    session cannot claim) needs a stable identifier instead. ``code`` is that
    identifier; ``scope_id`` names the entity the rejection is about (the wave
    id for a claim guard) so a structured daemon log line can be filtered
    without parsing the message.

    Because it subclasses :class:`LifecycleError`, every existing
    ``except LifecycleError`` site keeps catching it unchanged; the daemon
    mutation boundary adds a narrower clause ahead of that one so a guard
    rejection surfaces as ``-32002 validation_failed`` rather than the generic
    ``-32602 invalid params``.

    Attributes:
        code: Stable snake_case guard code (e.g. ``claim_session_not_found``).
        scope_id: The entity id the rejection anchors to.
        message: The operator-facing message (also the exception ``str``).
    """

    def __init__(self, code: LifecycleGuardCode, scope_id: str, message: str) -> None:
        """Build a coded guard rejection anchored to one lifecycle scope.

        Args:
            code: Stable lifecycle guard identifier.
            scope_id: Lifecycle entity the guard rejected.
            message: Human-readable rejection detail.
        """
        super().__init__(f"{code}: {message}")
        self.code = code
        self.scope_id = scope_id
        self.message = message


def check_disabled_waiver_policy(
    *,
    waiver_mode: Literal["A", "B", "C", "disabled"],
    scope_id: str,
    criteria: list[CriterionSpec],
    criteria_floor_waiver: object | None = None,
) -> None:
    """Reject state-carried waiver mechanisms when policy disables waivers.

    The helper is deliberately independent of config loading. Production
    boundaries resolve the effective layered policy and pass it here; library
    callers can exercise the transition with an explicit mode. Running this
    before any field assignment gives plan, edit, claim, and close one coded
    rejection with byte-identical state on failure.

    Args:
        waiver_mode: Effective strict waiver policy.
        scope_id: Wave whose authoring or execution was attempted.
        criteria: Candidate or persisted criteria to inspect.
        criteria_floor_waiver: Candidate or persisted criteria-floor waiver.

    Raises:
        LifecycleGuardError: When disabled mode sees a criteria-floor waiver
            or any raw ``CriterionSpec.waiver_reason``.
    """
    if waiver_mode != "disabled":
        return
    if criteria_floor_waiver is not None:
        raise LifecycleGuardError(
            WAIVER_MODE_DISABLED,
            scope_id,
            f"criteria-floor waiver rejected for wave {scope_id!r}: waiver mode is disabled",
        )
    waived_criteria = [criterion.id for criterion in criteria if criterion.waiver_reason]
    if waived_criteria:
        raise LifecycleGuardError(
            WAIVER_MODE_DISABLED,
            scope_id,
            f"raw criterion waiver_reason rejected for wave {scope_id!r}: "
            f"waiver mode is disabled (criteria={waived_criteria})",
        )
