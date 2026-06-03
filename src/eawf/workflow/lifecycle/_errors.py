"""Shared exception type for lifecycle transitions.

Lives in its own module so the per-entity transition modules
(:mod:`eawf.workflow.lifecycle.phase`, :mod:`eawf.workflow.lifecycle.iter_`,
:mod:`eawf.workflow.lifecycle.wave`, :mod:`eawf.workflow.lifecycle.project`) can share the
single :class:`LifecycleError` type without importing one another.
"""

from __future__ import annotations


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
