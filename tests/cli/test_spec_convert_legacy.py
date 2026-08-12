"""CLI-side tests for ``eawf spec convert-legacy``.

The command is always daemon-mediated — converting legacy criterion rows
mutates ``state.json`` (AGENTS rule 4), so there is no in-process
fallback. The CLI is a thin proxy forwarding ``scope_id`` / ``dry_run`` /
``repo_root`` to the daemon's ``spec.convert_legacy`` RPC and rendering
the per-row conversion report. Coverage:

* daemon DOWN -> refuses with a ``daemon_required`` envelope (non-zero);
* proxy on + daemon up -> forwards the right params + renders the
  per-row report (converted gate kind, refused named reason);
* ``--dry-run`` forwards ``dry_run=True`` and writes nothing CLI-side;
* a daemon ``-32002`` validation error surfaces as a CLI ValidationError.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import spec as spec_cmd

pytestmark = pytest.mark.unit


class _FakeClient:
    """A stand-in DaemonClient context manager recording the ``call`` args."""

    last_method: str | None = None
    last_params: dict[str, Any] | None = None

    def __init__(self, *, result: dict[str, Any] | None = None, error: Exception | None = None):
        self._result = result
        self._error = error

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        type(self).last_method = method
        type(self).last_params = dict(params)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _result_payload(*, dry_run: bool = False) -> dict[str, Any]:
    return {
        "operation": "convert_legacy",
        "scope_id": "P30-I20",
        "dry_run": dry_run,
        "converted_count": 1,
        "refused_count": 1,
        "rows": [
            {
                "wave_id": "P30-I20-W01",
                "criterion_id": "CR-01",
                "disposition": "converted",
                "reason": None,
                "gate_kind": "criterion_in_diff",
            },
            {
                "wave_id": "P30-I20-W01",
                "criterion_id": "CR-02",
                "disposition": "refused",
                "reason": "EAWF021 measurability: sub-floor signal",
                "gate_kind": None,
            },
        ],
        "before_version": "aa",
        "after_version": "bb",
        "envelope": {},
        "idempotent_replay": False,
    }


def test_convert_legacy_daemon_down_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the daemon unreachable, convert-legacy refuses daemon_required."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(spec_cmd, "_daemon_proxy_enabled_for_spec", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: False)

    runner = CliRunner()
    result = runner.invoke(app, ["--workspace", str(tmp_path), "spec", "convert-legacy", "P30-I20"])

    assert result.exit_code != 0
    assert "daemon_required" in str(result.exception)


def test_convert_legacy_forwards_params_and_renders_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The proxy forwards scope/dry_run/repo_root and prints per-row lines."""
    monkeypatch.setattr(spec_cmd, "_daemon_proxy_enabled_for_spec", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: True)
    monkeypatch.setattr(spec_cmd, "DaemonClient", lambda: _FakeClient(result=_result_payload()))

    runner = CliRunner()
    result = runner.invoke(app, ["--workspace", str(tmp_path), "spec", "convert-legacy", "P30-I20"])

    assert result.exit_code == 0, result.output
    assert _FakeClient.last_method == "spec.convert_legacy"
    assert _FakeClient.last_params is not None
    assert _FakeClient.last_params["scope_id"] == "P30-I20"
    assert _FakeClient.last_params["dry_run"] is False
    assert _FakeClient.last_params["repo_root"] == str(tmp_path.resolve())
    assert "converted=1" in result.output
    assert "refused=1" in result.output
    assert "gate=criterion_in_diff" in result.output
    assert "EAWF021 measurability" in result.output


def test_convert_legacy_dry_run_forwards_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--dry-run reaches the daemon params and the report says dry-run."""
    monkeypatch.setattr(spec_cmd, "_daemon_proxy_enabled_for_spec", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: True)
    monkeypatch.setattr(
        spec_cmd, "DaemonClient", lambda: _FakeClient(result=_result_payload(dry_run=True))
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "spec", "convert-legacy", "P30-I20", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeClient.last_params is not None
    assert _FakeClient.last_params["dry_run"] is True
    assert "dry-run" in result.output


def test_convert_legacy_daemon_validation_error_surfaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A daemon -32002 validation reject surfaces as a CLI validation error."""
    monkeypatch.setattr(spec_cmd, "_daemon_proxy_enabled_for_spec", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: True)
    error = DaemonRpcError(code=-32002, message="validation_failed: post-mutation invalid")
    monkeypatch.setattr(spec_cmd, "DaemonClient", lambda: _FakeClient(error=error))

    runner = CliRunner()
    result = runner.invoke(app, ["--workspace", str(tmp_path), "spec", "convert-legacy", "P30-I20"])

    assert result.exit_code != 0
    assert "validation_failed" in str(result.exception)
