"""CLI test for ``eawf metrics conduct``, the conduct deviation rate view."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from eawf.kernel.state.enums import IncidentSeverity
from eawf.platform.rules import conduct_obligation_ids, record_conduct_deviation
from eawf.surfaces.cli.app import app
from tests._epoch2_helpers import lay_epoch2_tree


def _seed_state(tmp_path: Path) -> Path:
    """Bear an epoch-2 tree whose Task ledger records no completed Task."""
    workspace = tmp_path / "ws"
    lay_epoch2_tree(workspace)
    return workspace


def test_metrics_conduct_reports_rate_from_local_store(tmp_path: Path) -> None:
    workspace = _seed_state(tmp_path)
    record_conduct_deviation(
        workspace / ".ea" / "state.json",
        obligation_id="conduct.default-continue",
        scope_id="P01-I01-W01",
        run_id="run-1",
        runtime="claude",
        detection="review",
        severity=IncidentSeverity.MEDIUM,
        evidence_ref="review:finding-1",
    )

    result = CliRunner().invoke(app, ["--json", "-w", str(workspace), "metrics", "conduct"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    body = payload.get("data", payload)
    assert body["deviation_count"] == 1
    assert body["by_runtime"] == {"claude": 1}
    assert body["by_obligation"]["conduct.default-continue"] == 1
    assert set(body["by_obligation"]) == conduct_obligation_ids()


def test_metrics_conduct_renders_empty_history(tmp_path: Path) -> None:
    workspace = _seed_state(tmp_path)

    result = CliRunner().invoke(app, ["-w", str(workspace), "metrics", "conduct"])

    assert result.exit_code == 0, result.output
    assert "conduct deviations: 0" in result.stdout
    assert f"never breached: {len(conduct_obligation_ids())} of" in result.stdout
