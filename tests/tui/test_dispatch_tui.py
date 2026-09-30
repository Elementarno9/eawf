"""Tests for the bare-``eawf`` / ``eawf ui`` dispatch: the console is the only app.

These pin the dispatch contract at the boundary
(:func:`eawf.surfaces.cli.app._dispatch_tui`):

* an interactive TTY constructs the console
  (:class:`~eawf.surfaces.tui.console.app.ConsoleApp`);
* the non-TTY / ``--plain`` / ``--no-input`` path writes the console's own frame in
  plain mode and opens no app at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import eawf.surfaces.cli.app as cli_app
import eawf.surfaces.tui.launch as launch
from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    GENERATIONS_DIRNAME,
    MARKER_FILENAME,
)
from eawf.surfaces.cli.app import app
from eawf.surfaces.tui.console.plain import OFFLINE_SNAPSHOT


def _activate_epoch2(repo: Path) -> None:
    """Declare and activate ``repo``'s tree, so it resolves to epoch 2."""
    ea = repo / ".ea"
    (ea / GENERATIONS_DIRNAME).mkdir(parents=True)
    (ea / CANARY_DECLARATION_FILENAME).write_text(
        json.dumps({"disposable": True, "declared_by": "test", "purpose": "dispatch test"})
    )
    (ea / GENERATIONS_DIRNAME / MARKER_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "epoch": 2,
                "generation_id": "gen-" + "ab" * 8,
                "manifest_digest": "cd" * 32,
                "generation_digest": "cd" * 32,
                "written_at": "2026-09-24T00:00:00Z",
            }
        )
    )


def _stub_dispatch(monkeypatch: pytest.MonkeyPatch, *, isatty: bool, repo: Path) -> list[object]:
    """Point the launch at ``repo``'s epoch-2 tree and catch any console it would run.

    Returns:
        The apps the launch handed to the event loop, in order.
    """
    _activate_epoch2(repo)
    monkeypatch.setenv("EA_STATE", str(repo / ".ea" / "state.json"))
    runs: list[object] = []
    monkeypatch.setattr(launch, "_run_console", lambda app, seam: runs.append(app) or 0)

    class _Stdout:
        @staticmethod
        def isatty() -> bool:
            return isatty

        @staticmethod
        def write(text: str) -> int:
            return len(text)

        @staticmethod
        def flush() -> None:
            return None

    monkeypatch.setattr("sys.stdout", _Stdout())
    return runs


def test_interactive_launches_the_console(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from eawf.surfaces.tui.console.app import ConsoleApp

    runs = _stub_dispatch(monkeypatch, isatty=True, repo=tmp_path)
    rc = cli_app._dispatch_tui(workspace=None, no_input=False, plain=False)
    assert rc == 0
    assert [type(run) for run in runs] == [ConsoleApp]


def test_legacy_env_no_longer_routes_anywhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``EAWF_TUI_LEGACY=1`` is dead — the TTY path still opens the console."""
    monkeypatch.setenv("EAWF_TUI_LEGACY", "1")
    runs = _stub_dispatch(monkeypatch, isatty=True, repo=tmp_path)
    assert cli_app._dispatch_tui(workspace=None, no_input=False, plain=False) == 0
    assert len(runs) == 1


@pytest.mark.parametrize(
    ("isatty", "no_input", "plain"),
    [(False, False, False), (True, False, True), (True, True, False)],
    ids=["non-tty", "plain", "no-input"],
)
def test_headless_paths_open_no_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    isatty: bool,
    no_input: bool,
    plain: bool,
) -> None:
    runs = _stub_dispatch(monkeypatch, isatty=isatty, repo=tmp_path)
    assert cli_app._dispatch_tui(workspace=None, no_input=no_input, plain=plain) == 0
    assert runs == []


# --------------------------------------------------------------------------
# CLI-level non-TTY contract: bare eawf / eawf ui write the console's plain frame.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [["--plain"], ["--plain", "ui"], ["--no-input"]],
    ids=["bare-plain", "ui-plain", "bare-no-input"],
)
def test_cli_headless_writes_the_console_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    _activate_epoch2(tmp_path)
    result = CliRunner().invoke(app, ["-w", str(tmp_path), *argv])
    assert result.exit_code == 0
    assert OFFLINE_SNAPSHOT in result.stdout
    assert result.stdout.isascii()


def test_cli_headless_on_a_folder_with_no_tree_exits_4(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    result = CliRunner().invoke(app, ["--plain", "-w", str(tmp_path)])
    assert result.exit_code == launch.TERMINAL_ENTRY_EXIT_CODE
    assert "eawf init" in result.stderr
