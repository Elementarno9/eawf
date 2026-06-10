"""Unit tests for ``active_phase_completion`` + active-phase ``_phase_cell`` (P27-I04-W24).

The workspace table's ``phase`` column scopes to a repo's *active* phase:
its phase id plus that phase's ``closed/total`` wave progress (e.g.
``P27 ⣿⣶ 119/143``), not the misleading whole-repo wave counts. These are
pure dict-in helpers, so the tests stay unit-level (no Pilot / app mount).

The active phase id resolves from the decoded per-repo state dict via the
``current.phase_id`` pointer when it names an existing ``"active"`` phase,
else the single ``"active"`` phase, else ``None``. Phase ids + repo codes
are abstract placeholders (``P01`` / ``P02`` / ``ABC``), never real-looking
project names.
"""

from __future__ import annotations

from typing import Any

from eawf.surfaces.render.brand import ACCENT_HEX as BRAND_ACCENT_HEX
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_BAND_PALETTE, DEFAULT_RENDER_MODE, EMPTY_STATE
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph, tint
from eawf.surfaces.tui.widgets.status_tint import BAND_HEX
from eawf.surfaces.tui.widgets.workspace_table import (
    PortfolioTotals,
    RepoRow,
    _brand_hex,
    _phase_cell,
    _repo_cell_markup,
    _totals_phase_cell,
    _totals_sigil_markup,
    active_phase_completion,
    build_repo_rows,
    completion_pair,
    repo_row_sigil,
    totals_row_sigil,
    warn_chip_markup,
)


def _repo_row(phase_id: str | None, done: int, total: int) -> RepoRow:
    """Build a minimal :class:`RepoRow` for a ``_phase_cell`` render check."""
    return RepoRow(
        code="ABC",
        path="/abs/path/abc",
        phase_id=phase_id,
        phase_done=done,
        phase_total=total,
        eu_consumed=0.0,
        eu_total=0.0,
        age="—",
    )


# --------------------------------------------------------------------------
# active_phase_completion — active-phase resolution
# --------------------------------------------------------------------------


def test_active_phase_completion_pointer_resolves_active_phase() -> None:
    """``current.phase_id`` pointing at an ``active`` phase selects that phase."""
    repo_state: dict[str, Any] = {
        "current": {"phase_id": "P02"},
        "phases": {
            "P01": {"id": "P01", "status": "closed"},
            "P02": {"id": "P02", "status": "active"},
        },
        "iters": {
            "P02-I01": {"id": "P02-I01", "phase_id": "P02", "status": "active"},
        },
        "waves": {
            "W1": {"iter_id": "P02-I01", "status": "closed"},
            "W2": {"iter_id": "P02-I01", "status": "pending"},
        },
    }
    assert active_phase_completion(repo_state) == ("P02", 1, 2)


def test_active_phase_completion_scans_when_pointer_stale() -> None:
    """A pointer at a non-active / missing phase falls back to the lone active phase."""
    repo_state: dict[str, Any] = {
        "current": {"phase_id": "P01"},  # stale: P01 is closed
        "phases": {
            "P01": {"id": "P01", "status": "closed"},
            "P02": {"id": "P02", "status": "active"},
        },
        "iters": {
            "P02-I01": {"id": "P02-I01", "phase_id": "P02", "status": "active"},
        },
        "waves": {
            "W1": {"iter_id": "P02-I01", "status": "closed"},
            "W2": {"iter_id": "P02-I01", "status": "closed"},
            "W3": {"iter_id": "P02-I01", "status": "pending"},
        },
    }
    assert active_phase_completion(repo_state) == ("P02", 2, 3)


def test_active_phase_completion_scans_when_pointer_absent() -> None:
    """No ``current.phase_id`` pointer at all still resolves the lone active phase."""
    repo_state: dict[str, Any] = {
        "phases": {
            "P01": {"id": "P01", "status": "closed"},
            "P02": {"id": "P02", "status": "active"},
        },
        "iters": {
            "P02-I01": {"id": "P02-I01", "phase_id": "P02", "status": "active"},
        },
        "waves": {
            "W1": {"iter_id": "P02-I01", "status": "closed"},
        },
    }
    assert active_phase_completion(repo_state) == ("P02", 1, 1)


