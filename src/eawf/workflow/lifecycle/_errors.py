"""Shared exception type for lifecycle transitions.

Lives in its own module so the per-entity transition modules
(:mod:`eawf.workflow.lifecycle.phase`, :mod:`eawf.workflow.lifecycle.iter_`,
:mod:`eawf.workflow.lifecycle.wave`, :mod:`eawf.workflow.lifecycle.project`) can share the
single :class:`LifecycleError` type without importing one another.
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
]


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
            code: Stable claim-session guard identifier.
            scope_id: Lifecycle entity the guard rejected.
            message: Human-readable rejection detail.
        """
        super().__init__(f"{code}: {message}")
        self.code = code
        self.scope_id = scope_id
        self.message = message


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


def check_criteria_floor(
    criteria: list[CriterionSpec],
    *,
    entity_kind: str,
    entity_id: str,
    waiver: object | None = None,
) -> None:
    """Enforce the plan-time typed-criteria floor at a wave-plan boundary.

    The authoring counterpart of the close-time verifier: a wave may not
    land with legacy-string (untyped) criteria, and a criterion that claims
    ``evidence_kind == "deterministic"`` may not land without at least one
    gate to falsify it (the gateless-deterministic hole). A typed
    :class:`~eawf.kernel.state.models.CriteriaFloorWaiver` bypasses the
    floor so repair-burst authoring stays possible but VISIBLE on the wave
    row -- the caller persists the waiver record.

    An empty criteria list passes: the authoring flow lands the wave first
    and materialises typed criteria via ``eawf spec sync`` before claim.

    Args:
        criteria: The success-criterion rows under the floor.
        entity_kind: Human label for the entity kind (``"wave"``).
        entity_id: The entity id, interpolated into the error.
        waiver: The typed waiver record, or ``None`` when not waived.

    Raises:
        LifecycleError: when a legacy row or a gateless deterministic
            criterion lands without a waiver.
    """
    from eawf.kernel.spec.common import GRANDFATHERED_KIND

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
