"""``eawf runtime certify`` sends one daemon RPC and renders the row it recorded."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import click
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.runtime import RUNTIME_CERTIFY
from tests.contract.surfaces.cli.conftest import FakeDaemon

pytestmark = pytest.mark.contract

runner = CliRunner()

_CERTIFIED: Final[dict[str, Any]] = {
    "outcome": "certified",
    "harness_version": "2.1.288",
    "reason": "claude-code 2.1.288 passed the conformance probe; certified until 2026-12-31",
}
_QUARANTINED: Final[dict[str, Any]] = {
    "outcome": "quarantined",
    "harness_version": "0.159.2",
    "reason": "codex 0.159.2 failed the conformance probe (capability_not_observed): tool_use",
}


def _certify(tmp_path: Path, runtime: str) -> Any:
    return runner.invoke(
        app, ["--workspace", str(tmp_path), "--json", "runtime", "certify", runtime]
    )


def test_certify_sends_the_runtime_and_prints_the_certification(
    daemon: FakeDaemon, tmp_path: Path
) -> None:
    daemon.result = _CERTIFIED
    result = _certify(tmp_path, "claude-code")
    assert result.exit_code == exit_codes.OK, result.output
    [(method, params)] = daemon.calls
    assert method == RUNTIME_CERTIFY
    assert params["runtime"] == "claude-code"
    assert "certified until 2026-12-31" in result.output


def test_a_quarantined_version_exits_non_zero_with_the_probe_findings(
    daemon: FakeDaemon, tmp_path: Path
) -> None:
    daemon.result = _QUARANTINED
    result = _certify(tmp_path, "codex")
    assert result.exit_code != exit_codes.OK
    assert "failed the conformance probe (capability_not_observed): tool_use" in result.output


def test_a_refused_probe_prints_the_daemon_code(daemon: FakeDaemon, tmp_path: Path) -> None:
    daemon.error = DaemonRpcError(
        -32002, "validation_failed: runtime_not_installed: claude-code has no binary"
    )
    result = _certify(tmp_path, "claude-code")
    assert result.exit_code != exit_codes.OK
    assert "runtime_not_installed" in result.output


def test_certify_without_a_runtime_sends_nothing(daemon: FakeDaemon, tmp_path: Path) -> None:
    """Error path: the runtime argument is required at argument parsing."""
    result = runner.invoke(app, ["--workspace", str(tmp_path), "runtime", "certify"])
    assert result.exit_code == click.UsageError("x").exit_code
    assert daemon.calls == []
