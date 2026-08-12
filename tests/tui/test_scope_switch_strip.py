"""Tests for the w/u scope-switch mode strip + per-scope snapshot goldens.

The workspace and user portfolio scope screens each mount a
:class:`~eawf.surfaces.tui.scopes.user.ScopeSwitchStrip` below their body --
the ``repo r  ·  workspace w  ·  portfolio u`` switch affordance the reskin
mock pins, with the host scope's token accented (the workspace screen
accents ``workspace``; the portfolio screen accents ``portfolio``) and the
other two muted. The strip reuses the footer's
:data:`~eawf.surfaces.tui.widgets.footer.MODE_ROW_SEP` bullet + the
``$accent`` / ``$muted`` brand markup, so it reads as one visual family
with the always-visible footer mode row -- no new glyphs or colours.

Two test bands:

* **Pure render** -- :func:`build_scope_switch_strip` is exercised without
  mounting (the active token is accented, the others muted, the mock order
  is preserved, an unknown label accents nothing).
* **Snapshot goldens** -- one golden per scope pins the reskinned screen
  PLUS its switch affordance in a single settled frame, byte-matched against
  a local ``golden/scope_switch_w06/`` fixture. Regenerate after an
  intentional layout change with::

      EAWF_SNAPSHOT_REGEN=1 EAWF_DAEMONLESS=1 \\
          uv run pytest tests/tui/test_scope_switch_strip.py

Determinism mirrors the snapshot suite: animations settled, the live git
probe neutralized, ``Path.home`` redirected to ``tmp_path``, and the cwd
chdir'd to a fresh non-git temp dir so no real branch / machine path leaks
into a golden. Repo codes are abstract placeholders (ABC / DEF), never
real-looking project names.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.models import State
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.scopes import UserScreen, WorkspaceScreen
from eawf.surfaces.tui.scopes.user import (
    SCOPE_SWITCH_ITEMS,
    ScopeSwitchStrip,
    build_scope_switch_strip,
)
from eawf.surfaces.tui.snapshot import assert_screen_snapshot, capture_screen_text, settle_screen
from eawf.surfaces.tui.widgets.git_pane import GitFields

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"

#: Local golden home for the per-scope reskin + switch-affordance snapshots
#: this wave adds -- a fresh dir local to this test, never the full-app
#: screen-snapshot goldens under ``tests/snapshots/tui/golden/``.
_GOLDEN = Path(__file__).resolve().parent / "golden" / "scope_switch_w06"


def _write_registry(home: Path, repos: dict[str, dict[str, str]]) -> Path:
    """Write a ``registry.json`` under *home*/.eawf and return its path.

    Args:
        home: A ``tmp_path``-rooted fake home directory.
        repos: Mapping of repo code -> entry payload (``code`` / ``path`` /
            optional ``title``).

    Returns:
        The written registry path under ``<home>/.eawf/registry.json``.
    """
    ea_dir = home / ".eawf"
    ea_dir.mkdir(parents=True, exist_ok=True)
    path = ea_dir / "registry.json"
    payload = {"version": "1", "repos": repos}
    path.write_bytes(orjson.dumps(payload))
    return path


def _workspace_state(codes: list[str]) -> State:
    """Return a workspace state seeded with the given abstract repo *codes*."""
    payload = orjson.loads(_WORKSPACE.read_bytes())
    payload["workspace"]["repos"] = {
        code: {
            "code": code,
            "path": f"/abs/path/{code.lower()}",
            "state_urn": f"urn:eawf:v1:repo:{code}",
            "project_code": code,
            "title": f"{code} repo",
            "status": "active",
        }
        for code in codes
    }
    payload["workspace"]["current_repo_code"] = codes[0]
    return State.model_validate(payload)


@pytest.fixture(autouse=True)
def _settle_determinism(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settle animations + neutralize the live git pane for stable captures.

    The same determinism guards the snapshot suite uses:

    * Patch ``constants.TEXTUAL_ANIMATIONS`` to ``"none"`` so the App copies
      the settled level at construction -- no time-driven chrome drifts the
      golden mid-capture.
    * Stub the workspace-table git probe to a deterministic clean tree so the
      live git column never leaks the real branch / drifts run-to-run.
    * Redirect ``Path.home`` to ``tmp_path`` so the user scope's registry
      synthesis reads the seeded fixture, never the operator's real
      ``~/.eawf/registry.json`` (which would leak machine paths).
    * Chdir into a fresh non-git temp dir so any residual git probe resolves
      a deterministic dash rather than this repo's tree.
    """
    import textual.constants as _tc

    monkeypatch.setattr(_tc, "TEXTUAL_ANIMATIONS", "none")
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)


