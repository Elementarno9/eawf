"""The CI-only phase-PR gates pass locally.

The coverage-gate parse and the snapshot-pairing gate fire first on the
phase PR (see the ship-process rule); running them here keeps them from
surfacing late — the P27 #24 lesson, now a standing suite.

The plugin-release publish gates belong to the same family and are worse:
they fire only on a ``v*`` tag push, so a defect in them is discovered by
the users it already shipped to. Both assert on the workflow source
instead, and each ships a companion test that feeds the checker a
synthetic defective workflow to prove the gate reds.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_RELEASE = _REPO_ROOT / ".github" / "workflows" / "plugin-release.yaml"


def test_coverage_gate_config_parses_and_classifies() -> None:
    """The coverage tool imports and its threshold config resolves."""
    sys.path.insert(0, str(_REPO_ROOT / "tools"))
    try:
        import coverage_gate

        assert callable(coverage_gate.run_gate)
        assert callable(coverage_gate._classes_for_gate)
    finally:
        sys.path.pop(0)


def test_snapshot_pairing_gate_passes_over_the_phase_range() -> None:
    """tools/snapshot_pairing_gate.py accepts the phase commit range."""
    base = subprocess.run(
        ["git", "merge-base", "origin/main", "HEAD"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=True,
    ).stdout.strip()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=True,
    ).stdout.strip()
    result = subprocess.run(
        [sys.executable, "tools/snapshot_pairing_gate.py", base, head],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"snapshot pairing gate failed over {base[:8]}..{head[:8]}:\n"
        f"{result.stdout[-1500:]}\n{result.stderr[-800:]}"
    )


# --- plugin-release publish gates -------------------------------------------


def _load_plugin_release() -> dict[str, Any]:
    """Parse ``.github/workflows/plugin-release.yaml``."""
    workflow: dict[str, Any] = yaml.safe_load(_PLUGIN_RELEASE.read_text(encoding="utf-8"))
    return workflow


def _find_step(job: dict[str, Any], name_fragment: str) -> dict[str, Any] | None:
    """Return the first step of *job* whose name contains *name_fragment*."""
    steps: list[dict[str, Any]] = job.get("steps", [])
    for step in steps:
        if name_fragment.lower() in str(step.get("name", "")).lower():
            return step
    return None


def npm_dist_tag_violations(workflow: dict[str, Any]) -> list[str]:
    """Report every way *workflow* could publish npm without a derived tag."""
    problems: list[str] = []
    jobs = workflow.get("jobs", {})

    package = jobs.get("package-validate", {})
    if "dist_tag" not in package.get("outputs", {}):
        problems.append("package-validate declares no 'dist_tag' job output")
    resolve = _find_step(package, "dist-tag")
    if resolve is None:
        problems.append("no package-validate step resolves the dist-tag")
    elif "dist_tag_for_version" not in str(resolve.get("run", "")):
        problems.append("dist-tag is not derived via dist_tag_for_version()")

    publish = _find_step(jobs.get("publish-claude-npm", {}), "publish to npm")
    if publish is None:
        problems.append("publish-claude-npm has no 'Publish to npm' step")
        return problems

    run = str(publish.get("run", ""))
    if "npm publish" not in run:
        problems.append("the npm publish step runs no 'npm publish'")
    if not re.search(r"--tag\s+\"?\$\{?DIST_TAG\}?", run):
        problems.append("npm publish does not pass --tag with the derived dist-tag")
    dist_tag_env = str(publish.get("env", {}).get("DIST_TAG", ""))
    if "needs.package-validate.outputs.dist_tag" not in dist_tag_env:
        problems.append("DIST_TAG is not wired from the package-validate output")
    return problems


def codex_history_violations(workflow: dict[str, Any]) -> list[str]:
    """Report every way *workflow* could erase a published Codex version."""
    problems: list[str] = []
    job = workflow.get("jobs", {}).get("publish-codex-branch", {})

    step = _find_step(job, "plugins-dist branch")
    if step is None:
        problems.append("publish-codex-branch has no plugins-dist publish step")
        return problems

    run = str(step.get("run", ""))
    pushes = [line.strip() for line in run.splitlines() if "git push" in line]
    if not pushes:
        problems.append("the plugins-dist publish step runs no 'git push'")
    if "--force" in run:
        problems.append("force-push to plugins-dist erases prior published versions")
    if re.search(r"git push\b[^\n]*\s-f\b", run):
        problems.append("force-push (-f) to plugins-dist erases prior published versions")
    if "--orphan" in run:
        problems.append("an orphan commit discards the published plugins-dist history")
    if not re.search(r"versions/\$\{?PLUGIN_VERSION\}?", run):
        problems.append("no per-version path retains the previous Codex plugin version")
    return problems


def test_npm_dist_tag_is_derived_from_the_published_version() -> None:
    """The live workflow publishes npm under the version-derived dist-tag."""
    assert npm_dist_tag_violations(_load_plugin_release()) == []


def test_npm_dist_tag_gate_reds_on_an_untagged_publish() -> None:
    """The gate fires on the real defect it exists to catch: a bare publish."""
    defective = yaml.safe_load(
        """
        jobs:
          package-validate:
            outputs:
              version: "0.7.0"
            steps:
              - name: Resolve plugin version
                run: echo version=0.7.0
          publish-claude-npm:
            steps:
              - name: Publish to npm
                run: npm publish --access public
        """
    )
    problems = npm_dist_tag_violations(defective)
    assert any("--tag" in problem for problem in problems), problems
    assert any("dist_tag" in problem for problem in problems), problems


def test_codex_history_survives_the_plugins_dist_publish() -> None:
    """The live workflow appends to plugins-dist and keeps a per-version path."""
    assert codex_history_violations(_load_plugin_release()) == []


def test_codex_history_gate_reds_on_an_orphan_force_push() -> None:
    """The gate fires on the real defect: an orphan commit force-pushed."""
    defective = yaml.safe_load(
        """
        jobs:
          publish-codex-branch:
            steps:
              - name: Force-write rendered Codex tree to plugins-dist branch
                run: |
                  git checkout -q --orphan plugins-dist
                  git commit -q -m publish
                  git push -q --force origin plugins-dist
        """
    )
    problems = codex_history_violations(defective)
    assert any("force-push" in problem for problem in problems), problems
    assert any("orphan" in problem for problem in problems), problems
    assert any("per-version" in problem for problem in problems), problems


def test_codex_history_gate_reds_on_a_missing_publish_step() -> None:
    """A publish job with no recognizable publish step is itself a violation."""
    defective = yaml.safe_load("jobs:\n  publish-codex-branch:\n    steps: []\n")
    assert codex_history_violations(defective) == [
        "publish-codex-branch has no plugins-dist publish step"
    ]
