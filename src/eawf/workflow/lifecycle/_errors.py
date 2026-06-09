"""Shared exception type for lifecycle transitions.

Lives in its own module so the per-entity transition modules
(:mod:`eawf.workflow.lifecycle.phase`, :mod:`eawf.workflow.lifecycle.iter_`,
:mod:`eawf.workflow.lifecycle.wave`, :mod:`eawf.workflow.lifecycle.project`) can share the
single :class:`LifecycleError` type without importing one another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eawf.kernel.spec.common import CriterionSpec


class LifecycleError(Exception):
    """Raised by lifecycle transitions when a guard rejects the change.

    The CLI layer catches this and remaps to the appropriate exit code.
    """


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
