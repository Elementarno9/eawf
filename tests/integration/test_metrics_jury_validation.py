"""CLI + render tests for ``eawf metrics jury-validation``.

The sub-verb renders the cross-vendor jury validated against its ground-truth
cohort. Two halves are pinned:

- the honest-negative path through the live CLI: a seeded state whose ballot /
  gold stores are empty reduces to an empty cohort, so the reducer refuses to
  score and the render carries the "insufficient signal (n=0)" banner with a
  zero exit code -- never a fabricated number;
- the pure renderer + the BlockAuthority derivation over directly-constructed
  :class:`JuryValidationReport` objects, so a scored cohort surfaces its Fleiss
  kappa / Brier / ECE / catch-rate lines without driving the whole CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from eawf.observability.eval.jury_validation import (
    JuryValidationReport,
    JuryValidationStatus,
)
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.metrics import (
    _AUTHORITY_ADVISORY,
    _AUTHORITY_BLOCKING,
    _block_authority,
    _render_jury_validation,
)

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "states"


def _seed_state(tmp_path: Path) -> Path:
    """Copy a valid state fixture into a temp workspace with an empty store."""
    workspace = tmp_path / "ws"
    state_dir = workspace / ".ea"
    state_dir.mkdir(parents=True)
    src = FIXTURES / "valid" / "09-estimates-and-actuals.json"
    (state_dir / "state.json").write_bytes(src.read_bytes())
    return workspace


def _scored_report() -> JuryValidationReport:
    """Build a scored report with a caught known-bad cohort (catch rate 0.0)."""
    return JuryValidationReport(
        n=24,
        status=JuryValidationStatus.SCORED,
        fleiss_kappa=0.875,
        brier=0.125,
        ece=0.0625,
        unanimous_pass_on_known_bad_rate=0.0,
        known_bad_n=6,
    )


# --- C: live CLI honest-empty banner exits 0 -------------------------------


def test_jury_validation_empty_cohort_renders_insufficient_banner(tmp_path: Path) -> None:
    """An empty cohort renders the insufficient-signal banner and exits 0."""
    workspace = _seed_state(tmp_path)
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "jury-validation"])
    assert result.exit_code == 0, result.output
    assert "insufficient signal (n=0)" in result.stdout
    assert "jury validation: insufficient" in result.stdout
    # No fabricated metric: every metric line stays dashed.
    assert "fleiss kappa     --" in result.stdout


def test_jury_validation_empty_cohort_json_envelope(tmp_path: Path) -> None:
    """``--json`` over an empty cohort emits the insufficient report + advisory tier."""
    workspace = _seed_state(tmp_path)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics", "jury-validation"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "insufficient"
    assert payload["n"] == 0
    assert payload["fleiss_kappa"] is None
    assert payload["brier"] is None
    assert payload["ece"] is None
    assert payload["block_authority"] == _AUTHORITY_ADVISORY


def test_jury_validation_no_state_exits_nonzero(tmp_path: Path) -> None:
    """With no reachable ``state.json`` the sub-verb fails closed (NotFound)."""
    workspace = tmp_path / "no-state"
    workspace.mkdir()
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "jury-validation"])
    assert result.exit_code != 0


# --- C: pure renderer over a scored report ---------------------------------


def test_render_scored_report_surfaces_metric_lines() -> None:
    """A scored cohort renders the kappa / Brier / ECE / catch-rate lines."""
    text = _render_jury_validation(_scored_report())
    assert "jury validation: scored (n=24)" in text
    assert "fleiss kappa     0.875" in text
    assert "brier            0.125" in text
    assert "ece              0.062" in text
    assert "0.000 over 6 known-bad" in text
    # A scored, well-caught cohort earns BLOCKING.
    assert f"block authority  {_AUTHORITY_BLOCKING}" in text
    # The insufficient banner must NOT appear on a scored report.
    assert "insufficient signal" not in text


def test_render_insufficient_report_dashes_every_metric() -> None:
    """An insufficient report dashes every metric and stays advisory."""
    report = JuryValidationReport(n=3, status=JuryValidationStatus.INSUFFICIENT, known_bad_n=1)
    text = _render_jury_validation(report)
    assert "insufficient signal (n=3)" in text
    assert "fleiss kappa     --" in text
    assert "brier            --" in text
    assert f"block authority  {_AUTHORITY_ADVISORY}" in text


# --- C: block-authority derivation -----------------------------------------


def test_block_authority_insufficient_is_advisory() -> None:
    """A starved cohort is held ADVISORY -- no scored signal earns block authority."""
    report = JuryValidationReport(n=2, status=JuryValidationStatus.INSUFFICIENT, known_bad_n=0)
    assert _block_authority(report) == _AUTHORITY_ADVISORY


def test_block_authority_scored_caught_is_blocking() -> None:
    """A scored cohort whose catch rate clears the floor earns BLOCKING."""
    assert _block_authority(_scored_report()) == _AUTHORITY_BLOCKING


def test_block_authority_scored_false_clean_stays_advisory() -> None:
    """A scored cohort that falsely cleaned its known-bad waves stays ADVISORY.

    A high unanimous-pass-on-known-bad rate (the jury's worst failure mode)
    keeps the jury advisory even on a scored cohort -- it cannot earn block
    authority while it lets known-bad waves through.
    """
    report = JuryValidationReport(
        n=24,
        status=JuryValidationStatus.SCORED,
        fleiss_kappa=0.9,
        brier=0.2,
        ece=0.1,
        unanimous_pass_on_known_bad_rate=0.5,
        known_bad_n=4,
    )
    assert _block_authority(report) == _AUTHORITY_ADVISORY


def test_block_authority_scored_no_known_bad_is_blocking() -> None:
    """A scored cohort with no known-bad denominator (rate None) earns BLOCKING.

    The false-clean rate is undefined with an empty known-bad denominator, so it
    cannot demote the jury back to advisory; a scored, agreeing cohort clears.
    """
    report = JuryValidationReport(
        n=24,
        status=JuryValidationStatus.SCORED,
        fleiss_kappa=0.95,
        brier=0.05,
        ece=0.02,
        unanimous_pass_on_known_bad_rate=None,
        known_bad_n=0,
    )
    assert _block_authority(report) == _AUTHORITY_BLOCKING
