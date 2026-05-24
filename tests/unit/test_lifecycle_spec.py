"""Unit tests for the Spec lifecycle DAG helpers (C01-IMPL W03).

Coverage:

- :data:`SPEC_TRANSITIONS` matches c01-foundations §5.4.15 verbatim
  (DRAFT -> READY -> IMPLEMENTED -> ARCHIVED, no backward edges).
- :func:`validate_spec_transition` accepts every legal edge and
  rejects every illegal one with :class:`SpecTransitionError`.
- :func:`next_spec_statuses` mirrors the transition map.
- :func:`is_terminal_spec_status` marks ARCHIVED terminal and every
  other status non-terminal.
"""

from __future__ import annotations

import pytest

from eawf.kernel.state.enums import SpecStatus
from eawf.lifecycle.spec import (
    SPEC_TRANSITIONS,
    SpecTransitionError,
    is_terminal_spec_status,
    next_spec_statuses,
    validate_spec_transition,
)


def test_spec_transitions_match_c01_dag() -> None:
    """SPEC_TRANSITIONS implements the c01-foundations §5.4.15 linear DAG."""
    assert {
        SpecStatus.DRAFT: frozenset({SpecStatus.READY}),
        SpecStatus.READY: frozenset({SpecStatus.IMPLEMENTED}),
        SpecStatus.IMPLEMENTED: frozenset({SpecStatus.ARCHIVED}),
        SpecStatus.ARCHIVED: frozenset(),
    } == SPEC_TRANSITIONS


def test_spec_transitions_covers_every_status() -> None:
    """Every SpecStatus value MUST appear as a transition map key.

    Regression guard: adding a new SpecStatus without wiring its
    outgoing edges leaves a KeyError trap for downstream callers.
    """
    assert set(SPEC_TRANSITIONS) == set(SpecStatus)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (SpecStatus.DRAFT, SpecStatus.READY),
        (SpecStatus.READY, SpecStatus.IMPLEMENTED),
        (SpecStatus.IMPLEMENTED, SpecStatus.ARCHIVED),
    ],
)
def test_validate_spec_transition_accepts_legal_edges(
    current: SpecStatus, target: SpecStatus
) -> None:
    validate_spec_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (SpecStatus.DRAFT, SpecStatus.IMPLEMENTED),
        (SpecStatus.DRAFT, SpecStatus.ARCHIVED),
        (SpecStatus.READY, SpecStatus.DRAFT),
        (SpecStatus.READY, SpecStatus.ARCHIVED),
        (SpecStatus.IMPLEMENTED, SpecStatus.DRAFT),
        (SpecStatus.IMPLEMENTED, SpecStatus.READY),
        (SpecStatus.ARCHIVED, SpecStatus.DRAFT),
        (SpecStatus.ARCHIVED, SpecStatus.READY),
        (SpecStatus.ARCHIVED, SpecStatus.IMPLEMENTED),
    ],
)
def test_validate_spec_transition_rejects_illegal_edges(
    current: SpecStatus, target: SpecStatus
) -> None:
    with pytest.raises(SpecTransitionError) as exc_info:
        validate_spec_transition(current, target)
    msg = str(exc_info.value)
    assert current.value in msg
    assert target.value in msg


@pytest.mark.parametrize(
    "status",
    [SpecStatus.DRAFT, SpecStatus.READY, SpecStatus.IMPLEMENTED],
)
def test_validate_spec_transition_rejects_self_loop(status: SpecStatus) -> None:
    """Idempotent self-loops are not part of the canonical DAG."""
    with pytest.raises(SpecTransitionError):
        validate_spec_transition(status, status)


def test_next_spec_statuses_returns_transition_set() -> None:
    assert next_spec_statuses(SpecStatus.DRAFT) == frozenset({SpecStatus.READY})
    assert next_spec_statuses(SpecStatus.READY) == frozenset({SpecStatus.IMPLEMENTED})
    assert next_spec_statuses(SpecStatus.IMPLEMENTED) == frozenset({SpecStatus.ARCHIVED})
    assert next_spec_statuses(SpecStatus.ARCHIVED) == frozenset()


def test_is_terminal_spec_status_archived_only() -> None:
    assert is_terminal_spec_status(SpecStatus.ARCHIVED) is True
    assert is_terminal_spec_status(SpecStatus.DRAFT) is False
    assert is_terminal_spec_status(SpecStatus.READY) is False
    assert is_terminal_spec_status(SpecStatus.IMPLEMENTED) is False
