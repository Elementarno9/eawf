"""``eawf ui``'s authority switch: which app opens, ``--verbose``, and SURF-085's exit 4.

Pins the launch contract at :func:`eawf.surfaces.tui.launch.launch_tui`, the library
:func:`eawf.surfaces.cli.app._dispatch_tui` delegates to: an epoch-2 tree opens the
native console over a live seam, an epoch-1 tree keeps the classic ``EaApp``, and a
canary tree stuck mid-migration exits 4 off a TTY rather than opening either. No test
here starts a live daemon or an interactive Textual run -- construction is verified by
monkeypatching the one function (:func:`eawf.surfaces.tui.launch._run_console`) that
would otherwise drive a real event loop.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

import pytest

import eawf.surfaces.cli.app as cli_app
import eawf.surfaces.tui.launch as launch
from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    GENERATIONS_DIRNAME,
    MARKER_FILENAME,
)
from eawf.kernel.state.epoch2.authority import AuthorityGap, resolve_authority
from eawf.surfaces.tui.console.chrome import load_chrome


def _declare_canary(root: Path) -> None:
    """Declare ``root`` a disposable epoch-2 canary, undeclared/unmarked otherwise."""
    root.mkdir(parents=True, exist_ok=True)
    (root / CANARY_DECLARATION_FILENAME).write_text(
        json.dumps({"disposable": True, "declared_by": "test", "purpose": "W30 launch test"})
    )
    (root / GENERATIONS_DIRNAME).mkdir(parents=True, exist_ok=True)


#: A syntactically valid generation id: ``gen-`` plus 16 hex digits.
_GENERATION_ID = "gen-" + "ab" * 8
#: A syntactically valid sha256 hex digest, reused for both digest fields a marker needs.
_DIGEST = "cd" * 32


def _activate_epoch2(root: Path) -> None:
    """Declare and activate ``root``, so :func:`resolve_authority` grants epoch 2."""
    _declare_canary(root)
    (root / GENERATIONS_DIRNAME / MARKER_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "epoch": 2,
                "generation_id": _GENERATION_ID,
                "manifest_digest": _DIGEST,
                "generation_digest": _DIGEST,
                "written_at": "2026-09-24T00:00:00Z",
            }
        )
    )


def _set_isatty(monkeypatch: pytest.MonkeyPatch, *, value: bool) -> None:
    class _Stdout:
        @staticmethod
        def isatty() -> bool:
            return value

    monkeypatch.setattr("sys.stdout", _Stdout())


# --------------------------------------------------------------------------
# CR-01: an epoch-2 tree opens the console over a live seam.
# --------------------------------------------------------------------------


def test_tui_opens_console_on_native_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _activate_epoch2(tmp_path / ".ea")
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _set_isatty(monkeypatch, value=True)

    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(launch, "_run_console", lambda app, seam: calls.append((app, seam)) or 0)

    def _no_epoch1(**_kwargs: object) -> int:
        # The epoch is declared inside ``.ea``; resolving it anywhere else reads epoch 1.
        raise AssertionError("an epoch-2 tree opened the epoch-1 app")

    monkeypatch.setattr(launch, "_launch_epoch1", _no_epoch1)

    rc = launch.launch_tui(workspace=None, no_input=False, plain=False, verbose=False)

    assert rc == 0
    assert len(calls) == 1
    app, seam = calls[0]
    from eawf.surfaces.tui.console.app import ConsoleApp
    from eawf.surfaces.tui.console.seam import ProjectionSeam

    assert isinstance(app, ConsoleApp)
    assert isinstance(seam, ProjectionSeam)
    assert app.seam is seam
    assert app.fixture.prototype is False
    # The daemon appends ``.ea`` to the root it is sent, so the seam names the repository.
    assert seam._repo_root == tmp_path


def test_tui_verbose_reserves_trace_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """SURF-173: ``--verbose`` reaches the console's ``verbose`` flag, off by default."""
    _activate_epoch2(tmp_path / ".ea")
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _set_isatty(monkeypatch, value=True)

    apps: list[object] = []
    monkeypatch.setattr(launch, "_run_console", lambda app, seam: apps.append(app) or 0)

    launch.launch_tui(workspace=None, no_input=False, plain=False, verbose=True)
    launch.launch_tui(workspace=None, no_input=False, plain=False, verbose=False)

    assert [app.verbose for app in apps] == [True, False]


# --------------------------------------------------------------------------
# CR-01: an epoch-1 tree keeps EaApp, with a one-line notice.
# --------------------------------------------------------------------------


