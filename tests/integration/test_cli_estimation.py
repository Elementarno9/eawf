"""End-to-end CLI tests for ``eawf metrics variance`` on a tree with no estimates.

A born epoch-2 tree holds empty estimate and actual ledgers. The variance
read must degrade to the declared empty-data payload instead of erroring
or printing a fabricated ``0.0%``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from tests._epoch2_helpers import lay_epoch2_tree

pytestmark = pytest.mark.integration

runner = CliRunner()


def _born_workspace(tmp_path: Path) -> Path:
    """Bear an epoch-2 tree with empty estimate and actual ledgers."""
    workspace = tmp_path / "ws"
    lay_epoch2_tree(workspace)
    return workspace


def test_metrics_variance_with_no_estimates_exits_zero_with_no_data(tmp_path: Path) -> None:
    workspace = _born_workspace(tmp_path)

    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics", "variance"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["sample_count"] == 0
    assert payload["planned_eu"] == pytest.approx(0.0)
    assert payload["actual_eu"] == pytest.approx(0.0)
    assert payload["variance_pct"] is None


def test_metrics_variance_with_no_estimates_renders_no_data_text(tmp_path: Path) -> None:
    workspace = _born_workspace(tmp_path)

    result = runner.invoke(app, ["-w", str(workspace), "metrics", "variance"])

    assert result.exit_code == 0, result.output
    assert "no data" in result.output
    assert "%" not in result.output
