"""End-to-end CLI smoke tests for the bare ``eawf metrics`` view.

The bare command renders the selected generation's estimation roll-up and
never writes. The tests drive the Typer app via :class:`CliRunner` against
a born epoch-2 tree and check the Rich table, the JSON envelope and the
ANSI-free plain render, plus the refusals: an absent tree exits 1
(NotFound) and an epoch-1 tree exits 4 (MigrationRequired).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests._epoch2_helpers import lay_epoch2_tree

pytestmark = pytest.mark.integration

runner = CliRunner()

_LABELS = ("Estimates", "Actuals", "Estimate-actual variance")


def _born_workspace(tmp_path: Path) -> Path:
    """Bear an epoch-2 tree with empty estimate and actual ledgers."""
    workspace = tmp_path / "ws"
    lay_epoch2_tree(workspace)
    return workspace


def test_metrics_default_render_emits_rich_table(tmp_path: Path) -> None:
    result = runner.invoke(app, ["-w", str(_born_workspace(tmp_path)), "metrics"])
    assert result.exit_code == exit_codes.OK, result.output
    assert "eawf metrics" in result.stdout
    for label in _LABELS:
        assert label in result.stdout


def test_metrics_json_envelope_reports_empty_ledgers(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--json", "-w", str(_born_workspace(tmp_path)), "metrics"])
    assert result.exit_code == exit_codes.OK, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["schema_version"] == 2
    assert (payload["estimate_count"], payload["actual_count"]) == (0, 0)
    assert payload["unreadable_count"] == 0
    assert payload["variance"]["sample_count"] == 0
    assert payload["variance"]["variance_pct"] is None


def test_metrics_plain_render_is_ansi_free(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--plain", "-w", str(_born_workspace(tmp_path)), "metrics"])
    assert result.exit_code == exit_codes.OK, result.output
    assert "\x1b[" not in result.stdout
    assert "eawf metrics" in result.stdout
    assert "n/a (samples=0)" in result.stdout
    for label in _LABELS:
        assert label in result.stdout


def test_metrics_missing_tree_exits_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = tmp_path / "no-state"
    workspace.mkdir()
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics"])
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    assert json.loads(result.stdout)["data"]["kind"] == "NotFound"


def test_metrics_epoch1_tree_exits_migration_required(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".ea").mkdir(parents=True)
    (workspace / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics"])
    assert result.exit_code == exit_codes.ATTACH_FAILURE, result.output
    assert json.loads(result.stdout)["data"]["kind"] == "MigrationRequired"
