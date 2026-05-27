"""Tests for the AGENTS.md tier-0 budget gate."""

from __future__ import annotations

from pathlib import Path

from eawf.platform.lint.tools import agents_md_budget


def _write_pyproject(tmp_path: Path, cap: int) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.eawf.agents_md_budget]\nmax-tier0-tokens = {cap}\n",
        encoding="utf-8",
    )


def test_count_tokens_is_stable_for_whitespace() -> None:
    assert agents_md_budget.count_tokens("one two\n\nthree") == 3


def test_check_budget_clean_for_reference_only_profiles(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, cap=1)

    report = agents_md_budget.check_budget(tmp_path)

    assert report.clean
    assert report.tokens == 0
    assert report.blocks == ()


def test_main_exits_zero_for_current_profiles(tmp_path: Path, capsys) -> None:
    _write_pyproject(tmp_path, cap=1)

    code = agents_md_budget.main(["--repo-root", str(tmp_path)])

    assert code == 0
    assert "agents-md-budget: clean" in capsys.readouterr().out
