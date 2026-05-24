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
import logging
import sys
from pathlib import Path

import orjson
import pytest
from textual.logging import TextualHandler

from eawf.kernel.state.enums import ScopeKind
from eawf.kernel.state.models import State
from eawf.surfaces.tui.app import (
    BRAND,
    DEFAULT_PROJECT_CODE,
    EaApp,
    Header,
    RepoScreen,
    UserScreen,
    WorkspaceScreen,
    _breadcrumb,
    _restore_root_logging,
    _swap_root_logging_to_textual,
    resolve_scope,
)
from eawf.surfaces.tui.state_binding import StateBinding, StateBindingCallbacks, load_state

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
    # Scope switch is the raw w/r/u keys (the W32 keybinding fix); the
    # ctrl+ chords remain as hidden muscle-memory aliases.
    assert {"w", "r", "u"} <= keys
    assert {"ctrl+r", "ctrl+w", "ctrl+u"} <= keys


def test_eaapp_raw_scope_switch_bindings_target_switch_scope() -> None:
    actions = {b.key: b.action for b in EaApp.BINDINGS}  # type: ignore[union-attr]
    assert actions["w"] == "switch_scope('workspace')"
    assert actions["r"] == "switch_scope('repo')"
    assert actions["u"] == "switch_scope('user')"


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


def test_state_binding_poll_loop_survives_stat_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TOCTOU ``stat()`` failure mid-poll is swallowed; the poll loop survives.

    Simulates the state file vanishing between the ``is_file()`` check and the
    ``stat()`` call inside ``_poll_loop`` (``is_file()`` lies ``True``,
    ``stat()`` raises). Without the guard the loop coroutine would raise and the
    poll task would die; with it the tick is skipped and the task stays alive.
    """

    async def _noop_state(_s: State) -> None:
        return None

    async def _noop_degraded(_d: bool) -> None:
        return None

    async def body() -> None:
        binder = StateBinding(
            state_path=_EMPTY_REPO,
            callbacks=StateBindingCallbacks(on_state=_noop_state, on_degraded=_noop_degraded),
            poll_interval_s=0.01,
        )
        await binder.connect()
        # TOCTOU: is_file() sees the file but the subsequent stat() loses it.
        monkeypatch.setattr(Path, "is_file", lambda _self: True)

        def _boom_stat(_self: Path, *_a: object, **_k: object) -> object:
            raise FileNotFoundError("state.json vanished after is_file()")

        monkeypatch.setattr(Path, "stat", _boom_stat)
        await asyncio.sleep(0.05)  # several poll ticks under the failing stat()
        assert binder._poll_task is not None
        assert not binder._poll_task.done()  # the loop swallowed the OSError
        await binder.disconnect()

    asyncio.run(body())


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


# --------------------------------------------------------------------------
# Root-logging swap — no stderr handler bleeds onto the live TUI screen
# --------------------------------------------------------------------------


def _has_terminal_stream_handler() -> bool:
    """Return ``True`` when a root handler still writes to stderr/stdout."""
    root = logging.getLogger()
    return any(
        isinstance(h, logging.StreamHandler) and h.stream in (sys.stderr, sys.stdout)
        for h in root.handlers
    )


@pytest.fixture
def _isolated_root_logging() -> object:
    """Save + restore the real root handler list around a swap test."""
    root = logging.getLogger()
    original = list(root.handlers)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in original:
        root.addHandler(handler)


def test_swap_root_logging_removes_stderr_handler(_isolated_root_logging: object) -> None:
    """The swap detaches the stderr StreamHandler and installs a TextualHandler."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    root.addHandler(stderr_handler)
    assert _has_terminal_stream_handler()  # precondition: the leak is present

    _swap_root_logging_to_textual()

    assert not _has_terminal_stream_handler()  # no handler writes to the screen
    assert any(isinstance(h, TextualHandler) for h in root.handlers)


def test_swap_root_logging_also_detaches_stdout(_isolated_root_logging: object) -> None:
    """A stdout-targeting StreamHandler is detached too (both terminal streams)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(logging.StreamHandler(stream=sys.stdout))

    _swap_root_logging_to_textual()

    assert not _has_terminal_stream_handler()
    assert any(isinstance(h, TextualHandler) for h in root.handlers)


def test_restore_root_logging_reinstates_prior_handlers(
    _isolated_root_logging: object,
) -> None:
    """Restore reinstates the exact handler list captured before the swap."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    root.addHandler(stderr_handler)

    saved = _swap_root_logging_to_textual()
    assert not _has_terminal_stream_handler()  # swapped out for the run

    _restore_root_logging(saved)

    assert root.handlers == [stderr_handler]  # exact prior list back
    assert _has_terminal_stream_handler()  # scrubbed stderr sink restored
    assert not any(isinstance(h, TextualHandler) for h in root.handlers)
