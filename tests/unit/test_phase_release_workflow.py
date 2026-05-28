from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_phase_release_workflow_exists_and_checks_version_source() -> None:
    workflow = _REPO_ROOT / ".github" / "workflows" / "phase-release.yaml"
    text = workflow.read_text(encoding="utf-8")

    assert "phase-release.yaml ships" in text
    assert "release=(v\\d+\\.\\d+\\.\\d+" in text
    assert "version mismatch" in text
    assert "src/eawf/_version.py" in text


def test_ci_release_readiness_matrix_job_exists() -> None:
    ci = (_REPO_ROOT / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")

    assert "ci.yaml release-readiness matrix job" in ci
    assert "release-readiness" in ci
    assert "commit-lint" in ci
    assert "phase-close-preflight" in ci
