"""CLI dispatch tests for ``eawf dispatch resume`` / ``eawf dispatch pause``.

Drives the Typer app via :class:`CliRunner` and checks the two headless
pause/resume verbs that toggle ``state.dispatch_paused`` through the daemon's
``agent.resume`` / ``agent.pause`` RPCs:

- ``dispatch resume`` calls ``agent.resume`` and reports ``dispatch_paused=False``.
- ``dispatch pause`` calls ``agent.pause`` and reports ``dispatch_paused=True``.
- ``--json`` emits the typed ``{"dispatch_paused": <bool>}`` envelope.
- The daemon-unavailable path returns the canonical ``DaemonUnreachable`` exit
  code (4) rather than faking a toggle.
- The command group registers (``eawf dispatch --help`` lists ``resume`` +
  ``pause``).

Both :func:`escalate_mutation` (so no real daemon is auto-spawned) and the
:class:`DaemonClient` (so no socket is opened) are monkeypatched; the fake
client echoes the persisted flag the real RPC would return so the verb's
reported value is exercised end-to-end.
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


class _FakeToggleClient:
    """Stand-in DaemonClient whose ``call`` returns the resume/pause flag.

    Records the dispatched method + params and returns the persisted
    ``dispatch_paused`` value the real ``agent.resume`` / ``agent.pause``
    RPCs return (``False`` for resume, ``True`` for pause).
    """

    captured: ClassVar[dict[str, Any]] = {}

    def __enter__(self) -> _FakeToggleClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        forwarded = params if params is not None else {}
        _FakeToggleClient.captured = {"method": method, "params": forwarded}
        paused = method == "agent.pause"
        return {"paused": paused}


class _FakeUnreachableClient:
    """Stand-in DaemonClient that fails on enter (daemon unreachable)."""

    def __enter__(self) -> _FakeUnreachableClient:
        raise OSError("daemon socket not found")

    def __exit__(self, *_args: Any) -> None:
        return None


class _FakeRpcErrorClient:
    """Stand-in DaemonClient whose ``call`` raises a JSON-RPC error envelope."""

    def __enter__(self) -> _FakeRpcErrorClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise DaemonRpcError(code=-32601, message="method not found")


@pytest.fixture(autouse=True)
def _stub_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``escalate_mutation`` so no real daemon is auto-spawned."""
    monkeypatch.setattr(
        "eawf.surfaces.cli._dispatch.escalate_mutation",
        lambda verb, *, flags, runtime_dir=None: 4242,
    )


def test_dispatch_resume_reports_not_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """``dispatch resume`` calls ``agent.resume`` and reports paused=false."""
    _FakeToggleClient.captured = {}
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeToggleClient)

    result = runner.invoke(app, ["dispatch", "resume"])
    assert result.exit_code == 0, result.output
    assert _FakeToggleClient.captured["method"] == "agent.resume"
    # P30-I23-W11: pause/resume carry the caller's repo root so the EP3
    # state-root guard can refuse a wrong-repo daemon bind.
    assert set(_FakeToggleClient.captured["params"]) == {"repo_root"}
    assert "dispatch_paused=False" in result.stdout


def test_dispatch_pause_reports_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """``dispatch pause`` calls ``agent.pause`` and reports paused=true."""
    _FakeToggleClient.captured = {}
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeToggleClient)

    result = runner.invoke(app, ["dispatch", "pause"])
    assert result.exit_code == 0, result.output
    assert _FakeToggleClient.captured["method"] == "agent.pause"
    assert "dispatch_paused=True" in result.stdout


def test_dispatch_resume_json_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json`` emits the typed ``{"dispatch_paused": false}`` envelope."""
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeToggleClient)

    result = runner.invoke(app, ["--json", "dispatch", "resume"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {"dispatch_paused": False}


def test_dispatch_pause_json_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json`` emits the typed ``{"dispatch_paused": true}`` envelope."""
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeToggleClient)

    result = runner.invoke(app, ["--json", "dispatch", "pause"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {"dispatch_paused": True}


def test_dispatch_resume_daemon_unreachable_exits_4(monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon-unreachable resume returns the canonical DAEMON_UNREACHABLE exit."""
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeUnreachableClient)

    result = runner.invoke(app, ["dispatch", "resume"])
    assert result.exit_code == exit_codes.DAEMON_UNREACHABLE, result.output
    assert "daemon unavailable for agent.resume" in result.output


def test_dispatch_pause_daemon_rpc_error_maps_to_cli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON-RPC error envelope maps onto a typed CLI error (non-zero exit)."""
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeRpcErrorClient)

    result = runner.invoke(app, ["dispatch", "pause"])
    # -32601 (method not found) maps to UserError (exit 1) per the RPC table.
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    assert "method not found" in result.output


def test_dispatch_no_args_is_help() -> None:
    """``dispatch`` with no sub-verb prints help (no_args_is_help)."""
    result = runner.invoke(app, ["dispatch"])
    assert result.exit_code in (0, 2)
    assert "resume" in result.output
    assert "pause" in result.output


def test_dispatch_help_lists_both_verbs() -> None:
    """``dispatch --help`` lists both registered verbs (group registered)."""
    result = runner.invoke(app, ["dispatch", "--help"])
    assert result.exit_code == 0, result.output
    assert "resume" in result.output
    assert "pause" in result.output