def test_active_phase_completion_no_active_phase_is_none() -> None:
    """A state whose phases are all closed / planned yields ``(None, 0, 0)``."""
    repo_state: dict[str, Any] = {
        "current": {"phase_id": "P01"},
        "phases": {
            "P01": {"id": "P01", "status": "closed"},
            "P02": {"id": "P02", "status": "planned"},
        },
        "iters": {"P01-I01": {"id": "P01-I01", "phase_id": "P01", "status": "closed"}},
        "waves": {"W1": {"iter_id": "P01-I01", "status": "closed"}},
    }
    assert active_phase_completion(repo_state) == (None, 0, 0)


# --------------------------------------------------------------------------
# active_phase_completion — phase-scoped counts across a multi-phase state
# --------------------------------------------------------------------------


def test_active_phase_completion_counts_only_active_phase_waves() -> None:
    """Counts include only the active phase's waves, not a sibling phase's.

    Two phases each own iters + waves. The closed phase has 3 closed
    waves; the active phase has 2 closed of 4. The bar must report the
    active phase's ``(2, 4)``, ignoring the closed phase entirely.
    """
    repo_state: dict[str, Any] = {
        "current": {"phase_id": "P02"},
        "phases": {
            "P01": {"id": "P01", "status": "closed"},
            "P02": {"id": "P02", "status": "active"},
        },
        "iters": {
            "P01-I01": {"id": "P01-I01", "phase_id": "P01", "status": "closed"},
            "P02-I01": {"id": "P02-I01", "phase_id": "P02", "status": "active"},
            "P02-I02": {"id": "P02-I02", "phase_id": "P02", "status": "active"},
        },
        "waves": {
            # P01 (closed phase) — must NOT count.
            "A1": {"iter_id": "P01-I01", "status": "closed"},
            "A2": {"iter_id": "P01-I01", "status": "closed"},
            "A3": {"iter_id": "P01-I01", "status": "closed"},
            # P02 (active phase) — 2 closed of 4 across both its iters.
            "B1": {"iter_id": "P02-I01", "status": "closed"},
            "B2": {"iter_id": "P02-I01", "status": "pending"},
            "B3": {"iter_id": "P02-I02", "status": "closed"},
            "B4": {"iter_id": "P02-I02", "status": "in_progress"},
        },
    }
    phase_id, closed, total = active_phase_completion(repo_state)
    assert phase_id == "P02"
    assert (closed, total) == (2, 4)
    # Regression guard: the whole-repo count would have reported 5/7.
    assert completion_pair(repo_state) == (5, 7)


# --------------------------------------------------------------------------
# active_phase_completion — boundary / error paths (never raise)
# --------------------------------------------------------------------------


def test_active_phase_completion_none_or_empty_is_none() -> None:
    """``None`` / empty state yields ``(None, 0, 0)`` without raising."""
    assert active_phase_completion(None) == (None, 0, 0)
    assert active_phase_completion({}) == (None, 0, 0)


def test_active_phase_completion_malformed_phases_is_none() -> None:
    """A non-dict ``phases`` yields ``(None, 0, 0)`` without raising."""
    assert active_phase_completion({"phases": "not-a-dict"}) == (None, 0, 0)


def test_active_phase_completion_malformed_iters_keeps_phase_id() -> None:
    """A resolved phase but non-dict ``iters`` yields ``(phase_id, 0, 0)``."""
    repo_state: dict[str, Any] = {
        "current": {"phase_id": "P02"},
        "phases": {"P02": {"id": "P02", "status": "active"}},
        "iters": "not-a-dict",
        "waves": {"W1": {"iter_id": "P02-I01", "status": "closed"}},
    }
    assert active_phase_completion(repo_state) == ("P02", 0, 0)


def test_active_phase_completion_malformed_waves_keeps_phase_id() -> None:
    """A resolved phase but non-dict ``waves`` yields ``(phase_id, 0, 0)``."""
    repo_state: dict[str, Any] = {
        "current": {"phase_id": "P02"},
        "phases": {"P02": {"id": "P02", "status": "active"}},
        "iters": {"P02-I01": {"id": "P02-I01", "phase_id": "P02", "status": "active"}},
        "waves": "x",
    }
    assert active_phase_completion(repo_state) == ("P02", 0, 0)