# --------------------------------------------------------------------------
# Pure render -- the strip text + active accent
# --------------------------------------------------------------------------


def test_scope_switch_strip_items_match_mock_order() -> None:
    """The switch items are the mock's repo / workspace / portfolio order."""
    assert SCOPE_SWITCH_ITEMS == (
        ("repo", "r"),
        ("workspace", "w"),
        ("portfolio", "u"),
    )


def test_build_scope_switch_strip_accents_active_token() -> None:
    """The active scope's token wears the bold ``$accent`` span; others muted."""
    strip = build_scope_switch_strip("workspace")
    # The active token is the bold-accent span carrying its label + key.
    assert "[$accent][b]workspace w[/b][/]" in strip
    # The inactive tokens render muted, never bold-accent.
    assert "[$muted]repo r[/]" in strip
    assert "[$muted]portfolio u[/]" in strip
    assert "[$accent][b]repo r" not in strip
    assert "[$accent][b]portfolio u" not in strip


def test_build_scope_switch_strip_portfolio_active() -> None:
    """The portfolio scope accents ``portfolio u`` and mutes the rest."""
    strip = build_scope_switch_strip("portfolio")
    assert "[$accent][b]portfolio u[/b][/]" in strip
    assert "[$muted]repo r[/]" in strip
    assert "[$muted]workspace w[/]" in strip


def test_build_scope_switch_strip_preserves_mock_order() -> None:
    """The three labels render left-to-right in the mock order (repo first)."""
    strip = build_scope_switch_strip("workspace")
    assert strip.index("repo r") < strip.index("workspace w") < strip.index("portfolio u")


def test_build_scope_switch_strip_unknown_label_accents_nothing() -> None:
    """A label naming no scope highlights nothing -- no fabricated active token."""
    strip = build_scope_switch_strip("nope")
    assert "[$accent]" not in strip
    assert strip.count("[$muted]") == len(SCOPE_SWITCH_ITEMS)


def test_scope_switch_strip_widget_exposes_active_label() -> None:
    """The widget records which scope it accents (read without scraping markup)."""
    strip = ScopeSwitchStrip("workspace")
    assert strip.active_label == "workspace"


# --------------------------------------------------------------------------
# Mounted -- both scope footers carry the strip with the host scope accented
# --------------------------------------------------------------------------


def test_workspace_screen_mounts_scope_switch_strip_workspace_active() -> None:
    """The workspace screen mounts the strip accenting its own ``workspace`` token."""

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.state = _workspace_state(["ABC", "DEF"])
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, WorkspaceScreen)
            strip = app.screen.query_one(ScopeSwitchStrip)
            assert strip.active_label == "workspace"
            # The reskinned frame renders the full switch affordance.
            rendered = capture_screen_text(app)
            assert "repo r" in rendered
            assert "workspace w" in rendered
            assert "portfolio u" in rendered

    asyncio.run(body())


def test_user_screen_mounts_scope_switch_strip_portfolio_active(tmp_path: Path) -> None:
    """The user portfolio screen mounts the strip accenting its ``portfolio`` token."""
    _write_registry(tmp_path, {"ABC": {"code": "ABC", "path": "/abs/path/abc"}})

    async def body() -> None:
        app = EaApp(scope="user", state_path=None)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, UserScreen)
            strip = app.screen.query_one(ScopeSwitchStrip)
            assert strip.active_label == "portfolio"
            rendered = capture_screen_text(app)
            assert "repo r" in rendered
            assert "workspace w" in rendered
            assert "portfolio u" in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# Per-scope snapshot goldens -- the reskinned screen + its switch affordance
# --------------------------------------------------------------------------


def test_workspace_scope_switch_snapshot() -> None:
    """The workspace frame pins the reskinned screen + the workspace-active strip."""

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            app.state = _workspace_state(["ABC", "DEF"])
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "workspace_scope_switch.txt")

    asyncio.run(body())


def test_user_scope_switch_snapshot(tmp_path: Path) -> None:
    """The user portfolio frame pins the reskinned screen + the portfolio-active strip."""
    _write_registry(
        tmp_path,
        {
            "ABC": {"code": "ABC", "path": "/abs/path/abc"},
            "DEF": {"code": "DEF", "path": "/abs/path/def"},
        },
    )

    async def body() -> None:
        app = EaApp(scope="user", state_path=None)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "user_scope_switch.txt")

    asyncio.run(body())
