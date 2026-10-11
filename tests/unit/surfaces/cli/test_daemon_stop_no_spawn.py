"""``eawf daemon stop`` never starts a daemon only to shut it down.

The verb used to route through the auto-spawning RPC, so with no daemon
running it cold-spawned one and then stopped it. These tests drive the
real POSIX transport against an empty runtime directory with the spawn
helper replaced by a recorder, so a regression shows up as a recorded
spawn rather than as a live process.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 drives the stop verb with no daemon

from eawf.surfaces.cli.app import app
from tests._epoch2_helpers import lay_epoch2_tree

runner = CliRunner()

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX socket transport")


@pytest.fixture
def spawns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Point the verb at an empty runtime dir and record every spawn attempt."""
    monkeypatch.setenv("EA_STATE", str(lay_epoch2_tree(tmp_path / "epoch2")))
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(tmp_path / "rt"))
    recorded: list[Path] = []

    def _record(runtime: Path) -> int:
        recorded.append(runtime)
        raise AssertionError("daemon stop must not spawn a daemon")

    monkeypatch.setattr("eawf.surfaces.cli.commands.daemon.auto_spawn_daemon", _record)
    monkeypatch.setattr(
        "eawf.surfaces.cli.commands.daemon._handle_supervised_agent_on_stop",
        lambda *, evict_service: False,
    )
    return recorded


def test_stop_without_a_daemon_reports_it_and_exits_zero(spawns: list[Path]) -> None:
    result = runner.invoke(app, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert "daemon not running" in result.output
    assert spawns == []


def test_stop_without_a_daemon_json_envelope(spawns: list[Path]) -> None:
    result = runner.invoke(app, ["--json", "daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"] == {
        "shutdown_at": None,
        "drained": None,
        "running": False,
    }
    assert spawns == []


def test_stop_with_a_daemon_sends_the_shutdown_without_spawning(
    spawns: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running daemon still receives the shutdown, over a no-spawn call."""
    calls: list[tuple[str, bool]] = []

    def _rpc(method: str, params: dict[str, object], *, spawn: bool = True) -> dict[str, object]:
        calls.append((method, spawn))
        return {"result": {"shutdown_at": "2026-01-01T00:00:00Z", "drained": True}}

    monkeypatch.setattr("eawf.surfaces.cli.commands.daemon._run_rpc", _rpc)

    result = runner.invoke(app, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert "shutting down" in result.output
    assert calls == [("daemon.shutdown", False)]
