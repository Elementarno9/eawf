"""The CI-only phase-PR gates pass locally.

The coverage-gate parse and the snapshot-pairing gate fire first on the
phase PR (see the ship-process rule); running them here keeps them from
surfacing late — the P27 #24 lesson, now a standing suite.

The plugin-release publish gates belong to the same family and are worse:
they fire only on a ``v*`` tag push, so a defect in them is discovered by
the users it already shipped to. Both assert on the workflow source
instead, and each ships a companion test that feeds the checker a
synthetic defective workflow to prove the gate reds.

The Linux real-host jail gate joins the same family from the other end: it
guards a job that must EXIST at all, because the sandbox's Linux backend is
otherwise exercised only by argv shape on hosts without bubblewrap -- a
prefix bwrap refuses to mount then reads as green.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.workflow.release.receipts import RECEIPT_FILENAMES

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_RELEASE = _REPO_ROOT / ".github" / "workflows" / "plugin-release.yaml"
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"
_RELEASE = _REPO_ROOT / ".github" / "workflows" / "release.yaml"


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


# --- release tag-push chokepoint --------------------------------------------


def _load_release() -> dict[str, Any]:
    """Parse ``.github/workflows/release.yaml``."""
    workflow: dict[str, Any] = yaml.safe_load(_RELEASE.read_text(encoding="utf-8"))
    return workflow


def tag_chokepoint_violations(workflow: dict[str, Any]) -> list[str]:
    """Report every way *workflow* could publish a tag no preflight swept.

    ``eawf release tag --push`` runs the readiness sweep before it
    pushes, but the tag is only a git ref: a hand-pushed one reaches the
    publish job by the same route with nothing checked. The publish job
    must therefore need a job that runs the same sweep, and that job must
    actually run on a tag push and be allowed to fail the workflow.

    Args:
        workflow: The parsed release workflow.

    Returns:
        One human-readable problem per violation; empty when the preflight
        job is present, tag-push-reaching, on ubuntu-24.04, running
        ``eawf release preflight`` with no swallowed exit code, and needed
        by the publish job alongside the green-CI gate.
    """
    problems: list[str] = []
    jobs = workflow.get("jobs", {})
    job = jobs.get("release-preflight")
    if job is None:
        return ["release.yaml declares no 'release-preflight' job"]

    if job.get("runs-on") != "ubuntu-24.04":
        problems.append("the release-preflight job does not run on ubuntu-24.04")
    condition = str(job.get("if", ""))
    if condition and "refs/tags/v" not in condition:
        problems.append("the release-preflight job's condition can skip it on a tag push")

    steps: list[dict[str, Any]] = job.get("steps", [])
    if not any("eawf release preflight" in str(step.get("run", "")) for step in steps):
        problems.append("no release-preflight step runs 'eawf release preflight'")
    if any(step.get("continue-on-error") for step in steps):
        problems.append("a release-preflight step is continue-on-error, so a red sweep passes")

    publish_needs = jobs.get("publish-pypi", {}).get("needs", [])
    for required in ("release-preflight", "require-green-ci"):
        if required not in publish_needs:
            problems.append(f"publish-pypi does not need the {required} job")
    return problems


def test_tag_chokepoint_gates_the_publish_job() -> None:
    """The live release workflow sweeps the tag before it can publish it."""
    assert tag_chokepoint_violations(_load_release()) == []


def test_tag_chokepoint_gate_reds_on_a_bypassing_publish() -> None:
    """The gate fires on the real defect: a publish that needs no preflight.

    The synthetic workflow also swallows the sweep's exit code, pins the
    wrong runner, and scopes the job to pull requests -- the three other
    ways a job can look like this gate's subject while proving nothing.
    """
    defective = yaml.safe_load(
        """
        jobs:
          release-preflight:
            runs-on: macos-26
            if: github.event_name == 'pull_request'
            steps:
              - name: Preflight
                continue-on-error: true
                run: uv run eawf release preflight 0.7.0.dev1
          publish-pypi:
            needs: [build-wheel]
            steps:
              - name: Publish
                uses: pypa/gh-action-pypi-publish@release/v1
        """
    )
    problems = tag_chokepoint_violations(defective)
    assert any("publish-pypi does not need the release-preflight" in p for p in problems), problems
    assert any("publish-pypi does not need the require-green-ci" in p for p in problems), problems
    assert any("continue-on-error" in p for p in problems), problems
    assert any("ubuntu-24.04" in p for p in problems), problems
    assert any("skip it on a tag push" in p for p in problems), problems


def test_tag_chokepoint_gate_reds_on_a_sweepless_preflight_job() -> None:
    """A preflight job that runs no sweep is the emptiest bypass of all."""
    defective = yaml.safe_load(
        """
        jobs:
          release-preflight:
            runs-on: ubuntu-24.04
            if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')
            steps:
              - name: Checkout
                uses: actions/checkout@v4
          publish-pypi:
            needs: [release-preflight, require-green-ci]
        """
    )
    assert tag_chokepoint_violations(defective) == [
        "no release-preflight step runs 'eawf release preflight'"
    ]


def test_tag_chokepoint_gate_reds_on_a_missing_job() -> None:
    """A release workflow with no preflight job at all is the violation."""
    assert tag_chokepoint_violations(yaml.safe_load("jobs: {}\n")) == [
        "release.yaml declares no 'release-preflight' job"
    ]


# --- Linux real-host jail gate ----------------------------------------------


def _load_ci() -> dict[str, Any]:
    """Parse ``.github/workflows/ci.yaml``."""
    workflow: dict[str, Any] = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    return workflow


def linux_real_host_violations(workflow: dict[str, Any]) -> list[str]:
    """Report every way *workflow* could stop proving the jail launches on Linux.

    The Linux bubblewrap backend has no host that exercises it by default:
    the developer machines are macOS and the CI test matrix carries no
    bwrap, so every real-jail case skips and only the argv SHAPE is checked.
    A prefix bwrap refuses to mount then reads as green while the jail is
    dead. The gate below keeps a real-host job in the workflow.

    Args:
        workflow: The parsed CI workflow.

    Returns:
        One human-readable problem per violation; empty when the real-host
        job is present, unconditional, on ubuntu-24.04, installing real
        bubblewrap, and running the launch + sunset tests.
    """
    problems: list[str] = []
    job = workflow.get("jobs", {}).get("linux-jail")
    if job is None:
        return ["ci.yaml declares no 'linux-jail' job"]

    if job.get("runs-on") != "ubuntu-24.04":
        problems.append("the linux-jail job does not run on ubuntu-24.04")
    if job.get("if") is not None:
        problems.append("the linux-jail job is conditional, so the launch can go unproven")

    install = _find_step(job, "bubblewrap")
    if install is None or "apt-get install" not in str(install.get("run", "")):
        problems.append("the linux-jail job installs no real bubblewrap")

    launch = _find_step(job, "pytest")
    if launch is None:
        problems.append("the linux-jail job runs no pytest step")
        return problems

    run = str(launch.get("run", ""))
    for path in (
        "tests/runtime/sandbox/test_jail.py",
        "tests/runtime/sandbox/test_jail_expiry.py",
    ):
        if path not in run:
            problems.append(f"the linux-jail job does not run {path}")
    return problems


def test_ci_proves_the_jail_launches_on_a_linux_real_host() -> None:
    """The live CI workflow launches the jail against real bwrap on ubuntu-24.04."""
    assert linux_real_host_violations(_load_ci()) == []


def test_linux_real_host_job_runs_a_guarded_launch_test() -> None:
    """The launch cases the job runs exist and skip cleanly off a bwrap host."""
    source = (_REPO_ROOT / "tests" / "runtime" / "sandbox" / "test_jail.py").read_text(
        encoding="utf-8"
    )
    assert "def test_bwrap_jail_linux_launch_" in source
    assert 'shutil.which("bwrap")' in source
    assert "pytest.mark.skipif" in source


def test_linux_real_host_gate_reds_on_a_bwrap_free_job() -> None:
    """The gate fires on the real defect: a job that never installs bubblewrap.

    Without the install the launch tests skip and the job passes while
    proving nothing -- the exact failure mode this gate exists to catch.
    """
    defective = yaml.safe_load(
        """
        jobs:
          linux-jail:
            runs-on: macos-26
            if: github.event_name == 'pull_request'
            steps:
              - name: Pytest (sandbox)
                run: uv run pytest tests/runtime/sandbox/test_jail.py
        """
    )
    problems = linux_real_host_violations(defective)
    assert any("bubblewrap" in problem for problem in problems), problems
    assert any("ubuntu-24.04" in problem for problem in problems), problems
    assert any("conditional" in problem for problem in problems), problems
    assert any("test_jail_expiry.py" in problem for problem in problems), problems


def test_linux_real_host_gate_reds_on_a_missing_job() -> None:
    """A workflow with no linux-jail job at all is itself the violation."""
    assert linux_real_host_violations(yaml.safe_load("jobs: {}\n")) == [
        "ci.yaml declares no 'linux-jail' job"
    ]


# --- twice-green full-suite gate --------------------------------------------


def _just_test_all_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the steps of *job* that invoke ``just test-all``."""
    steps: list[dict[str, Any]] = job.get("steps", [])
    return [step for step in steps if "just test-all" in str(step.get("run", ""))]


