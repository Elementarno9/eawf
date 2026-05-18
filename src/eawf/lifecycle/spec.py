"""Spec entity lifecycle DAG helpers (C01-IMPL W03 placeholder).

C01-IMPL reserves the Spec status vocabulary + canonical transition map
so C02 daemon writes and C03 spec CLI verbs share one source of truth.
The full PhaseSpec / IterSpec / WaveSpec Pydantic schemas + filesystem
storage layout land in C03-IMPL; this module exposes the typed DAG
helpers callers need before then.

Per c01-foundations §5.4.15 the lifecycle is strictly forward:

    DRAFT -> READY -> IMPLEMENTED -> ARCHIVED

No backward edges, no idempotent self-loops. ARCHIVED is terminal —
the daemon ``git rm``s the spec file and the operator restores from
``git log`` rather than transitioning the entity back.

Library-private. No CLI surface in v0.3; C05 spec CLI verbs land in
C05-IMPL and dispatch through C02 daemon RPC.
"""

from __future__ import annotations

import logging

from eawf.state.enums import SpecStatus

logger = logging.getLogger(__name__)


SPEC_TRANSITIONS: dict[SpecStatus, frozenset[SpecStatus]] = {
    SpecStatus.DRAFT: frozenset({SpecStatus.READY}),
    SpecStatus.READY: frozenset({SpecStatus.IMPLEMENTED}),
    SpecStatus.IMPLEMENTED: frozenset({SpecStatus.ARCHIVED}),
    SpecStatus.ARCHIVED: frozenset(),
}


class SpecTransitionError(Exception):
    """Raised when a Spec status transition violates the canonical DAG.

    C03 spec CLI verbs catch this and emit ``VALIDATION_FAILED`` exit
    codes; C02 daemon writes treat it as an internal-state-machine
    invariant violation.
    """


def validate_spec_transition(current: SpecStatus, target: SpecStatus) -> None:
    """Guard: raise :class:`SpecTransitionError` when ``current -> target`` is illegal.

    Args:
        current: Spec status before the transition.
        target: Spec status the caller intends to move to.

    Raises:
        SpecTransitionError: When the transition is not in
            :data:`SPEC_TRANSITIONS`.
    """
    allowed = SPEC_TRANSITIONS[current]
    if target not in allowed:
        raise SpecTransitionError(
            f"spec transition {current.value!r} -> {target.value!r} not allowed; "
            f"valid: {sorted(s.value for s in allowed)}"
        )
    logger.debug(f"validate_spec_transition current={current.value} target={target.value}")


def next_spec_statuses(current: SpecStatus) -> frozenset[SpecStatus]:
    """Return the set of statuses reachable from ``current`` in one transition."""
    return SPEC_TRANSITIONS[current]


def is_terminal_spec_status(status: SpecStatus) -> bool:
    """Return ``True`` when ``status`` has no outgoing transitions."""
    return not SPEC_TRANSITIONS[status]
