"""Unit + Pilot tests for the C06 shared ``Footer`` + ``Heartbeat`` (P26-W18).

Covers the pure hint formatter (:func:`format_hints`), the Footer's
default + overridden hint strip, the weekly-burn line builder + its
empty-state fallback, the embedded Heartbeat pulse glyph + degraded-colour
class flip (D22), and a Pilot-driven paint confirming the footer hints +
heartbeat dot render under the real palette.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Static

from eawf.state.enums import ActualStatus
from eawf.state.models import ActualSummary, State
from eawf.tui_v2.widgets.footer import (
    DEFAULT_HINTS,
    HEARTBEAT_GLYPH,
    WEEKLY_BURN_EMPTY,
    Footer,
    Heartbeat,
    build_weekly_burn_line,
    format_hints,
)

from ._palette_harness import PaletteHarnessApp

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "tui_v2" / "theme.tcss"

#: Fixed clock anchor for the weekly-burn tests. The seeded actual's
#: ``updated_at`` sits at this instant, so injecting ``now=_T0`` keeps the
#: in-window actual inside the trailing-7-day window regardless of
#: wall-clock date (the W24 deterministic-window pattern).
_T0 = datetime(2026, 5, 1, tzinfo=UTC)


def _state(*, weekly_eu_target: float | None, actual_eu: float | None) -> State:
    """Build a minimal valid State with an optional target + one actual.

    Args:
        weekly_eu_target: The project's weekly EU budget, or ``None`` to
            leave it unset.
        actual_eu: When set, seeds a single in-window actual carrying this
            ``elapsed_eu``; ``None`` leaves ``actuals`` empty.

    Returns:
        A validated :class:`~eawf.state.models.State`.
    """
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
            "weekly_eu_target": weekly_eu_target,
        },
        "current": {
            "project_code": "QR",
            "subproject_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state = State.model_validate(payload)
    if actual_eu is not None:
        state.actuals = {
            "P01-I01-W01": ActualSummary(
                id="ACT-P01-I01-W01",
                scope_id="P01-I01-W01",
                status=ActualStatus.DONE,
                elapsed_eu=actual_eu,
                current_store_record_id="REC-P01-I01-W01",
                updated_at=_T0,
            )
        }
    return state


class _Harness(PaletteHarnessApp):
    """Production-style host loading the real palette CSS."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield Footer(id="ftr")