def twice_green_violations(workflow: dict[str, Any]) -> list[str]:
    """Report every way *workflow* could stop proving the suite is twice-green.

    A suite that passes once but not twice in a row is not green, it is
    lucky: the second run inherits whatever the first left on disk, which
    is where the shared-runtime-dir and stale-lock flakes lived. The
    ``test`` matrix runs the suite once and cannot see that class at all.

    Args:
        workflow: The parsed CI workflow.

    Returns:
        One human-readable problem per violation; empty when a fresh,
        unconditional ubuntu-24.04 job checks the tree out and runs
        ``just test-all`` exactly twice, with neither run's exit code
        swallowed.
    """
    problems: list[str] = []
    job = workflow.get("jobs", {}).get("twice-green")
    if job is None:
        return ["ci.yaml declares no 'twice-green' job"]

    if job.get("runs-on") != "ubuntu-24.04":
        problems.append("the twice-green job does not run on ubuntu-24.04")
    if job.get("if") is not None:
        problems.append("the twice-green job is conditional, so the rerun can go unproven")

    steps: list[dict[str, Any]] = job.get("steps", [])
    if not any(str(step.get("uses", "")).startswith("actions/checkout") for step in steps):
        problems.append("the twice-green job checks out no fresh tree")

    runs = _just_test_all_steps(job)
    if len(runs) != 2:
        problems.append(f"the twice-green job runs 'just test-all' {len(runs)} time(s), not twice")
    if any(step.get("continue-on-error") for step in runs):
        problems.append("a 'just test-all' step is continue-on-error, so a red run reads as green")
    return problems