def test_active_phase_completion_malformed_current_falls_back_to_scan() -> None:
    """A non-dict ``current`` is ignored; the lone active phase is scanned."""
    repo_state: dict[str, Any] = {
        "current": "not-a-dict",
        "phases": {"P02": {"id": "P02", "status": "active"}},
        "iters": {"P02-I01": {"id": "P02-I01", "phase_id": "P02", "status": "active"}},
        "waves": {"W1": {"iter_id": "P02-I01", "status": "closed"}},
    }
    assert active_phase_completion(repo_state) == ("P02", 1, 1)


# --------------------------------------------------------------------------
# _phase_cell — phase-id prefix in both render modes
# --------------------------------------------------------------------------


def test_phase_cell_prefixes_phase_id_unicode() -> None:
    """The phase cell starts with the phase id + a space in unicode mode."""
    cell = _phase_cell(_repo_row("P27", 119, 143), mode="unicode")
    assert cell.startswith("P27 ")
    assert "119/143" in cell


def test_phase_cell_prefixes_phase_id_ascii() -> None:
    """The phase cell starts with the phase id + a space in ascii mode."""
    cell = _phase_cell(_repo_row("P27", 3, 6), mode="ascii")
    assert cell.startswith("P27 ")
    assert "3/6" in cell


def test_phase_cell_dash_prefix_when_no_active_phase() -> None:
    """A ``None`` phase id renders the em-dash prefix, not a blank, in both modes."""
    for mode in ("unicode", "ascii"):
        cell = _phase_cell(_repo_row(None, 0, 0), mode=mode)  # type: ignore[arg-type]
        assert cell.startswith("— ")
        assert EMPTY_STATE in cell


# --------------------------------------------------------------------------
# build_repo_rows — populated phase_id field
# --------------------------------------------------------------------------


def test_build_repo_rows_none_state_is_empty() -> None:
    """A ``None`` workspace state yields no rows (the field plumbing is intact)."""
    assert build_repo_rows(None) == []


# --------------------------------------------------------------------------
# Reskin helpers (P30-I08-W02): leading sigil + green bar + warn chip
# --------------------------------------------------------------------------


def _flag_row(
    *, phase_id: str | None = "P01", blocker: bool = False, stale: bool = False
) -> RepoRow:
    """Build a minimal :class:`RepoRow` carrying the reskin lifecycle flags."""
    return RepoRow(
        code="ABC",
        path="/abs/path/abc",
        phase_id=phase_id,
        phase_done=0,
        phase_total=0,
        eu_consumed=0.0,
        eu_total=0.0,
        age="—",
        blocker=blocker,
        stale=stale,
    )


def _green() -> str:
    """Return the concrete green status hex the reskin tints per-repo sigils + bars with."""
    return DEFAULT_BAND_PALETTE["ok"]


def _brand() -> str:
    """Return the concrete brand-accent hex the reskin tints the totals row with."""
    return BRAND_ACCENT_HEX


# repo_row_sigil -- lifecycle -> sigil mapping


def test_repo_row_sigil_active_phase_is_running() -> None:
    """A repo with an active phase leads with the RUNNING diamond."""
    assert repo_row_sigil(_flag_row(phase_id="P01")) is Sigil.RUNNING


def test_repo_row_sigil_stale_no_phase_is_abandoned() -> None:
    """A stale repo with no active phase leads with the ABANDONED circled-slash."""
    assert repo_row_sigil(_flag_row(phase_id=None, stale=True)) is Sigil.ABANDONED


def test_repo_row_sigil_calm_no_phase_is_closed() -> None:
    """A calm repo with no active phase leads with the CLOSED filled circle."""
    assert repo_row_sigil(_flag_row(phase_id=None)) is Sigil.CLOSED


def test_repo_row_sigil_active_wins_over_stale() -> None:
    """An active-phase-and-stale repo reads in-flight (RUNNING wins over stale)."""
    assert repo_row_sigil(_flag_row(phase_id="P01", stale=True)) is Sigil.RUNNING


# totals_row_sigil -- aggregate lifecycle -> sigil mapping


def _totals(*, done: int, total: int) -> PortfolioTotals:
    """Build a :class:`PortfolioTotals` carrying only the wave counts."""
    return PortfolioTotals(
        repo_count=1,
        wave_done=done,
        wave_total=total,
        eu_consumed=0.0,
        eu_total=0.0,
        open_prs=0,
    )


