"""Tests for the ``/find`` fuzzy ID + title search verb (P26-I02-W03).

Two layers: the pure :func:`rank_find_hits` ranker (waves + backlog pooled,
scored by id + title, best first) without mounting Textual, and a
Pilot-driven check that the verb drills into the top hit's
:class:`~eawf.surfaces.tui.screens.overlays.detail.DetailModal` through the App's
modal-cap-aware push path.

The Pilot fixture writes a tmp ``state.json`` that carries both the
``03`` fixture's real wave and an injected backlog item, since the
shipped repo fixtures populate one or the other but not both — ``/find``
ranks across the union.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.models import State
from eawf.surfaces.tui.palette.verbs import _handle_find, rank_find_hits
from eawf.surfaces.tui.screens.overlays.detail import DetailModal

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

#: The real wave shipped in the ``03`` fixture (id + title).
_WAVE_ID = "P01-I01-W01"
_WAVE_TITLE = "Implement validate"


def _backlog_item(item_id: str, title: str) -> dict[str, object]:
    """Return a minimal valid backlog-item payload for state injection."""
    return {
        "id": item_id,
        "scope_id": "EAWF",
        "title": title,
        "priority": "P1",
        "status": "open",
        "created_at": "2026-05-08T00:00:00Z",
        "closed_at": None,
        "resolution": None,
        "commit": None,
    }


def _state_with_waves_and_backlog(backlog: dict[str, dict[str, object]]) -> State:
    """Load the ``03`` fixture (one wave) and inject *backlog*."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["backlog"] = backlog
    return State.model_validate(payload)


