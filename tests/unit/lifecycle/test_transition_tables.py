"""Unit + property tests for the lifecycle FSM transition tables (FS07).

The wave / phase / iter status guards used to be scattered across
:mod:`eawf.workflow.lifecycle.wave`, :mod:`~eawf.workflow.lifecycle.phase`, and
:mod:`~eawf.workflow.lifecycle.iter_` as inline ``if status != ...`` checks. FS07
extracted them into named guarded tables in
:mod:`eawf.workflow.lifecycle.spec` consulted via :func:`validate_transition`.

This module pins the extracted tables three ways:

- **Boundary** -- every legal edge in each table is accepted by
  :func:`validate_transition` (with satisfied guards); terminal statuses carry
  empty out-edge sets.
- **Error-path** -- an absent edge raises :class:`LifecycleError`; a guarded
  edge with an unmet predicate raises with the contract message substring.
- **CR-3 differential** -- a frozen reference edge-set encodes the legal
  transitions independently derived from the pre-refactor guard logic; the
  live tables must match it exactly (a regression pin), and a Hypothesis
  ``@given`` sweep over ``(frm, to)`` wave-status pairs asserts
  :func:`validate_transition` accepts/rejects identically to that reference.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from eawf.kernel.state.enums import (
    IterStatus,
    PhaseStatus,
    SpecStatus,
    WaveStatus,
)
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.spec import (
    ITER_TRANSITIONS,
    PHASE_TRANSITIONS,
    SPEC_TRANSITIONS,
    WAVE_TRANSITIONS,
    GuardContext,
    GuardName,
    validate_transition,
)

# ---------------------------------------------------------------------------
# Reference edge-sets: legal (frm, to) -> guards derived INDEPENDENTLY from
# reading the pre-refactor inline guard logic. These are the CR-3 regression
# pins; the live tables MUST match them exactly.
# ---------------------------------------------------------------------------

#: claim_wave (pending -> claimed, deps + sibling + pause guards), start_wave
#: (claimed -> in_progress), close_wave (claimed/in_progress -> closed),
#: fail_wave (pending/claimed/in_progress -> failed), release_wave
#: (claimed/in_progress -> pending).
_WAVE_REFERENCE: dict[tuple[WaveStatus, WaveStatus], frozenset[GuardName]] = {
    (WaveStatus.PENDING, WaveStatus.CLAIMED): frozenset(
        {GuardName.DEPS_CLOSED, GuardName.SIBLING_ORDERED, GuardName.NOT_PAUSED}
    ),
    (WaveStatus.PENDING, WaveStatus.FAILED): frozenset({GuardName.NONE}),
    (WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS): frozenset({GuardName.NONE}),
    (WaveStatus.CLAIMED, WaveStatus.CLOSED): frozenset({GuardName.NONE}),
    (WaveStatus.CLAIMED, WaveStatus.FAILED): frozenset({GuardName.NONE}),
    (WaveStatus.CLAIMED, WaveStatus.PENDING): frozenset({GuardName.NONE}),
    (WaveStatus.IN_PROGRESS, WaveStatus.CLOSED): frozenset({GuardName.NONE}),
    (WaveStatus.IN_PROGRESS, WaveStatus.FAILED): frozenset({GuardName.NONE}),
    (WaveStatus.IN_PROGRESS, WaveStatus.PENDING): frozenset({GuardName.NONE}),
}

#: activate_phase (planned -> active), close_phase (planned/active -> closed),
#: archive_phase (planned -> archived), reopen_phase (closed -> active).
_PHASE_REFERENCE: dict[tuple[PhaseStatus, PhaseStatus], frozenset[GuardName]] = {
    (PhaseStatus.PLANNED, PhaseStatus.ACTIVE): frozenset({GuardName.NONE}),
    (PhaseStatus.PLANNED, PhaseStatus.CLOSED): frozenset({GuardName.NONE}),
    (PhaseStatus.PLANNED, PhaseStatus.ARCHIVED): frozenset({GuardName.NONE}),
    (PhaseStatus.ACTIVE, PhaseStatus.CLOSED): frozenset({GuardName.NONE}),
    (PhaseStatus.CLOSED, PhaseStatus.ACTIVE): frozenset({GuardName.NONE}),
}

#: activate_iter (planned -> active), close_iter (planned/active -> closed),
#: archive cascade (planned/active -> abandoned).
_ITER_REFERENCE: dict[tuple[IterStatus, IterStatus], frozenset[GuardName]] = {
    (IterStatus.PLANNED, IterStatus.ACTIVE): frozenset({GuardName.NONE}),
    (IterStatus.PLANNED, IterStatus.CLOSED): frozenset({GuardName.NONE}),
    (IterStatus.PLANNED, IterStatus.ABANDONED): frozenset({GuardName.NONE}),
    (IterStatus.ACTIVE, IterStatus.CLOSED): frozenset({GuardName.NONE}),
    (IterStatus.ACTIVE, IterStatus.ABANDONED): frozenset({GuardName.NONE}),
}


def _table_as_reference(
    table: dict[object, frozenset[tuple[object, GuardName]]],
) -> dict[tuple[object, object], frozenset[GuardName]]:
    """Collapse a guarded table into a ``(frm, to) -> guards`` mapping."""
    out: dict[tuple[object, object], frozenset[GuardName]] = {}
    for frm, edges in table.items():
        for to, guard in edges:
            out.setdefault((frm, to), frozenset())
            out[(frm, to)] = out[(frm, to)] | {guard}
    return out


# ---------------------------------------------------------------------------
# CR-3 differential: live tables match the independently-derived reference.
# ---------------------------------------------------------------------------


def test_wave_table_matches_reference_edge_set() -> None:
    """WAVE_TRANSITIONS encodes exactly the pre-refactor legal wave edges."""
    assert _table_as_reference(WAVE_TRANSITIONS) == _WAVE_REFERENCE


def test_phase_table_matches_reference_edge_set() -> None:
    """PHASE_TRANSITIONS encodes exactly the pre-refactor legal phase edges."""
    assert _table_as_reference(PHASE_TRANSITIONS) == _PHASE_REFERENCE


def test_iter_table_matches_reference_edge_set() -> None:
    """ITER_TRANSITIONS encodes exactly the pre-refactor legal iter edges."""
    assert _table_as_reference(ITER_TRANSITIONS) == _ITER_REFERENCE


# ---------------------------------------------------------------------------
# Boundary: every legal edge is accepted; terminal statuses have no out-edges.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("frm_to", sorted(_WAVE_REFERENCE, key=lambda p: (p[0].value, p[1].value)))
def test_validate_transition_accepts_legal_wave_edges(
    frm_to: tuple[WaveStatus, WaveStatus],
) -> None:
    """Every legal wave edge passes with an all-satisfied guard context."""
    frm, to = frm_to
    validate_transition(WAVE_TRANSITIONS, frm, to, GuardContext())


@pytest.mark.parametrize("frm_to", sorted(_PHASE_REFERENCE, key=lambda p: (p[0].value, p[1].value)))
def test_validate_transition_accepts_legal_phase_edges(
    frm_to: tuple[PhaseStatus, PhaseStatus],
) -> None:
    frm, to = frm_to
    validate_transition(PHASE_TRANSITIONS, frm, to)


@pytest.mark.parametrize("frm_to", sorted(_ITER_REFERENCE, key=lambda p: (p[0].value, p[1].value)))
def test_validate_transition_accepts_legal_iter_edges(
    frm_to: tuple[IterStatus, IterStatus],
) -> None:
    frm, to = frm_to
    validate_transition(ITER_TRANSITIONS, frm, to)


@pytest.mark.parametrize(
    ("table", "terminal"),
    [
        (WAVE_TRANSITIONS, WaveStatus.CLOSED),
        (WAVE_TRANSITIONS, WaveStatus.FAILED),
        (WAVE_TRANSITIONS, WaveStatus.ABANDONED),
        (PHASE_TRANSITIONS, PhaseStatus.ARCHIVED),
        (ITER_TRANSITIONS, IterStatus.CLOSED),
        (ITER_TRANSITIONS, IterStatus.ABANDONED),
    ],
)
def test_terminal_statuses_have_no_out_edges(
    table: dict[object, frozenset[tuple[object, GuardName]]],
    terminal: object,
) -> None:
    """Terminal statuses carry an empty out-edge set in their table."""
    assert table[terminal] == frozenset()


def test_spec_table_legal_edges_still_validate() -> None:
    """The unguarded SPEC table is untouched and still maps the linear DAG."""
    assert SPEC_TRANSITIONS[SpecStatus.DRAFT] == frozenset({SpecStatus.READY})
    assert SPEC_TRANSITIONS[SpecStatus.READY] == frozenset({SpecStatus.IMPLEMENTED})
    assert SPEC_TRANSITIONS[SpecStatus.IMPLEMENTED] == frozenset({SpecStatus.ARCHIVED})
    assert SPEC_TRANSITIONS[SpecStatus.ARCHIVED] == frozenset()


# ---------------------------------------------------------------------------
# Error-path: illegal edges + unmet guards raise LifecycleError.
# ---------------------------------------------------------------------------


def test_validate_transition_rejects_absent_edge() -> None:
    """An edge absent from the table raises LifecycleError (CR-1)."""
    with pytest.raises(LifecycleError, match="illegal transition"):
        validate_transition(WAVE_TRANSITIONS, WaveStatus.CLOSED, WaveStatus.CLAIMED)


def test_validate_transition_absent_edge_uses_caller_message() -> None:
    """The caller-supplied illegal_message is surfaced verbatim."""
    with pytest.raises(LifecycleError, match="cannot start"):
        validate_transition(
            WAVE_TRANSITIONS,
            WaveStatus.PENDING,
            WaveStatus.IN_PROGRESS,
            illegal_message="wave 'W' is not claimed (status='pending'); cannot start",
        )


def test_validate_transition_rejects_unmet_deps_closed_guard() -> None:
    """A pending -> claimed edge with deps not closed raises with the message."""
    ctx = GuardContext(
        deps_closed=False,
        messages={GuardName.DEPS_CLOSED: "wave 'W2' blocked on un-closed dep waves: ['W1']"},
    )
    with pytest.raises(LifecycleError, match="un-closed dep waves"):
        validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, ctx)


def test_validate_transition_rejects_unmet_sibling_ordered_guard() -> None:
    """A claim skipping a lower-W## ready sibling raises with the message."""
    ctx = GuardContext(
        sibling_ordered=False,
        messages={
            GuardName.SIBLING_ORDERED: (
                "wave 'W2' would skip lower-numbered ready siblings: ['W1']; "
                "pass --out-of-order to claim regardless"
            )
        },
    )
    with pytest.raises(LifecycleError, match="lower-numbered ready siblings"):
        validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, ctx)


