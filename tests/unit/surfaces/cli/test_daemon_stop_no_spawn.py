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


_REPLY = {"result": {"shutdown_at": "2026-01-01T00:00:00Z", "drained": True}}


@pytest.fixture
def timeline(spawns: list[Path], monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    """Record the pid read, the shutdown call and the exit wait, in order."""
    events: list[tuple[object, ...]] = []

    def _read_pid(runtime: Path) -> int | None:
        events.append(("read_pid", runtime))
        return 41

    def _rpc(method: str, params: dict[str, object], *, spawn: bool = True) -> dict[str, object]:
        events.append(("rpc", method, params["timeout_seconds"]))
        return _REPLY

    def _wait(runtime: Path, *, previous_pid: int | None, timeout_seconds: float) -> None:
        events.append(("wait", runtime, previous_pid, timeout_seconds))

    monkeypatch.setattr("eawf.surfaces.cli.commands.daemon.read_daemon_pid", _read_pid)
    monkeypatch.setattr("eawf.surfaces.cli.commands.daemon._run_rpc", _rpc)
    monkeypatch.setattr("eawf.runtime.daemon.lifecycle.wait_for_daemon_exit", _wait)
    return events


def test_stop_waits_for_exit_after_the_reply(
    timeline: list[tuple[object, ...]], tmp_path: Path
) -> None:
    """The pid is read before the shutdown and the wait runs after its reply."""
    result = runner.invoke(app, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    rt = tmp_path / "rt"
    assert timeline == [
        ("read_pid", rt),
        ("rpc", "daemon.shutdown", 30),
        ("wait", rt, 41, 30.0),
    ]
    assert result.stdout.strip() == "daemon shutting down at=2026-01-01T00:00:00Z drained=True"


@pytest.mark.parametrize("timeout", [1, 600])
def test_stop_bounds_the_wait_by_its_timeout(
    timeline: list[tuple[object, ...]], timeout: int
) -> None:
    result = runner.invoke(app, ["daemon", "stop", "--timeout", str(timeout)])

    assert result.exit_code == 0, result.output
    assert timeline[-1][-1] == pytest.approx(float(timeout))


def test_stop_json_keys_are_unchanged_after_the_wait(
    timeline: list[tuple[object, ...]],
) -> None:
    result = runner.invoke(app, ["--json", "daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"] == {
        "shutdown_at": "2026-01-01T00:00:00Z",
        "drained": True,
    }
    assert [event[0] for event in timeline] == ["read_pid", "rpc", "wait"]


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_stop_wait_timeout_fails_with_exit_one(
    spawns: list[Path], monkeypatch: pytest.MonkeyPatch, json_flag: list[str]
) -> None:
    from eawf.runtime.daemon.lifecycle import DaemonLifecycleError

    def _wait(runtime: Path, *, previous_pid: int | None, timeout_seconds: float) -> None:
        raise DaemonLifecycleError("daemon did not exit within 30.0s pid=41")

    monkeypatch.setattr("eawf.surfaces.cli.commands.daemon._run_rpc", lambda *_a, **_k: _REPLY)
    monkeypatch.setattr("eawf.runtime.daemon.lifecycle.wait_for_daemon_exit", _wait)

    result = runner.invoke(app, [*json_flag, "daemon", "stop"])

    assert result.exit_code == 1, result.output
    assert "daemon stop failed: daemon did not exit within 30.0s pid=41" in result.stderr
    assert "shutting down" not in result.stdout
    assert result.stdout == ""


def test_stop_zero_timeout_exits_zero_when_the_daemon_exits_within_the_floor(
    spawns: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--timeout 0`` drops the drain window, not the wait for the process."""
    from eawf.runtime.daemon import lifecycle

    lock_states = iter([True, True, True, False])
    params_seen: list[dict[str, object]] = []

    def _rpc(method: str, params: dict[str, object], *, spawn: bool = True) -> dict[str, object]:
        params_seen.append(params)
        return _REPLY

    monkeypatch.setattr("eawf.surfaces.cli.commands.daemon._run_rpc", _rpc)
    monkeypatch.setattr("eawf.surfaces.cli.commands.daemon.read_daemon_pid", lambda _rt: 41)
    monkeypatch.setattr(lifecycle, "read_daemon_pid", lambda _rt: 41)
    monkeypatch.setattr(lifecycle, "daemon_singleton_locked", lambda _rt: next(lock_states))
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)

    result = runner.invoke(app, ["daemon", "stop", "--timeout", "0"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "daemon shutting down at=2026-01-01T00:00:00Z drained=True"
    assert params_seen == [{"drain": True, "timeout_seconds": 0}]
    assert next(lock_states, None) is None


def test_stop_error_reply_skips_the_wait(
    spawns: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(
        "eawf.surfaces.cli.commands.daemon._run_rpc",
        lambda *_a, **_k: {"error": {"code": -32602, "message": "bad params"}},
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.lifecycle.wait_for_daemon_exit",
        lambda _rt, *, previous_pid, timeout_seconds: waits.append(timeout_seconds),
    )

    result = runner.invoke(app, ["daemon", "stop"])

    assert result.exit_code == 1, result.output
    assert "daemon error" in result.stderr
    assert waits == []


def test_stop_without_a_daemon_does_not_wait(
    spawns: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(
        "eawf.runtime.daemon.lifecycle.wait_for_daemon_exit",
        lambda _rt, *, previous_pid, timeout_seconds: waits.append(timeout_seconds),
    )

    result = runner.invoke(app, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert "daemon not running" in result.output
    assert waits == []


def test_stop_evict_service_neither_calls_nor_waits(
    spawns: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "eawf.surfaces.cli.commands.daemon._handle_supervised_agent_on_stop",
        lambda *, evict_service: evict_service,
    )
    monkeypatch.setattr(
        "eawf.surfaces.cli.commands.daemon._run_rpc",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no rpc after eviction")),
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.lifecycle.wait_for_daemon_exit",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no wait after eviction")),
    )

    result = runner.invoke(app, ["--json", "daemon", "stop", "--evict-service"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"] == {
        "shutdown_at": None,
        "drained": False,
        "service_evicted": True,
    }
