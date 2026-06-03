"""Tests for the AGENTS.md tier-0 budget gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.platform.lint.tools import agents_md_budget

# The always-on tier-0 set tagged in ``core.yaml`` (P29-I07-W06). These
# are the load-bearing blocks an agent must internalise before a wave.
_EXPECTED_TIER0_BLOCK_IDS = {
    "non-negotiable-rules",
    "state-vs-specs",
    "worktree-discipline",
    "planned-scope-revisability",
    "prep-plan-mode",
    "iter-phase-close-timing",
    "agent-report-contract",
}


def _write_pyproject(tmp_path: Path, cap: int) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.eawf.agents_md_budget]\nmax-tier0-tokens = {cap}\n",
        encoding="utf-8",
    )


def test_count_tokens_is_stable_for_whitespace() -> None:
    assert agents_md_budget.count_tokens("one two\n\nthree") == 3


def test_check_budget_reports_nonzero_tier0_weight(tmp_path: Path) -> None:
    """The bundled profiles tag a non-empty tier-0 set with real weight."""
    _write_pyproject(tmp_path, cap=10_000)

    report = agents_md_budget.check_budget(tmp_path)

    # Tier-0 blocks are tagged, so the gate accounts for a positive weight
    # (it is no longer vacuously zero as it was before any block was tagged).
    assert report.tokens > 0
    assert report.blocks  # non-empty
    tagged_ids = {entry.split(":")[1] for entry in report.blocks}
    assert tagged_ids >= _EXPECTED_TIER0_BLOCK_IDS


def test_check_budget_clean_under_generous_cap(tmp_path: Path) -> None:
    """With a cap above the tagged weight the gate is clean."""
    _write_pyproject(tmp_path, cap=10_000)

    report = agents_md_budget.check_budget(tmp_path)

    assert report.clean
    assert report.tokens <= report.cap


def test_check_budget_dirty_when_cap_below_weight(tmp_path: Path) -> None:
    """A cap below the tagged tier-0 weight trips the gate."""
    _write_pyproject(tmp_path, cap=1)

    report = agents_md_budget.check_budget(tmp_path)

    assert not report.clean
    assert report.tokens > report.cap


def test_main_exits_zero_under_generous_cap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pyproject(tmp_path, cap=10_000)

    code = agents_md_budget.main(["--repo-root", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "agents-md-budget: clean" in out
    assert "tier0_tokens=" in out


def test_main_exits_one_when_cap_below_weight(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pyproject(tmp_path, cap=1)

    code = agents_md_budget.main(["--repo-root", str(tmp_path)])

    err = capsys.readouterr().err
    assert code == 1
    assert "exceeds cap=1" in err
