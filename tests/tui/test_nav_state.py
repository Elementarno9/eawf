"""Tests for the bound SCOPE x MODE navigation state machine (P29-I02-W17).

The TUI runs two orthogonal axes -- a **scope** (``repo`` / ``workspace`` /
``user``, switched with ``w`` / ``r`` / ``u``) and a **mode** (``home`` /
``trust`` / ``doctor`` / ``evidence`` / ``feed`` / ``config`` /
``research_board``, switched with digit keys ``1``..``7``). W16 left every
``(scope, mode)`` pair reachable;
this wave bounds the genuinely-legal subset and rejects the rest **at the
boundary** so a switch never lands in a sourceless view. These tests pin:

* the pure validator (:func:`is_legal_position` + :class:`NavState`): the
  legal matrix, an accepted transition, and an illegal ``(scope, mode)``
  rejected with the current position preserved (testable without Textual);
* the app-level boundary: a digit / palette mode switch into an illegal
  mode is rejected (toast + no-op, no crash) and a scope switch into an
  illegal scope is likewise rejected, while every legal combo still flips;
* the breadcrumb renders the **bound** nav position.

Determinism follows the Pilot-worker rule: each Pilot body drains workers
via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting. The
autouse ``_isolate_registry`` fixture redirects ``Path.home`` so a ``u``
switch never reads the operator's real registry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.nav import (
    NAV_SCOPES,
    NavPosition,
    NavState,
    NavTransition,
    is_legal_position,
    legal_scopes_for_mode,
)
from eawf.surfaces.tui.modes.registry import MODE_REGISTRY
from eawf.surfaces.tui.scopes import RepoScreen, UserScreen
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.header import BRAND

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_REPO = _FIXTURES / "03-phase-iter-wave-active.json"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"

#: The modes whose pane reads a single scope's state -- illegal at the
#: cross-repo ``user`` portfolio scope (no single repo state to read).
_SCOPE_BOUND = ("trust", "evidence", "feed", "research_board")
#: The scope-agnostic modes -- legal at every scope.
_SCOPE_FREE = ("home", "doctor", "config")


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate registry + probe-cache writes into ``tmp_path``.

    The ``u`` scope switch reads ``~/.eawf/registry.json``; redirecting
    ``Path.home`` keeps the switch deterministic. The Doctor mode runs the
    instrument probe on mount, which writes a cache under the workspace --
    redirect it into ``tmp_path`` to keep a stray file out of fixtures.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(tmp_path / "instrument-probe.json"))


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the workspace git probe to a deterministic clean tree."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


# --------------------------------------------------------------------------
# Validator -- pure-unit (no Textual mount)
# --------------------------------------------------------------------------


def test_is_legal_position_allows_scope_free_modes_everywhere() -> None:
    """The scope-agnostic modes are legal at every scope."""
    for mode in _SCOPE_FREE:
        for scope in NAV_SCOPES:
            assert is_legal_position(scope, mode), f"{mode} should be legal at {scope}"


def test_is_legal_position_allows_scope_bound_modes_at_repo_and_workspace() -> None:
    """The single-scope data modes are legal at repo + workspace."""
    for mode in _SCOPE_BOUND:
        assert is_legal_position("repo", mode)
        assert is_legal_position("workspace", mode)


def test_is_legal_position_rejects_scope_bound_modes_at_user() -> None:
    """The single-scope data modes are illegal at the user portfolio scope."""
    for mode in _SCOPE_BOUND:
        assert not is_legal_position("user", mode), f"{mode} must be illegal at user"


def test_is_legal_position_covers_every_registered_mode() -> None:
    """Every registered mode is either scope-free or one of the scope-bound."""
    registered = {spec.name for spec in MODE_REGISTRY}
    assert registered == set(_SCOPE_FREE) | set(_SCOPE_BOUND)


def test_legal_scopes_for_mode_filters_by_matrix() -> None:
    """``legal_scopes_for_mode`` returns the matrix row for a mode, in order."""
    assert legal_scopes_for_mode("home") == NAV_SCOPES
    assert legal_scopes_for_mode("doctor") == NAV_SCOPES
    assert legal_scopes_for_mode("trust") == ("repo", "workspace")
    assert legal_scopes_for_mode("feed") == ("repo", "workspace")


def test_nav_position_is_legal_reports_pair_legality() -> None:
    """``NavPosition.is_legal`` mirrors :func:`is_legal_position`."""
    assert NavPosition(scope="repo", mode="trust").is_legal
    assert not NavPosition(scope="user", mode="trust").is_legal


def test_nav_state_initial_rejects_illegal_launch_position() -> None:
    """A launch position must be reachable; an illegal pair raises."""
    assert NavState.initial("user", "home").position == NavPosition(scope="user", mode="home")
    with pytest.raises(ValueError, match="illegal launch position"):
        NavState.initial("user", "trust")


def test_resolve_mode_accepts_a_legal_target() -> None:
    """A legal mode switch is accepted and advances the position."""
    nav = NavState.initial("repo", "home")
    transition = nav.resolve_mode("trust")
    assert transition.accepted
    assert transition.reason == ""
    assert transition.position == NavPosition(scope="repo", mode="trust")


def test_resolve_mode_rejects_an_illegal_target_and_preserves_position() -> None:
    """An illegal mode switch is rejected with the current position kept."""
    nav = NavState.initial("user", "home")
    transition = nav.resolve_mode("trust")
    assert not transition.accepted
    assert "trust" in transition.reason
    assert "user" in transition.reason
    # Position is unchanged so the app can no-op against it.
    assert transition.position == nav.position


def test_resolve_scope_accepts_a_legal_target() -> None:
    """A legal scope switch (keeping the current mode) is accepted."""
    nav = NavState.initial("repo", "trust")
    transition = nav.resolve_scope("workspace")
    assert transition.accepted
    assert transition.position == NavPosition(scope="workspace", mode="trust")


def test_resolve_scope_rejects_an_illegal_target_and_preserves_position() -> None:
    """A scope switch that makes the current mode illegal is rejected."""
    nav = NavState.initial("repo", "trust")
    transition = nav.resolve_scope("user")
    assert not transition.accepted
    assert "trust" in transition.reason
    assert "user" in transition.reason
    assert transition.position == nav.position


def test_resolve_mode_returns_named_transition_type() -> None:
    """The resolver yields a typed :class:`NavTransition` (not a bare tuple)."""
    transition = NavState.initial("repo", "home").resolve_mode("doctor")
    assert isinstance(transition, NavTransition)


# --------------------------------------------------------------------------
# App boundary -- Pilot-driven rejection (toast + no-op, no crash)
# --------------------------------------------------------------------------


def test_app_nav_position_seeds_launch_scope_and_default_mode() -> None:
    """The app boots with the bound nav position at (launch scope, home)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert app.nav_position == NavPosition(scope="repo", mode="home")

    asyncio.run(body())


