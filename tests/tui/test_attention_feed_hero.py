"""Pilot tests for the two-state attention-feed hero (P30-I02-W08).

The Home overview band
(:class:`~eawf.surfaces.tui.widgets.attention_feed.AttentionFeed`) reskins
to a two-state hero off the merged acute source:

* **idle** -- nothing acute -- renders a calm ``$accent`` line pinning the
  literal :data:`~eawf.surfaces.tui.attention.EMPTY_FEED_TEXT` under the
  CLOSED (all-clear) sigil plus a verb-led next-action sub-line, and **no**
  warn band.
* **needs-you** -- at least one acute item -- renders a ``$warn`` band that
  NAMES each acute item with its sigil (attention triangle / failed cross)
  and shows an ``Enter to review`` affordance; the footer needs_user badge,
  fed by the same open-pause source, agrees; ``Enter`` over an acute pause
  row fires its :class:`AttentionFeed.PauseSelected` action.

The band is mounted **in isolation** under a bare themed harness App (not
the full :class:`~eawf.surfaces.tui.app.EaApp`) so these gates pin the
widget's render + select seam without booting the unrelated app graph. The
harness exposes the same hooks the production App does -- ``state``,
``_all_open_pauses``, ``_attention_now``, and ``render_mode`` -- so the
band resolves its acute source, clock, and glyph column the same way.

Glyphs are written with ``\\uXXXX`` escapes so the source stays ASCII-clean
(the unicode column: attention triangle ``\\u25b3``, failed cross
``\\u2715``, closed circle ``\\u25cf``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widgets import Static

from eawf.kernel.state.enums import Urgency
from eawf.kernel.state.models import State
from eawf.surfaces.tui.attention import (
    EMPTY_FEED_TEXT,
    IDLE_NEXT_ACTION_DEFAULT,
    AttentionItem,
    AttentionKind,
)
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets.attention_feed import AttentionFeed
from eawf.surfaces.tui.widgets.footer import format_needs_user_badge
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import OpenPause

from ._palette_harness import PaletteHarnessApp

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
#: An OPEN high-severity incident -> one acute INCIDENT attention row.
_INCIDENT = _FIXTURES / "08-incident-open.json"
#: An active wave + no incidents / questions / pauses -> honest-empty feed.
_EMPTY = _FIXTURES / "03-phase-iter-wave-active.json"
_SCOPE = "urn:eawf:v1:state:QR"
_SESSION = "urn:eawf:v1:session:cli/SES-tui"

#: A fixed reference instant so a seeded row's time-ago is deterministic.
_FIXED_NOW = datetime(2099, 1, 1, tzinfo=UTC)

#: Unicode-column glyphs the hero renders (see sigils.py); spelled with
#: escapes so the source stays ASCII-clean.
_ATTENTION_GLYPH = "\u25b3"  # attention chrome triangle
_FAILED_GLYPH = "\u2715"  # FAILED lifecycle cross
_CLOSED_GLYPH = "\u25cf"  # CLOSED lifecycle circle (idle all-clear)


def _question(text: str) -> UserQuestion:
    return UserQuestion(
        question=text,
        options=[UserQuestionOption(label="apply"), UserQuestionOption(label="cancel")],
    )


def _pause(text: str, urgency: Urgency) -> OpenPause:
    return OpenPause(
        pause_urn=f"urn:eawf:v1:event:{_SCOPE}/needs-user-{text.replace(' ', '-')}",
        scope_id=_SCOPE,
        session=_SESSION,
        question=_question(text),
        urgency=urgency,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class _Harness(PaletteHarnessApp):
    """Bare themed host that mounts an :class:`AttentionFeed` in isolation.

    Exposes the production App's band hooks so the feed resolves its acute
    source (``state`` + ``_all_open_pauses``), its time-ago clock
    (``_attention_now``), and its glyph column (``render_mode``) the same
    way -- without booting the unrelated app graph.
    """

    CSS_PATH = str(_THEME)

    render_mode: reactive[str] = reactive("unicode")

    def __init__(self, *, state: State | None, pauses: tuple[OpenPause, ...]) -> None:
        super().__init__()
        self._seed_state = state
        self._pauses = pauses

    def compose(self) -> ComposeResult:
        yield AttentionFeed(id="feed")

    def on_mount(self) -> None:
        self.query_one(AttentionFeed).state = self._seed_state

    def _all_open_pauses(self) -> list[OpenPause]:
        return list(self._pauses)

    def _attention_now(self) -> datetime:
        return _FIXED_NOW


def _load(path: Path) -> State:
    return State.model_validate_json(path.read_text(encoding="utf-8"))


def _row_text(feed: AttentionFeed) -> str:
    rows = feed.query(".attention-row")
    return " ".join(str(row.render()) for row in rows.results(Static))


# --------------------------------------------------------------------------
# idle hero -- calm all-clear, next-action sub-line, NO warn band
# --------------------------------------------------------------------------


def test_idle_hero_pins_all_clear_literal_and_next_action() -> None:
    # Zero acute items -> the calm idle render: it pins the literal
    # "nothing needs you" under the CLOSED all-clear sigil plus the verb-led
    # next-action sub-line, and renders NO warn (.attention-row) band.
    async def body() -> tuple[str, str, int]:
        app = _Harness(state=_load(_EMPTY), pauses=())
        async with app.run_test(size=(120, 12)) as pilot:
            await settle_screen(pilot)
            feed = app.query_one(AttentionFeed)
            assert feed.items() == ()
            idle = feed.query(".attention-idle")
            sub = feed.query(".attention-idle-sub")
            idle_text = str(idle.first(Static).render())
            sub_text = str(sub.first(Static).render())
            warn_rows = len(feed.query(".attention-row"))
            return idle_text, sub_text, warn_rows

    idle_text, sub_text, warn_rows = asyncio.run(body())
    # The pinned all-clear literal under the CLOSED all-clear sigil...
    assert EMPTY_FEED_TEXT in idle_text
    assert EMPTY_FEED_TEXT == "nothing needs you"
    assert _CLOSED_GLYPH in idle_text
    # ...the verb-led next-action sub-line (nothing ready -> review roadmap)...
    assert sub_text.strip() == IDLE_NEXT_ACTION_DEFAULT
    assert IDLE_NEXT_ACTION_DEFAULT == "review the roadmap"
    # ...and an idle feed renders NO warn band.
    assert warn_rows == 0


def test_idle_hero_never_renders_warn_band_even_with_advisory_ready_wave() -> None:
    # A feed carrying only an advisory ready-to-claim wave is NOT acute, so
    # the hero stays idle -- still no warn band, still the all-clear render.
    async def body() -> tuple[int, str]:
        app = _Harness(state=_load(_EMPTY), pauses=())
        async with app.run_test(size=(120, 12)) as pilot:
            await settle_screen(pilot)
            feed = app.query_one(AttentionFeed)
            # Inject the advisory item + repaint the DOM directly (the
            # internal repaint seam paints the CURRENT _items, without the
            # item fold rebuild() would run -- which recomputes off state).
            feed._items = (
                AttentionItem(
                    urgency=Urgency.NORMAL,
                    kind=AttentionKind.READY_WAVE,
                    title="P01-I01-W02 ready",
                    detail="ready to claim",
                ),
            )
            feed._request_rebuild_dom()
            await settle_screen(pilot)
            warn_rows = len(feed.query(".attention-row"))
            idle = feed.query(".attention-idle")
            idle_text = str(idle.first(Static).render()) if idle else ""
            return warn_rows, idle_text

    warn_rows, idle_text = asyncio.run(body())
    # The advisory ready wave never raises a warn band...
    assert warn_rows == 0
    # ...and the calm idle all-clear hero is what painted.
    assert EMPTY_FEED_TEXT in idle_text


# --------------------------------------------------------------------------
# needs-you hero -- warn band names each item w/ its sigil + Enter to review
# --------------------------------------------------------------------------


def test_needs_you_band_names_each_item_with_sigil_and_review_hint() -> None:
    # One HIGH pause + one OPEN incident -> two acute rows. The warn band
    # NAMES each (the pause question text + the incident id/title) with the
    # attention sigil, and each row shows "Enter to review".
    incident_state = _load(_INCIDENT)
    pause = _pause("answer me now", Urgency.HIGH)

    async def body() -> tuple[str, int, int]:
        app = _Harness(state=incident_state, pauses=(pause,))
        async with app.run_test(size=(120, 12)) as pilot:
            await settle_screen(pilot)
            feed = app.query_one(AttentionFeed)
            items = feed.items()
            kinds = {i.kind for i in items}
            text = _row_text(feed)
            idle_rows = len(feed.query(".attention-idle"))
            return text, len(items), idle_rows, kinds  # type: ignore[return-value]

    text, count, idle_rows, kinds = asyncio.run(body())  # type: ignore[misc]
    # Two acute items: the needs_user pause + the open incident.
    assert count == 2
    assert kinds == {AttentionKind.NEEDS_USER, AttentionKind.INCIDENT}
    # The band NAMES each item...
    assert "answer me now" in text  # the pause question
    assert "Validate command exits 0 on invariant violations" in text  # the incident
    # ...with its (attention triangle) sigil...
    assert _ATTENTION_GLYPH in text
    # ...and every acute row shows the review affordance.
    assert text.count("Enter to review") == 2
    # The needs-you band IS rendered (no idle all-clear line present).
    assert idle_rows == 0


def test_failed_wave_row_wears_the_failed_cross_sigil() -> None:
    # A failed-wave acute row wears the FAILED lifecycle cross (not the
    # attention triangle the other acute kinds wear).
    async def body() -> str:
        app = _Harness(state=_load(_EMPTY), pauses=())
        async with app.run_test(size=(120, 12)) as pilot:
            await settle_screen(pilot)
            feed = app.query_one(AttentionFeed)
            # Inject the failed-wave acute item + repaint directly (the
            # repaint seam paints CURRENT _items, skipping the state-driven
            # item fold rebuild() would run).
            feed._items = (
                AttentionItem(
                    urgency=Urgency.URGENT,
                    kind=AttentionKind.FAILED_WAVE,
                    title="P01-I01-W01 the failed wave",
                    detail="wave failed",
                ),
            )
            feed._request_rebuild_dom()
            await settle_screen(pilot)
            return _row_text(feed)

    text = asyncio.run(body())
    assert "P01-I01-W01 the failed wave" in text
    assert _FAILED_GLYPH in text
    assert "Enter to review" in text


def test_footer_needs_user_badge_agrees_with_warn_band() -> None:
    # The footer needs_user badge is fed by the SAME open-pause source the
    # band reads (App._all_open_pauses), so the badge count agrees with the
    # number of needs_user rows the warn band names.
    pause = _pause("answer me now", Urgency.HIGH)

    async def body() -> tuple[int, int]:
        app = _Harness(state=_load(_INCIDENT), pauses=(pause,))
        async with app.run_test(size=(120, 12)) as pilot:
            await settle_screen(pilot)
            feed = app.query_one(AttentionFeed)
            needs_user_rows = sum(1 for i in feed.items() if i.kind is AttentionKind.NEEDS_USER)
            badge_count = len(app._all_open_pauses())
            return needs_user_rows, badge_count

    needs_user_rows, badge_count = asyncio.run(body())
    # One pause -> one needs_user row in the band AND a "needs_user 1" badge.
    assert needs_user_rows == 1
    assert badge_count == 1
    assert format_needs_user_badge(badge_count) == "needs_user 1 "


def test_enter_over_acute_pause_row_fires_its_action() -> None:
    # Enter over an acute needs_user row still fires PauseSelected (the
    # existing select seam stays wired through the reskin), carrying the
    # pause's urn + question so the host re-opens the modal.
    pause = _pause("answer me from the band", Urgency.URGENT)

    async def body() -> tuple[list[AttentionFeed.PauseSelected], str]:
        app = _Harness(state=_load(_EMPTY), pauses=(pause,))
        captured: list[AttentionFeed.PauseSelected] = []

        async with app.run_test(size=(120, 12)) as pilot:
            await settle_screen(pilot)
            feed = app.query_one(AttentionFeed)

            # Intercept the posted message on the feed itself.
            original = feed.post_message

            def _capture(message: Any) -> bool:
                if isinstance(message, AttentionFeed.PauseSelected):
                    captured.append(message)
                return original(message)

            feed.post_message = _capture  # type: ignore[method-assign]
            feed.focus()
            feed.selected = 0
            await settle_screen(pilot)
            feed.action_activate()
            await settle_screen(pilot)
            return captured, pause.pause_urn

    captured, expected_urn = asyncio.run(body())
    assert len(captured) == 1
    assert captured[0].pause_urn == expected_urn
    assert captured[0].question.question == "answer me from the band"
