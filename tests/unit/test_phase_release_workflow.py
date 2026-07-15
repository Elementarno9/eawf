from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The four release-automation contracts (commit-lint / phase-close-preflight /
# version-coupling / release-notes) are pinned by these four test files. They
# used to also run in a dedicated `release-readiness` matrix job in ci.yaml,
# but that job was pure duplication: the main `test` job already collects the
# whole tests/ tree, so it runs these four files too.
_RELEASE_CONTRACT_TESTS = (
    "tests/unit/test_commit_prefix_lint.py",
    "tests/unit/test_lifecycle_phase_prepare_close.py",
    "tests/unit/test_version_coupling.py",
    "tests/unit/test_render_release_notes.py",
)


def test_phase_release_workflow_exists_and_checks_version_source() -> None:
    workflow = _REPO_ROOT / ".github" / "workflows" / "phase-release.yaml"
    text = workflow.read_text(encoding="utf-8")

    assert "phase-release.yaml ships" in text
    assert "release=(v\\d+\\.\\d+\\.\\d+" in text
    assert "version mismatch" in text
    assert "src/eawf/_version.py" in text


def test_release_contract_tests_run_in_main_test_job() -> None:
    ci_path = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"
    ci_text = ci_path.read_text(encoding="utf-8")
    ci = yaml.safe_load(ci_text)

    # The redundant release-readiness matrix job is gone; its four checks are
    # already covered by the four release-contract test files below.
    assert "release-readiness" not in ci["jobs"]
    assert "ci.yaml release-readiness matrix job" not in ci_text

    # The main `test` job runs the whole tests/ tree (pyproject testpaths) minus
    # the TUI dirs it --ignores. Gather every path the parallel step excludes.
    test_job = ci["jobs"]["test"]
    run_lines = " ".join(
        step["run"] for step in test_job["steps"] if isinstance(step.get("run"), str)
    )
    assert "uv run pytest" in run_lines
    ignored = {
        token.split("=", 1)[1] for token in run_lines.split() if token.startswith("--ignore=")
    }

    # Every release-contract test file exists, lives under tests/, and is not
    # excluded by an --ignore, so the main `test` job collects and runs it.
    for rel in _RELEASE_CONTRACT_TESTS:
        assert (_REPO_ROOT / rel).is_file(), rel
        assert rel.startswith("tests/"), rel
        excluded = any(
            rel == ignore or rel.startswith(ignore.rstrip("/") + "/") for ignore in ignored
        )
        assert not excluded, rel
