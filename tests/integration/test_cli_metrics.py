"""End-to-end CLI smoke tests for ``eawf metrics``.

The command is read-only — it never mutates ``state.json`` or appends to
the store. The tests drive the Typer app via :class:`CliRunner` against a
seeded ``.ea/state.json`` fixture and check three things:

1. Default text render: the Rich table is present and the four canonical
   metric labels appear.
2. JSON envelope: ``--json`` emits a payload with ``schema_version=1`` and
   the four sub-metric objects.
3. Plain render: ``--plain`` emits an ANSI-free table that callers (TUI
   overlay / release-notes paste) can consume.

A fourth case covers the error path: with no ``.ea/state.json`` reachable
the command exits 4 (NotFound) without panicking.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "states"


def _seed_state(tmp_path: Path) -> Path:
    """Copy ``09-estimates-and-actuals.json`` into a temp workspace."""
    workspace = tmp_path / "ws"
    state_dir = workspace / ".ea"
    state_dir.mkdir(parents=True)
    src = FIXTURES / "valid" / "09-estimates-and-actuals.json"
    state_path = state_dir / "state.json"
    state_path.write_bytes(src.read_bytes())
    return workspace


def test_metrics_default_render_emits_rich_table(tmp_path: Path) -> None:
    """Default invocation prints the four-metric rich table to stdout."""
    workspace = _seed_state(tmp_path)
    result = runner.invoke(app, ["-w", str(workspace), "metrics"])
    assert result.exit_code == 0, result.output
    assert "eawf metrics" in result.stdout
    assert "EU variance" in result.stdout
    assert "Audit pass rate" in result.stdout
    assert "Wave elapsed" in result.stdout
    assert "Planned vs reactive" in result.stdout


def test_metrics_json_envelope_schema_version_one(tmp_path: Path) -> None:
    """``--json`` emits a typed envelope with schema_version=1 and four metrics."""
    workspace = _seed_state(tmp_path)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics"])
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert set(payload.keys()) == {
        "schema_version",
        "eu_variance",
        "audit_pass_rate",
        "wave_elapsed",
        "planned_vs_reactive",
    }
    # Fixture has one IN_PROGRESS wave with no closed_at: every wave-derived
    # metric should read as zero samples.
    assert payload["eu_variance"]["sample_count"] == 0
    assert payload["wave_elapsed"]["sample_count"] == 0
    # Fixture has no audits ⇒ decided_count == 0.
    assert payload["audit_pass_rate"]["decided_count"] == 0
    # Single I01 wave ⇒ planned=1, reactive=0.
    assert payload["planned_vs_reactive"]["planned_count"] == 1
    assert payload["planned_vs_reactive"]["reactive_count"] == 0


def test_metrics_plain_render_is_ansi_free(tmp_path: Path) -> None:
    """``--plain`` emits a header + four metric rows without ANSI escape codes."""
    workspace = _seed_state(tmp_path)
    result = runner.invoke(app, ["--plain", "-w", str(workspace), "metrics"])
    assert result.exit_code == 0, result.output
    # The plain renderer uses a literal header string; the rich branch wraps
    # the title in box-drawing characters. Spotting either rules out the
    # branches accidentally swapping.
    assert "\x1b[" not in result.stdout
    assert "eawf metrics" in result.stdout
    for label in (
        "EU variance",
        "Audit pass rate",
        "Wave elapsed (min)",
        "Planned vs reactive",
    ):
        assert label in result.stdout


def test_metrics_missing_state_exits_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace pointer that resolves to a missing state file → exit 2 (NotFound).

    Using ``-w <empty-dir>`` is the most reliable way to drive the
    NotFound path under Typer's CliRunner: the resolver appends
    ``.ea/state.json`` to the workspace, and the load attempt fails
    closed with the canonical NotFound error rather than panicking.
    """
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = tmp_path / "no-state"
    workspace.mkdir()
    result = runner.invoke(app, ["-w", str(workspace), "metrics"])
    # Exit code 2 maps to NotFound in :mod:`eawf.cli.exit_codes`.
    assert result.exit_code == 2
