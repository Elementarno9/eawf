"""Tests for the Textual MODES chassis (P29-I02-W16, TUI-1).

The TUI runs on Textual's native :attr:`textual.app.App.MODES` +
``switch_mode``: a **mode** is a content surface (Home / Trust / Doctor /
Evidence / Feed / Config) switched with digit keys ``1``..``6``, declared
once in :mod:`eawf.surfaces.tui.modes.registry` -- the one seam the nine
per-pane waves extend. These tests pin the chassis the pane waves build
on:

* the registry composes ``App.MODES``, the digit bindings, and the
  ``/<mode>`` palette verbs from one declarative source (the pure-unit
  half, testable without Textual);
* the app boots into the default (Home) mode; digit ``1``..``6`` switch
  modes; ``switch_mode`` no-ops when already in the mode; a palette verb
  switches mode;
* the breadcrumb leads with the active mode title and keeps the ``Eae``
  brand outside-left;
* the scope switch (``w`` / ``r`` / ``u``) stays an in-mode operation
  (mode and scope are orthogonal axes);
* switching mode away from a zoomed workspace and back preserves the zoom
  (the W15 guard stays intact through the mode-switch suspend);
* a placeholder mode renders an honest-empty ``<title> - coming soon``
  body so all six digit keys work before the pane waves land.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` (``pilot.pause()``
is CPU-idle-based, not worker-aware) before asserting. The autouse
``_isolate_registry`` fixture redirects ``Path.home`` so a ``u`` switch
never reads the operator's real registry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes import PlaceholderModeScreen
from eawf.surfaces.tui.modes.registry import (
    DEFAULT_MODE,
    MODE_REGISTRY,
    build_modes,
    mode_bindings,
    mode_for_name,
    mode_title,
)
from eawf.surfaces.tui.modes.trust import TrustModeScreen
from eawf.surfaces.tui.scopes import RepoScreen, UserScreen, WorkspaceScreen
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.header import BRAND
from eawf.surfaces.tui.widgets.workspace_table import WorkspaceTable

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_REPO = _FIXTURES / "03-phase-iter-wave-active.json"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"
#: The single repo code seeded in the workspace fixture's repo index.
_FIXTURE_REPO = "QR"

#: The expected default six-mode layout (name, digit, title) seeded on the
#: chassis. Pinned here so a registry reshape that breaks the digit axis or
#: the launch default is caught loudly.
_EXPECTED_MODES: tuple[tuple[str, str, str], ...] = (
    ("home", "1", "Home"),
    ("trust", "2", "Trust"),
    ("doctor", "3", "Doctor"),
    ("evidence", "4", "Evidence"),
    ("feed", "5", "Feed"),
    ("config", "6", "Config"),
)


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate registry + probe-cache writes into ``tmp_path``.

    The ``u`` scope switch reads ``~/.eawf/registry.json``; redirecting
    ``Path.home`` keeps the switch deterministic and reads no real
    registry. The Doctor mode (digit ``3``) runs the instrument probe on
    mount, which writes a cache to ``<workspace>/.ea/instrument-probe.json``
    -- the workspace resolves to the fixture tree, so redirect the cache
    into ``tmp_path`` to keep a stray probe file out of ``tests/fixtures/``.
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
# Registry -- pure-unit composition (no Textual mount)
# --------------------------------------------------------------------------


def test_mode_registry_seeds_the_default_six_mode_layout() -> None:
    """The registry declares the six (name, digit, title) modes in order."""
    got = tuple((spec.name, spec.digit, spec.title) for spec in MODE_REGISTRY)
    assert got == _EXPECTED_MODES


def test_default_mode_is_home() -> None:
    """The launch default is the scope-bearing Home mode (digit 1)."""
    assert DEFAULT_MODE == "home"
    assert MODE_REGISTRY[0].name == DEFAULT_MODE


def test_mode_bindings_compose_digit_switch_per_mode() -> None:
    """``mode_bindings`` yields one ``<digit> switch_mode('<name>')`` per mode."""
    bindings = mode_bindings()
    assert [b.key for b in bindings] == [spec.digit for spec in MODE_REGISTRY]
    assert [b.action for b in bindings] == [f"switch_mode({spec.name!r})" for spec in MODE_REGISTRY]
    # Digits are the mode axis only -- hidden from the footer (arrows primary).
    assert all(b.show is False for b in bindings)


def test_build_modes_maps_each_name_to_a_zero_arg_factory() -> None:
    """``build_modes`` returns ``{name: () -> Screen | str}`` for every mode."""
    app = EaApp(scope="repo", state_path=_REPO)
    modes = build_modes(app)
    assert sorted(modes) == sorted(spec.name for spec in MODE_REGISTRY)
    # Home resolves to the cached scope-screen NAME (so switch_screen reuses
    # the same instance); trust builds its real pane; an unbuilt mode
    # (config) still builds a PlaceholderModeScreen.
    assert modes["home"]() == "repo"
    assert isinstance(modes["trust"](), TrustModeScreen)
    assert isinstance(modes["config"](), PlaceholderModeScreen)


def test_build_modes_home_factory_tracks_resolved_scope() -> None:
    """The Home factory returns the screen name for the app's resolved scope."""
    for scope, expected in (("repo", "repo"), ("workspace", "workspace"), ("user", "user")):
        app = EaApp(scope=scope, state_path=_REPO)  # type: ignore[arg-type]
        assert build_modes(app)["home"]() == expected