def test_validate_transition_out_of_order_satisfies_sibling_guard() -> None:
    """The out-of-order override satisfies the sibling-ordering guard."""
    ctx = GuardContext(sibling_ordered=False, out_of_order=True)
    validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, ctx)


def test_validate_transition_pause_gate_blocks_even_out_of_order() -> None:
    """The not-paused guard is unconditional: out_of_order does not bypass it."""
    ctx = GuardContext(
        not_paused=False,
        out_of_order=True,
        messages={GuardName.NOT_PAUSED: "dispatch paused: resume before claiming 'W1'"},
    )
    with pytest.raises(LifecycleError, match="dispatch paused: resume before claiming"):
        validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, ctx)


def test_validate_transition_deps_guard_surfaces_before_sibling() -> None:
    """Multiple unmet guards surface in the legacy deps -> sibling order."""
    ctx = GuardContext(
        deps_closed=False,
        sibling_ordered=False,
        messages={
            GuardName.DEPS_CLOSED: "deps marker",
            GuardName.SIBLING_ORDERED: "sibling marker",
        },
    )
    with pytest.raises(LifecycleError, match="deps marker"):
        validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, ctx)


def test_validate_transition_unmet_guard_without_message_uses_fallback() -> None:
    """A guard with no supplied message falls back to a generic phrasing."""
    ctx = GuardContext(not_paused=False)
    with pytest.raises(LifecycleError, match="blocked by guard"):
        validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, ctx)


# ---------------------------------------------------------------------------
# CR-3 Hypothesis sweep: accept/reject identical to the reference for every
# wave (frm, to) status pair, with all guards satisfied.
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(
    frm=st.sampled_from(list(WaveStatus)),
    to=st.sampled_from(list(WaveStatus)),
)
def test_validate_transition_matches_reference_for_all_wave_pairs(
    frm: WaveStatus, to: WaveStatus
) -> None:
    """For every wave status pair, accept iff the reference marks it legal.

    Guards are all satisfied (default context) so a legal edge always passes;
    an absent edge always raises. This is the holds-for-all differential
    between the new table-driven path and the independently-derived reference.
    """
    legal = (frm, to) in _WAVE_REFERENCE
    if legal:
        validate_transition(WAVE_TRANSITIONS, frm, to, GuardContext())
    else:
        with pytest.raises(LifecycleError):
            validate_transition(WAVE_TRANSITIONS, frm, to, GuardContext())
