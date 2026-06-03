"""Cold-mount Pilot gates for the Home attention band (P29-I08-W19).

These tests pin the T2 contract from
``.ea/local/research/2026-06-03-i08-uiux-validation-specs.md`` -- the
attention feed is populated on cold launch with no flash-of-stale-empty,
and a late ``None -> State`` push populates within one delivered
``on_state``. They are the deterministic (load-bearing) gates the brief
names; the perceived-flash judgement over a provenance-pinned cast is the
separate jury residual.

The flash the W18 fix closes is transient: under the pre-W18 code the
band's :meth:`~eawf.surfaces.tui.widgets.attention_feed.AttentionFeed.on_mount`
ran while ``app.state`` was still ``None`` (it bound only after the async
``await connect()``), so the band's FIRST DOM rebuild composed the
honest-empty Static, and a SECOND rebuild re-mounted the real rows once
``connect()`` delivered state. A plain capture at pilot entry cannot see
that flash because ``run_test`` awaits ``on_mount`` (and thus
``connect()``) to completion before yielding -- by then the second
rebuild has already populated the band in both the regressed and the
fixed build. So test (a) instruments the band's FIRST DOM rebuild pass
(via a recording subclass spliced in at the scope ``attention_band``
seam, which resolves the ``AttentionFeed`` name at compose time) and
asserts that first pass already mounted populated rows with the
empty-class Static ABSENT -- exactly the frame that was the empty flash
before the fix. Determinism follows the Pilot-worker rule
(:meth:`textual.worker.WorkerManager.wait_for_complete` drains the async
DOM-rebuild worker); the assertion reads the recorded first-pass DOM, so
it observes the flash window rather than the settled steady state.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.widgets import Static

from eawf.kernel.state.models import State
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.attention import EMPTY_FEED_TEXT
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets.attention_feed import AttentionFeed

#: A populated repo fixture: one OPEN high-severity incident, which the
#: attention reducer surfaces as a single ranked feed item. Chosen over the
#: base active-wave fixture (which is legitimately empty) so the cold-mount
#: frame has a row to assert -- the flash would otherwise be invisible.
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_POPULATED = _FIXTURES / "08-incident-open.json"
_SIZE = (120, 40)

#: A substring of the seeded incident row so a frame assertion can confirm
#: the populated row text (not just a row count) made the first frame.
_INCIDENT_TITLE_FRAGMENT = "Validate command exits 0 on invariant violations"


class _FirstDomRecordingFeed(AttentionFeed):
    """Band that records its FIRST async DOM-rebuild pass for the cold-mount gate.

    The cold-mount flash is the band's first rebuild composing empty (state
    not yet bound). This subclass captures, on the first
    :meth:`_rebuild_dom` pass only, the mounted child classes + the
    rendered row text, so a test can assert the first frame the band ever
    showed was populated rather than the honest-empty Static.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dom_pass_count = 0
        self.first_pass_row_count: int | None = None
        self.first_pass_empty_count: int | None = None
        self.first_pass_text: str = ""

    async def _rebuild_dom(self) -> None:
        await super()._rebuild_dom()
        self.dom_pass_count += 1
        if self.first_pass_row_count is None:
            rows = self.query(".attention-row")
            empties = self.query(".attention-empty")
            self.first_pass_row_count = len(rows)
            self.first_pass_empty_count = len(empties)
            self.first_pass_text = " ".join(str(row.render()) for row in rows.results(Static))