def test_mode_title_resolves_registered_else_passthrough() -> None:
    """``mode_title`` returns the title for a known mode, else the name itself."""
    assert mode_title("home") == "Home"
    assert mode_title("trust") == "Trust"
    assert mode_title("unregistered") == "unregistered"


def test_mode_for_name_resolves_or_none() -> None:
    """``mode_for_name`` resolves a registered spec, else ``None``."""
    spec = mode_for_name("doctor")
    assert spec is not None
    assert (spec.digit, spec.title) == ("3", "Doctor")
    assert mode_for_name("nope") is None


def test_eaapp_class_wires_modes_and_default_mode() -> None:
    """``EaApp`` exposes the digit bindings + overrides ``DEFAULT_MODE``."""
    digit_bindings = [
        b for b in EaApp.BINDINGS if getattr(b, "key", None) in {s.digit for s in MODE_REGISTRY}
    ]
    assert {b.key for b in digit_bindings} == {s.digit for s in MODE_REGISTRY}
    assert EaApp.DEFAULT_MODE == DEFAULT_MODE


# --------------------------------------------------------------------------
# Palette -- one /<mode> verb per (non-colliding) mode
# --------------------------------------------------------------------------


def test_palette_exposes_a_switch_verb_per_non_colliding_mode() -> None:
    """Each mode gets a ``/<name>`` verb except where a non-mode verb claims it."""
    from eawf.surfaces.tui.palette.verbs import VERBS, visible_verbs

    names = {verb.name for verb in VERBS}
    # home / trust / doctor / evidence / feed get their /<mode> verb.
    for stem in ("home", "trust", "doctor", "evidence", "feed"):
        assert f"/{stem}" in names
    # /config keeps its pre-existing config-window meaning (the config MODE
    # collides and is reachable via digit 6 instead).
    config_verbs = [v for v in VERBS if v.name == "/config"]
    assert len(config_verbs) == 1
    assert "config window" in config_verbs[0].hint
    # The mode verbs are offered on every scope.
    repo_verbs = {v.name for v in visible_verbs("repo")}
    assert {"/home", "/trust", "/feed"} <= repo_verbs


# --------------------------------------------------------------------------
# Chassis -- Pilot-driven mode switching
# --------------------------------------------------------------------------


def test_app_boots_into_the_default_home_mode() -> None:
    """The chassis launches into Home; its base screen is the resolved scope."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert app.current_mode == "home"
            assert isinstance(app.screen, RepoScreen)

    asyncio.run(body())


def test_digit_keys_switch_modes() -> None:
    """Digit ``1``..``6`` switch ``current_mode`` to each registered mode."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            for spec in MODE_REGISTRY:
                await pilot.press(spec.digit)
                await settle_screen(pilot)
                assert app.current_mode == spec.name

    asyncio.run(body())


def test_switch_mode_no_ops_when_already_in_mode() -> None:
    """A repeat digit press keeps the same screen (``switch_mode`` no-ops)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("2")  # -> trust
            await settle_screen(pilot)
            screen_before = app.screen
            assert app.current_mode == "trust"
            await pilot.press("2")  # already in trust
            await settle_screen(pilot)
            assert app.current_mode == "trust"
            assert app.screen is screen_before

    asyncio.run(body())


def test_palette_verb_switches_mode() -> None:
    """The ``/trust`` palette verb switches the active mode to ``trust``."""

    async def body() -> None:
        from eawf.surfaces.tui.palette.verbs import VERBS

        verb = next(v for v in VERBS if v.name == "/trust")
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert app.current_mode == "home"
            verb.handler(app, "")
            await settle_screen(pilot)
            assert app.current_mode == "trust"
            assert isinstance(app.screen, TrustModeScreen)

    asyncio.run(body())


def test_breadcrumb_leads_with_mode_and_keeps_brand_outside_left() -> None:
    """The header row is ``Eae  <Mode> > <scope> > ...`` -- brand outside-left."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            header_row = normalize_snapshot(capture_screen_text(app)).splitlines()[0]
            # Brand is outside-left of the breadcrumb; the mode title leads it.
            assert BRAND in header_row
            assert header_row.index(BRAND) < header_row.index("Home")
            assert header_row.index("Home") < header_row.index("repo")
            # Switching modes repaints the breadcrumb's leading segment.
            await pilot.press("2")  # -> trust
            await settle_screen(pilot)
            trust_row = normalize_snapshot(capture_screen_text(app)).splitlines()[0]
            assert BRAND in trust_row
            assert "Trust" in trust_row
            assert "Home" not in trust_row

    asyncio.run(body())


