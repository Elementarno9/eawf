"""Unit + Pilot tests for the C06 shared ``Footer`` + ``Heartbeat`` (P26-W18).

Covers the pure hint formatter (:func:`format_hints`), the Footer's
default + overridden hint strip, the weekly-burn line builder + its
empty-state fallback, the always-visible mode row
(:func:`build_mode_row` + the mounted Footer's active-mode highlight),
the embedded Heartbeat pulse glyph + degraded-colour class flip (D22),
and a Pilot-driven paint confirming the footer hints + heartbeat dot
render under the real palette.

The footer is **two rows** (the operator-chosen layout): row 1 merges
the key-hint strip (left) with the status cells (weekly-burn + needs_user
badge + heartbeat, right); row 2 is the always-visible mode row derived
from ``MODE_REGISTRY`` with the active mode highlighted. The footer stays
height 2.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Static

from eawf.kernel.state.enums import ActualStatus
from eawf.kernel.state.models import ActualSummary, State
from eawf.surfaces.tui.modes.registry import MODE_REGISTRY
from eawf.surfaces.tui.widgets import eu_bar
from eawf.surfaces.tui.widgets.footer import (
    DEFAULT_HINTS,
    HEARTBEAT_GLYPH,
    WEEKLY_BURN_EMPTY,
    Footer,
    Heartbeat,
    build_mode_row,
    build_weekly_burn_line,
    format_hints,
)

from ._palette_harness import PaletteHarnessApp

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"

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
        A validated :class:`~eawf.kernel.state.models.State`.
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


# --------------------------------------------------------------------------
# WEEKLY_BURN_EMPTY DRY — sourced from the canonical eu_bar sentinel
# --------------------------------------------------------------------------


def test_weekly_burn_empty_is_canonical_eu_bar_sentinel() -> None:
    # The footer's empty marker must be the SAME object as the canonical
    # eu_bar sentinel (DRY): both "no data" surfaces stay in lockstep.
    assert WEEKLY_BURN_EMPTY is eu_bar.EMPTY_STATE


# --------------------------------------------------------------------------
# Two-row footer — hints + status share row 1, mode row is row 2, height 2
# --------------------------------------------------------------------------


def test_footer_is_two_rows_tall() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            # The operator-chosen layout: the footer occupies two terminal
            # rows -- the merged hints+status row and the always-visible mode
            # row -- and stays height 2 (it does NOT grow to 3 rows).
            assert footer.size.height == 2

    asyncio.run(body())


def test_footer_hints_carry_repo_set_at_120() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.scopes.repo import _REPO_HINTS

        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.set_hints(_REPO_HINTS)
            await pilot.pause()
            # The hint lane shares row 1 with the auto-width status cells, so
            # the whole repo hint set (incl. ``q quit``) lives in the hint
            # Static's content (Textual clips at paint time, not in the
            # renderable, so the tail is never lost from the source string).
            assert "q quit" in str(footer.query_one(".footer-hints", Static).render())

    asyncio.run(body())


def test_footer_hints_carry_workspace_set_at_120() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.scopes.workspace import _WORKSPACE_HINTS

        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.set_hints(_WORKSPACE_HINTS)
            await pilot.pause()
            assert "q quit" in str(footer.query_one(".footer-hints", Static).render())

    asyncio.run(body())


def test_footer_hints_and_status_share_first_row() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.state = _state(weekly_eu_target=10.0, actual_eu=3.5)
            await pilot.pause()
            # The operator merged the status cell onto row 1: the hints, the
            # burn cell, and the heartbeat all sit on the same row, with the
            # mode row alone on row 2 below them.
            hints = app.query_one(".footer-hints", Static)
            burn = app.query_one(".footer-burn", Static)
            modes = app.query_one(".footer-modes", Static)
            assert burn.region.y == hints.region.y
            assert modes.region.y > hints.region.y
            assert "/ 10 EU" in str(burn.render())
            assert footer.query(Heartbeat)

    asyncio.run(body())


# --------------------------------------------------------------------------
# build_mode_row — every mode, digit + lowercased title, active highlighted
# --------------------------------------------------------------------------


def _mode_row_plain(markup: str) -> str:
    """Strip content-markup tags from a mode-row string for token assertions."""
    import re

    return re.sub(r"\[[^\]]*\]", "", markup).replace("\\[", "[")


def test_build_mode_row_lists_all_modes_in_registry_order() -> None:
    # Every registered mode renders as ``<digit> <title-lowercased>`` in
    # registry (digit) order, joined by the bullet separator.
    plain = _mode_row_plain(build_mode_row(None))
    tokens = [tok.strip() for tok in plain.split("·")]
    expected = [f"{spec.digit} {spec.title.lower()}" for spec in MODE_REGISTRY]
    assert tokens == expected
    # The operator example lead/tail tokens are present + lowercased.
    assert tokens[0] == "1 home"
    assert "2 autopilot" in tokens


def test_build_mode_row_highlights_active_mode_only() -> None:
    # The active mode's token carries the bold accent span; the others are
    # muted. Pick a non-first mode so the assertion is not order-trivial.
    active = MODE_REGISTRY[1]  # autopilot
    markup = build_mode_row(active.name)
    active_token = f"{active.digit} {active.title.lower()}"
    assert f"[$accent][b]{active_token}[/b][/]" in markup
    # A different, non-active mode renders muted (no accent/bold span).
    other = MODE_REGISTRY[0]
    other_token = f"{other.digit} {other.title.lower()}"
    assert f"[$muted]{other_token}[/]" in markup
    assert f"[$accent][b]{other_token}[/b][/]" not in markup


def test_build_mode_row_none_highlights_nothing() -> None:
    # No active mode (or a name matching no mode) leaves every token muted.
    markup = build_mode_row(None)
    assert "[$accent][b]" not in markup
    # Every registered mode still appears, muted.
    for spec in MODE_REGISTRY:
        assert f"[$muted]{spec.digit} {spec.title.lower()}[/]" in markup


def test_build_mode_row_unknown_active_highlights_nothing() -> None:
    # A current_mode that names no registered mode (e.g. Textual's bare
    # "_default") highlights nothing rather than raising.
    markup = build_mode_row("_default")
    assert "[$accent][b]" not in markup


# --------------------------------------------------------------------------
# Mounted Footer mode row — row 2, active highlight seeds + updates
# --------------------------------------------------------------------------


def test_footer_mounts_mode_row_on_second_row() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            hints = app.query_one(".footer-hints", Static)
            modes = app.query_one(".footer-modes", Static)
            # The mode row sits on row 2 (below the merged hints+status row)
            # and lists every mode token; the footer stays height 2.
            assert modes.region.y > hints.region.y
            assert footer.size.height == 2
            rendered = _mode_row_plain(str(modes.render()))
            for spec in MODE_REGISTRY:
                assert f"{spec.digit} {spec.title.lower()}" in rendered

    asyncio.run(body())


def _token_styles(modes: Static, token: str) -> set[str]:
    """Collect the content-markup styles applied to *token*'s text range.

    The mounted Static renders a Textual ``Content`` whose ``str()`` strips
    the markup, so a markup-substring assertion does not work; instead this
    locates *token* in the rendered plain text and returns the set of span
    styles (e.g. ``{"$accent", "b"}`` for the highlighted active token,
    ``{"$muted"}`` for a muted one) that cover it.
    """
    content = modes.render()
    plain = content.plain  # type: ignore[attr-defined]
    start = plain.index(token)
    end = start + len(token)
    return {
        span.style  # type: ignore[attr-defined]
        for span in content.spans  # type: ignore[attr-defined]
        if span.start <= start and span.end >= end
    }


def test_footer_mode_row_highlight_updates_on_active_mode_change() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            # Drive the active mode directly (the standalone-test seam), the
            # same way the other footer tests drive ``state`` /
            # ``pending_pauses``.
            first = MODE_REGISTRY[1]  # autopilot
            footer.active_mode = first.name
            await pilot.pause()
            modes = app.query_one(".footer-modes", Static)
            first_token = f"{first.digit} {first.title.lower()}"
            # The active token carries the accent + bold spans; a different
            # mode stays muted.
            assert {"$accent", "b"} <= _token_styles(modes, first_token)
            other_token = f"{MODE_REGISTRY[3].digit} {MODE_REGISTRY[3].title.lower()}"
            assert "$accent" not in _token_styles(modes, other_token)
            # A change repaints the highlight onto the new active mode.
            second = MODE_REGISTRY[3]  # trust
            footer.active_mode = second.name
            await pilot.pause()
            modes = app.query_one(".footer-modes", Static)
            second_token = f"{second.digit} {second.title.lower()}"
            assert {"$accent", "b"} <= _token_styles(modes, second_token)
            # The previously-active mode is no longer highlighted.
            assert "$accent" not in _token_styles(modes, first_token)

    asyncio.run(body())


def test_footer_mode_row_seeds_highlight_from_app_current_mode() -> None:
    """A host exposing ``current_mode`` seeds the mode-row highlight on mount.

    Mirrors the live path: each mode owns its own scope screen, so the footer
    mounts fresh on a mode switch and reads ``app.current_mode``. A bare
    harness whose host exposes a registry mode name highlights that mode
    without a manual ``active_mode`` assignment.
    """

    class _ModeHarness(PaletteHarnessApp):
        CSS_PATH = str(_THEME)

        def __init__(self, current_mode: str) -> None:
            super().__init__()
            self._seed_mode = current_mode

        @property
        def current_mode(self) -> str:  # type: ignore[override]
            return self._seed_mode

        def compose(self) -> ComposeResult:
            yield Footer(id="ftr")

    async def body() -> None:
        target = MODE_REGISTRY[3]  # trust
        app = _ModeHarness(target.name)
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            assert footer.active_mode == target.name
            modes = app.query_one(".footer-modes", Static)
            token = f"{target.digit} {target.title.lower()}"
            assert {"$accent", "b"} <= _token_styles(modes, token)

    asyncio.run(body())