def test_switch_mode_rejects_illegal_mode_at_user_scope_with_toast() -> None:
    """A digit switch into trust at the user scope is rejected (toast + no-op)."""

    async def body() -> None:
        notices: list[tuple[str, str | None]] = []
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            # The empty test registry auto-opens the init wizard; dismiss it.
            if app.screen.__class__.__name__ == "InitWizardModal":
                await pilot.press("escape")
                await settle_screen(pilot)
            assert app.nav_position == NavPosition(scope="user", mode="home")
            app.notify = lambda message, *_a, **kw: notices.append(  # type: ignore[method-assign]
                (message, kw.get("severity"))
            )
            await pilot.press("2")  # -> trust (illegal at user)
            await settle_screen(pilot)
            # No-op: the mode stays home, the position is unchanged, no crash.
            assert app.current_mode == "home"
            assert app.nav_position == NavPosition(scope="user", mode="home")
            assert isinstance(app.screen, UserScreen)
            assert notices, "an illegal mode switch must toast"
            message, severity = notices[-1]
            assert severity == "warning"
            assert "trust" in message

    asyncio.run(body())


def test_switch_mode_accepts_legal_mode_at_user_scope() -> None:
    """A scope-free mode (doctor) is reachable at the user scope."""

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            if app.screen.__class__.__name__ == "InitWizardModal":
                await pilot.press("escape")
                await settle_screen(pilot)
            await pilot.press("3")  # -> doctor (scope-free, legal everywhere)
            await settle_screen(pilot)
            assert app.current_mode == "doctor"
            assert app.nav_position == NavPosition(scope="user", mode="doctor")

    asyncio.run(body())


