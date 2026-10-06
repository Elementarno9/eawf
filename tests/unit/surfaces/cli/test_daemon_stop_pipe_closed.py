"""``eawf daemon stop`` when the daemon closes the transport before replying.

The shutdown RPC races its own reply: the daemon exits as it answers, so the
client's read can fail with ERROR_BROKEN_PIPE against a daemon that did stop.
Two defects met on that path -- ``pywintypes.error`` does not subclass
``OSError``, so the win32 client's ``except OSError`` let a bare
``(109, 'ReadFile', ...)`` tuple reach the operator, and the verb treated a
lost reply as a failure. These tests pin the classification and the verb's
reading of it.
"""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 drives the stop verb over a mocked pipe

from eawf.surfaces.cli import _daemon_client
from eawf.surfaces.cli.app import app

runner = CliRunner()


class _FakeWin32Error(Exception):
    """A ``pywintypes.error`` lookalike: not an ``OSError``, args-carried code."""

    def __init__(self, winerror: int, func: str, message: str) -> None:
        super().__init__(winerror, func, message)
        self.winerror = winerror


@pytest.mark.parametrize("code", sorted(_daemon_client.PIPE_CLOSED_CODES))
def test_pipe_closed_codes_are_recognized(code: int) -> None:
    """Every peer-went-away code classifies, by attribute or by args."""
    assert _daemon_client._pipe_closed_code(_FakeWin32Error(code, "ReadFile", "closed")) == code
    assert _daemon_client._pipe_closed_code(OSError(code, "closed")) == code


def test_unrelated_win32_error_is_not_a_closed_pipe() -> None:
    """A different failure must keep its own error path."""
    assert _daemon_client._pipe_closed_code(_FakeWin32Error(5, "CreateFile", "denied")) is None
    assert _daemon_client._pipe_closed_code(RuntimeError("boom")) is None


@pytest.mark.skipif(sys.platform != "win32", reason="win32 pipe client transport")
def test_pipe_client_broken_pipe_becomes_connection_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``pywintypes.error`` 109 surfaces as ``ConnectionResetError``."""
    import pywintypes

    def _raise(*_args: object, **_kwargs: object) -> bytes:
        raise pywintypes.error(109, "ReadFile", "The pipe has been ended.")

    monkeypatch.setattr("eawf.runtime.daemon.windows_pipe.pipe_client_call", _raise)
    client = _daemon_client.DaemonClient.__new__(_daemon_client.DaemonClient)
    client._pipe_name = r"\\.\pipe\eawfd-test"
    client._call_timeout_seconds = 1.0

    with pytest.raises(ConnectionResetError):
        client._call_over_pipe(b"{}\n", method="daemon.shutdown")


@pytest.mark.skipif(sys.platform != "win32", reason="win32 pipe client transport")
def test_pipe_client_other_error_stays_a_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An access denial is a round-trip failure, not a closed pipe."""
    import pywintypes

    def _raise(*_args: object, **_kwargs: object) -> bytes:
        raise pywintypes.error(5, "CreateFile", "Access is denied.")

    monkeypatch.setattr("eawf.runtime.daemon.windows_pipe.pipe_client_call", _raise)
    client = _daemon_client.DaemonClient.__new__(_daemon_client.DaemonClient)
    client._pipe_name = r"\\.\pipe\eawfd-test"
    client._call_timeout_seconds = 1.0

    with pytest.raises(RuntimeError) as excinfo:
        client._call_over_pipe(b"{}\n", method="daemon.status")
    assert not isinstance(excinfo.value, ConnectionResetError)


def _reset_on_shutdown(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    """Make ``daemon.shutdown`` lose its reply to a reset under *platform*."""

    def _raise(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ConnectionResetError("daemon closed the pipe before replying")

    monkeypatch.setattr("eawf.surfaces.cli.commands.daemon._run_rpc", _raise)
    monkeypatch.setattr(
        "eawf.surfaces.cli.commands.daemon._handle_supervised_agent_on_stop",
        lambda *, evict_service: False,
    )
    monkeypatch.setattr(sys, "platform", platform)


def test_stop_reports_a_lost_reply_as_a_shutdown_on_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On win32 a closed pipe on ``daemon.shutdown`` exits clean, not with a tuple."""
    _reset_on_shutdown(monkeypatch, "win32")

    result = runner.invoke(app, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert "shutting down" in result.output
    assert "109" not in result.output


def test_stop_lost_reply_json_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON envelope names the lost reply and leaves the timing unknown."""
    _reset_on_shutdown(monkeypatch, "win32")

    result = runner.invoke(app, ["--json", "daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"] == {
        "shutdown_at": None,
        "drained": None,
        "reply_lost": True,
    }


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_stop_reset_is_a_fault_off_win32(
    monkeypatch: pytest.MonkeyPatch, platform: str, json_flag: list[str]
) -> None:
    """A reset over a Unix socket is a real failure, never a lost reply."""
    _reset_on_shutdown(monkeypatch, platform)

    result = runner.invoke(app, [*json_flag, "daemon", "stop"])

    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "reply_lost" not in result.output
    assert "shutting down" not in result.output
    assert "daemon shutdown failed" in result.output