class _HeartbeatHarness(PaletteHarnessApp):
    """Host mounting a standalone Heartbeat for the degraded-flip test."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield Heartbeat(id="hb")


# --------------------------------------------------------------------------
# build_weekly_burn_line — populated + empty-state paths
# --------------------------------------------------------------------------


def test_build_weekly_burn_line_renders_figure_when_target_and_actuals_set() -> None:
    state = _state(weekly_eu_target=10.0, actual_eu=3.5)
    # Inject the fixture anchor so the trailing-7-day window includes the
    # in-window actual regardless of wall-clock date.
    line = build_weekly_burn_line(state, now=_T0)
    assert line == "weekly burn: 3.5 / 10 EU"


def test_build_weekly_burn_line_none_target_renders_empty_state() -> None:
    # Actuals present but no target set: never a 0/None bar, the placeholder.
    state = _state(weekly_eu_target=None, actual_eu=3.5)
    line = build_weekly_burn_line(state, now=_T0)
    assert line == f"weekly burn: {WEEKLY_BURN_EMPTY}"


def test_build_weekly_burn_line_empty_actuals_renders_empty_state() -> None:
    # Target set but no actuals rolled up yet: the empty-state placeholder.
    state = _state(weekly_eu_target=10.0, actual_eu=None)
    line = build_weekly_burn_line(state, now=_T0)
    assert line == f"weekly burn: {WEEKLY_BURN_EMPTY}"


def test_build_weekly_burn_line_none_state_renders_empty_state() -> None:
    # Boundary: no bound state at all (pre-load) renders the placeholder.
    assert build_weekly_burn_line(None) == f"weekly burn: {WEEKLY_BURN_EMPTY}"


# --------------------------------------------------------------------------
# format_hints — empty + single + many
# --------------------------------------------------------------------------


def test_format_hints_empty_is_blank() -> None:
    assert format_hints(()) == ""


def test_format_hints_single_has_no_separator() -> None:
    assert format_hints(("q quit",)) == "q quit"


def test_format_hints_many_joined_with_bullet() -> None:
    out = format_hints(("a", "b", "c"))
    assert out == "a  ·  b  ·  c"
    assert out.count("·") == 2


# --------------------------------------------------------------------------
# Footer hints — default + override via set_hints
# --------------------------------------------------------------------------


def test_footer_paints_default_hints() -> None:
    async def body() -> None:
        app = _Harness()
        # Wide canvas: the default hint strip now carries the global
        # w/r/u scope-switch + F5 refresh affordances and overflows 80
        # cols; the real scope screens render at 120.
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            rendered = app.export_screenshot()
            assert "quit" in rendered
            assert "palette" in rendered
            assert "scope" in rendered
            assert "refresh" in rendered

    asyncio.run(body())


def test_footer_set_hints_repaints_strip() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.set_hints(("xyzzy custom",))
            await pilot.pause()
            assert footer.hints == ("xyzzy custom",)
            assert "xyzzy" in app.export_screenshot()

    asyncio.run(body())


def test_footer_default_hints_use_full_key_names() -> None:
    # Operator convention: full key names (no "PgUp" abbreviations).
    joined = format_hints(DEFAULT_HINTS)
    assert "PgUp" not in joined
    assert "PgDn" not in joined


# --------------------------------------------------------------------------
# Footer owns a Heartbeat — D3 shared-chassis bundling
# --------------------------------------------------------------------------


def test_footer_owns_heartbeat() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            assert footer.query(Heartbeat)
            rendered = app.export_screenshot()
            assert HEARTBEAT_GLYPH in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# Footer weekly-burn cell — paints the figure / empty-state from state
# --------------------------------------------------------------------------


def _burn_text(app: App[None]) -> str:
    """Read the footer burn cell's rendered text.

    Goes through the widget's own ``Static`` content rather than the SVG
    screenshot so the assertion is independent of how ``export_screenshot``
    encodes inter-word spacing.
    """
    burn = app.query_one(".footer-burn", Static)
    return str(burn.render())


def test_footer_paints_burn_empty_state_without_state() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            # The bare harness exposes no ``state`` attribute, so the burn
            # cell falls back to the empty-state placeholder.
            assert WEEKLY_BURN_EMPTY in _burn_text(app)

    asyncio.run(body())


def test_footer_paints_burn_figure_when_state_populated() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.state = _state(weekly_eu_target=10.0, actual_eu=3.5)
            await pilot.pause()
            # The widget anchors the rollup on wall-clock (no ``now``
            # override), so assert the figure *form* — the target + ``EU``
            # unit — not the window-dependent consumed value (covered by
            # the pure ``build_weekly_burn_line`` unit tests).
            text = _burn_text(app)
            assert text.startswith("weekly burn:")
            assert "/ 10 EU" in text
            assert WEEKLY_BURN_EMPTY not in text

    asyncio.run(body())


def test_footer_repaints_burn_to_empty_state_on_state_change() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.state = _state(weekly_eu_target=10.0, actual_eu=3.5)
            await pilot.pause()
            assert "/ 10 EU" in _burn_text(app)
            # A revision dropping the target flips the cell back to the
            # placeholder — the watcher repaints in place.
            footer.state = _state(weekly_eu_target=None, actual_eu=None)
            await pilot.pause()
            text = _burn_text(app)
            assert "EU" not in text
            assert WEEKLY_BURN_EMPTY in text

    asyncio.run(body())


# --------------------------------------------------------------------------
# Heartbeat — pulse glyph + degraded class + ack
# --------------------------------------------------------------------------


def test_heartbeat_paints_glyph_when_lit() -> None:
    async def body() -> None:
        app = _HeartbeatHarness()
        async with app.run_test(size=(20, 3)) as pilot:
            await pilot.pause()
            assert HEARTBEAT_GLYPH in app.export_screenshot()

    asyncio.run(body())


def test_heartbeat_degraded_flag_sets_class() -> None:
    async def body() -> None:
        app = _HeartbeatHarness()
        async with app.run_test(size=(20, 3)) as pilot:
            await pilot.pause()
            hb = app.query_one("#hb", Heartbeat)
            assert not hb.has_class("-degraded")
            hb.degraded = True
            await pilot.pause()
            assert hb.has_class("-degraded")

    asyncio.run(body())


def test_heartbeat_ack_forces_lit_frame() -> None:
    async def body() -> None:
        app = _HeartbeatHarness()
        async with app.run_test(size=(20, 3)) as pilot:
            await pilot.pause()
            hb = app.query_one("#hb", Heartbeat)
            hb._lit = False
            await pilot.pause()
            hb.ack()
            await pilot.pause()
            assert hb._lit is True
            assert HEARTBEAT_GLYPH in app.export_screenshot()

    asyncio.run(body())
