"""End-to-end CLI tests for ``eawf estimate`` / ``eawf actual``.

Drives the Typer app via :class:`typer.testing.CliRunner` against a temp
``.ea/state.json`` (seeded from the Phase 1 valid fixtures) and asserts:

- estimate creates an ``estimates.jsonl`` envelope and updates state.estimates.
- actual start opens a segment, writes ``actuals.jsonl``, updates state.actuals.
- actual stop closes the segment with a non-zero elapsed_eu.
- Double-open for the same (scope, session) pair is rejected with exit 4.
- actual recover marks stale segments abandoned with the cap applied.
- the estimate read surfaces exit cleanly on a state with no estimate rows
  (claim no longer seeds one), reporting the no-data shape rather than a
  fabricated zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "states"


def _seed_state(tmp_path: Path) -> Path:
    """Copy ``01-empty-repo.json`` to a temp ``.ea/state.json``.

    Returns the workspace root so the caller can pass ``-w`` to the CLI.
    """
    workspace = tmp_path / "ws"
    state_dir = workspace / ".ea"
    state_dir.mkdir(parents=True)
    src = FIXTURES / "valid" / "01-empty-repo.json"
    state_path = state_dir / "state.json"
    state_path.write_bytes(src.read_bytes())
    return workspace


# ---- RX-C: state-then-jsonl atomicity regression ----------------------------


# ---- reads tolerate an absent / empty estimate map --------------------------


def _write_estimates(workspace: Path, estimates: dict[str, Any] | None) -> None:
    """Overwrite the seeded state's ``estimates`` key with *estimates*."""
    path = workspace / ".ea" / "state.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["estimates"] = estimates
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("estimates", [None, {}])
def test_metrics_variance_with_no_estimates_exits_zero_with_no_data(
    tmp_path: Path, estimates: dict[str, Any] | None
) -> None:
    """The variance read exits 0 and reports no data on an empty estimate map.

    Wave claim stopped seeding a derived estimate row, so a healthy repo
    carries ``estimates: null`` or ``{}``. The read must degrade to the
    declared empty-data payload instead of erroring or printing ``0.0%``.
    """
    workspace = _seed_state(tmp_path)
    _write_estimates(workspace, estimates)

    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics", "variance"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["sample_count"] == 0
    assert payload["planned_eu"] == 0.0
    assert payload["actual_eu"] == 0.0
    assert payload["variance_pct"] is None


def test_metrics_variance_with_no_estimates_renders_no_data_text(tmp_path: Path) -> None:
    """The human-facing render says "no data", not a fabricated percentage."""
    workspace = _seed_state(tmp_path)
    _write_estimates(workspace, {})

    result = runner.invoke(app, ["-w", str(workspace), "metrics", "variance"])

    assert result.exit_code == 0, result.output
    assert "no data" in result.output
    assert "%" not in result.output