def test_ci_runs_the_full_suite_twice_green() -> None:
    """The live CI workflow runs ``just test-all`` twice on a fresh checkout."""
    assert twice_green_violations(_load_ci()) == []


def test_twice_green_gate_reds_on_a_single_run() -> None:
    """The gate fires on the real defect: one run, so a rerun-only flake hides.

    The synthetic job also swallows the exit code and pins the wrong
    runner, which are the other two ways a job can look like this gate's
    subject while proving nothing.
    """
    defective = yaml.safe_load(
        """
        jobs:
          twice-green:
            runs-on: macos-26
            if: github.event_name == 'pull_request'
            steps:
              - name: Checkout
                uses: actions/checkout@v4
              - name: Pytest
                continue-on-error: true
                run: just test-all
        """
    )
    problems = twice_green_violations(defective)
    assert any("1 time(s), not twice" in problem for problem in problems), problems
    assert any("continue-on-error" in problem for problem in problems), problems
    assert any("ubuntu-24.04" in problem for problem in problems), problems
    assert any("conditional" in problem for problem in problems), problems


def test_twice_green_gate_reds_on_a_checkoutless_job() -> None:
    """Two runs over a tree nobody checked out are not two FRESH runs."""
    defective = yaml.safe_load(
        """
        jobs:
          twice-green:
            runs-on: ubuntu-24.04
            steps:
              - name: Pytest (run 1)
                run: just test-all
              - name: Pytest (run 2)
                run: just test-all
        """
    )
    assert twice_green_violations(defective) == ["the twice-green job checks out no fresh tree"]


