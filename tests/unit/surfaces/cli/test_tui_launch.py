"""``eawf tui``'s authority switch: which app opens, ``--verbose``, and SURF-085's exit 4.

Pins the launch contract at :func:`eawf.surfaces.tui.launch.launch_tui`, the library
:func:`eawf.surfaces.cli.app._dispatch_tui` delegates to: an epoch-2 tree opens the
native console over a live seam, an epoch-1 tree keeps the classic ``EaApp``, and a
canary tree stuck mid-migration exits 4 off a TTY rather than opening either. No test
here starts a live daemon or an interactive Textual run -- construction is verified by
monkeypatching the one function (:func:`eawf.surfaces.tui.launch._run_console`) that
would otherwise drive a real event loop.
"""

from __future__ import annotations

import json
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


def _declare_canary(root: Path) -> None:
    """Declare ``root`` a disposable epoch-2 canary, undeclared/unmarked otherwise."""
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
    _activate_epoch2(tmp_path)
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _set_isatty(monkeypatch, value=True)

    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(launch, "_run_console", lambda app, seam: calls.append((app, seam)) or 0)

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


def test_tui_verbose_reserves_trace_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """SURF-173: ``--verbose`` reaches the console's ``verbose`` flag, off by default."""
    _activate_epoch2(tmp_path)
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
    ("break_marker", "expected_gap", "expected_state"),
    [
        (False, AuthorityGap.MARKER_ABSENT, "migration"),
        (True, AuthorityGap.MARKER_UNREADABLE, "interrupted"),
    ],
    ids=["marker-absent", "marker-unreadable"],
)
def test_tui_migration_required_exits_4_off_tty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    break_marker: bool,
    expected_gap: AuthorityGap,
    expected_state: str,
) -> None:
    _declare_canary(tmp_path)
    if break_marker:
        (tmp_path / GENERATIONS_DIRNAME / MARKER_FILENAME).write_text("not json")
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _set_isatty(monkeypatch, value=False)

    authority = resolve_authority(tmp_path)
    assert authority.gap is expected_gap
    assert launch.entry_state_id_for(authority) == expected_state

    called = {"n": 0}
    monkeypatch.setattr(launch, "_run_console", lambda app, seam: called.__setitem__("n", 1) or 0)
    monkeypatch.setattr(
        "eawf.surfaces.tui.offline.emit_status", lambda **_kwargs: called.__setitem__("n", 1) or 0
    )

    rc = launch.launch_tui(workspace=None, no_input=False, plain=False)

    assert rc == launch.TERMINAL_ENTRY_EXIT_CODE
    assert rc == 4
    assert called["n"] == 0


# --------------------------------------------------------------------------
# Boundary / error-path coverage for the resolver-to-entry-state mapping.
# --------------------------------------------------------------------------


def test_entry_state_id_for_epoch2_is_none(tmp_path: Path) -> None:
    """Epoch 2 has no pre-session layer to name -- the launcher opens the console."""
    _activate_epoch2(tmp_path)
    authority = resolve_authority(tmp_path)
    assert authority.epoch == 2
    assert launch.entry_state_id_for(authority) is None


def test_entry_state_id_for_undeclared_epoch1_is_none(tmp_path: Path) -> None:
    """An ordinary, never-declared epoch-1 tree is not a stuck migration."""
    authority = resolve_authority(tmp_path)
    assert authority.gap is AuthorityGap.UNDECLARED
    assert launch.entry_state_id_for(authority) is None


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
    }
