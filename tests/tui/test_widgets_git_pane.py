"""Unit + Pilot tests for the C06 ``GitPane`` widget (P26-W17).

Covers the pure line formatter (:func:`format_git_lines`), the live git
probe (:func:`gather_git_fields`) against a throwaway repo and against a
non-git directory (dash fallback), and a Pilot-driven paint.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from textual.app import ComposeResult

from eawf.surfaces.tui.theme import WONG_VARIABLES
from eawf.surfaces.tui.widgets.git_pane import (
    ADDED_VAR,
    CHANGED_VAR,
    DASH,
    REMOVED_VAR,
    GitFields,
    GitPane,
    classify_porcelain,
    format_git_lines,
    format_status_markup,
    gather_git_fields,
)

from ._palette_harness import PaletteHarnessApp

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"

#: The dark-theme (harness default) hexes the rotated status-segment vars
#: resolve to: added through the green ``$ok``, removed through the ``$err``
#: red, changed through the ``$warn`` amber. Read off the canonical Wong
#: palette so the per-cell capture reds when the palette is reverted.
_ADDED_HEX = WONG_VARIABLES["ok"]
_REMOVED_HEX = WONG_VARIABLES["err"]
_CHANGED_HEX = WONG_VARIABLES["warn"]


def _git(args: list[str], *, cwd: Path) -> None:
    """Run a git command in *cwd*, raising on failure (test setup only)."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_repo(root: Path) -> Path:
    """Initialise a one-commit git repo at *root* and return it."""
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], cwd=root)
    _git(["config", "user.email", "test@example.com"], cwd=root)
    _git(["config", "user.name", "Test"], cwd=root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=root)
    _git(["commit", "-q", "-m", "initial commit"], cwd=root)
    return root


class _Harness(PaletteHarnessApp):
    """Production-style host loading the real palette CSS."""

    CSS_PATH = str(_THEME)
    cwd: Path

    def compose(self) -> ComposeResult:
        yield GitPane(cwd=self.cwd, id="gp")


# --------------------------------------------------------------------------
# format_git_lines — pure render
# --------------------------------------------------------------------------


def test_format_git_lines_clean_no_recent() -> None:
    fields = GitFields(
        branch="main",
        dirty="clean",
        added=0,
        removed=0,
        changed=0,
        ahead_behind="up-to-date",
        recent_commits=(),
    )
    lines = format_git_lines(fields)
    assert "branch:   main" in lines
    assert "status:   clean" in lines
    assert "upstream: up-to-date" in lines
    assert not any(line == "recent:" for line in lines)


def test_format_git_lines_with_recent_commits() -> None:
    fields = GitFields(
        branch="feature/x",
        dirty="2 changed",
        added=0,
        removed=0,
        changed=2,
        ahead_behind="+1 / -0",
        recent_commits=("abc123 first", "def456 second"),
    )
    lines = format_git_lines(fields)
    assert "recent:" in lines
    assert "  abc123 first" in lines
    assert "  def456 second" in lines


def test_format_git_lines_all_dashes() -> None:
    fields = GitFields(
        branch=DASH,
        dirty=DASH,
        added=0,
        removed=0,
        changed=0,
        ahead_behind=DASH,
        recent_commits=(),
    )
    lines = format_git_lines(fields)
    assert f"branch:   {DASH}" in lines


# --------------------------------------------------------------------------
# gather_git_fields — live probe + fallbacks
# --------------------------------------------------------------------------