def test_twice_green_gate_reds_on_a_missing_job() -> None:
    """A workflow with no twice-green job at all is itself the violation."""
    assert twice_green_violations(yaml.safe_load("jobs: {}\n")) == [
        "ci.yaml declares no 'twice-green' job"
    ]


# --- release inventory-and-reproducibility job -------------------------------


def _upload_names(job: dict[str, Any]) -> dict[str, str]:
    """Return ``artifact name -> uploaded path`` for one job's upload steps."""
    uploaded: dict[str, str] = {}
    for step in job.get("steps", []):
        if not str(step.get("uses", "")).startswith("actions/upload-artifact"):
            continue
        with_block = step.get("with") or {}
        name = with_block.get("name")
        if isinstance(name, str):
            uploaded[name] = str(with_block.get("path", ""))
    return uploaded


def inventory_job_violations(workflow: dict[str, Any]) -> list[str]:
    """Report every way *workflow* could publish without the three receipts.

    The dependency inventory, the vulnerability report and the
    double-build receipt are produced in CI and read back by the
    readiness sweep. A publish that does not need the producing job
    reaches PyPI with three gates reporting ``unavailable``, which is
    exactly as unchecked as having no gates at all.

    Args:
        workflow: The parsed release workflow.

    Returns:
        One human-readable problem per violation; empty when the job
        exists, runs the producer, uploads all three receipts under the
        names the library declares, and is needed by the publish job.
    """
    problems: list[str] = []
    jobs = workflow.get("jobs", {})
    job = jobs.get("inventory-and-reproducibility")
    if job is None:
        return ["release.yaml declares no 'inventory-and-reproducibility' job"]

    if job.get("runs-on") != "ubuntu-24.04":
        problems.append("the inventory-and-reproducibility job does not run on ubuntu-24.04")
    steps: list[dict[str, Any]] = job.get("steps", [])
    if not any("eawf.workflow.release.produce" in str(step.get("run", "")) for step in steps):
        problems.append("no step runs the release receipt producer")
    if any(step.get("continue-on-error") for step in steps):
        problems.append("an inventory-and-reproducibility step is continue-on-error")

    uploaded = _upload_names(job)
    for artifact_name, filename in sorted(RECEIPT_FILENAMES.items()):
        if artifact_name not in uploaded:
            problems.append(f"the job uploads no {artifact_name!r} artifact")
        elif not uploaded[artifact_name].endswith(filename):
            problems.append(
                f"the {artifact_name!r} artifact uploads {uploaded[artifact_name]!r}, "
                f"not the {filename!r} the producer writes"
            )

    if "inventory-and-reproducibility" not in jobs.get("publish-pypi", {}).get("needs", []):
        problems.append("publish-pypi does not need the inventory-and-reproducibility job")
    return problems


def test_inventory_job_gates_the_publish_job() -> None:
    """The live release workflow produces all three receipts before publish."""
    assert inventory_job_violations(_load_release()) == []


def test_inventory_job_uploads_every_declared_receipt() -> None:
    """The uploaded artifact names are exactly the ones the sweep reads back."""
    job = _load_release()["jobs"]["inventory-and-reproducibility"]
    assert set(_upload_names(job)) == set(RECEIPT_FILENAMES)
    assert set(RECEIPT_FILENAMES) == {
        "dependency-manifest",
        "vulnerability-report",
        "reproducible-build-receipt",
    }


