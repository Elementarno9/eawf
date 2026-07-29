"""Lifecycle tests for launchd/systemd management in ``eawf daemon`` verbs.

A daemon under a launchd LaunchAgent (macOS, KeepAlive) or a systemd user unit
(Linux, Restart=) auto-restarts on exit, so a plain ``daemon.shutdown`` RPC is
undone the moment it lands and a manual ``daemon run`` forks a rival. W15 adds:

- ``daemon stop --evict-service`` -- boot the loaded agent out (``launchctl
  bootout`` / ``systemctl --user stop``) BEFORE the shutdown RPC so a KeepAlive
  cannot immediately undo the stop; without the flag the verb warns loudly.
- ``daemon run`` -- defer to a loaded agent instead of forking a rival.

Every launchctl / systemctl call routes through the injectable
:data:`eawf.runtime.daemon.service_install._service_runner` seam so these
tests capture + order the supervisor calls with a recording stub and never
mutate the host: no agent is installed, loaded, or booted out for real.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.runtime.daemon import service_install
from eawf.runtime.daemon.lifecycle import DaemonLifecycleResult
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.unit

runner = CliRunner()


class _RecordingRunner:
    """A service-runner stub that records argv and returns canned results.

    Args:
        returncode: The exit code every canned invocation reports.
        stdout: The stdout every canned invocation returns (``launchctl
            print`` output carries a ``pid = N`` line the detector parses).
    """

    def __init__(self, *, returncode: int = 0, stdout: str = "pid = 4242\n") -> None:
        self.calls: list[list[str]] = []
        self._returncode = returncode
        self._stdout = stdout

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=self._returncode, stdout=self._stdout, stderr=""
        )


def _force_launchd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force the launchd branch + a deterministic uid, hermetic runtime dir."""
    monkeypatch.setattr(service_install, "_current_platform", lambda: "darwin")
    monkeypatch.setattr(service_install, "_invoking_uid", lambda: 501)
    # No pid file under the tmp runtime dir -> rival detection stays None.
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(tmp_path / "eawfd"))


# ---- detect_supervised_agent ------------------------------------------------


def test_detect_launchd_loaded_via_injected_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``launchctl print`` rc=0 reports the agent loaded (via injection)."""
    _force_launchd(monkeypatch, tmp_path)
    rec = _RecordingRunner(returncode=0)
    report = service_install.detect_supervised_agent(platform="darwin", runner=rec)
    assert report.supervisor == "launchd"
    assert report.loaded is True
    assert rec.calls == [["launchctl", "print", "gui/501/dev.eawf.eawfd"]]


def test_detect_launchd_not_loaded_when_print_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero ``launchctl print`` reports the agent NOT loaded."""
    _force_launchd(monkeypatch, tmp_path)
    rec = _RecordingRunner(returncode=1, stdout="")
    report = service_install.detect_supervised_agent(platform="darwin", runner=rec)
    assert report.loaded is False


def test_detect_windows_reports_none_without_shelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows / unsupported hosts report ``none`` and never call the runner."""
    rec = _RecordingRunner()
    report = service_install.detect_supervised_agent(platform="win32", runner=rec)
    assert report.supervisor == "none"
    assert report.loaded is False
    assert rec.calls == []


# ---- evict_supervised_agent -------------------------------------------------


def test_evict_launchd_runs_bootout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """launchd eviction shells out to ``launchctl bootout`` for the label."""
    monkeypatch.setattr(service_install, "_invoking_uid", lambda: 501)
    rec = _RecordingRunner(returncode=1)
    target = service_install.evict_supervised_agent(platform="darwin", runner=rec)
    assert target == "gui/501/dev.eawf.eawfd"
    assert rec.calls == [
        ["launchctl", "bootout", "gui/501/dev.eawf.eawfd"],
        ["launchctl", "print", "gui/501/dev.eawf.eawfd"],
    ]


def test_evict_systemd_runs_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """systemd eviction stops the user unit (parity with launchd bootout)."""
    rec = _RecordingRunner()
    unit = service_install.evict_supervised_agent(platform="linux", runner=rec)
    assert unit == "eawfd.service"
    assert rec.calls == [["systemctl", "--user", "stop", "eawfd.service"]]


# ---- daemon stop --evict-service: supervisor owns termination ---------------


def test_stop_evict_service_does_not_respawn_for_shutdown_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stop --evict-service`` lets ``launchctl bootout`` own termination.

    Issuing the normal RPC after bootout can cold-spawn an unsupervised rival
    when launchd already removed the socket. The control path must therefore
    stop after supervisor eviction.
    """
    import eawf.surfaces.cli.commands.daemon as daemon_cmd

    _force_launchd(monkeypatch, tmp_path)
    timeline: list[str] = []
    unloaded = False

    def _rec_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal unloaded
        timeline.append(" ".join(cmd))
        if cmd[1] == "bootout":
            unloaded = True
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1 if cmd[1] == "print" and unloaded else 0,
            stdout="" if unloaded else "pid = 4242\n",
            stderr="",
        )

    def _rec_rpc(method: str, params: dict[str, object]) -> dict[str, object]:
        timeline.append(f"rpc:{method}")
        return {"result": {"shutdown_at": "2026-01-01T00:00:00Z", "drained": True}}

    monkeypatch.setattr(service_install, "_service_runner", _rec_runner)
    monkeypatch.setattr(daemon_cmd, "_run_rpc", _rec_rpc)

    res = runner.invoke(app, ["daemon", "stop", "--evict-service"])
    assert res.exit_code == 0, res.output

    bootout = "launchctl bootout gui/501/dev.eawf.eawfd"
    assert bootout in timeline
    assert "rpc:daemon.shutdown" not in timeline
    assert "evicted" in res.output
    assert "service stopped" in res.output


