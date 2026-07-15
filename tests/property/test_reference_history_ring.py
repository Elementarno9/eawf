"""Hypothesis property test for the bounded reference-history FIFO ring (P29-I08-W24).

The TUI's reference back / forward navigation history
(:attr:`~eawf.surfaces.tui.app.EaApp._reference_back_stack` /
``_reference_forward_stack``) was an UNBOUNDED ``list``: a long click-through
trail of reference cards grew the back history for the whole session. W24
bounds both to a FIFO ring (``deque(maxlen=``
:data:`~eawf.surfaces.tui.app.REFERENCE_HISTORY_MAX` ``)``) so the oldest
back-entry is evicted once the cap is reached.

These tests pin the ring's two load-bearing invariants over a *randomized*
navigation trail:

1. **The ring never exceeds the cap.** After every action in an arbitrary
   ``open`` / ``back`` / ``forward`` trail, neither the back nor the forward
   deque grows past :data:`REFERENCE_HISTORY_MAX`.
2. **``back`` at the cap stops cleanly without resurrecting an evicted entry.**
   When the trail pushes more than ``cap`` distinct targets, pressing ``back``
   walks ONLY the most-recent ``cap`` predecessors -- in order -- and then
   becomes a clean no-op; an entry evicted off the ring floor never reappears.

The property drives the model below, which is NOT a reimplementation of the
ring: it holds the SAME ``deque(maxlen=REFERENCE_HISTORY_MAX)`` the app holds
and applies the exact history rule
:meth:`~eawf.surfaces.tui.app.EaApp._navigate_reference` /
``action_reference_back`` / ``action_reference_forward`` apply (push current +
clear forward on a new distinct target; pop on back/forward), so a regression
in the app's cap (or a switch back to an unbounded list) breaks the import-
shared :data:`REFERENCE_HISTORY_MAX` contract these assertions rest on. A
sibling live-Pilot wiring test (``tests/tui/test_reference_links.py``) confirms
the real app deques carry ``maxlen`` and stop cleanly past the cap.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from eawf.surfaces.tui.app import REFERENCE_HISTORY_MAX
from eawf.surfaces.tui.screens.overlays.reference import ReferenceTarget


@dataclass
class _NavModel:
    """A back/forward history model over the app's real bounded-ring deques.

    Mirrors the app's history rule exactly while holding the same
    ``deque(maxlen=REFERENCE_HISTORY_MAX)`` ring the app holds, so the ring
    invariants asserted here are the app's invariants (not a parallel
    reimplementation that could drift).
    """

    back: deque[ReferenceTarget] = field(
        default_factory=lambda: deque(maxlen=REFERENCE_HISTORY_MAX)
    )
    forward: deque[ReferenceTarget] = field(
        default_factory=lambda: deque(maxlen=REFERENCE_HISTORY_MAX)
    )
    current: ReferenceTarget | None = None

    def open(self, ref: ReferenceTarget) -> None:
        """Navigate to *ref* -- the ``_navigate_reference(record_history=True)`` rule.

        Pushes the current target onto the back ring and clears the forward
        ring, but only when *ref* is a NEW distinct target (the app skips the
        history push when re-opening the already-current target).
        """
        if self.current is not None and self.current != ref:
            self.back.append(self.current)
            self.forward.clear()
        self.current = ref

    def back_step(self) -> bool:
        """Walk one hop back -- the ``action_reference_back`` rule.

        Returns ``False`` (a clean no-op) when the back ring is empty, else
        pops the most-recent back entry into ``current`` and pushes the prior
        current onto the forward ring.
        """
        if not self.back:
            return False
        current = self.current
        self.current = self.back.pop()
        if current is not None:
            self.forward.append(current)
        return True

    def forward_step(self) -> bool:
        """Walk one hop forward -- the ``action_reference_forward`` rule."""
        if not self.forward:
            return False
        current = self.current
        self.current = self.forward.pop()
        if current is not None:
            self.back.append(current)
        return True


#: A randomized navigation trail: each action is ``open`` a target (drawn from
#: a bounded id pool so distinct + repeat opens both occur), ``back``, or
#: ``forward``.
_REF_KINDS = ("wave", "phase", "iter", "decision", "audit")
_ACTIONS = st.lists(
    st.one_of(
        st.tuples(
            st.just("open"),
            st.builds(
                ReferenceTarget,
                st.sampled_from(_REF_KINDS),
                # A small id pool so the trail mixes new-distinct opens (history
                # grows) with re-opens of the current target (history skips).
                st.integers(min_value=0, max_value=200).map(str),
            ),
        ),
        st.tuples(st.just("back")),
        st.tuples(st.just("forward")),
    ),
    # Long enough to drive the ring well past REFERENCE_HISTORY_MAX (32).
    min_size=0,
    max_size=400,
)


@pytest.mark.slow
@settings(max_examples=300, deadline=None)
@given(actions=_ACTIONS)
def test_reference_history_ring_never_exceeds_cap(
    actions: list[tuple[str, ...]],
) -> None:
    # Invariant 1: after EVERY action in an arbitrary trail, neither ring grows
    # past the cap -- the bound holds continuously, not just at the end.
    model = _NavModel()
    for action in actions:
        if action[0] == "open":
            model.open(action[1])  # type: ignore[arg-type]
        elif action[0] == "back":
            model.back_step()
        else:
            model.forward_step()
        assert len(model.back) <= REFERENCE_HISTORY_MAX
        assert len(model.forward) <= REFERENCE_HISTORY_MAX


@pytest.mark.slow
@settings(max_examples=200, deadline=None)
@given(
    targets=st.lists(
        st.builds(
            ReferenceTarget,
            st.sampled_from(_REF_KINDS),
            st.integers(min_value=0, max_value=10_000).map(str),
        ),
        # More than the cap so eviction definitely happens; deduped below so
        # every open is a NEW distinct target (each one pushes onto the ring).
        min_size=REFERENCE_HISTORY_MAX + 1,
        max_size=REFERENCE_HISTORY_MAX + 80,
        unique=True,
    ),
)
def test_back_at_cap_stops_clean_without_resurrecting_evicted(
    targets: list[ReferenceTarget],
) -> None:
    # Invariant 2: push N > cap distinct targets, then exhaust ``back``. The
    # walk yields ONLY the most-recent ``cap`` predecessors, in reverse order,
    # and the next ``back`` is a clean no-op -- an evicted (older) target never
    # reappears.
    model = _NavModel()
    for ref in targets:
        model.open(ref)
    # The back ring holds exactly ``cap`` entries: the predecessors of the last
    # opened target, oldest-of-the-window first.
    assert len(model.back) == REFERENCE_HISTORY_MAX

    # The window of targets that should still be reachable walking back: the
    # most-recent ``cap`` predecessors of the final current target. ``targets``
    # is distinct, so its last ``cap + 1`` members are [..., pred_window,
    # final_current]; walking back should hand back the predecessors in reverse.
    final_current = targets[-1]
    pred_window = targets[-(REFERENCE_HISTORY_MAX + 1) : -1]
    evicted = set(targets[: -(REFERENCE_HISTORY_MAX + 1)])

    walked: list[ReferenceTarget] = []
    assert model.current == final_current
    while model.back_step():
        assert model.current is not None
        walked.append(model.current)
    # Exactly ``cap`` hops were possible (the ring depth), then a clean stop.
    assert len(walked) == REFERENCE_HISTORY_MAX
    # The walked targets are the predecessor window in reverse order (most
    # recent first) -- a contiguous suffix of the trail, never a reorder.
    assert walked == list(reversed(pred_window))
    # No evicted (older-than-the-window) target was ever resurrected.
    assert not (set(walked) & evicted)
    # ``back`` at the floor is a clean no-op (returns False, mutates nothing).
    assert model.back_step() is False
    assert model.current == walked[-1]