def _state_file_with_backlog(tmp_path: Path, backlog: dict[str, dict[str, object]]) -> Path:
    """Write a tmp ``state.json`` from the ``03`` fixture plus *backlog*."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["backlog"] = backlog
    state_file = tmp_path / "state.json"
    state_file.write_bytes(orjson.dumps(payload))
    return state_file


# --------------------------------------------------------------------------
# rank_find_hits — pooled wave + backlog ranking
# --------------------------------------------------------------------------


def test_rank_find_hits_ranks_wave_by_id() -> None:
    state = _state_with_waves_and_backlog({})
    hits = rank_find_hits(state, "W01")
    assert hits[0] == _WAVE_ID


def test_rank_find_hits_ranks_backlog_by_title() -> None:
    state = _state_with_waves_and_backlog(
        {"BL-042": _backlog_item("BL-042", "Wire metrics dashboard")}
    )
    # "metrics" subsequence-matches the backlog title but not the wave.
    hits = rank_find_hits(state, "metrics")
    assert hits == ["BL-042"]


def test_rank_find_hits_pools_waves_and_backlog() -> None:
    state = _state_with_waves_and_backlog({"BL-001": _backlog_item("BL-001", "Refactor loader")})
    hits = rank_find_hits(state, "")
    # An empty query returns nothing to drill into.
    assert hits == []
    # A query that matches both surfaces both.
    state2 = _state_with_waves_and_backlog({_WAVE_ID.lower(): _backlog_item("bl-i", "iter board")})
    both = rank_find_hits(state2, "i")
    assert _WAVE_ID in both
    assert "bl-i" in both


def test_rank_find_hits_best_match_first_by_score() -> None:
    state = _state_with_waves_and_backlog(
        {
            "BL-1": _backlog_item("BL-1", "metrics"),
            "BL-2": _backlog_item("BL-2", "m-e-t-r-i-c-s scattered"),
        }
    )
    hits = rank_find_hits(state, "metrics")
    # The contiguous title scores lower (better) than the scattered one.
    assert hits.index("BL-1") < hits.index("BL-2")


def test_rank_find_hits_scores_id_and_title_takes_better() -> None:
    # B's title matches "loader" tightly (prefix -> score 0) while its id
    # does not match at all; A matches "loader" only loosely via a padded
    # id and not via its title. The better-of-id-or-title rule must rank B
    # ahead of A, proving the title score is consulted (not just the id).
    state = _state_with_waves_and_backlog(
        {
            "AA-padxloaderpad": _backlog_item("AA-padxloaderpad", "no field hit here"),
            "BB-nohit": _backlog_item("BB-nohit", "loader queue"),
        }
    )
    hits = rank_find_hits(state, "loader")
    assert hits[0] == "BB-nohit"  # title "loader queue" -> score 0
    assert hits.index("BB-nohit") < hits.index("AA-padxloaderpad")


def test_rank_find_hits_no_match_returns_empty() -> None:
    state = _state_with_waves_and_backlog({"BL-1": _backlog_item("BL-1", "Refactor loader")})
    assert rank_find_hits(state, "zzzzzz") == []


def test_rank_find_hits_none_state_returns_empty() -> None:
    assert rank_find_hits(None, "anything") == []


def test_rank_find_hits_empty_query_returns_empty() -> None:
    state = _state_with_waves_and_backlog({})
    assert rank_find_hits(state, "   ") == []


def test_rank_find_hits_handles_state_with_no_backlog() -> None:
    # 03 fixture has backlog == None until injected; the ranker must still
    # rank the waves rather than crash on the missing backlog table.
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    state = State.model_validate(payload)
    assert state.backlog is None
    assert rank_find_hits(state, "W01") == [_WAVE_ID]


# --------------------------------------------------------------------------
# /find verb — drill into the top hit's DetailModal (Pilot)
# --------------------------------------------------------------------------


def test_handle_find_opens_detail_modal_for_top_backlog_hit(tmp_path: Path) -> None:
    state_file = _state_file_with_backlog(
        tmp_path, {"BL-042": _backlog_item("BL-042", "Wire metrics dashboard")}
    )

    async def body() -> None:
        from eawf.surfaces.tui.app import EaApp

        app = EaApp(scope="repo", state_path=state_file)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _handle_find(app, "metrics")
            await pilot.pause()
            assert isinstance(app.screen, DetailModal)
            # The opened card is the picked backlog item, not the wave.
            assert app.screen._card.title == "backlog BL-042"

    asyncio.run(body())


def test_handle_find_opens_detail_modal_for_top_wave_hit(tmp_path: Path) -> None:
    state_file = _state_file_with_backlog(
        tmp_path, {"BL-001": _backlog_item("BL-001", "unrelated")}
    )

    async def body() -> None:
        from eawf.surfaces.tui.app import EaApp

        app = EaApp(scope="repo", state_path=state_file)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _handle_find(app, _WAVE_ID)
            await pilot.pause()
            assert isinstance(app.screen, DetailModal)
            assert app.screen._card.title == f"wave {_WAVE_ID}"

    asyncio.run(body())


def test_handle_find_empty_query_opens_no_modal(tmp_path: Path) -> None:
    state_file = _state_file_with_backlog(tmp_path, {})

    async def body() -> None:
        from eawf.surfaces.tui.app import EaApp

        notices: list[tuple[str, str | None]] = []
        app = EaApp(scope="repo", state_path=state_file)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.notify = lambda message, *_a, **kw: notices.append(  # type: ignore[method-assign]
                (message, kw.get("severity"))
            )
            _handle_find(app, "   ")
            await pilot.pause()
            assert app.modal_depth() == 0
            assert notices and notices[-1][1] == "warning"

    asyncio.run(body())


def test_handle_find_no_match_opens_no_modal(tmp_path: Path) -> None:
    state_file = _state_file_with_backlog(
        tmp_path, {"BL-001": _backlog_item("BL-001", "unrelated")}
    )

    async def body() -> None:
        from eawf.surfaces.tui.app import EaApp

        notices: list[tuple[str, str | None]] = []
        app = EaApp(scope="repo", state_path=state_file)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.notify = lambda message, *_a, **kw: notices.append(  # type: ignore[method-assign]
                (message, kw.get("severity"))
            )
            _handle_find(app, "zzzzzz")
            await pilot.pause()
            assert app.modal_depth() == 0
            assert notices and "zzzzzz" in notices[-1][0]

    asyncio.run(body())


def test_handle_find_routes_through_modal_cap(tmp_path: Path) -> None:
    state_file = _state_file_with_backlog(
        tmp_path, {"BL-042": _backlog_item("BL-042", "Wire metrics dashboard")}
    )

    async def body() -> None:
        from eawf.surfaces.tui.app import EaApp
        from eawf.surfaces.tui.screens.overlays.metrics import MetricsModal

        app = EaApp(scope="repo", state_path=state_file)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for _ in range(EaApp.MAX_MODAL_DEPTH):
                app.push_modal(MetricsModal())
                await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH
            _handle_find(app, "metrics")
            await pilot.pause()
            # Cap holds — the drill-in push is rejected, not stacked.
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH

    asyncio.run(body())


def test_handle_find_carries_entity_id_for_dedup(tmp_path: Path) -> None:
    """The /find drill tags its DetailModal with the picked id (the dedup key)."""
    state_file = _state_file_with_backlog(
        tmp_path, {"BL-001": _backlog_item("BL-001", "unrelated")}
    )

    async def body() -> None:
        from eawf.surfaces.tui.app import EaApp

        app = EaApp(scope="repo", state_path=state_file)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Drain the GitPane git-probe worker (a real ``git`` subprocess
            # off the event loop) spawned on mount before driving /find. Left
            # in flight it keeps the screen's message count non-zero, so a
            # follow-on ``pilot.pause`` can hit Textual's 30 s
            # ``WaitForScreenTimeout`` under a saturated ``-n auto`` matrix.
            await app.workers.wait_for_complete()
            _handle_find(app, _WAVE_ID)
            await pilot.pause()
            assert isinstance(app.screen, DetailModal)
            # The card carries the entity id so the chokepoint dedups a re-/find.
            assert app.screen.entity_id == _WAVE_ID
            assert app.screen.dedupe_key == _WAVE_ID

    asyncio.run(body())


def test_handle_find_refind_same_top_entity_is_noop(tmp_path: Path) -> None:
    """Re-/find of the entity already on the modal top stacks no duplicate.

    The criterion's choose-P29-in-modal case: with a DetailModal already on
    top, choosing the SAME entity again (a re-fired ``/find``) routes through
    the ``push_modal`` chokepoint, which no-ops the duplicate by entity key --
    the depth is unchanged and no extra card mounts.
    """
    state_file = _state_file_with_backlog(
        tmp_path, {"BL-001": _backlog_item("BL-001", "unrelated")}
    )

    async def body() -> None:
        from eawf.surfaces.tui.app import EaApp

        app = EaApp(scope="repo", state_path=state_file)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _handle_find(app, _WAVE_ID)
            await pilot.pause()
            assert app.modal_depth() == 1
            top = app._top_modal()
            assert isinstance(top, DetailModal)
            # Re-/find the same wave -> deduped at the chokepoint (no +1).
            _handle_find(app, _WAVE_ID)
            await pilot.pause()
            assert app.modal_depth() == 1
            assert app._top_modal() is top

    asyncio.run(body())


def test_handle_find_different_entity_on_top_still_stacks(tmp_path: Path) -> None:
    """A /find for a DIFFERENT entity than the modal top stacks a new card."""
    state_file = _state_file_with_backlog(
        tmp_path, {"BL-042": _backlog_item("BL-042", "Wire metrics dashboard")}
    )

    async def body() -> None:
        from eawf.surfaces.tui.app import EaApp

        app = EaApp(scope="repo", state_path=state_file)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _handle_find(app, _WAVE_ID)
            await pilot.pause()
            assert app.modal_depth() == 1
            # The wave is on top; /find for the backlog item is a different
            # entity, so it stacks (the dedup is entity-scoped + top-only).
            _handle_find(app, "metrics")
            await pilot.pause()
            assert app.modal_depth() == 2
            top = app._top_modal()
            assert isinstance(top, DetailModal)
            assert top.entity_id == "BL-042"

    asyncio.run(body())


@pytest.mark.parametrize("query", ["W01", "metrics", "implement"])
def test_handle_find_top_hit_card_matches_ranker(tmp_path: Path, query: str) -> None:
    """The opened card always matches ``rank_find_hits``'s top id."""
    backlog = {"BL-042": _backlog_item("BL-042", "Wire metrics dashboard")}
    state_file = _state_file_with_backlog(tmp_path, backlog)
    expected = rank_find_hits(_state_with_waves_and_backlog(backlog), query)[0]

    async def body() -> None:
        from eawf.surfaces.tui.app import EaApp

        app = EaApp(scope="repo", state_path=state_file)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _handle_find(app, query)
            await pilot.pause()
            assert isinstance(app.screen, DetailModal)
            assert expected in app.screen._card.title

    asyncio.run(body())