class _RebuildCountingFeed(AttentionFeed):
    """Band that counts :meth:`_rebuild` calls for the late-push gate."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rebuild_calls = 0

    def _rebuild(self) -> None:
        self.rebuild_calls += 1
        super()._rebuild()


def _splice_feed(monkeypatch: Any, feed_cls: type[AttentionFeed]) -> None:
    """Bind *feed_cls* into the scope ``attention_band`` compose seam.

    ``attention_band`` references the module-level ``AttentionFeed`` name at
    compose time, so rebinding the name on the scopes module swaps the band
    every scope screen leads its body with -- the one seam the cold-mount
    instrumentation needs, without touching production code.
    """
    import eawf.surfaces.tui.scopes as scopes_mod

    monkeypatch.setattr(scopes_mod, "AttentionFeed", feed_cls)


# --------------------------------------------------------------------------
# (a) Cold-mount first frame: populated rows, empty-class Static ABSENT
# --------------------------------------------------------------------------


def test_cold_mount_first_frame_is_populated(monkeypatch: Any) -> None:
    # The FIRST DOM rebuild the band runs at cold launch must already carry
    # the populated rows -- never the honest-empty Static. Pre-W18 the band
    # composed while app.state was None, so its first pass mounted the empty
    # Static (the flash) and only a second pass re-mounted rows after
    # connect(); the synchronous bind closes that window.
    _splice_feed(monkeypatch, _FirstDomRecordingFeed)

    async def body() -> _FirstDomRecordingFeed:
        app = EaApp(scope="repo", state_path=_POPULATED)
        async with app.run_test(size=_SIZE) as pilot:
            # Drain the DOM-rebuild worker so the first pass is recorded;
            # deliberately NO settle_screen first -- a multi-cycle settle
            # would mask the transient first-pass content the gate inspects.
            await pilot.app.workers.wait_for_complete()
            return app.screen.query_one(_FirstDomRecordingFeed)

    feed = asyncio.run(body())
    # The first DOM pass mounted populated rows...
    assert feed.first_pass_row_count == 1
    # ...and the honest-empty Static is ABSENT from that first frame.
    assert feed.first_pass_empty_count == 0
    # The populated row text -- not the empty placeholder -- made the frame.
    assert _INCIDENT_TITLE_FRAGMENT in feed.first_pass_text
    assert EMPTY_FEED_TEXT not in feed.first_pass_text


def test_cold_mount_no_empty_then_populate_churn(monkeypatch: Any) -> None:
    # The fix collapses the two-pass flash into one: the band rebuilds its
    # DOM exactly once at cold launch (the synchronous bind + the binder's
    # re-delivery of the same state coalesce), so there is no empty-then-row
    # churn. Pre-W18 this recorded two passes (empty, then populated).
    _splice_feed(monkeypatch, _FirstDomRecordingFeed)

    async def body() -> int:
        app = EaApp(scope="repo", state_path=_POPULATED)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.app.workers.wait_for_complete()
            return app.screen.query_one(_FirstDomRecordingFeed).dom_pass_count

    assert asyncio.run(body()) == 1


# --------------------------------------------------------------------------
# (b) Late None -> State push populates within ONE delivered on_state
# --------------------------------------------------------------------------


def test_late_push_populates_within_one_on_state(monkeypatch: Any) -> None:
    # A band that cold-launched against no on-disk state (the fresh-workspace
    # / daemon-cold-spawn window) shows the honest-empty Static; a single
    # late on_state delivery carrying populated state must populate the feed
    # and fire exactly one _rebuild for that delivery (no double rebuild).
    _splice_feed(monkeypatch, _RebuildCountingFeed)
    populated = State.model_validate_json(_POPULATED.read_text(encoding="utf-8"))

    async def body() -> tuple[int, int, int, int]:
        # A missing state path: load_state returns None, so the cold mount
        # binds no state and the band starts honest-empty.
        missing = Path("does-not-exist") / ".ea" / "state.json"
        app = EaApp(scope="repo", state_path=missing)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            feed = app.screen.query_one(_RebuildCountingFeed)
            empty_before = len(feed.query(".attention-empty"))
            calls_before = feed.rebuild_calls
            # The late push -- the same hook the binder calls on a fresh
            # revision; it reassigns app.state, firing the band's state watch.
            await app._on_state(populated)
            await settle_screen(pilot)
            calls_after = feed.rebuild_calls
            rows_after = len(feed.query(".attention-row"))
            empty_after = len(feed.query(".attention-empty"))
        return empty_before, calls_after - calls_before, rows_after, empty_after

    empty_before, rebuild_delta, rows_after, empty_after = asyncio.run(body())
    # The band started honest-empty (no on-disk state to bind)...
    assert empty_before == 1
    # ...one on_state delivery fired exactly one _rebuild...
    assert rebuild_delta == 1
    # ...and the feed populated (the empty Static gone, the row mounted).
    assert rows_after == 1
    assert empty_after == 0