def test_gather_git_fields_clean_repo(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    fields = gather_git_fields(repo)
    assert fields.branch == "main"
    assert fields.dirty == "clean"
    # No upstream configured on a local-only repo -> dash.
    assert fields.ahead_behind == DASH
    assert len(fields.recent_commits) == 1
    assert "initial commit" in fields.recent_commits[0]


def test_gather_git_fields_dirty_repo(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    fields = gather_git_fields(repo)
    assert "changed" in fields.dirty


def test_gather_git_fields_non_git_dir_dashes(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    fields = gather_git_fields(plain)
    assert fields.branch == DASH
    assert fields.dirty == DASH
    assert fields.ahead_behind == DASH
    assert fields.recent_commits == ()


# --------------------------------------------------------------------------
# Pilot paint — renders branch under the real palette
# --------------------------------------------------------------------------


def test_git_pane_paints_branch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")

    async def body() -> None:
        app = _Harness()
        app.cwd = repo
        async with app.run_test(size=(60, 12)) as pilot:
            # The probe now runs in a worker; wait for it before sampling.
            await app.workers.wait_for_complete()
            await pilot.pause()
            rendered = app.export_screenshot()
            assert "main" in rendered
            assert "clean" in rendered

    asyncio.run(body())


def test_git_pane_refresh_force_reprobes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")

    async def body() -> None:
        app = _Harness()
        app.cwd = repo
        async with app.run_test(size=(60, 12)) as pilot:
            await app.workers.wait_for_complete()  # initial mount probe
            await pilot.pause()
            pane = app.query_one("#gp", GitPane)
            (repo / "dirty.txt").write_text("y\n", encoding="utf-8")
            pane.refresh_git(force=True)
            await app.workers.wait_for_complete()  # the forced re-probe worker
            await pilot.pause()
            assert "changed" in app.export_screenshot()

    asyncio.run(body())


# --------------------------------------------------------------------------
# classify_porcelain — added / removed / changed tally (P30-I02-W09)
# --------------------------------------------------------------------------


def test_classify_porcelain_empty_is_zeros() -> None:
    assert classify_porcelain("") == (0, 0, 0)


def test_classify_porcelain_counts_each_code() -> None:
    porcelain = "\n".join(
        [
            "?? new.txt",  # untracked -> added
            "A  staged.txt",  # staged-new -> added
            " D gone.txt",  # deleted -> removed
            " M edited.txt",  # modified -> changed
            "R  old.txt -> renamed.txt",  # renamed -> changed
        ]
    )
    added, removed, changed = classify_porcelain(porcelain)
    assert (added, removed, changed) == (2, 1, 2)


def test_classify_porcelain_deletion_dominates_modification() -> None:
    """A line carrying ``D`` counts as removed even alongside another code."""
    added, removed, changed = classify_porcelain("MD both.txt\n")
    assert (added, removed, changed) == (0, 1, 0)


def test_classify_porcelain_skips_blank_lines() -> None:
    added, removed, changed = classify_porcelain(" M one.txt\n\n M two.txt\n")
    assert (added, removed, changed) == (0, 0, 2)


# --------------------------------------------------------------------------
# format_status_markup — tinted segments through the rotated vars
# --------------------------------------------------------------------------


def _dirty_fields(*, added: int, removed: int, changed: int) -> GitFields:
    total = added + removed + changed
    return GitFields(
        branch="main",
        dirty=f"{total} changed",
        added=added,
        removed=removed,
        changed=changed,
        ahead_behind="up-to-date",
        recent_commits=(),
    )


def test_format_status_markup_tints_each_segment_through_rotated_vars() -> None:
    markup = format_status_markup(_dirty_fields(added=2, removed=1, changed=3))
    # The three working-tree segments carry the rotated palette-var tints.
    assert f"[{ADDED_VAR}]+2[/]" in markup
    assert f"[{REMOVED_VAR}]-1[/]" in markup
    assert f"[{CHANGED_VAR}]3 changed[/]" in markup
    # The label keeps the accent tint, matching the other rows.
    assert markup.startswith("[$accent]status:[/]")


def test_format_status_markup_clean_tree_is_untinted_summary() -> None:
    """A clean / no-count tree renders the plain summary, no segment tints."""
    fields = GitFields(
        branch="main",
        dirty="clean",
        added=0,
        removed=0,
        changed=0,
        ahead_behind="up-to-date",
        recent_commits=(),
    )
    markup = format_status_markup(fields)
    assert markup == "[$accent]status:[/]   clean"
    assert ADDED_VAR not in markup
    assert REMOVED_VAR not in markup


# --------------------------------------------------------------------------
# Per-cell truecolor proof — segments resolve through the rotated vars
# --------------------------------------------------------------------------

#: A per-cell foreground map: char -> the set of foreground hexes it carries.
_CellMap = dict[str, set[str | None]]


def _cell_fg_hexes(widget: object) -> _CellMap:
    """Map each rendered char to the set of foreground hexes it carries.

    Reads the truecolor foreground off the compositor's rendered strips so a
    segment's resolved palette-var hue is observable -- the colour-aware
    proof a colourless ``.txt`` golden cannot give (the W01 pattern).
    """
    strips = widget.screen._compositor.render_strips()  # type: ignore[attr-defined]
    out: _CellMap = {}
    for strip in strips:
        for segment in strip._segments:
            style = segment.style
            fg: str | None = None
            if style is not None and style.color is not None:
                trip = style.color.get_truecolor()
                fg = f"#{trip.red:02x}{trip.green:02x}{trip.blue:02x}"
            for char in segment.text:
                out.setdefault(char, set()).add(fg)
    return out


def test_git_pane_status_segments_render_rotated_var_hexes(tmp_path: Path) -> None:
    """A dirty repo's status counts render through the rotated palette vars.

    Mounts the pane over a repo with one added, one deleted, and one
    modified path, then reads the per-cell foreground off the compositor:
    the ``+`` added marker carries the green ``$ok`` hex, the ``-`` removed
    marker the ``$err`` red, and the ``changed`` count the ``$warn`` amber.
    A revert to the pre-reskin palette reds this gate.
    """
    repo = _make_repo(tmp_path / "repo")
    # Stage an add (A), a delete (D), and an unstaged modification (M).
    (repo / "added.txt").write_text("a\n", encoding="utf-8")
    _git(["add", "added.txt"], cwd=repo)
    _git(["rm", "-q", "README.md"], cwd=repo)
    (repo / "added.txt").write_text("a\nb\n", encoding="utf-8")  # keep it dirty/staged

    async def body() -> None:
        app = _Harness()
        app.cwd = repo
        async with app.run_test(size=(60, 12)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            pane = app.query_one("#gp", GitPane)
            cells = _cell_fg_hexes(pane)
            # The signed markers + the "changed" word resolve their var hues.
            assert "+" in cells and _ADDED_HEX in cells["+"]
            assert "-" in cells and _REMOVED_HEX in cells["-"]
            assert "c" in cells and _CHANGED_HEX in cells["c"]

    asyncio.run(body())
