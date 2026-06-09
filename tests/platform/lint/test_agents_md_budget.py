"""Tests for the AGENTS.md tier-0 budget gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.platform.lint.tools import agents_md_budget
from eawf.platform.profiles import load_profile
from eawf.platform.render_block import DEFAULT_TIER0_TOKEN_CAP

# The always-on tier-0 set tagged in ``core.yaml``. These are the
# irreversible "no-tooling-backstop" rules: a lapse cannot be caught by
# any lint or gate, so the agent must internalise them before a wave.
_EXPECTED_TIER0_BLOCK_IDS = {
    "non-negotiable-rules",
    "state-vs-specs",
    "worktree-discipline",
    "prep-plan-mode",
    "iter-phase-close-timing",
}

# Rules whose discipline has an automated backstop, so they are
# ``reference`` (off the tier-0 budget), not tier-0.
#   * planned-scope-revisability -> the PENDING-only guard in
#     ``edit_wave_plan`` / ``remove_wave_plan``.
#   * agent-report-contract -> the typed ``AgentReportBody`` /
#     ``AgentReportVerdict`` ingestion boundary.
_TOOLING_BACKED_REFERENCE_BLOCK_IDS = {
    "planned-scope-revisability",
    "agent-report-contract",
}

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_pyproject(tmp_path: Path, cap: int) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.eawf.agents_md_budget]\nmax-tier0-tokens = {cap}\n",
        encoding="utf-8",
    )


def _write_overcap_profile(workspace: Path, *, profile_id: str, token_count: int) -> None:
    """Drop a synthetic profile with one tier-0 block of *token_count* tokens.

    The block body is ``token_count`` space-separated words, so the
    gate's whitespace token counter reports exactly that weight. Written
    under ``<workspace>/.ea/profiles/`` so profile discovery surfaces it
    alongside the bundled set.
    """
    profiles_dir = workspace / ".ea" / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    body = " ".join(["word"] * token_count)
    (profiles_dir / f"{profile_id}.yaml").write_text(
        "name: " + profile_id + "\n"
        'version: "1.0"\n'
        "render_blocks:\n"
        "  - id: synthetic-overcap\n"
        "    target: AGENTS.md\n"
        "    tier: tier0\n"
        '    version: "1.0"\n'
        "    body_template: |\n"
        f"      {body}\n",
        encoding="utf-8",
    )


def test_count_tokens_is_stable_for_whitespace() -> None:
    assert agents_md_budget.count_tokens("one two\n\nthree") == 3


def test_check_budget_names_tier0_blocks_under_cap_on_real_tree() -> None:
    """The bundled tier-0 set is exactly the no-tooling-backstop rules and fits the cap.

    Binding-proof: ``check_budget`` over the real repo names the expected
    tier-0 block ids, reports their summed tokens at or under the 1200
    cap, and excludes the tooling-backed rules (which are ``reference``).
    """
    report = agents_md_budget.check_budget(_REPO_ROOT)

    tagged_ids = {entry.split(":")[1] for entry in report.blocks}
    assert tagged_ids == _EXPECTED_TIER0_BLOCK_IDS
    # No tooling-backed rule leaked into the tier-0 set.
    assert tagged_ids.isdisjoint(_TOOLING_BACKED_REFERENCE_BLOCK_IDS)
    # The cap the gate enforces is the 1200-token ratchet.
    assert report.cap == DEFAULT_TIER0_TOKEN_CAP == 1200
    assert report.tokens <= 1200
    assert report.clean


def test_check_budget_tooling_backed_rules_are_reference() -> None:
    """A rule with an automated backstop is ``reference``, never tier-0.

    The two reconciled blocks are enforced by typed transitions / models,
    so they stay off the always-on tier-0 budget layer.
    """
    core = load_profile("core")
    by_id = {b.id: b for b in core.render_blocks}

    for block_id in _TOOLING_BACKED_REFERENCE_BLOCK_IDS:
        assert by_id[block_id].tier == "reference"


def test_check_budget_reports_nonzero_tier0_weight(tmp_path: Path) -> None:
    """The bundled profiles tag a non-empty tier-0 set with real weight."""
    _write_pyproject(tmp_path, cap=10_000)

    report = agents_md_budget.check_budget(tmp_path)

    # Tier-0 blocks are tagged, so the gate accounts for a positive weight
    # (it is no longer vacuously zero as it was before any block was tagged).
    assert report.tokens > 0
    assert report.blocks  # non-empty
    tagged_ids = {entry.split(":")[1] for entry in report.blocks}
    assert tagged_ids == _EXPECTED_TIER0_BLOCK_IDS


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


def test_check_budget_over_cap_when_synthetic_tier0_exceeds_cap(tmp_path: Path) -> None:
    """A synthesized tier-0 profile that blows the cap reports over-budget.

    Negative-path: the workspace overlay adds a tier-0 block whose token
    weight alone exceeds the 1200 cap, so ``check_budget`` reports the
    summed tier-0 weight over the cap and names the synthetic block.
    """
    _write_pyproject(tmp_path, cap=DEFAULT_TIER0_TOKEN_CAP)
    _write_overcap_profile(tmp_path, profile_id="overcap", token_count=1500)

    report = agents_md_budget.check_budget(tmp_path, workspace=tmp_path)

    assert not report.clean
    assert report.tokens > report.cap
    assert any("overcap:synthetic-overcap" in entry for entry in report.blocks)


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


def test_main_exits_one_when_synthetic_tier0_exceeds_cap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A synthesized over-cap tier-0 set drives ``main()`` to exit non-zero."""
    _write_pyproject(tmp_path, cap=DEFAULT_TIER0_TOKEN_CAP)
    _write_overcap_profile(tmp_path, profile_id="overcap", token_count=1500)

    code = agents_md_budget.main(["--repo-root", str(tmp_path), "--workspace", str(tmp_path)])

    err = capsys.readouterr().err
    assert code == 1
    assert f"exceeds cap={DEFAULT_TIER0_TOKEN_CAP}" in err