def test_scope_switch_stays_in_mode_orthogonal_axes() -> None:
    """``w`` / ``r`` / ``u`` switch scope within the active mode (no mode change)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert app.current_mode == "home"
            assert isinstance(app.screen, RepoScreen)
            # Scope switch within Home: the mode is unchanged, the screen swaps.
            await pilot.press("w")
            await settle_screen(pilot)
            assert app.current_mode == "home"
            assert isinstance(app.screen, WorkspaceScreen)
            assert app._scope == "workspace"
            await pilot.press("u")
            await settle_screen(pilot)
            assert app.current_mode == "home"
            # The empty test registry auto-opens the init wizard over the user
            # scope; dismiss it to reach the underlying scope screen.
            if app.screen.__class__.__name__ == "InitWizardModal":
                await pilot.press("escape")
                await settle_screen(pilot)
            assert app.current_mode == "home"
            assert isinstance(app.screen, UserScreen)
            assert app._scope == "user"

    asyncio.run(body())


def test_scope_survives_a_mode_round_trip() -> None:
    """A mode flip away from a non-default scope and back preserves the scope.

    Mode and scope are orthogonal: the active scope lives on the Home
    mode's own screen stack, so switching to another mode and back returns
    to the same cached scope screen.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("w")  # Home/workspace
            await settle_screen(pilot)
            home_screen = app.screen
            assert isinstance(home_screen, WorkspaceScreen)
            await pilot.press("3")  # -> doctor (a real pane, orthogonal to scope)
            await settle_screen(pilot)
            assert app.current_mode == "doctor"
            await pilot.press("1")  # back to Home
            await settle_screen(pilot)
            assert app.current_mode == "home"
            assert app.screen is home_screen  # same cached scope screen
            assert app._scope == "workspace"

    asyncio.run(body())


def test_mode_switch_away_from_zoomed_workspace_and_back_preserves_zoom() -> None:
    """The W15 zoom guard survives a mode-switch suspend, not just a modal push.

    Zooming a workspace repo, switching to another mode (a ``switch_mode``
    suspend on the still-stacked workspace screen), then switching back must
    rebuild the same quadrant -- the orthogonal mode axis must not tear the
    operator out of their zoom. This pins the
    :meth:`~eawf.surfaces.tui.scopes._zoom.RepoZoomMixin._suspend_is_transient`
    extension that treats a mode switch as transient (the screen stays on
    its own mode's stack even after the current-mode pointer moves).
    """

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            await screen.on_workspace_table_row_zoomed(WorkspaceTable.RowZoomed(_FIXTURE_REPO))
            await settle_screen(pilot)
            assert screen.zoomed
            assert screen._zoomed_code == _FIXTURE_REPO
            # Switch to another mode (Doctor), then back to Home.
            await pilot.press("3")
            await settle_screen(pilot)
            assert app.current_mode == "doctor"
            await pilot.press("1")
            await settle_screen(pilot)
            back = app.screen
            assert isinstance(back, WorkspaceScreen)
            assert back is screen  # cached scope screen reused
            assert back.zoomed  # zoom rebuilt across the mode round-trip
            assert back._zoomed_code == _FIXTURE_REPO
            assert len(back.query("#zoom-quadrant")) == 1

    asyncio.run(body())


# --------------------------------------------------------------------------
# Placeholder mode -- honest-empty body
# --------------------------------------------------------------------------


def test_placeholder_mode_renders_honest_empty_coming_soon() -> None:
    """A mode whose pane wave has not landed renders ``<title> - coming soon``."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")  # -> config (still a placeholder)
            await settle_screen(pilot)
            assert isinstance(app.screen, PlaceholderModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "Config - coming soon" in frame
            # The placeholder keeps the shared chassis: brand + breadcrumb.
            assert BRAND in frame.splitlines()[0]

    asyncio.run(body())


def test_every_placeholder_mode_boots_and_titles_itself() -> None:
    """Each still-unbuilt placeholder mode renders its own coming-soon title.

    Drives only the modes whose factory still produces a
    :class:`PlaceholderModeScreen`, derived from the registry so a pane
    wave that fills a mode (Home, Doctor, ...) drops out of the set
    automatically rather than failing this assertion.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            placeholder_specs = [
                spec
                for spec in MODE_REGISTRY
                if isinstance(spec.factory(app), PlaceholderModeScreen)
            ]
            # At least one mode is still an unbuilt placeholder this band.
            assert placeholder_specs
            for spec in placeholder_specs:
                await pilot.press(spec.digit)
                await settle_screen(pilot)
                assert isinstance(app.screen, PlaceholderModeScreen)
                frame = normalize_snapshot(capture_screen_text(app))
                assert f"{spec.title} - coming soon" in frame

    asyncio.run(body())
