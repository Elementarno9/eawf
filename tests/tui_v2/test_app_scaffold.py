"""Smoke tests for the C06 Textual ``EaApp`` scaffold (P26-W16).

Covers the scaffold contract this wave establishes: read-only state
load, scope-name resolution, breadcrumb rendering, ``EaApp``
construction per scope, and a Pilot-driven first-paint that confirms the
``Eä`` brand reaches the rendered screen. The concrete per-scope
compositions land in later waves; these tests pin the shell so those
waves have a stable base.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest

from eawf.state.enums import ScopeKind
from eawf.state.models import State
from eawf.tui_v2.app import (
    BRAND,
    DEFAULT_PROJECT_CODE,
    EaApp,
    Header,
    RepoScreen,
    UserScreen,
    WorkspaceScreen,
    _breadcrumb,
    resolve_scope,
)
from eawf.tui_v2.state_binding import StateBinding, StateBindingCallbacks, load_state

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_EMPTY_REPO = _FIXTURES / "01-empty-repo.json"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"


def _load_fixture(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


# --------------------------------------------------------------------------
# load_state — read-only, error-tolerant
# --------------------------------------------------------------------------


def test_load_state_none_path_returns_none() -> None:
    assert load_state(None) is None


def test_load_state_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_state(tmp_path / "absent" / "state.json") is None


def test_load_state_corrupt_json_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "state.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_state(bad) is None


def test_load_state_schema_mismatch_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "state.json"
    bad.write_text(orjson.dumps({"schema_version": "1.0"}).decode(), encoding="utf-8")
    assert load_state(bad) is None


def test_load_state_valid_repo_fixture() -> None:
    state = load_state(_EMPTY_REPO)
    assert state is not None
    assert state.scope_kind is ScopeKind.REPO


def test_load_state_does_not_mutate_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(_EMPTY_REPO.read_bytes())
    before = target.read_bytes()
    load_state(target)
    assert target.read_bytes() == before


# --------------------------------------------------------------------------
# resolve_scope — scope_kind -> EaApp scope name
# --------------------------------------------------------------------------


def test_resolve_scope_repo() -> None:
    assert resolve_scope(ScopeKind.REPO) == "repo"


def test_resolve_scope_workspace() -> None:
    assert resolve_scope(ScopeKind.WORKSPACE) == "workspace"


# --------------------------------------------------------------------------
# _breadcrumb — brand-less crumb with sane fallback
# --------------------------------------------------------------------------


def test_breadcrumb_none_state_falls_back_to_default_code() -> None:
    assert _breadcrumb(None) == DEFAULT_PROJECT_CODE


def test_breadcrumb_repo_fixture_includes_scope_and_code() -> None:
    crumb = _breadcrumb(_load_fixture(_EMPTY_REPO))
    assert "repo" in crumb
    assert "QR" in crumb


def test_breadcrumb_workspace_fixture_uses_default_code_when_no_project() -> None:
    crumb = _breadcrumb(_load_fixture(_WORKSPACE))
    assert "workspace" in crumb
    assert DEFAULT_PROJECT_CODE in crumb


# --------------------------------------------------------------------------
# EaApp construction — per scope
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "screen_cls"),
    [
        ("repo", RepoScreen),
        ("workspace", WorkspaceScreen),
        ("user", UserScreen),
    ],
)
def test_eaapp_constructs_per_scope(scope: str, screen_cls: type) -> None:
    app = EaApp(scope=scope, state_path=None)  # type: ignore[arg-type]
    assert app._scope == scope
    assert app.SCREENS[scope] is screen_cls


def test_eaapp_css_path_is_tcss() -> None:
    assert EaApp.CSS_PATH == "theme.tcss"


def test_eaapp_arrow_and_vim_bindings_present() -> None:
    keys = {b.key for b in EaApp.BINDINGS}  # type: ignore[union-attr]
    # Vim aliases declared app-wide; arrow keys are bound per-screen but
    # the scope-switch + quit chords live here.
    assert {"h", "j", "k", "l"} <= keys
    assert "ctrl+r" in keys and "ctrl+w" in keys and "ctrl+u" in keys


# --------------------------------------------------------------------------
# StateBinding — read-only initial load via callbacks
# --------------------------------------------------------------------------


def test_state_binding_connect_pushes_initial_state_and_degraded() -> None:
    seen_state: list[State] = []
    seen_degraded: list[bool] = []

    async def on_state(s: State) -> None:
        seen_state.append(s)

    async def on_degraded(d: bool) -> None:
        seen_degraded.append(d)

    async def body() -> None:
        binder = StateBinding(
            state_path=_EMPTY_REPO,
            callbacks=StateBindingCallbacks(on_state=on_state, on_degraded=on_degraded),
            poll_interval_s=0.01,
        )
        await binder.connect()
        await binder.disconnect()

    asyncio.run(body())
    assert len(seen_state) == 1
    assert seen_state[0].scope_kind is ScopeKind.REPO
    # Fallback leg active until the daemon-push leg is wired.
    assert seen_degraded == [True]


# --------------------------------------------------------------------------
# Pilot first-paint smoke — confirms the shell renders the brand
# --------------------------------------------------------------------------


def test_eaapp_first_paint_renders_brand() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_EMPTY_REPO)
        async with app.run_test() as pilot:
            await pilot.pause()
            # The real RepoScreen now composes the shared chassis Header.
            header = app.screen.query_one(Header)
            assert BRAND in str(header.render())
            # SVG screenshot is a true end-to-end paint proof.
            assert BRAND in app.export_screenshot()

    asyncio.run(body())
