"""Jury advisory-to-block authority scorecard in the Trust mode.

The Trust mode (digit ``4``) gains a JURY AUTHORITY section: the
cross-vendor jury's CURRENT authority state, rendered honestly. The jury
is held ADVISORY (its veto is logged, the close still proceeds) until the
I07 validation pass earns it block authority, so the pane renders that
literal advisory state -- a number-based scorecard of dashes plus
sample-count / cohort notes, never a fabricated trust number -- with the
validation metrics pinned as ``[needs I07]`` placeholders the next roadmap
owns (:func:`~eawf.surfaces.tui.modes.trust.render_jury_authority`).

The measurable signal is the snapshot assertion: the trust scorecard
renders sigil rows where the mock shows them (the amber attention banner +
the overridden marker on the authority row), keeps the number-based metric
rows with dashes + sample counts, places the overridden marker ONLY on the
advisory-authority row, and pins the literal starved / no-data copy rather
than a fabricated trust number.

This module pins:

* the pure :func:`render_jury_authority` helper (no Textual mount) -- the
  sigil rows, the dash + sample-count metric rows, the overridden marker on
  the authority row alone, and the honest-negative copy; and
* the Pilot-driven section -- mounting the Trust mode surfaces the
  jury-authority scorecard with the literal advisory copy and never a
  fabricated trust number.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 EAWF_DAEMONLESS=1 uv run pytest \
        tests/snapshots/tui/test_trust_jury_authority.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.trust import (
    JURY_AUTHORITY_ADVISORY,
    JURY_ECE_STARVED,
    JURY_FLEISS_NEED_COHORT,
    JURY_KNOWN_BAD_NEED_LABELS,
    JURY_VARIANCE_STARVED,
    JURY_WILSON_FLOOR,
    NEEDS_I07_PLACEHOLDER,
    TrustModeScreen,
    render_jury_authority,
)
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph

_SIZE = (120, 40)
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"

#: The attention + overridden sigils in the unicode column (the column the
#: app resolves under the Pilot harness). Pinned here so the test asserts the
#: exact glyphs the mock shows, not a hand-typed copy that could drift.
_ATTENTION_UNICODE = chrome("attention", mode="unicode")
_OVERRIDDEN_UNICODE = glyph(Sigil.CLAIMED, mode="unicode")


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every git probe so the rendered chrome is deterministic."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )
    monkeypatch.setattr("eawf.surfaces.tui.widgets.git_pane._git_run", lambda *a, **k: None)


# --------------------------------------------------------------------------
# Pure helper -- no Textual mount
# --------------------------------------------------------------------------


def test_render_jury_authority_leads_with_attention_sigil_banner() -> None:
    """The banner row wears the attention sigil, never the pending ring.

    Warn maps to the attention triangle (``chrome("attention")``); a degraded
    / advisory state must NOT reuse the pending hollow ring, which would make
    it shape-identical to a not-yet-run state.
    """
    body = render_jury_authority(mode="unicode")
    first_line = body.splitlines()[0]
    assert _ATTENTION_UNICODE in first_line
    # The pending ring must NOT appear anywhere in the section -- advisory is
    # an attention state, not a pending one.
    assert glyph(Sigil.PENDING, mode="unicode") not in body


def test_render_jury_authority_places_overridden_marker_only_on_authority_row() -> None:
    """The overridden marker lands on the authority row and nowhere else."""
    body = render_jury_authority(mode="unicode")
    rows_with_marker = [line for line in body.splitlines() if _OVERRIDDEN_UNICODE in line]
    assert len(rows_with_marker) == 1
    assert f"authority {JURY_AUTHORITY_ADVISORY}" in rows_with_marker[0]


def test_render_jury_authority_metric_rows_are_number_based_with_dashes() -> None:
    """Each validation metric is a dash + sample-count note, not a fake number."""
    body = render_jury_authority(mode="unicode")
    assert JURY_FLEISS_NEED_COHORT in body
    assert JURY_ECE_STARVED in body
    assert JURY_VARIANCE_STARVED in body
    assert JURY_KNOWN_BAD_NEED_LABELS in body
    # The Wilson lower-bound is number-based: a measured 0.00 against the floor.
    assert f"0.00 / {JURY_WILSON_FLOOR:.2f}" in body


def test_render_jury_authority_pins_needs_i07_placeholder_not_fake_number() -> None:
    """The advisory banner names the I07 placeholder, never a trust number."""
    body = render_jury_authority(mode="unicode")
    assert NEEDS_I07_PLACEHOLDER in body
    # Honest-negative is sacred: no fabricated "trusted" / "blocking" verdict.
    assert "trusted" not in body
    assert "blocking" not in body


def test_render_jury_authority_ascii_column_uses_ascii_sigils() -> None:
    """The ascii render mode swaps to the ascii glyph column for the sigils."""
    body = render_jury_authority(mode="ascii")
    assert chrome("attention", mode="ascii") in body
    assert glyph(Sigil.CLAIMED, mode="ascii") in body
    # The unicode glyphs must not leak into the ascii column.
    assert _ATTENTION_UNICODE not in body
    assert _OVERRIDDEN_UNICODE not in body


# --------------------------------------------------------------------------
# Pilot-driven section -- the mounted pane surfaces the scorecard
# --------------------------------------------------------------------------


def test_trust_pane_renders_jury_authority_scorecard() -> None:
    """The mounted Trust mode renders the jury advisory-authority scorecard."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, TrustModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "JURY AUTHORITY" in frame
            # The advisory banner + overridden authority row both render.
            assert _ATTENTION_UNICODE in frame
            assert _OVERRIDDEN_UNICODE in frame
            assert JURY_AUTHORITY_ADVISORY in frame
            # Number-based metric rows with dashes + sample counts.
            assert JURY_FLEISS_NEED_COHORT in frame
            assert f"0.00 / {JURY_WILSON_FLOOR:.2f}" in frame
            # Honest-negative: the I07 placeholder, never a fake trust number.
            assert NEEDS_I07_PLACEHOLDER in frame
            assert_screen_snapshot(app, _GOLDEN / "trust_jury_authority.txt")

    asyncio.run(body())