def test_totals_row_sigil_open_work_is_running() -> None:
    """The totals row reads RUNNING while any tracked wave is still open."""
    assert totals_row_sigil(_totals(done=8, total=16)) is Sigil.RUNNING


def test_totals_row_sigil_all_closed_is_closed() -> None:
    """The totals row reads CLOSED once every tracked wave has landed."""
    assert totals_row_sigil(_totals(done=16, total=16)) is Sigil.CLOSED


def test_totals_row_sigil_empty_portfolio_is_closed() -> None:
    """An empty (zero-wave) portfolio reads CLOSED, never a fabricated in-flight."""
    assert totals_row_sigil(_totals(done=0, total=0)) is Sigil.CLOSED


# warn_chip_markup -- the attention chip renders as the warn triangle


def test_warn_chip_markup_calm_is_none() -> None:
    """A repo tripping neither threshold renders no chip."""
    assert warn_chip_markup(_flag_row(), mode=DEFAULT_RENDER_MODE) is None


def test_warn_chip_markup_blocker_renders_warn_triangle() -> None:
    """A blocker repo renders the warn triangle trailing the ``blocked`` word."""
    chip = warn_chip_markup(_flag_row(blocker=True), mode=DEFAULT_RENDER_MODE)
    triangle = chrome("attention", mode=DEFAULT_RENDER_MODE)
    assert chip == f"[{BAND_HEX['warn']}]{triangle} blocked[/]"


def test_warn_chip_markup_stale_renders_warn_triangle() -> None:
    """A stale repo renders the warn triangle trailing the ``stale`` word."""
    chip = warn_chip_markup(_flag_row(stale=True), mode=DEFAULT_RENDER_MODE)
    triangle = chrome("attention", mode=DEFAULT_RENDER_MODE)
    assert chip == f"[{BAND_HEX['warn']}]{triangle} stale[/]"


def test_warn_chip_markup_both_trails_blocker_first() -> None:
    """A repo tripping both trails both words after one shared triangle, blocker first."""
    chip = warn_chip_markup(_flag_row(blocker=True, stale=True), mode=DEFAULT_RENDER_MODE)
    triangle = chrome("attention", mode=DEFAULT_RENDER_MODE)
    assert chip == f"[{BAND_HEX['warn']}]{triangle} blocked stale[/]"


def test_warn_chip_markup_is_not_a_bare_word() -> None:
    """The chip is the triangle marker, never the legacy bare ``(blocked)`` word."""
    chip = warn_chip_markup(_flag_row(blocker=True), mode=DEFAULT_RENDER_MODE)
    assert chip is not None
    assert "(blocked)" not in chip
    assert chrome("attention", mode=DEFAULT_RENDER_MODE) in chip


# _phase_cell -- per-repo green status-tinted bar; _totals_phase_cell -- brand accent


def test_phase_cell_bar_is_green_tinted() -> None:
    """The per-repo phase cell wraps its completion bar in the green status span."""
    cell = _phase_cell(_repo_row("P27", 3, 6), mode="unicode")
    assert cell.startswith("P27 ")
    assert f"[{_green()}]" in cell
    assert "3/6" in cell


def test_phase_cell_empty_state_is_not_tinted() -> None:
    """A no-progress phase cell leaves the empty-state sentinel untinted."""
    cell = _phase_cell(_repo_row(None, 0, 0), mode="unicode")
    assert EMPTY_STATE in cell
    assert f"[{_green()}]" not in cell


def test_brand_hex_falls_back_to_brand_accent() -> None:
    """An ``accent``-less palette resolves the brand accent fallback."""
    assert _brand_hex({}) == BRAND_ACCENT_HEX


def test_brand_hex_reads_accent_key() -> None:
    """A palette carrying an ``accent`` key wins over the brand fallback."""
    assert _brand_hex({"accent": "#123456"}) == "#123456"


def test_totals_phase_cell_bar_is_brand_tinted() -> None:
    """The totals phase cell wraps its summed completion bar in the brand-accent span."""
    cell = _totals_phase_cell(_totals(done=8, total=16), mode="unicode")
    assert cell.startswith("1 repos ")
    assert f"[{_brand()}]" in cell
    assert "8/16" in cell