def test_inventory_job_gate_reds_on_a_bypassing_publish() -> None:
    """The gate fires on the real defect: a publish that needs no receipts.

    The synthetic workflow also drops two uploads, swallows the
    producer's exit code and pins the wrong runner -- the other ways a
    job can look like this gate's subject while proving nothing.
    """
    defective = yaml.safe_load(
        """
        jobs:
          inventory-and-reproducibility:
            runs-on: macos-26
            steps:
              - name: Produce
                continue-on-error: true
                run: uv run python -m eawf.workflow.release.produce --source-sha deadbeef
              - name: Upload dependency manifest
                uses: actions/upload-artifact@v4
                with:
                  name: dependency-manifest
                  path: dist/release-receipts/dependency-manifest.json
          publish-pypi:
            needs: [build-wheel]
        """
    )
    problems = inventory_job_violations(defective)
    assert any("publish-pypi does not need" in problem for problem in problems), problems
    assert any("no 'vulnerability-report' artifact" in problem for problem in problems), problems
    assert any("no 'reproducible-build-receipt' artifact" in problem for problem in problems), (
        problems
    )
    assert any("continue-on-error" in problem for problem in problems), problems
    assert any("ubuntu-24.04" in problem for problem in problems), problems


def test_inventory_job_gate_reds_on_an_artifact_pointing_at_the_wrong_file() -> None:
    """An upload named right but pointing elsewhere ships an empty artifact."""
    defective = yaml.safe_load(
        """
        jobs:
          inventory-and-reproducibility:
            runs-on: ubuntu-24.04
            steps:
              - name: Produce
                run: uv run python -m eawf.workflow.release.produce --source-sha deadbeef
              - name: Upload dependency manifest
                uses: actions/upload-artifact@v4
                with:
                  name: dependency-manifest
                  path: dist/
              - name: Upload vulnerability report
                uses: actions/upload-artifact@v4
                with:
                  name: vulnerability-report
                  path: dist/release-receipts/vulnerability-report.json
              - name: Upload build receipt
                uses: actions/upload-artifact@v4
                with:
                  name: reproducible-build-receipt
                  path: dist/release-receipts/reproducible-build-receipt.json
          publish-pypi:
            needs: [inventory-and-reproducibility]
        """
    )
    assert inventory_job_violations(defective) == [
        "the 'dependency-manifest' artifact uploads 'dist/', not the "
        "'dependency-manifest.json' the producer writes"
    ]


def test_inventory_job_gate_reds_on_a_producerless_job() -> None:
    """A job that uploads receipts it never produced uploads yesterday's."""
    defective = yaml.safe_load(
        """
        jobs:
          inventory-and-reproducibility:
            runs-on: ubuntu-24.04
            steps:
              - name: Checkout
                uses: actions/checkout@v4
              - name: Upload dependency manifest
                uses: actions/upload-artifact@v4
                with:
                  name: dependency-manifest
                  path: dist/release-receipts/dependency-manifest.json
              - name: Upload vulnerability report
                uses: actions/upload-artifact@v4
                with:
                  name: vulnerability-report
                  path: dist/release-receipts/vulnerability-report.json
              - name: Upload build receipt
                uses: actions/upload-artifact@v4
                with:
                  name: reproducible-build-receipt
                  path: dist/release-receipts/reproducible-build-receipt.json
          publish-pypi:
            needs: [inventory-and-reproducibility]
        """
    )
    assert inventory_job_violations(defective) == ["no step runs the release receipt producer"]


def test_inventory_job_gate_reds_on_a_missing_job() -> None:
    """A release workflow with no producing job at all is the violation."""
    assert inventory_job_violations(yaml.safe_load("jobs: {}\n")) == [
        "release.yaml declares no 'inventory-and-reproducibility' job"
    ]