def test_stop_without_evict_warns_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain ``stop`` against a loaded agent warns that the stop is undone."""
    import eawf.surfaces.cli.commands.daemon as daemon_cmd

    _force_launchd(monkeypatch, tmp_path)
    monkeypatch.setattr(service_install, "_service_runner", _RecordingRunner(returncode=0))
    monkeypatch.setattr(
        daemon_cmd,
        "_run_rpc",
        lambda method, params: {"result": {"shutdown_at": "t", "drained": True}},
    )

    res = runner.invoke(app, ["daemon", "stop"])
    assert res.exit_code == 0, res.output
    assert "WARNING" in res.output
    assert "--evict-service" in res.output


# ---- daemon run: defer to a loaded agent ------------------------------------


def test_run_defers_to_loaded_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``daemon run`` defers to a loaded agent instead of forking a rival."""
    _force_launchd(monkeypatch, tmp_path)
    monkeypatch.setattr(service_install, "_service_runner", _RecordingRunner(returncode=0))

    booted: list[bool] = []

    def _fake_run(*, foreground: bool = True) -> int:
        booted.append(foreground)
        return 0

    monkeypatch.setattr("eawf.runtime.daemon.main.run", _fake_run)

    res = runner.invoke(app, ["daemon", "run"])
    assert res.exit_code == 0, res.output
    assert "deferring to loaded launchd agent" in res.output
    assert booted == []  # the daemon boot path was never reached


def test_run_boots_when_no_agent_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no loaded agent, ``daemon run`` boots the daemon as before."""
    _force_launchd(monkeypatch, tmp_path)
    # rc=1 -> agent not loaded -> no defer.
    monkeypatch.setattr(
        service_install, "_service_runner", _RecordingRunner(returncode=1, stdout="")
    )

    booted: list[bool] = []

    def _fake_run(*, foreground: bool = True) -> int:
        booted.append(foreground)
        return 0

    monkeypatch.setattr("eawf.runtime.daemon.main.run", _fake_run)

    res = runner.invoke(app, ["daemon", "run", "--foreground"])
    assert res.exit_code == 0, res.output
    assert booted == [True]


# ---- daemon start/restart: thin CLI dispatch -------------------------------


def test_start_dispatches_to_lifecycle_library(monkeypatch: pytest.MonkeyPatch) -> None:
    """``daemon start`` renders the typed library result."""
    monkeypatch.setattr(
        "eawf.runtime.daemon.lifecycle.start_daemon",
        lambda: DaemonLifecycleResult(
            action="started",
            pid=42,
            previous_pid=None,
            supervisor="none",
        ),
    )

    res = runner.invoke(app, ["daemon", "start"])

    assert res.exit_code == 0, res.output
    assert "daemon started pid=42 previous_pid=none supervisor=none" in res.output


def test_restart_forwards_no_drain_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """``daemon restart`` forwards bounded shutdown options unchanged."""
    calls: list[tuple[bool, int]] = []

    def _restart(*, drain: bool, timeout_seconds: int) -> DaemonLifecycleResult:
        calls.append((drain, timeout_seconds))
        return DaemonLifecycleResult(
            action="restarted",
            pid=42,
            previous_pid=41,
            supervisor="launchd",
        )

    monkeypatch.setattr("eawf.runtime.daemon.lifecycle.restart_daemon", _restart)

    res = runner.invoke(app, ["daemon", "restart", "--no-drain", "--timeout", "7"])

    assert res.exit_code == 0, res.output
    assert calls == [(False, 7)]
    assert "previous_pid=41" in res.output


# ---- daemon service-status: fold in the daemon-health advisories ------------


def test_service_status_carries_agent_and_size_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``service-status --json`` folds in the launchd-agent + runtime-dir rows."""
    import orjson

    from eawf.runtime.daemon.service_install import ServiceStatus

    _force_launchd(monkeypatch, tmp_path)
    # rc=1 -> agent not loaded (plist under a nonexistent home -> not installed).
    monkeypatch.setattr(
        service_install, "_service_runner", _RecordingRunner(returncode=1, stdout="")
    )
    monkeypatch.setattr(service_install, "service_status", lambda: ServiceStatus.NOT_INSTALLED)

    res = runner.invoke(app, ["--json", "daemon", "service-status"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())
    assert payload["status"] == "not-installed"
    assert payload["launchd_agent"]["status"] in {"ok", "warn"}
    assert payload["runtime_dir_size"]["status"] in {"ok", "warn"}
