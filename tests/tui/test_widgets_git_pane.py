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

from eawf.surfaces.tui.widgets.git_pane import (
    DASH,
    GitFields,
    GitPane,
    format_git_lines,
    gather_git_fields,
)

from ._palette_harness import PaletteHarnessApp

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"


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
    fields = GitFields(branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=())
    lines = format_git_lines(fields)
    assert "branch:   main" in lines
    assert "status:   clean" in lines
    assert "upstream: up-to-date" in lines
    assert not any(line == "recent:" for line in lines)


def test_format_git_lines_with_recent_commits() -> None:
    fields = GitFields(
        branch="feature/x",
        dirty="2 changed",
        ahead_behind="+1 / -0",
        recent_commits=("abc123 first", "def456 second"),
    )
    lines = format_git_lines(fields)
    assert "recent:" in lines
    assert "  abc123 first" in lines
    assert "  def456 second" in lines


def test_format_git_lines_all_dashes() -> None:
    fields = GitFields(branch=DASH, dirty=DASH, ahead_behind=DASH, recent_commits=())
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
