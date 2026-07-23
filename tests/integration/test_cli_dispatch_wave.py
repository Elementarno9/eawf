"""CLI dispatch tests for ``eawf dispatch wave <wave-id>`` (P30-I06-W02).

Drives the Typer app via :class:`CliRunner` and checks the headless live-spawn
verb that asks the daemon to SPAWN + dispatch a wave through the
``agent.dispatch`` ``spawn=True`` path:

- The recording client asserts the request frame carries ``spawn=True`` + the
  wave id, and the result line reports the captured pid + serving runtime the
  daemon returned.
- ``--daemonless`` / ``EAWF_DAEMONLESS=1`` raises a typed ``UserError``
  (exit-code 1) before any wire traffic — no spawned line is printed.
- An unknown wave id surfaces the daemon's ``-32602`` (invalid params) as a
  typed ``CliError`` (``UserError`` exit-code 1 per the RPC table), never a
  faked "spawned ..." line.

The :class:`DaemonClient` is monkeypatched with a recording stand-in so no
socket is opened; the daemonless test stubs nothing on the client (the
escalation rule must reject before the client is ever constructed).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app

runner = CliRunner()


class _RecordingSpawnClient:
    """Recording DaemonClient that echoes a captured pid + serving runtime.

    Records the dispatched method + params (so a test can assert the request
    frame carried ``spawn=True`` + the wave id) and returns a JSON-mode
    ``DispatchPlan`` shape with the captured pid + serving runtime the real
    ``agent.dispatch`` spawn path returns.
    """

    captured: ClassVar[dict[str, Any]] = {}
    pid: ClassVar[int] = 54321
    runtime: ClassVar[str] = "claude-code"
    session_id: ClassVar[str] = "sess-abc123"
    call_timeout_seconds: ClassVar[float | None] = None

    def __init__(self, *, call_timeout_seconds: float) -> None:
        type(self).call_timeout_seconds = call_timeout_seconds

    def __enter__(self) -> _RecordingSpawnClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        forwarded = params if params is not None else {}
        _RecordingSpawnClient.captured = {"method": method, "params": forwarded}
        return {
            "session_id": _RecordingSpawnClient.session_id,
            "attempt": 1,
            "pid": _RecordingSpawnClient.pid,
            "runtime": _RecordingSpawnClient.runtime,
        }


class _UnknownWaveClient:
    """Recording DaemonClient whose ``call`` raises ``-32602`` (unknown wave)."""

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _UnknownWaveClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise DaemonRpcError(code=-32602, message="unknown wave: 'P99-I01-W99'")


class _NoConnectClient:
    """DaemonClient that must never be constructed (daemonless rejects first)."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:  # pragma: no cover - guard
        raise AssertionError("DaemonClient must not be constructed on a daemonless spawn")


@pytest.fixture(autouse=True)
def _stub_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``escalate_mutation`` so no real daemon is auto-spawned.

    The real :func:`escalate_mutation` rejects ``--daemonless`` itself, so the
    daemonless test does NOT install this stub (it asserts the real rejection);
    the autouse stub only short-circuits the auto-spawn for the happy + RPC
    paths.
    """
    monkeypatch.setattr(
        "eawf.surfaces.cli._dispatch.escalate_mutation",
        lambda verb, *, flags, runtime_dir=None: 4242,
    )


def test_dispatch_wave_sends_spawn_frame_and_reports_pid_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request frame carries ``spawn=True`` + the wave id; the line reports pid + runtime."""
    _RecordingSpawnClient.captured = {}
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _RecordingSpawnClient)

    result = runner.invoke(app, ["dispatch", "wave", "P30-I06-W02"])
    assert result.exit_code == 0, result.output
    assert _RecordingSpawnClient.captured["method"] == "agent.dispatch"
    assert _RecordingSpawnClient.captured["params"] == {"wave_id": "P30-I06-W02", "spawn": True}
    # The reported line carries the captured pid + the serving runtime.
    assert "spawned P30-I06-W02 on claude-code (pid=54321)" in result.stdout


def test_dispatch_wave_uses_mutation_wire_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live spawn waits beyond the generic 30-second request timeout."""
    _RecordingSpawnClient.call_timeout_seconds = None
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _RecordingSpawnClient)
    monkeypatch.setattr(
        "eawf.runtime.daemon.limits.configured_juror_wall_clock",
        lambda _repo_root: 75.0,
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.limits.cli_mutation_timeout_for",
        lambda ceiling: 321.0 if ceiling == 75.0 else 0.0,
    )

    result = runner.invoke(app, ["dispatch", "wave", "P30-I06-W02"])

    assert result.exit_code == 0, result.output
    assert _RecordingSpawnClient.call_timeout_seconds == pytest.approx(321.0)


def test_dispatch_wave_json_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json`` emits the typed pid + serving-runtime envelope."""
    _RecordingSpawnClient.captured = {}
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _RecordingSpawnClient)

    result = runner.invoke(app, ["--json", "dispatch", "wave", "P30-I06-W02"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "wave_id": "P30-I06-W02",
        "pid": 54321,
        "runtime": "claude-code",
        "session_id": "sess-abc123",
    }


def test_dispatch_wave_daemonless_flag_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--daemonless`` raises a typed UserError (exit 1); no spawned line printed."""
    # Drop the autouse stub so the REAL escalation rule fires + reject daemonless.
    monkeypatch.undo()
    # The client must never be constructed — the rejection precedes any wire traffic.
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _NoConnectClient)

    result = runner.invoke(app, ["--daemonless", "dispatch", "wave", "P30-I06-W02"])
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    assert "--daemonless rejected" in result.output
    assert "spawned" not in result.output


def test_dispatch_wave_daemonless_env_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """``EAWF_DAEMONLESS=1`` raises a typed UserError (exit 1); no spawned line printed."""
    monkeypatch.undo()
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _NoConnectClient)

    result = runner.invoke(app, ["dispatch", "wave", "P30-I06-W02"])
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    assert "--daemonless rejected" in result.output
    assert "spawned" not in result.output


def test_dispatch_wave_unknown_wave_surfaces_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown wave id surfaces the daemon's -32602 as a typed CliError, not a faked line."""
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _UnknownWaveClient)

    result = runner.invoke(app, ["dispatch", "wave", "P99-I01-W99"])
    # -32602 (invalid params) maps to UserError (exit 1) per the RPC table.
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    assert "unknown wave" in result.output
    assert "spawned" not in result.output


def test_dispatch_wave_registered_in_help() -> None:
    """``dispatch --help`` lists the ``wave`` verb (group registered)."""
    result = runner.invoke(app, ["dispatch", "--help"])
    assert result.exit_code == 0, result.output
    assert "wave" in result.output
