"""Integration tests for ``verify_implements`` — real git diff + cadence.

These tests stand up a real git repo in ``tmp_path`` and run the
kind end-to-end so the ``git diff <base>...HEAD`` path is exercised
against actual git refs.

Coverage:

* cadence matrix end-to-end — every-wave / every-iter / every-phase
  / manual each fire on their matching trigger, short-circuit
  otherwise.
* git diff actually drives the file_scopes restriction (marker in
  unchanged file is not counted).
* missing marker yields the canonical "unmet verify-implements"
  diagnostic carrying the wave id + verdict id.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.audit_dsl import CheckSpec
from eawf.audit_dsl.kinds.verify_implements import check_verify_implements


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True)


def _init_git_repo(repo: Path) -> None:
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "config", "user.email", "test@example.local"], repo)
    _run(["git", "config", "user.name", "test"], repo)
    # Disable global hooks (project pre-commit) for the tmp repo.
    _run(["git", "config", "core.hooksPath", "/dev/null"], repo)


def _write_wave_spec(
    repo: Path,
    *,
    wave_id: str,
    iter_id: str,
    phase_id: str,
    file_scopes: list[str],
    verdict_ids: list[str],
) -> Path:
    spec_dir = repo / ".ea" / "specs" / phase_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "kind: WaveSpec",
        f"id: {wave_id}",
        f"iter_id: {iter_id}",
        f"phase_id: {phase_id}",
        f"title: {wave_id} title",
        "agent_role: executor",
        "effort_bucket: L",
        "file_scopes:",
    ]
    lines.extend(f"  - {p}" for p in file_scopes)
    lines.append("implements:")
    for vid in verdict_ids:
        lines.append(f"  - verdict_id: {vid}")
        lines.append("    brief: .ea/artifacts/research/2026-05-16-c03-spec-infrastructure.md")
    lines.extend(
        [
            "behaviors:",
            "  - id: B1",
            "    text: observable behaviour described in twenty characters or more",
            "failure_modes:",
            "  - drift between spec and implementation",
            "---",
            "body",
        ]
    )
    target = spec_dir / f"{wave_id}.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _commit_all(repo: Path, message: str) -> None:
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", message], repo)


@pytest.fixture
def repo_with_spec(tmp_path: Path) -> Path:
    """A git repo seeded with a baseline commit + a WaveSpec + a stub source file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    # Baseline (main) commit — empty README, no spec yet.
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _commit_all(repo, "baseline")
    # Add the WaveSpec + a source file that will be edited on a branch.
    _write_wave_spec(
        repo,
        wave_id="P25-I01-W01",
        iter_id="P25-I01",
        phase_id="P25",
        file_scopes=["src/eawf/spec/common.py"],
        verdict_ids=["V12"],
    )
    (repo / "src" / "eawf" / "spec").mkdir(parents=True)
    (repo / "src" / "eawf" / "spec" / "common.py").write_text(
        "# baseline content\n", encoding="utf-8"
    )
    _commit_all(repo, "add spec + baseline source")
    return repo


def _build_spec(args: dict[str, Any]) -> CheckSpec:
    return CheckSpec(kind="verify_implements", name="vi-it", args=args)


def test_cadence_skip_does_not_invoke_git(repo_with_spec: Path) -> None:
    # cadence=manual + trigger=every-phase → must short-circuit even
    # before computing the diff.
    spec = _build_spec(
        {
            "phase_id": "P25",
            "diff_base": "main",
            "cadence": "manual",
            "current_trigger": "every-phase",
        }
    )
    result = check_verify_implements(spec, repo_with_spec)
    assert result.passed is True
    assert result.details is not None
    assert "skipped" in result.details


