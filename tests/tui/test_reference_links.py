"""Tests for TUI clickable references, ``/goto``, and ref nav."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

from textual.widgets import Static
from textual.widgets._toast import Toast

from eawf.kernel.state.models import State
from eawf.surfaces.tui.app import REFERENCE_HISTORY_MAX, EaApp
from eawf.surfaces.tui.modes.brief_viewer import BriefViewerScreen
from eawf.surfaces.tui.palette.verbs import _handle_goto, rank_goto_refs
from eawf.surfaces.tui.screens.overlays.detail import DetailCard, DetailModal
from eawf.surfaces.tui.screens.overlays.reference import (
    ReferenceModal,
    resolve_reference,
    tooltip_for_text,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_WAVE_ID = "P01-I01-W01"


def _state() -> State:
    return State.model_validate_json(_PHASE_ITER_WAVE.read_text())


def test_resolve_reference_returns_card_for_each_catalog_kind() -> None:
    state = _state()
    targets = {
        "repo": "QR",
        "project": "QR",
        "phase": "P01",
        "iter": "P01-I01",
        "wave": _WAVE_ID,
        "hypothesis": "H01-01",
        "decision": "DEC-001",
        "audit": "AUD-001",
        "artifact": "ART-001",
        "memory": "MEM-001",
        "report": "REPORT-001",
        "event": "EVENT-001",
        "profile": "profile:engineering",
        "spec": _WAVE_ID,
    }
    for kind, target in targets.items():
        card = resolve_reference(state, kind, target)  # type: ignore[arg-type]
        assert card.kind == kind
        assert card.rows


def test_rank_goto_refs_prefers_explicit_link_wrap_match() -> None:
    hits = rank_goto_refs(_state(), _WAVE_ID)
    assert hits[0].kind == "wave"
    assert hits[0].target == _WAVE_ID


def test_rank_goto_refs_fuzzy_matches_state_title() -> None:
    hits = rank_goto_refs(_state(), "validate")
    assert any(hit.kind == "wave" and hit.target == _WAVE_ID for hit in hits)


def test_tooltip_for_text_uses_reference_preview() -> None:
    tooltip = tooltip_for_text(_state(), f"see {_WAVE_ID}")
    assert tooltip is not None
    assert f"wave {_WAVE_ID}" in tooltip


def test_detail_modal_rows_get_hover_tooltip() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.push_modal(
                DetailModal(
                    DetailCard(title="refs", rows=(("ref", f"see {_WAVE_ID}"),)),
                    state=app.state,
                )
            )
            await pilot.pause()
            row = app.screen.query_one(".detail-row", Static)
            assert row.tooltip is not None
            assert f"wave {_WAVE_ID}" in str(row.tooltip)

    asyncio.run(body())


def test_app_reference_nav_stack_back_and_forward() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.action_open_wave_ref(_WAVE_ID)
            await pilot.pause()
            assert isinstance(app.screen, ReferenceModal)
            assert app._current_reference is not None
            assert app._current_reference.target == _WAVE_ID

            app.action_open_phase_ref("P01")
            await pilot.pause()
            assert app._current_reference is not None
            assert app._current_reference.kind == "phase"
            assert [ref.target for ref in app._reference_back_stack] == [_WAVE_ID]

            app.action_reference_back()
            await pilot.pause()
            assert app._current_reference is not None
            assert app._current_reference.target == _WAVE_ID
            assert [ref.target for ref in app._reference_forward_stack] == ["P01"]

            await pilot.press("escape")
            await pilot.pause()
            app.action_reference_forward()
            await pilot.pause()
            assert app._current_reference is not None
            assert app._current_reference.kind == "phase"

    asyncio.run(body())


def test_reference_history_stacks_are_bounded_rings() -> None:
    # W24 wiring: the live app's back/forward history are bounded FIFO rings
    # (deque with maxlen == REFERENCE_HISTORY_MAX), not unbounded lists. The
    # ring math is property-tested in tests/property/test_reference_history_ring;
    # this pins that the real app carries the bound.
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app._reference_back_stack, deque)
            assert isinstance(app._reference_forward_stack, deque)
            assert app._reference_back_stack.maxlen == REFERENCE_HISTORY_MAX
            assert app._reference_forward_stack.maxlen == REFERENCE_HISTORY_MAX

    asyncio.run(body())


def test_reference_back_on_empty_history_stops_clean() -> None:
    # W24: ``back`` with no history is a clean no-op -- it notifies and mutates
    # neither ring nor the current reference (the floor-stop the ring relies on).
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # No navigation yet: both rings empty, no current reference.
            assert not app._reference_back_stack
            app.action_reference_back()
            await pilot.pause()
            assert not app._reference_back_stack
            assert not app._reference_forward_stack
            assert app._current_reference is None

    asyncio.run(body())


def test_reference_back_replaces_top_modal_in_place() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.action_open_wave_ref(_WAVE_ID)
            await pilot.pause()
            app.action_open_phase_ref("P01")
            await pilot.pause()
            app.action_open_iter_ref("P01-I01")
            await pilot.pause()
            # Three reference drills stack three modals (well under the cap).
            depth_before = app.modal_depth()
            assert depth_before == 3
            assert app._current_reference is not None
            assert app._current_reference.kind == "iter"

            # Back navigation pops the top ReferenceModal and pushes the
            # previous one, so the depth is unchanged (a replace, not a push).
            app.action_reference_back()
            await pilot.pause()
            assert app.modal_depth() == depth_before
            assert isinstance(app.screen, ReferenceModal)
            assert app._current_reference is not None
            assert app._current_reference.kind == "phase"
            assert [ref.kind for ref in app._reference_forward_stack] == ["iter"]

    asyncio.run(body())


def test_goto_handler_opens_reference_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            _handle_goto(app, _WAVE_ID)
            await pilot.pause()
            assert isinstance(app.screen, ReferenceModal)
            assert app.screen._card.title == f"wave {_WAVE_ID}"

    asyncio.run(body())


# --------------------------------------------------------------------------
# Brief viewer exempt from the modal-depth cap (P29-I09-W01)
# --------------------------------------------------------------------------


def _reference_modal(app: EaApp) -> ReferenceModal:
    """Build a ReferenceModal over the active-wave fixture for cap tests."""
    card = resolve_reference(app.state, "wave", _WAVE_ID)
    return ReferenceModal(card, state=app.state)


def test_max_modal_depth_raised_to_six() -> None:
    # The cap was raised from 3 to 6 so a brief plus five reference drills
    # fits without tripping the depth-cap toast.
    assert EaApp.MAX_MODAL_DEPTH == 6


def test_modal_depth_skips_brief_viewer_but_counts_reference_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # The brief viewer opts out of the cap, so it does not count.
            assert app.push_modal(BriefViewerScreen("# brief")) is True
            await pilot.pause()
            assert app.modal_depth() == 0
            # A reference drill on top of the brief DOES count.
            assert app.push_modal(_reference_modal(app)) is True
            await pilot.pause()
            assert app.modal_depth() == 1

    asyncio.run(body())


def test_brief_viewer_class_opts_out_of_depth_count() -> None:
    # The exemption is a class-level opt-out, not a per-instance flag; the
    # ReferenceModal (the drill overlay) keeps the default and counts.
    assert BriefViewerScreen.counts_toward_depth is False
    assert getattr(ReferenceModal, "counts_toward_depth", True) is True


def test_brief_plus_five_reference_drills_never_hits_cap() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # The criterion scenario: open a brief, then drill five references
            # off it. The brief is exempt, so the five drills sit at depth 5 --
            # one below the cap -- and every push succeeds with no cap toast.
            assert app.push_modal(BriefViewerScreen("# brief")) is True
            await pilot.pause()
            results = []
            for _ in range(5):
                results.append(app.push_modal(_reference_modal(app)))
                await pilot.pause()
            assert results == [True, True, True, True, True]
            assert app.modal_depth() == 5
            # No depth-cap toast mounted -- the brief never counted against it.
            assert not list(app.query(Toast))

    asyncio.run(body())


def test_reference_drills_still_cap_without_brief_exemption() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # Counting overlays still cap at six: six reference drills fill the
            # stack and the seventh is rejected (the brief exemption does not
            # widen the cap for counting overlays, it just excludes the brief).
            for _ in range(EaApp.MAX_MODAL_DEPTH):
                assert app.push_modal(_reference_modal(app)) is True
                await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH
            assert app.push_modal(_reference_modal(app)) is False
            await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH

    asyncio.run(body())