def test_switch_scope_rejects_user_when_current_mode_is_scope_bound() -> None:
    """A ``u`` scope switch while in trust mode is rejected (toast + no-op)."""

    async def body() -> None:
        notices: list[tuple[str, str | None]] = []
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("2")  # -> trust (legal at repo)
            await settle_screen(pilot)
            assert app.nav_position == NavPosition(scope="repo", mode="trust")
            app.notify = lambda message, *_a, **kw: notices.append(  # type: ignore[method-assign]
                (message, kw.get("severity"))
            )
            await pilot.press("u")  # user scope -> illegal with trust mode
            await settle_screen(pilot)
            # No-op: still repo/trust, no scope swap, no crash.
            assert app._scope == "repo"
            assert app.current_mode == "trust"
            assert app.nav_position == NavPosition(scope="repo", mode="trust")
            assert notices, "an illegal scope switch must toast"
            _message, severity = notices[-1]
            assert severity == "warning"

    asyncio.run(body())


def test_switch_scope_within_a_scope_free_mode_stays_orthogonal() -> None:
    """W16 orthogonality holds where legal: home-mode scope switch is free."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert app.current_mode == "home"
            await pilot.press("u")  # home is scope-free -> user is legal
            await settle_screen(pilot)
            if app.screen.__class__.__name__ == "InitWizardModal":
                await pilot.press("escape")
                await settle_screen(pilot)
            assert app.current_mode == "home"
            assert app._scope == "user"
            assert app.nav_position == NavPosition(scope="user", mode="home")
            assert isinstance(app.screen, UserScreen)

    asyncio.run(body())


def test_legal_mode_switch_at_repo_scope_flips_mode() -> None:
    """A legal digit switch at the repo scope still flips the bound mode."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert isinstance(app.screen, RepoScreen)
            for spec in MODE_REGISTRY:
                await pilot.press(spec.digit)
                await settle_screen(pilot)
                assert app.current_mode == spec.name
                assert app.nav_position == NavPosition(scope="repo", mode=spec.name)

    asyncio.run(body())


def test_breadcrumb_reflects_the_bound_nav_position() -> None:
    """The header row renders the bound ``(mode, scope)`` nav position."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            row = normalize_snapshot(capture_screen_text(app)).splitlines()[0]
            # Brand outside-left; the bound mode (Home) leads the bound scope.
            assert BRAND in row
            assert row.index(BRAND) < row.index("Home") < row.index("repo")
            # Flip to a legal mode; the breadcrumb tracks the bound position.
            await pilot.press("2")  # -> trust
            await settle_screen(pilot)
            assert app.nav_position == NavPosition(scope="repo", mode="trust")
            trust_row = normalize_snapshot(capture_screen_text(app)).splitlines()[0]
            assert "Trust" in trust_row
            assert "Home" not in trust_row

    asyncio.run(body())


def test_rejected_switch_leaves_breadcrumb_on_prior_position() -> None:
    """A rejected mode switch does not move the breadcrumb's mode segment."""

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            if app.screen.__class__.__name__ == "InitWizardModal":
                await pilot.press("escape")
                await settle_screen(pilot)
            await pilot.press("2")  # -> trust, rejected at user scope
            await settle_screen(pilot)
            row = normalize_snapshot(capture_screen_text(app)).splitlines()[0]
            # Breadcrumb still leads with Home (the unchanged bound mode).
            assert "Home" in row
            assert "Trust" not in row

    asyncio.run(body())
