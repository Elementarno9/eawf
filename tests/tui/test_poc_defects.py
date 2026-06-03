"""Direct tests for the W10 flag-gated planted PoC defects.

These tests confirm -- WITHOUT going through the W11 jury -- that each of
the three planted defects is genuinely LIVE under the
:data:`~eawf.surfaces.tui.poc_defects.POC_DEFECTS_ENV` build flag and
ABSENT (the real, correct behaviour) without it. The flag is the contract
the jury PoC rests on: arm it and the surface misbehaves in three
specific, probe-visible ways; leave it unset and the surface is
byte-identical to the un-instrumented TUI.

The three defects and their direct (non-jury) probes:

* **dead-click** -- ``app.poc_dead_click()`` RESOLVES but moves no
  observable signal (the W09 behaviour probe classifies it ``no_op``); with
  the flag unset the action does not resolve at all (``unresolved`` -- no
  live handler).
* **stale-feed** -- after a fresh ``on_state`` delivery carrying a new
  attention signal the band's :meth:`AttentionFeed.items` does NOT change
  under the flag (stale), but DOES refresh without it.
* **hard near-miss** -- the breadcrumb ``code`` segment renders as PLAIN
  (no ``[@click=`` markup) under the flag yet its
  ``app.switch_mode('home')`` action still RESOLVES (de-linked look, live
  behaviour); without the flag the segment carries the ``[@click=`` link.

Each test monkeypatches the env var (set + delete) so the default-OFF path
is asserted alongside the armed-ON path, keeping the cases isolated.
Determinism follows the Pilot-worker rule: every body drains workers via
:func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import State, Wave
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.poc_defects import POC_DEFECTS_ENV, poc_defects_enabled
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.snapshot.behaviour_probe import (
    ProbeStatus,
    record_behaviour_transcript,
)
from eawf.surfaces.tui.widgets.attention_feed import AttentionFeed
from eawf.surfaces.tui.widgets.header import build_breadcrumb

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_SIZE = (120, 40)
_COMMIT = "abc1234"

#: The dead-click action string the breadcrumb / probe drives.
_DEAD_CLICK_ACTION = "app.poc_dead_click()"
#: The near-miss segment's underlying action -- resolvable whether or not
#: the segment renders as a link.
_HOME_ACTION = "app.switch_mode('home')"


def _base_state() -> State:
    """Load the active-wave fixture (1 in-progress wave; empty feed)."""
    return State.model_validate_json(_PHASE_ITER_WAVE.read_text(encoding="utf-8"))


def _failed_wave_state() -> State:
    """Return the fixture state with one FAILED wave (a feed item appears)."""
    failed = Wave(
        id="P01-I01-W09",
        iter_id="P01-I01",
        title="Wave P01-I01-W09",
        status=WaveStatus.FAILED,
        deps=[],
        opened_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return _base_state().model_copy(update={"waves": {"P01-I01-W09": failed}})


# --------------------------------------------------------------------------
# Flag helper -- default OFF, the load-bearing contract
# --------------------------------------------------------------------------


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default (un-instrumented) surface: with the env var absent the flag
    # reads OFF, so none of the three defects is armed.
    monkeypatch.delenv(POC_DEFECTS_ENV, raising=False)
    assert poc_defects_enabled() is False


def test_flag_armed_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(POC_DEFECTS_ENV, "1")
    assert poc_defects_enabled() is True


def test_flag_empty_value_reads_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty value is honoured as OFF -- only a non-empty value arms it.
    monkeypatch.setenv(POC_DEFECTS_ENV, "")
    assert poc_defects_enabled() is False


# --------------------------------------------------------------------------
# Defect (a) -- dead-click: resolves-but-inert under the flag, absent without
# --------------------------------------------------------------------------


def _probe_dead_click() -> ProbeStatus:
    async def body() -> ProbeStatus:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=_SIZE) as pilot:
            transcript = await record_behaviour_transcript(
                pilot, [_DEAD_CLICK_ACTION], source_commit=_COMMIT
            )
        return transcript.outcomes[0].status

    return asyncio.run(body())


def test_dead_click_is_live_no_op_under_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Armed: the action resolves (run_action truthy) but moves no observable
    # signal -- the resolved-but-inert dead-click the probe marks no_op.
    monkeypatch.setenv(POC_DEFECTS_ENV, "1")
    assert _probe_dead_click() is ProbeStatus.NO_OP


def test_dead_click_is_absent_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default: the action raises SkipAction so it never resolves -- the
    # honest "no live handler" shape (unresolved), i.e. effectively absent.
    monkeypatch.delenv(POC_DEFECTS_ENV, raising=False)
    assert _probe_dead_click() is ProbeStatus.UNRESOLVED


def test_dead_click_run_action_return_flips_with_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same action string resolves under the flag and does not without it
    # -- a direct run_action assertion complementing the probe classification.
    async def body() -> tuple[bool, bool]:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            monkeypatch.setenv(POC_DEFECTS_ENV, "1")
            resolved_on = await app.run_action(_DEAD_CLICK_ACTION)
            monkeypatch.delenv(POC_DEFECTS_ENV, raising=False)
            resolved_off = await app.run_action(_DEAD_CLICK_ACTION)
        return resolved_on, resolved_off

    resolved_on, resolved_off = asyncio.run(body())
    assert resolved_on is True
    assert resolved_off is False


# --------------------------------------------------------------------------
# Defect (b) -- stale-feed: no refresh on a new state push under the flag
# --------------------------------------------------------------------------


def _feed_count_after_push(*, armed: bool, monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    """Mount the band on an empty feed, push a failed-wave state, return counts.

    Returns ``(before, after)`` -- the band's item count after the initial
    settle (empty feed) and after a fresh ``on_state`` delivery carrying a
    failed wave. The flag is set/cleared per *armed* before the push.
    """

    async def body() -> tuple[int, int]:
        if armed:
            monkeypatch.setenv(POC_DEFECTS_ENV, "1")
        else:
            monkeypatch.delenv(POC_DEFECTS_ENV, raising=False)
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            feed = app.screen.query_one(AttentionFeed)
            before = len(feed.items())
            # A real daemon-style delivery of a fresh revision -- the same hook
            # the binder calls; it reassigns app.state, firing the band's
            # state watcher (which the flag gates).
            await app._on_state(_failed_wave_state())
            await settle_screen(pilot)
            after = len(feed.items())
        return before, after

    return asyncio.run(body())


def test_feed_does_not_refresh_under_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Armed: the band ignores the fresh on_state delivery -- the feed stays
    # empty even though the new state carries a failed-wave attention item.
    before, after = _feed_count_after_push(armed=True, monkeypatch=monkeypatch)
    assert before == 0
    assert after == 0


def test_feed_refreshes_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default: the band rebuilds on the fresh delivery, so the failed-wave
    # item appears -- the real, correct behaviour.
    before, after = _feed_count_after_push(armed=False, monkeypatch=monkeypatch)
    assert before == 0
    assert after == 1


# --------------------------------------------------------------------------
# Defect (c) -- hard near-miss: code segment de-linked but action still resolves
# --------------------------------------------------------------------------


#: The fully-wrapped code (project) segment when it is a live link. The
#: project code in the fixture is ``QR``; the trailing mode segment also
#: links to ``app.switch_mode('home')``, so the near-miss is asserted
#: against this code-segment-specific markup, not the bare action string.
_CODE_LINK_MARKUP = f"[@click={_HOME_ACTION}]QR[/]"


def test_near_miss_code_segment_is_plain_under_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Armed: the clickable breadcrumb renders the code (project) segment as
    # PLAIN text -- the [@click=...]QR[/] wrapper is gone from the markup,
    # so the segment looks de-linked.
    monkeypatch.setenv(POC_DEFECTS_ENV, "1")
    crumb = build_breadcrumb(_base_state(), "repo", "Home", mode_name="home", clickable=True)
    assert "QR" in crumb  # the segment still renders...
    assert _CODE_LINK_MARKUP not in crumb  # ...but de-linked (looks right)


def test_near_miss_code_segment_is_linked_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default: the code segment is a genuine [@click=app.switch_mode('home')]
    # link -- the real, correct behaviour.
    monkeypatch.delenv(POC_DEFECTS_ENV, raising=False)
    crumb = build_breadcrumb(_base_state(), "repo", "Home", mode_name="home", clickable=True)
    assert _CODE_LINK_MARKUP in crumb


def test_near_miss_action_still_resolves_under_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # The subtle part: even with the segment de-linked, the underlying
    # app.switch_mode('home') action STILL resolves -- looks de-linked,
    # behaves live (the regression a golden frame cannot see).
    async def body() -> bool:
        monkeypatch.setenv(POC_DEFECTS_ENV, "1")
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            # Move off home first so the home switch is a real transition.
            await app.run_action("app.switch_mode('doctor')")
            await settle_screen(pilot)
            resolved = await app.run_action(_HOME_ACTION)
            await settle_screen(pilot)
            on_home = app.current_mode == "home"
        return resolved and on_home

    assert asyncio.run(body()) is True


# --------------------------------------------------------------------------
# Flag-off path is byte-unchanged across the three defect sites at once
# --------------------------------------------------------------------------


def test_all_defects_inert_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # One body asserting the default surface is the real one on every site:
    # the dead-click is unresolved, the feed refreshes, the code segment links.
    monkeypatch.delenv(POC_DEFECTS_ENV, raising=False)

    async def body() -> tuple[bool, int, bool]:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            feed = app.screen.query_one(AttentionFeed)
            dead_click_resolved = await app.run_action(_DEAD_CLICK_ACTION)
            await app._on_state(_failed_wave_state())
            await settle_screen(pilot)
            feed_count = len(feed.items())
        crumb = build_breadcrumb(app.state, "repo", "Home", mode_name="home", clickable=True)
        code_linked = _CODE_LINK_MARKUP in crumb
        return dead_click_resolved, feed_count, code_linked

    dead_click_resolved, feed_count, code_linked = asyncio.run(body())
    assert dead_click_resolved is False  # dead-click absent
    assert feed_count == 1  # feed refreshed
    assert code_linked is True  # code segment linked
