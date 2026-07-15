"""Unit tests for ``eawf skill reconcile``.

Pins:

- ``skill reconcile --skills-root <clean>`` exits 0 and reports no drift
  (text + ``--json`` shapes).
- An injected drift (missing-on-disk / extra-on-disk / flag-mismatch) is
  reported; ``--check`` flips the exit code to ``VALIDATION_ERROR`` (2)
  while the default report-only mode stays exit 0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.runtime.runtimes.claude.plugin_install import _render_skill
from eawf.surfaces.cli.app import app
from eawf.surfaces.render.skills import SKILL_REGISTRY


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def _render_clean_tree(root: Path) -> None:
    for spec in SKILL_REGISTRY:
        skill_dir = root / spec.skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_render_skill(spec), encoding="utf-8")


def test_reconcile_clean_tree_exit_zero(cli_runner: CliRunner, tmp_path: Path) -> None:
    """A clean rendered tree reconciles with exit 0 and a no-drift line."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    result = cli_runner.invoke(app, ["skill", "reconcile", "--skills-root", str(root)])
    assert result.exit_code == 0, result.stdout
    assert "no drift" in result.stdout


def test_reconcile_clean_tree_json(cli_runner: CliRunner, tmp_path: Path) -> None:
    """``--json`` emits a machine-readable no-drift payload."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    result = cli_runner.invoke(app, ["--json", "skill", "reconcile", "--skills-root", str(root)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["has_drift"] is False
    assert payload["missing_on_disk"] == []
    assert payload["extra_on_disk"] == []
    assert payload["flag_mismatches"] == []


def test_reconcile_drift_reported_default_exit_zero(cli_runner: CliRunner, tmp_path: Path) -> None:
    """Default (report-only) mode reports drift but still exits 0."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    for child in (root / "audit").iterdir():
        child.unlink()
    (root / "audit").rmdir()
    result = cli_runner.invoke(app, ["skill", "reconcile", "--skills-root", str(root)])
    assert result.exit_code == 0, result.stdout
    assert "missing on disk" in result.stdout
    assert "/audit" in result.stdout


def test_reconcile_check_flag_exits_validation_error_on_drift(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    """``--check`` exits VALIDATION_ERROR (2) when drift is present."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    stray = root / "totally-bogus"
    stray.mkdir()
    (stray / "SKILL.md").write_text(
        "---\nname: totally-bogus\nuser-invocable: true\n"
        "disable-model-invocation: false\n---\nbody\n",
        encoding="utf-8",
    )
    result = cli_runner.invoke(app, ["skill", "reconcile", "--skills-root", str(root), "--check"])
    assert result.exit_code == 2, result.stdout
    assert "extra on disk" in result.stdout
    assert "/totally-bogus" in result.stdout


def test_reconcile_check_flag_clean_tree_exit_zero(cli_runner: CliRunner, tmp_path: Path) -> None:
    """``--check`` on a clean tree stays exit 0."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    result = cli_runner.invoke(app, ["skill", "reconcile", "--skills-root", str(root), "--check"])
    assert result.exit_code == 0, result.stdout
    assert "no drift" in result.stdout


def test_reconcile_flag_mismatch_json(cli_runner: CliRunner, tmp_path: Path) -> None:
    """A flag mismatch surfaces in the ``--json`` payload with both sides."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    research_md = root / "research" / "SKILL.md"
    research_md.write_text(
        research_md.read_text(encoding="utf-8").replace(
            "disable-model-invocation: false",
            "disable-model-invocation: true",
        ),
        encoding="utf-8",
    )
    result = cli_runner.invoke(app, ["--json", "skill", "reconcile", "--skills-root", str(root)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["has_drift"] is True
    mismatches = payload["flag_mismatches"]
    assert [m["name"] for m in mismatches] == ["research"]
    assert mismatches[0]["registry"]["disable_model_invocation"] is False
    assert mismatches[0]["disk"]["disable_model_invocation"] is True