def test_totals_phase_cell_bar_is_not_the_per_repo_green() -> None:
    """The totals bar carries the brand accent, not the per-repo band ``ok`` green.

    The brand accent (``#16b384``) and the per-repo status green (``#009e73``)
    are distinct hexes, so the roll-up reads in the brand voice rather than as
    one more per-repo status bar.
    """
    assert _brand() != _green()
    cell = _totals_phase_cell(_totals(done=8, total=16), mode="unicode")
    assert f"[{_green()}]" not in cell


def test_totals_phase_cell_empty_state_is_not_tinted() -> None:
    """A zero-wave totals phase cell leaves the empty-state sentinel untinted."""
    cell = _totals_phase_cell(_totals(done=0, total=0), mode="unicode")
    assert EMPTY_STATE in cell
    assert f"[{_brand()}]" not in cell


# _totals_sigil_markup -- the totals row's leading sigil carries the brand accent


def test_totals_sigil_markup_carries_brand_accent() -> None:
    """The totals sigil is tinted the brand accent regardless of lifecycle shape."""
    running = _totals_sigil_markup(Sigil.RUNNING, mode=DEFAULT_RENDER_MODE, palette={})
    closed = _totals_sigil_markup(Sigil.CLOSED, mode=DEFAULT_RENDER_MODE, palette={})
    assert running == f"[{_brand()}]{glyph(Sigil.RUNNING, mode=DEFAULT_RENDER_MODE)}[/]"
    assert closed == f"[{_brand()}]{glyph(Sigil.CLOSED, mode=DEFAULT_RENDER_MODE)}[/]"


def test_totals_sigil_markup_not_the_per_repo_closed_green() -> None:
    """The totals CLOSED sigil reads brand accent, not the per-repo CLOSED band green."""
    closed = _totals_sigil_markup(
        Sigil.CLOSED, mode=DEFAULT_RENDER_MODE, palette=DEFAULT_BAND_PALETTE
    )
    assert f"[{_brand()}]" in closed
    assert f"[{_green()}]" not in closed


# _repo_cell_markup -- leading sigil + code + warn chip


def _leading_sigil_span(sigil: Sigil) -> str:
    """Return the leading tinted-sigil span a cell for *sigil* opens with.

    The CLOSED hue is theme-resolved off the band palette's ``ok`` green; the
    other lifecycle hues come from the COLOUR layer. Built off the canonical
    :func:`glyph` / :func:`tint` helpers so the test hard-codes no glyph / hex.
    """
    mark = glyph(sigil, mode=DEFAULT_RENDER_MODE)
    if sigil is Sigil.CLOSED:
        return f"[{_green()}]{mark}[/]"
    hue = tint(sigil)
    return f"[{hue}]{mark}[/]" if hue is not None else f"[{BAND_HEX['warn']}]{mark}[/]"


def test_repo_cell_markup_leads_with_lifecycle_sigil() -> None:
    """A calm repo cell leads with its tinted lifecycle sigil then the code."""
    cell = _repo_cell_markup(
        _flag_row(phase_id="P01"), mode=DEFAULT_RENDER_MODE, palette=DEFAULT_BAND_PALETTE
    )
    assert cell.startswith(_leading_sigil_span(Sigil.RUNNING))
    assert cell.endswith(" ABC")


def test_repo_cell_markup_calm_has_no_chip() -> None:
    """A calm repo cell renders just ``<sigil> <code>`` with no warn marker."""
    cell = _repo_cell_markup(
        _flag_row(phase_id="P01"), mode=DEFAULT_RENDER_MODE, palette=DEFAULT_BAND_PALETTE
    )
    assert chrome("attention", mode=DEFAULT_RENDER_MODE) not in cell


def test_repo_cell_markup_attention_appends_warn_chip() -> None:
    """An attention repo cell carries the warn-marker chip after the code."""
    row = _flag_row(phase_id="P01", blocker=True)
    cell = _repo_cell_markup(row, mode=DEFAULT_RENDER_MODE, palette=DEFAULT_BAND_PALETTE)
    chip = warn_chip_markup(row, mode=DEFAULT_RENDER_MODE)
    assert chip is not None
    assert cell.startswith(_leading_sigil_span(Sigil.RUNNING))
    assert "ABC" in cell
    assert chip in cell