def test_marker_in_diff_passes(repo_with_spec: Path) -> None:
    # Edit the in-scope file on HEAD with a marker; diff base = first
    # baseline commit (HEAD~2 / main).
    target = repo_with_spec / "src" / "eawf" / "spec" / "common.py"
    target.write_text("# IMPLEMENTS: V12\nvalue = 1\n", encoding="utf-8")
    _commit_all(repo_with_spec, "add IMPLEMENTS marker")
    spec = _build_spec(
        {
            "phase_id": "P25",
            "diff_base": "main~1",
            "cadence": "every-phase",
            "current_trigger": "every-phase",
        }
    )
    result = check_verify_implements(spec, repo_with_spec)
    assert result.passed is True
    assert result.details is not None
    assert "all WaveSpec.implements markers satisfied" in result.details


def test_marker_missing_yields_canonical_diagnostic(
    repo_with_spec: Path,
) -> None:
    # Edit the in-scope file but WITHOUT the marker.
    target = repo_with_spec / "src" / "eawf" / "spec" / "common.py"
    target.write_text("# no marker here\nvalue = 1\n", encoding="utf-8")
    _commit_all(repo_with_spec, "edit common.py without marker")
    spec = _build_spec(
        {
            "phase_id": "P25",
            "diff_base": "main~1",
            "cadence": "every-phase",
            "current_trigger": "every-phase",
        }
    )
    result = check_verify_implements(spec, repo_with_spec)
    assert result.passed is False
    assert result.details is not None
    # Diagnostic shape locked by success criterion #3.
    assert "unmet verify-implements: wave='P25-I01-W01'" in result.details
    assert "expected_marker=V12" in result.details


def test_marker_in_out_of_scope_file_not_counted(
    repo_with_spec: Path,
) -> None:
    # Marker lives in a file NOT under the wave's file_scopes — must
    # be ignored (success criterion #2: grep RESTRICTED to file_scopes).
    (repo_with_spec / "src" / "eawf" / "spec" / "other.py").write_text(
        "# IMPLEMENTS: V12\n", encoding="utf-8"
    )
    (repo_with_spec / "src" / "eawf" / "spec" / "common.py").write_text(
        "# no marker here\n", encoding="utf-8"
    )
    _commit_all(repo_with_spec, "marker in out-of-scope file")
    spec = _build_spec(
        {
            "phase_id": "P25",
            "diff_base": "main~1",
            "cadence": "every-phase",
            "current_trigger": "every-phase",
        }
    )
    result = check_verify_implements(spec, repo_with_spec)
    assert result.passed is False
    assert result.details is not None
    assert "expected_marker=V12" in result.details


@pytest.mark.parametrize(
    "cadence, current_trigger, should_fire",
    [
        ("every-wave", "every-wave", True),
        ("every-iter", "every-iter", True),
        ("every-phase", "every-phase", True),
        ("manual", "manual", True),
        ("every-wave", "every-phase", False),
        ("every-iter", "every-phase", False),
        ("every-phase", "every-iter", False),
        ("manual", "every-phase", False),
    ],
)
def test_cadence_matrix_fires_on_match_only(
    repo_with_spec: Path,
    cadence: str,
    current_trigger: str,
    should_fire: bool,
) -> None:
    # Stage a real diff (without marker) so a "firing" cadence will
    # fail and a "skipping" cadence will pass with skipped details.
    (repo_with_spec / "src" / "eawf" / "spec" / "common.py").write_text(
        "# no marker here\n", encoding="utf-8"
    )
    _commit_all(repo_with_spec, "edit common.py")
    spec = _build_spec(
        {
            "phase_id": "P25",
            "diff_base": "main~1",
            "cadence": cadence,
            "current_trigger": current_trigger,
        }
    )
    result = check_verify_implements(spec, repo_with_spec)
    if should_fire:
        assert result.passed is False
        assert result.details is not None
        assert "unmet verify-implements" in result.details
    else:
        assert result.passed is True
        assert result.details is not None
        assert "skipped" in result.details
        assert f"cadence={cadence}" in result.details
        assert f"trigger={current_trigger}" in result.details