def test_tui_epoch1_tree_keeps_epoch1_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _set_isatty(monkeypatch, value=True)

    calls = {"epoch1": 0, "console": 0}
    monkeypatch.setattr(
        "eawf.surfaces.tui.app.run_app",
        lambda scope, state_path: calls.__setitem__("epoch1", calls["epoch1"] + 1) or 0,
    )
    monkeypatch.setattr(
        launch,
        "_run_console",
        lambda app, seam: calls.__setitem__("console", calls["console"] + 1) or 0,
    )

    rc = launch.launch_tui(workspace=None, no_input=False, plain=False)

    assert rc == 0
    assert calls == {"epoch1": 1, "console": 0}
    assert launch.EPOCH1_NOTICE in capsys.readouterr().err


# --------------------------------------------------------------------------
# CR-01: a canary tree stuck mid-migration exits 4 off a TTY (SURF-085).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("break_marker", "expected_gap", "expected_command"),
    [
        (False, AuthorityGap.MARKER_ABSENT, "--rollback"),
        (True, AuthorityGap.MARKER_UNREADABLE, "--recover"),
    ],
    ids=["marker-absent", "marker-unreadable"],
)
def test_tui_migration_required_exits_4_off_tty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    break_marker: bool,
    expected_gap: AuthorityGap,
    expected_command: str,
) -> None:
    _declare_canary(tmp_path / ".ea")
    if break_marker:
        (tmp_path / ".ea" / GENERATIONS_DIRNAME / MARKER_FILENAME).write_text("not json")
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _set_isatty(monkeypatch, value=False)

    authority = resolve_authority(tmp_path / ".ea")
    assert authority.gap is expected_gap

    called = {"n": 0}
    monkeypatch.setattr(launch, "_run_console", lambda app, seam: called.__setitem__("n", 1) or 0)
    monkeypatch.setattr(
        "eawf.surfaces.tui.chassis.offline.emit_status",
        lambda **_kwargs: called.__setitem__("n", 1) or 0,
    )

    rc = launch.launch_tui(workspace=None, no_input=False, plain=False)

    assert rc == launch.TERMINAL_ENTRY_EXIT_CODE
    assert rc == 4
    assert called["n"] == 0
    err = capsys.readouterr().err
    assert f"eawf migrate epoch2 {expected_command} --target-root {tmp_path / '.ea'}" in err


def test_tui_migration_required_builds_entry_session_on_a_held_tty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stuck migration on a held TTY reaches ``_launch_entry``, unlike the off-TTY
    case above: with a TTY open, ``launch_tui`` does not short-circuit on
    :func:`launch._is_terminal` and instead builds a real ``SessionSetup`` for the
    entry layer. No prior test exercised that construction -- the only other
    stuck-migration test holds the TTY closed, which returns before
    ``_launch_entry`` runs at all. This test stubs only ``_run_console`` (the
    event loop), so ``SessionSetup(..., entrySel=...)`` at
    :func:`eawf.surfaces.tui.launch._launch_entry` runs unstubbed.
    """
    _declare_canary(tmp_path / ".ea")
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _set_isatty(monkeypatch, value=True)

    apps: list[object] = []
    monkeypatch.setattr(launch, "_run_console", lambda app, seam: apps.append(app) or 0)

    rc = launch.launch_tui(workspace=None, no_input=False, plain=False)

    assert rc == launch.TERMINAL_ENTRY_EXIT_CODE
    assert len(apps) == 1
    app = apps[0]

    assert app.session.route == "entry"
    assert app.session.entry_sel == launch._entry_sel(load_chrome(), "migration")
    commands = app.fixture.proto.entry[app.session.entry_sel].commands
    assert f"eawf migrate epoch2 --rollback --target-root {tmp_path / '.ea'}" in commands


# --------------------------------------------------------------------------
# Boundary / error-path coverage for the resolver-to-entry-state mapping.
# --------------------------------------------------------------------------


def test_an_epoch2_tree_named_outright_opens_on_the_resolving_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The console's chrome carries the resolving trace the launch actually ran."""
    _activate_epoch2(tmp_path / ".ea")
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _set_isatty(monkeypatch, value=True)
    from eawf.surfaces.tui.console.app import ConsoleApp

    apps: list[ConsoleApp] = []

    def _caught(app: ConsoleApp, _seam: object) -> int:
        apps.append(app)
        return 0

    monkeypatch.setattr(launch, "_run_console", _caught)

    launch.launch_tui(workspace=None, no_input=False, plain=False)

    (app,) = apps
    resolving = app.fixture.proto.entry[0]
    assert resolving.id == "resolving"
    assert resolving.panes is not None
    assert resolving.panes[0][1].endswith("named outright · EA_STATE")


