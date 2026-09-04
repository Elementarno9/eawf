"""Shared exception type for lifecycle transitions.

Lives in its own module so the per-entity transition modules
(:mod:`eawf.workflow.lifecycle.phase`, :mod:`eawf.workflow.lifecycle.iter_`,
:mod:`eawf.workflow.lifecycle.wave`, :mod:`eawf.workflow.lifecycle.project`) can share the
single :class:`LifecycleError` type without importing one another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from eawf.kernel.spec.common import CriterionSpec, GateSpec


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


def check_title_clarity(title: str, *, entity_kind: str, entity_id: str) -> None:
    """Run the EAWF016 title-clarity gate at a lifecycle mutation boundary.

    The shared wrapper the ``plan_wave`` / ``plan_iter`` / ``open_iter`` /
    ``plan_phase`` / ``open_phase`` transitions call so a new entity title is
    rejected at author time. The lint
    (:func:`eawf.platform.lint.eawf016_title_clarity.assert_title_clarity`)
    raises :class:`ValueError`; the lifecycle boundary uniformly raises
    :class:`LifecycleError`, so the message is re-wrapped without losing the
    rule detail. A clean title is a no-op.

    Args:
        title: The candidate entity title.
        entity_kind: Human label for the entity kind (``"wave"`` / ``"iter"``
            / ``"phase"`` / ``"decision"``).
        entity_id: The entity id, interpolated into the error.

    Raises:
        LifecycleError: when *title* fails one or more title-clarity rules.
    """
    from eawf.platform.lint.eawf016_title_clarity import assert_title_clarity

    try:
        assert_title_clarity(title, entity_kind=entity_kind, entity_id=entity_id)
    except ValueError as exc:
        raise LifecycleError(str(exc)) from exc


def check_criteria_measurability(
    criteria: list[CriterionSpec],
    *,
    entity_kind: str,
    entity_id: str,
) -> None:
    """Run the EAWF021 measurability gate at a wave-plan mutation boundary.

    The shared wrapper the ``plan_wave`` / ``edit_wave_plan`` transitions call
    so an unmeasurable typed success criterion is rejected at author time
    rather than slipping onto the wave row and failing only at the close gate.
    Each non-legacy row is run through the EAWF021 entrypoint
    (:func:`eawf.platform.lint.eawf021_measurable_criterion.check_criterion_spec`)
    and the findings are re-wrapped as a :class:`LifecycleError`. A
    :data:`~eawf.kernel.spec.common.GRANDFATHERED_KIND` legacy row is exempt: a
    free-form string wrapped via
    :func:`~eawf.kernel.spec.common.grandfather_criterion` carries no typed
    observation contract by construction, so linting it would reject every
    pre-typed on-disk wave on round-trip. An empty or all-measurable criterion
    list is a no-op.

    Args:
        criteria: The typed success-criterion rows to inspect.
        entity_kind: Human label for the entity kind (``"wave"``).
        entity_id: The entity id, interpolated into the error.

    Raises:
        LifecycleError: when one or more non-legacy criteria are unmeasurable.
    """
    from eawf.kernel.spec.common import GRANDFATHERED_KIND
    from eawf.platform.lint.eawf021_measurable_criterion import check_criterion_spec

    bodies: list[str] = []
    for criterion in criteria:
        if criterion.kind == GRANDFATHERED_KIND:
            continue
        bodies.extend(finding.render() for finding in check_criterion_spec(criterion))
    if bodies:
        raise LifecycleError(
            f"{entity_kind} {entity_id!r} has unmeasurable success criteria: " + "; ".join(bodies)
        )


def _check_gate_refs_resolve(
    criteria: list[CriterionSpec],
    gates: list[GateSpec],
    *,
    entity_kind: str,
    entity_id: str,
) -> None:
    """Reject a criterion/gate pair whose cross-references are not symmetric.

    Both directions of the binding are load-bearing at the close boundary,
    where the receipt preflight rejects any receipt whose ``gate_id`` is
    absent from the scored criterion's ``gate_ids``:

    * forward -- a ``gate_ids`` entry naming a gate the wave does not own
      is dangling, so the criterion claims a falsifier that can never run;
    * reverse -- a gate whose ``criterion_id`` names a criterion that does
      not name the gate back binds one-way. The gate RUNS and PASSES, its
      receipt is stored, and the close then deadlocks on
      ``close_preflight_stale`` because the stored receipt fails the
      binding check. A gate that never ran emits no receipt to reject, so
      the passing gate is precisely what makes this shape fatal.

    Rejection is preferred over inserting the missing id: a silent
    normalisation would hide the authoring error that produced the
    asymmetry, and the author is the only party who knows which of the two
    rows is wrong.

    Args:
        criteria: The success-criterion rows under the floor.
        gates: The gate rows the criteria are scored by.
        entity_kind: Human label for the entity kind (``"wave"``).
        entity_id: The entity id, interpolated into the error.

    Raises:
        LifecycleError: when a ``gate_ids`` entry does not resolve against
            *gates*, or a gate's ``criterion_id`` names a criterion whose
            ``gate_ids`` omits that gate.
    """
    known_gate_ids = {gate.id for gate in gates}
    dangling = [
        f"{criterion.id}->{ref}"
        for criterion in criteria
        for ref in criterion.gate_ids
        if ref not in known_gate_ids
    ]
    if dangling:
        raise LifecycleError(
            f"{entity_kind} {entity_id!r} fails the typed-criteria floor: "
            f"criteria reference unknown gate ids {dangling}; attach the gate "
            "(spec sync) or drop the dangling gate_ids entry"
        )
    criterion_by_id = {criterion.id: criterion for criterion in criteria}
    one_way = [
        f"{gate.id}->{gate.criterion_id}"
        for gate in gates
        if (owner := criterion_by_id.get(gate.criterion_id)) is not None
        and gate.id not in owner.gate_ids
    ]
    if one_way:
        raise LifecycleError(
            f"{entity_kind} {entity_id!r} fails the typed-criteria floor: "
            f"gates {one_way} name a criterion that does not list them in "
            "gate_ids; add the gate id to that criterion's gate_ids so the "
            "close-time receipt binding resolves"
        )


def check_criteria_floor(
    criteria: list[CriterionSpec],
    *,
    entity_kind: str,
    entity_id: str,
    waiver: object | None = None,
    gates: list[GateSpec] | None = None,
) -> None:
    """Enforce the plan-time typed-criteria floor at a wave-plan boundary.

    The authoring counterpart of the close-time verifier: a wave may not
    land with legacy-string (untyped) criteria, a criterion that claims
    ``evidence_kind == "deterministic"`` may not land without at least one
    gate to falsify it (the gateless-deterministic hole), and every
    criterion/gate cross-reference must resolve symmetrically against
    *gates*. A typed
    :class:`~eawf.kernel.state.models.CriteriaFloorWaiver` bypasses the
    quality legs of the floor so repair-burst authoring stays possible but
    VISIBLE on the wave row -- the caller persists the waiver record.

    The cross-reference leg runs BEFORE the waiver is honoured and is
    therefore unwaivable. The waiver exists to admit criteria that are
    merely under-specified, which a later ``spec sync`` can upgrade; an
    unresolved gate reference instead strands the wave at close, where the
    receipt preflight validates stored receipts before any waiver is
    considered. Letting the waiver through here would buy an authoring
    convenience at the price of a close-time deadlock.

    An empty criteria list passes: the authoring flow lands the wave first
    and materialises typed criteria via ``eawf spec sync`` before claim.

    Args:
        criteria: The success-criterion rows under the floor.
        entity_kind: Human label for the entity kind (``"wave"``).
        entity_id: The entity id, interpolated into the error.
        waiver: The typed waiver record, or ``None`` when not waived.
        gates: The authoritative gate set the criteria resolve against, or
            ``None`` when the caller does not know it. The distinction is
            deliberate: a list (empty included) asserts "these are the
            entity's gates", so the cross-reference leg runs, while
            ``None`` means the gate set has not been decided yet and the
            leg is skipped. Only :func:`~eawf.workflow.lifecycle.wave.plan_wave`
            passes ``None`` -- a wave being inserted earns its gates from a
            later ``spec sync``, and a deterministic criterion must already
            carry ``gate_ids`` to clear the gateless leg, so resolving
            against the not-yet-existent set would make such a criterion
            unauthorable. Every edit path supplies a list.

    Raises:
        LifecycleError: when a criterion/gate cross-reference does not
            resolve, or (absent a waiver) a legacy row or a gateless
            deterministic criterion lands.
    """
    from eawf.kernel.spec.common import GRANDFATHERED_KIND

    if gates is not None:
        _check_gate_refs_resolve(
            criteria,
            list(gates),
            entity_kind=entity_kind,
            entity_id=entity_id,
        )
    if waiver is not None:
        return
    legacy = [criterion.id for criterion in criteria if criterion.kind == GRANDFATHERED_KIND]
    if legacy:
        raise LifecycleError(
            f"{entity_kind} {entity_id!r} fails the typed-criteria floor: "
            f"legacy-string criteria {legacy}; author typed criteria (spec sync) "
            "or attach a criteria_floor_waiver with a >= 20-char reason"
        )
    gateless = [
        criterion.id
        for criterion in criteria
        if criterion.evidence_kind == "deterministic" and not criterion.gate_ids
    ]
    if gateless:
        raise LifecycleError(
            f"{entity_kind} {entity_id!r} fails the typed-criteria floor: "
            f"criteria {gateless} claim evidence_kind=deterministic with no gate "
            "attached (the gateless-deterministic hole); attach a falsifying gate"
        )
