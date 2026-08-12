"""CLI-side tests for ``eawf spec sync``.

``spec sync`` is always daemon-mediated — materialising the parsed criteria
+ gates onto ``state.json`` is a canonical state mutation (AGENTS rule 4),
so there is no in-process fallback. The CLI dispatcher is a thin proxy that
forwards ``wave_id`` / ``spec_path`` / ``repo_root`` to the daemon's
``spec.sync`` RPC and renders the result. Coverage:

* daemon DOWN → refuses with a ``daemon_required`` envelope (exit non-zero);
* proxy on + daemon up → forwards the right params + renders the result;
* a daemon ``-32002`` validation error (lint / PENDING reject) surfaces as a
  CLI ValidationError (non-zero exit).
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


def test_spec_sync_daemon_down_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With the daemon unreachable, ``spec sync`` refuses with daemon_required."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(spec_cmd, "_daemon_proxy_enabled_for_spec", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: False)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "spec", "sync", "P29-I12-W05"],
    )
    assert result.exit_code != 0
    assert "daemon_required" in str(result.exception)


def test_spec_sync_proxy_forwards_params_and_renders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Proxy on + daemon up forwards wave_id / spec_path and renders the result."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(spec_cmd, "_daemon_proxy_enabled_for_spec", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: True)

    fake_result = {
        "operation": "sync",
        "wave_id": "P29-I12-W05",
        "criteria_count": 2,
        "gates_count": 1,
        "before_version": "aaaa",
        "after_version": "bbbb",
        "envelope": {},
        "idempotent_replay": False,
    }
    monkeypatch.setattr(spec_cmd, "DaemonClient", lambda *a, **k: _FakeClient(result=fake_result))

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "spec",
            "sync",
            "P29-I12-W05",
            "--spec-path",
            ".ea/specs/custom.md",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _FakeClient.last_method == "spec.sync"
    assert _FakeClient.last_params is not None
    assert _FakeClient.last_params["wave_id"] == "P29-I12-W05"
    assert _FakeClient.last_params["spec_path"] == ".ea/specs/custom.md"
    assert "repo_root" in _FakeClient.last_params
    assert "sync ok" in result.output
    assert "criteria=2" in result.output


def test_spec_sync_validation_error_surfaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A daemon -32002 (lint / PENDING reject) surfaces as a non-zero exit."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(spec_cmd, "_daemon_proxy_enabled_for_spec", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: True)

    err = DaemonRpcError(-32002, "validation_failed: spec sync lint findings: EAWF021 ...")
    monkeypatch.setattr(spec_cmd, "DaemonClient", lambda *a, **k: _FakeClient(error=err))

    runner = CliRunner()
    result = runner.invoke(app, ["--workspace", str(tmp_path), "spec", "sync", "P29-I12-W05"])
    assert result.exit_code != 0
    assert "EAWF021" in str(result.exception)