def test_an_undeclared_epoch1_tree_is_not_resolved_through_the_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ordinary epoch-1 tree keeps the classic app, whatever the registry holds."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EA_STATE", raising=False)
    _set_isatty(monkeypatch, value=True)
    monkeypatch.setattr(launch, "_launch_epoch1", lambda **_kwargs: 0)
    monkeypatch.setattr(launch, "_run_console", lambda app, seam: 1 / 0)

    assert launch.launch_tui(workspace=None, no_input=False, plain=False) == 0


def test_hand_over_names_the_title_and_skips_an_empty_command() -> None:
    state = load_chrome().entry[2].model_copy(update={"commands": ("eawf workspace list", "")})
    assert launch.hand_over(state).splitlines() == [
        f"eawf ui: {state.title}",
        "  eawf workspace list",
    ]


def test_entry_sel_raises_for_an_unknown_state_id() -> None:
    from eawf.surfaces.tui.console.chrome import load_chrome

    with pytest.raises(ValueError, match="no entry state"):
        launch._entry_sel(load_chrome(), "not-a-real-state")


def test_dispatch_tui_delegates_to_launch_tui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI stays dispatch-only: it forwards every flag to the library function."""
    captured: dict[str, object] = {}
    workspace = tmp_path / "ws"

    def fake_launch_tui(**kwargs: object) -> int:
        captured.update(kwargs)
        return 7

    monkeypatch.setattr(launch, "launch_tui", fake_launch_tui)
    rc = cli_app._dispatch_tui(workspace=workspace, no_input=True, plain=True, verbose=True)

    assert rc == 7
    assert captured == {
        "workspace": workspace,
        "no_input": True,
        "plain": True,
        "verbose": True,
        "operator": None,
    }


def test_native_console_keeps_log_records_off_the_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """While the console owns the screen no root handler writes to the terminal,
    and the CLI's terminal handler is back once the console exits."""
    _activate_epoch2(tmp_path / ".ea")
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _set_isatty(monkeypatch, value=True)
    terminal = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal)
    root = logging.getLogger()
    handler = logging.StreamHandler(stream=sys.stderr)
    root.addHandler(handler)
    during: list[logging.Handler] = []
    monkeypatch.setattr(launch, "_run_console", lambda app, seam: during.extend(root.handlers) or 0)
    try:
        launch.launch_tui(workspace=None, no_input=False, plain=False, verbose=False)
        assert during
        assert handler not in during
        assert not any(
            isinstance(h, logging.StreamHandler) and h.stream is terminal for h in during
        )
        assert handler in root.handlers
    finally:
        root.removeHandler(handler)


def test_native_console_restores_the_terminal_handler_when_the_run_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Crashing:
        async def run_async(self) -> None:
            raise RuntimeError("console crashed")

    terminal = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal)
    root = logging.getLogger()
    handler = logging.StreamHandler(stream=sys.stderr)
    root.addHandler(handler)
    try:
        with pytest.raises(RuntimeError, match="console crashed"):
            launch._run_console(_Crashing(), None)  # type: ignore[arg-type]
        assert handler in root.handlers
    finally:
        root.removeHandler(handler)


# --------------------------------------------------------------------------
# An interactive launch prints no log line on the terminal it takes.
# --------------------------------------------------------------------------


def test_an_interactive_launch_keeps_info_logs_off_the_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _activate_epoch2(tmp_path / ".ea")
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _set_isatty(monkeypatch, value=True)
    monkeypatch.setattr(launch, "_run_console", lambda app, seam: 0)
    root = logging.getLogger()  # noqa: EAWF003 (the CLI's root-logger sink, as it installs it)
    sink = logging.StreamHandler(stream=sys.stderr)
    root.addHandler(sink)
    level = root.level
    root.setLevel(logging.INFO)
    try:
        launch.launch_tui(workspace=None, no_input=False, plain=False, verbose=False)
        # the sink is the CLI's again once the console has exited
        assert sink in root.handlers
        logging.getLogger("eawf.kernel.migration.epoch2.canary").info("after the console")
    finally:
        root.removeHandler(sink)
        root.setLevel(level)
    err = capsys.readouterr().err
    assert "require root=" not in err
    assert "after the console" in err
