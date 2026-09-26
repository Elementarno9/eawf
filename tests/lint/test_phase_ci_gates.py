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

The changes gate guards the cost cut that skips the heavy jobs on a
state-only push: the skip must hinge on the classifier's verdict alone,
and the jobs outside it must keep running on every push.

The twice-green gate pins when the rerun happens as well as how: on main
and release pushes plus nightly and manual runs, never on a pull request.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.workflow.release.pipeline_receipts import RECEIPT_FILENAMES
from eawf.workflow.release.publication_receipt import receipt_filename
from eawf.workflow.release.source_host_assets import source_host_assets

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_RELEASE = _REPO_ROOT / ".github" / "workflows" / "plugin-release.yaml"
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"
_RELEASE = _REPO_ROOT / ".github" / "workflows" / "release.yaml"
_PHASE_RELEASE = _REPO_ROOT / ".github" / "workflows" / "phase-release.yaml"

#: The shell expansion the source-host job names the published version
#: with. Substituting it into the library's asset names yields the exact
#: strings the workflow must carry, so the gate compares the workflow
#: against the library rather than against a second copy of the names.
_VERSION_EXPANSION = "${PLUGIN_VERSION}"


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
        "tests/integration/runtime/sandbox/test_jail.py",
        "tests/unit/runtime/sandbox/test_jail_expiry.py",
    ):
        if path not in run:
            problems.append(f"the linux-jail job does not run {path}")
    return problems


def test_ci_proves_the_jail_launches_on_a_linux_real_host() -> None:
    """The live CI workflow launches the jail against real bwrap on ubuntu-24.04."""
    assert linux_real_host_violations(_load_ci()) == []


def test_linux_real_host_job_runs_a_guarded_launch_test() -> None:
    """The launch cases the job runs exist and skip cleanly off a bwrap host."""
    source = (
        _REPO_ROOT / "tests" / "integration" / "runtime" / "sandbox" / "test_jail.py"
    ).read_text(encoding="utf-8")
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
                run: uv run pytest tests/integration/runtime/sandbox/test_jail.py
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

#: True once the classifier proved the tree already green, or the classifier
#: job itself did not conclude success -- a crashed or cancelled classifier
#: fails open to running rather than silently skipping a required check.
_CLASSIFIER_SAYS_RUN = "(needs.changes.result != 'success' || needs.changes.outputs.code == 'true')"

#: The one job-level condition the test matrix carries: skip only when the
#: changes job concluded successfully and proved the code tree is the one
#: the last green run tested; !cancelled() keeps the condition evaluating
#: instead of auto-skipping when the changes job itself failed.
_CHANGES_CONDITION = f"!cancelled() && {_CLASSIFIER_SAYS_RUN}"

#: The one job-level condition twice-green carries: a push runs it under the
#: changes gate, a scheduled or dispatched run always, a pull request never.
_TWICE_GREEN_CONDITION = (
    f"!cancelled() && ((github.event_name == 'push' && {_CLASSIFIER_SAYS_RUN})"
    " || github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')"
)

#: The push branches the twice-green cadence names: main and the release train.
_PUSH_BRANCHES = ["main", "release", "release/**"]

#: The sentence the job comment and the lint docstring both carry, so the
#: cadence a reader sees is the cadence the lint enforces.
_REL026_CADENCE = "the REL-026 proof runs on main and release pushes plus nightly and manual runs"

_TWICE_GREEN_CONDITION_PROBLEM = (
    "the twice-green job is not gated to main and release pushes plus nightly and manual "
    "runs, so it can run on a pull request or skip a run it owes"
)
_NIGHTLY_PROBLEM = "ci.yaml has no nightly schedule, so twice-green never re-proves main"
_DISPATCH_PROBLEM = "ci.yaml has no workflow_dispatch trigger, so twice-green cannot run by hand"
_PUSH_PROBLEM = (
    f"ci.yaml's push trigger does not name exactly the branches {_PUSH_BRANCHES}, "
    "so twice-green runs on the wrong pushes"
)
_CONCURRENCY_PROBLEM = (
    "the concurrency group ignores the event name, so a main push cancels the nightly run"
)


def _just_test_all_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the steps of *job* that invoke ``just test-all``."""
    steps: list[dict[str, Any]] = job.get("steps", [])
    return [step for step in steps if "just test-all" in str(step.get("run", ""))]


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    """Return the trigger table of *workflow*.

    YAML 1.1 reads the bare key ``on`` as the boolean ``True``, so both
    spellings are looked up.
    """
    raw = workflow.get(True, workflow.get("on"))
    return raw if isinstance(raw, dict) else {}


def _is_nightly(cron: str) -> bool:
    """Return whether *cron* fires once a day at a fixed hour and minute."""
    fields = cron.split()
    return (
        len(fields) == 5
        and fields[0].isdigit()
        and fields[1].isdigit()
        and fields[2:] == ["*", "*", "*"]
    )


def twice_green_violations(workflow: dict[Any, Any]) -> list[str]:
    """Report every way *workflow* could stop proving the suite is twice-green.

    A suite that passes once but not twice in a row is not green, it is
    lucky: the second run inherits whatever the first left on disk, which
    is where the shared-runtime-dir and stale-lock flakes lived. The
    ``test`` matrix runs the suite once and cannot see that class at all.

    Cadence: the REL-026 proof runs on main and release pushes plus nightly
    and manual runs. A pull request never runs it: there the rerun bounded
    every push's wall-clock without catching a defect the first run missed.
    A push skips it only when the changes job proved the code tree already
    went green, while a scheduled or dispatched run has no diff and always
    runs it. The cadence therefore needs the push branch filter, a nightly
    schedule, a dispatch trigger, and a concurrency group split by event,
    without which a merge to main cancels the nightly run on the same ref.

    Args:
        workflow: The parsed CI workflow.

    Returns:
        One human-readable problem per violation; empty when a fresh
        ubuntu-24.04 job carrying exactly the cadence condition checks the
        tree out and runs ``just test-all`` exactly twice with neither exit
        code swallowed, and the workflow's triggers and concurrency group
        deliver that cadence.
    """
    problems: list[str] = []
    job = workflow.get("jobs", {}).get("twice-green")
    if job is None:
        return ["ci.yaml declares no 'twice-green' job"]

    if job.get("runs-on") != "ubuntu-24.04":
        problems.append("the twice-green job does not run on ubuntu-24.04")
    if job.get("if") != _TWICE_GREEN_CONDITION:
        problems.append(_TWICE_GREEN_CONDITION_PROBLEM)

    steps: list[dict[str, Any]] = job.get("steps", [])
    if not any(str(step.get("uses", "")).startswith("actions/checkout") for step in steps):
        problems.append("the twice-green job checks out no fresh tree")

    runs = _just_test_all_steps(job)
    if len(runs) != 2:
        problems.append(f"the twice-green job runs 'just test-all' {len(runs)} time(s), not twice")
    if any(step.get("continue-on-error") for step in runs):
        problems.append("a 'just test-all' step is continue-on-error, so a red run reads as green")
    return problems + _cadence_violations(workflow)


def _cadence_violations(workflow: dict[Any, Any]) -> list[str]:
    """Report the ways the workflow's triggers could break the twice-green cadence."""
    problems: list[str] = []
    triggers = _triggers(workflow)
    push = triggers.get("push")
    if not isinstance(push, dict) or push.get("branches") != _PUSH_BRANCHES:
        problems.append(_PUSH_PROBLEM)

    schedule = triggers.get("schedule")
    entries = schedule if isinstance(schedule, list) else []
    if not any(
        isinstance(entry, dict) and _is_nightly(str(entry.get("cron"))) for entry in entries
    ):
        problems.append(_NIGHTLY_PROBLEM)
    if "workflow_dispatch" not in triggers:
        problems.append(_DISPATCH_PROBLEM)

    concurrency = workflow.get("concurrency")
    group = concurrency.get("group") if isinstance(concurrency, dict) else concurrency
    if concurrency is not None and "github.event_name" not in str(group):
        problems.append(_CONCURRENCY_PROBLEM)
    return problems


def _job_comment(source: str, job_name: str) -> str | None:
    """Return the job-level comment of *job_name* as one whitespace-normalised line.

    Args:
        source: The raw workflow text; parsing it as YAML drops comments.
        job_name: The job whose own comment lines are joined; step comments
            and other jobs' comments are left out.

    Returns:
        The joined comment, or None when the workflow declares no such job.
    """
    lines = source.splitlines()
    header = f"  {job_name}:"
    if header not in lines:
        return None
    comment: list[str] = []
    for line in lines[lines.index(header) + 1 :]:
        if line.strip() and not line.startswith("    "):
            break
        if line.startswith("    #"):
            comment.append(line.strip().removeprefix("#"))
    return " ".join(" ".join(comment).split())


def test_ci_runs_the_full_suite_twice_green() -> None:
    """The live CI workflow runs ``just test-all`` twice on a fresh checkout."""
    assert twice_green_violations(_load_ci()) == []


def test_twice_green_gate_reds_on_a_single_run() -> None:
    """The gate fires on the real defect: one run, so a rerun-only flake hides.

    The synthetic job also swallows the exit code, pins the wrong runner
    and runs on pull requests, and the workflow keeps none of the cadence
    triggers, which are the other ways a job can look like this gate's
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
    assert twice_green_violations(defective) == [
        "the twice-green job does not run on ubuntu-24.04",
        _TWICE_GREEN_CONDITION_PROBLEM,
        "the twice-green job runs 'just test-all' 1 time(s), not twice",
        "a 'just test-all' step is continue-on-error, so a red run reads as green",
        _PUSH_PROBLEM,
        _NIGHTLY_PROBLEM,
        _DISPATCH_PROBLEM,
    ]


def _twice_green_workflow(condition: str | None = _TWICE_GREEN_CONDITION) -> dict[Any, Any]:
    """Return a well-formed twice-green workflow whose job carries *condition*.

    Args:
        condition: The job-level ``if``; None leaves the job unconditional.
    """
    workflow: dict[Any, Any] = yaml.safe_load(
        """
        on:
          push:
            branches: [main, release, "release/**"]
          pull_request:
          schedule:
            - cron: "17 3 * * *"
          workflow_dispatch:
        concurrency:
          group: ci-${{ github.event_name }}-${{ github.ref }}
          cancel-in-progress: true
        jobs:
          twice-green:
            needs: changes
            runs-on: ubuntu-24.04
            steps:
              - name: Checkout
                uses: actions/checkout@v4
              - name: Pytest (run 1)
                run: just test-all
              - name: Pytest (run 2)
                run: just test-all
        """
    )
    if condition is not None:
        workflow["jobs"]["twice-green"]["if"] = condition
    return workflow


def test_twice_green_gate_reds_on_a_checkoutless_job() -> None:
    """Two runs over a tree nobody checked out are not two FRESH runs."""
    defective = _twice_green_workflow()
    defective["jobs"]["twice-green"]["steps"].pop(0)
    assert twice_green_violations(defective) == ["the twice-green job checks out no fresh tree"]


def test_twice_green_gate_reds_on_a_missing_job() -> None:
    """A workflow with no twice-green job at all is itself the violation."""
    assert twice_green_violations(yaml.safe_load("jobs: {}\n")) == [
        "ci.yaml declares no 'twice-green' job"
    ]


def test_twice_green_gate_accepts_the_cadence_condition() -> None:
    """Main and release pushes under the changes gate plus nightly and manual runs."""
    assert twice_green_violations(_twice_green_workflow()) == []


def test_twice_green_gate_accepts_a_workflow_without_concurrency() -> None:
    """Without a concurrency block no run cancels another, so the nightly survives."""
    workflow = _twice_green_workflow()
    del workflow["concurrency"]
    assert twice_green_violations(workflow) == []


@pytest.mark.parametrize(
    "condition",
    [
        None,
        _CHANGES_CONDITION,
        "github.event_name == 'pull_request'",
        "github.event_name != 'schedule' && needs.changes.outputs.code == 'true'",
        f"{_TWICE_GREEN_CONDITION} || github.event_name == 'pull_request'",
        "always()",
    ],
)
def test_twice_green_gate_reds_on_a_pull_request_run(condition: str | None) -> None:
    """The gate fires on the cadence it replaces: a rerun on every PR push."""
    assert twice_green_violations(_twice_green_workflow(condition)) == [
        _TWICE_GREEN_CONDITION_PROBLEM
    ]


@pytest.mark.parametrize(
    "condition",
    [
        f"${{{{ {_TWICE_GREEN_CONDITION} }}}}",
        "github.event_name != 'pull_request'",
        "github.event_name != 'pull_request' && needs.changes.outputs.code == 'true'",
        f"(github.event_name == 'push' && {_CHANGES_CONDITION}) || github.event_name == 'schedule'",
    ],
)
def test_twice_green_gate_reds_on_any_other_condition(condition: str) -> None:
    """Near misses of the cadence condition still read as the wrong gate."""
    assert twice_green_violations(_twice_green_workflow(condition)) == [
        _TWICE_GREEN_CONDITION_PROBLEM
    ]


@pytest.mark.parametrize(
    "schedule",
    [None, [], [{"cron": "17 3 * * 1"}], [{"cron": "0 * * * *"}], [{"cron": "17 3 * *"}], ["x"]],
)
def test_twice_green_gate_reds_without_a_nightly_schedule(schedule: object) -> None:
    """With no daily schedule the rerun waits for the next push to main."""
    workflow = _twice_green_workflow()
    if schedule is None:
        del workflow[True]["schedule"]
    else:
        workflow[True]["schedule"] = schedule
    assert twice_green_violations(workflow) == [_NIGHTLY_PROBLEM]


def test_twice_green_gate_accepts_a_nightly_schedule_among_others() -> None:
    """A weekly entry beside the nightly one does not hide the nightly run."""
    workflow = _twice_green_workflow()
    workflow[True]["schedule"] = [{"cron": "0 6 * * 1"}, {"cron": "5 2 * * *"}]
    assert twice_green_violations(workflow) == []


def test_twice_green_gate_reds_without_a_dispatch_trigger() -> None:
    """With no dispatch trigger nobody can rerun the proof after a fix."""
    workflow = _twice_green_workflow()
    del workflow[True]["workflow_dispatch"]
    assert twice_green_violations(workflow) == [_DISPATCH_PROBLEM]


@pytest.mark.parametrize(
    "push",
    [
        None,
        {"branches": ["**"]},
        {"branches": ["main"]},
        {"branches": ["main", "release", "release/**", "feature/**"]},
        {"branches-ignore": ["gh-pages"]},
    ],
)
def test_twice_green_gate_reds_on_a_branch_push_trigger(push: object) -> None:
    """A push filter wider or narrower than main and release moves the rerun."""
    workflow = _twice_green_workflow()
    workflow[True]["push"] = push
    assert twice_green_violations(workflow) == [_PUSH_PROBLEM]


def test_twice_green_gate_reds_without_a_push_trigger() -> None:
    """With no push trigger a merge to main never reruns the suite."""
    workflow = _twice_green_workflow()
    del workflow[True]["push"]
    assert twice_green_violations(workflow) == [_PUSH_PROBLEM]


@pytest.mark.parametrize(
    "concurrency",
    [
        {"group": "ci-${{ github.ref }}", "cancel-in-progress": True},
        {"cancel-in-progress": True},
        "ci-${{ github.ref }}",
    ],
)
def test_twice_green_gate_reds_on_a_shared_concurrency_group(concurrency: object) -> None:
    """A per-ref group lets a merge to main cancel the nightly run on main."""
    workflow = _twice_green_workflow()
    workflow["concurrency"] = concurrency
    assert twice_green_violations(workflow) == [_CONCURRENCY_PROBLEM]


def test_twice_green_gate_reads_the_on_key_under_either_spelling() -> None:
    """A loader that keeps ``on`` as a string still yields the same triggers."""
    workflow = _twice_green_workflow()
    workflow["on"] = workflow.pop(True)
    assert twice_green_violations(workflow) == []


def test_twice_green_comment_states_the_rel026_cadence() -> None:
    """The job comment and the lint docstring both state the enforced cadence."""
    assert _REL026_CADENCE in (_job_comment(_CI.read_text(encoding="utf-8"), "twice-green") or "")
    assert _REL026_CADENCE in " ".join((twice_green_violations.__doc__ or "").split())


def test_twice_green_comment_gate_reds_on_a_cadence_in_another_job() -> None:
    """Only the twice-green job's own comment counts, not a neighbour's or a step's."""
    source = (
        "jobs:\n"
        "  twice-green:\n"
        "    # The rerun runs on every pull request push.\n"
        "    steps:\n"
        "      - name: Pytest\n"
        f"        # Cadence: {_REL026_CADENCE}.\n"
        "        run: just test-all\n"
        "  prose-gate:\n"
        f"    # Cadence: {_REL026_CADENCE}.\n"
        "    runs-on: ubuntu-24.04\n"
    )
    assert _job_comment(source, "twice-green") == "The rerun runs on every pull request push."
    assert _job_comment(source, "prose-gate") == f"Cadence: {_REL026_CADENCE}."
    assert _job_comment(source, "windows") is None


# --- changes gate on the heavy jobs ------------------------------------------

#: Jobs expensive enough to skip on a bookkeeping-only diff, each with the
#: one condition it may carry. Twice-green applies the changes gate to push
#: runs only, since a scheduled or dispatched run has no diff to classify.
#: Both conditions fail open to running when the classifier job itself did
#: not conclude success.
_HEAVY_JOBS: dict[str, str] = {
    "test": _CHANGES_CONDITION,
    "twice-green": _TWICE_GREEN_CONDITION,
}

#: Jobs that run whatever the classifier says: linux-jail is a release-train
#: receipt, windows and the wheel smoke cover what the matrix cannot, and the
#: two PR gates are cheap enough that skipping them saves nothing.
_CHEAP_JOBS = ("linux-jail", "windows", "tool-install-smoke", "snapshot-pairing", "prose-gate")


def _needs(job: dict[str, Any]) -> list[str]:
    """Return the job names *job* needs, whichever YAML form it uses."""
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def changes_gate_violations(workflow: dict[str, Any]) -> list[str]:
    """Report every way *workflow* could skip a job it must run.

    The heavy jobs skip only when the changes job concluded successfully
    and said the tree is already green: each job's own condition also
    fails open, via ``!cancelled()`` plus a check on the classifier job's own
    result, so a crashed or cancelled classifier runs the matrix instead of
    silently skipping a required check that would otherwise read as
    passing. The cheap jobs must never see the classifier at all.

    Args:
        workflow: The parsed CI workflow.

    Returns:
        One human-readable problem per violation; empty when an
        unconditional changes job runs the classifier over full history
        and exports its verdict, each heavy job needs it and carries
        exactly its sanctioned condition, and no cheap job depends on it.
    """
    jobs = workflow.get("jobs", {})
    changes = jobs.get("changes")
    if changes is None:
        return ["ci.yaml declares no 'changes' job"]
    problems = _changes_job_shape(changes)
    for name, condition in _HEAVY_JOBS.items():
        job = jobs.get(name)
        if job is None:
            problems.append(f"ci.yaml declares no {name!r} job")
            continue
        if "changes" not in _needs(job):
            problems.append(f"the {name} job does not need the changes job")
        if job.get("if") != condition:
            problems.append(f"the {name} job is not gated on exactly {condition!r}")
    for name in _CHEAP_JOBS:
        job = jobs.get(name)
        if job is None:
            problems.append(f"ci.yaml declares no {name!r} job")
        elif "changes" in _needs(job) or "needs.changes" in str(job.get("if", "")):
            problems.append(f"the {name} job depends on the changes job, so it can be skipped")
    return problems


def _changes_job_shape(job: dict[str, Any]) -> list[str]:
    """Report the ways the changes job could exist yet leave the gate open."""
    problems: list[str] = []
    steps: list[dict[str, Any]] = job.get("steps", [])
    if job.get("if") is not None:
        problems.append("the changes job is conditional, so skipping it skips the matrix")
    if job.get("continue-on-error") or any(step.get("continue-on-error") for step in steps):
        problems.append("the changes job is continue-on-error, so a crash skips the matrix")

    checkout = next(
        (step for step in steps if str(step.get("uses", "")).startswith("actions/checkout")), None
    )
    if checkout is None or (checkout.get("with") or {}).get("fetch-depth") != 0:
        problems.append("the changes job checks out no full history to diff against")

    classify = next(
        (step for step in steps if "tools/ci_changes.py" in str(step.get("run", ""))), None
    )
    if classify is None:
        problems.append("no changes step runs tools/ci_changes.py")
        return problems
    wired = f"${{{{ steps.{classify.get('id')}.outputs.code }}}}"
    if str((job.get("outputs") or {}).get("code", "")) != wired:
        problems.append("the changes job's code output is not wired from the classifier step")
    return problems


def test_ci_gates_the_heavy_jobs_on_the_changes_output() -> None:
    """The live CI workflow skips only the heavy jobs, and only on the verdict."""
    assert changes_gate_violations(_load_ci()) == []


def test_changes_gate_reds_on_an_open_gate() -> None:
    """The gate fires on the real defect: a matrix that can skip unproven.

    The synthetic classifier is conditional, swallows its own crash, diffs
    a shallow clone and exports another step's output; the matrix is not
    gated at all, twice-green carries a near-miss condition, and two cheap
    jobs were pulled behind the classifier.
    """
    defective = yaml.safe_load(
        """
        jobs:
          changes:
            if: github.event_name == 'push'
            outputs:
              code: ${{ steps.other.outputs.code }}
            steps:
              - name: Checkout
                uses: actions/checkout@v4
              - name: Classify
                id: classify
                continue-on-error: true
                run: python3 tools/ci_changes.py
          test:
            runs-on: ubuntu-24.04
          twice-green:
            needs: changes
            if: needs.changes.outputs.code != 'false'
          linux-jail: {}
          windows:
            needs: [changes]
          tool-install-smoke: {}
          snapshot-pairing:
            if: github.event_name == 'pull_request' && needs.changes.outputs.code == 'true'
          prose-gate:
            if: github.event_name == 'pull_request'
        """
    )
    assert changes_gate_violations(defective) == [
        "the changes job is conditional, so skipping it skips the matrix",
        "the changes job is continue-on-error, so a crash skips the matrix",
        "the changes job checks out no full history to diff against",
        "the changes job's code output is not wired from the classifier step",
        "the test job does not need the changes job",
        f"the test job is not gated on exactly {_CHANGES_CONDITION!r}",
        f"the twice-green job is not gated on exactly {_TWICE_GREEN_CONDITION!r}",
        "the windows job depends on the changes job, so it can be skipped",
        "the snapshot-pairing job depends on the changes job, so it can be skipped",
    ]


def test_changes_gate_reds_on_a_required_job_that_skips_on_a_classifier_failure() -> None:
    """The gate fires on the real defect: a classifier crash reads as green.

    A failed changes job leaves ``outputs.code`` unset, so the old bare
    ``needs.changes.outputs.code == 'true'`` condition skips ``test``
    instead of running it -- and GitHub counts a skipped required check as
    passing, so the PR merges with the classifier's crash unproven. The
    live workflow's fixed condition (asserted empty by
    ``test_ci_gates_the_heavy_jobs_on_the_changes_output``) is what closes
    this gap.
    """
    workflow = _load_ci()
    workflow["jobs"]["test"]["if"] = "needs.changes.outputs.code == 'true'"
    assert changes_gate_violations(workflow) == [
        f"the test job is not gated on exactly {_CHANGES_CONDITION!r}"
    ]


def test_changes_gate_reds_on_a_classifierless_job() -> None:
    """A changes job that never runs the classifier exports nothing to gate on."""
    workflow = _load_ci()
    workflow["jobs"]["changes"]["steps"] = [
        {"name": "Checkout", "uses": "actions/checkout@v4", "with": {"fetch-depth": 0}}
    ]
    assert changes_gate_violations(workflow) == ["no changes step runs tools/ci_changes.py"]


def test_changes_gate_reds_on_a_missing_cheap_job() -> None:
    """A renamed cheap job would otherwise make its unconditional check vacuous."""
    workflow = _load_ci()
    del workflow["jobs"]["windows"]
    assert changes_gate_violations(workflow) == ["ci.yaml declares no 'windows' job"]


def test_changes_gate_reds_on_a_missing_job() -> None:
    """A workflow with no changes job at all is itself the violation."""
    assert changes_gate_violations(yaml.safe_load("jobs: {}\n")) == [
        "ci.yaml declares no 'changes' job"
    ]


# --- wall-clock budgets run serially -----------------------------------------

#: Test trees that assert a wall-clock budget. Inside a parallel worker pool
#: the pool's own load becomes part of what they measure: the cold-import
#: ceiling tripped at 777 ms against 750 ms in a twice-green run while the
#: same import takes about 300 ms on an idle host.
_TIMING_SUITES = ("tests/perf/surfaces/tui", "tests/perf/surfaces/cli")

_JUSTFILE = _REPO_ROOT / "justfile"


def _pytest_commands(text: str) -> list[str]:
    """Return every ``uv run pytest`` command line in *text*."""
    return [line.strip() for line in text.splitlines() if "uv run pytest" in line]


def _just_recipe(text: str, name: str) -> str:
    """Return the body of the justfile recipe *name* (its indented lines)."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}:"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break
        body.append(line)
    return "\n".join(body)


def timing_suite_violations(commands: list[str]) -> list[str]:
    """Report every timing suite a parallel run includes or no serial run names.

    Args:
        commands: The ``uv run pytest`` command lines of one test pipeline.

    Returns:
        One problem per timing suite that a ``-n auto`` run does not ignore,
        or that no ``-n0`` run names; empty when each runs serially and only
        serially.
    """
    problems: list[str] = []
    parallel = [command for command in commands if "-n auto" in command]
    serial = [command for command in commands if "-n0" in command]
    for suite in _TIMING_SUITES:
        if any(f"--ignore={suite}" not in command for command in parallel):
            problems.append(f"a parallel run includes the timing suite {suite}")
        if not any(suite in command.split() for command in serial):
            problems.append(f"no serial run names the timing suite {suite}")
    return problems


def test_ci_test_leg_runs_timing_suites_serially() -> None:
    """The CI test matrix times its budgets outside the parallel pool."""
    steps = _load_ci()["jobs"]["test"]["steps"]
    commands = _pytest_commands("\n".join(str(step.get("run", "")) for step in steps))
    assert timing_suite_violations(commands) == []


def test_just_test_all_runs_timing_suites_serially() -> None:
    """``just test-all``, which twice-green runs, times its budgets serially."""
    recipe = _just_recipe(_JUSTFILE.read_text(encoding="utf-8"), "test-all")
    assert timing_suite_violations(_pytest_commands(recipe)) == []


def test_timing_suite_gate_reds_on_a_parallel_cold_import_budget() -> None:
    """The gate fires on the layout that let the cold-import ceiling trip."""
    commands = [
        "uv run pytest -n auto --ignore=tests/snapshots/tui --ignore=tests/perf/surfaces/tui",
        "uv run pytest -n0 tests/perf/surfaces/tui",
    ]
    assert timing_suite_violations(commands) == [
        "a parallel run includes the timing suite tests/perf/surfaces/cli",
        "no serial run names the timing suite tests/perf/surfaces/cli",
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


# --- source-host release assets ---------------------------------------------


def _load_phase_release() -> dict[str, Any]:
    """Parse ``.github/workflows/phase-release.yaml``."""
    workflow: dict[str, Any] = yaml.safe_load(_PHASE_RELEASE.read_text(encoding="utf-8"))
    return workflow


def _expected_source_host_assets() -> tuple[str, ...]:
    """Return the three asset filenames the workflow must name, in order."""
    assets = source_host_assets(_VERSION_EXPANSION)
    return tuple(assets[kind] for kind in sorted(assets, key=lambda kind: kind.value))


def source_host_asset_violations(workflow: dict[str, Any]) -> list[str]:
    """Report every way *workflow* could publish an incomplete tag release.

    The ``github`` target declares three artifact kinds, and the
    source-host observation adapter reads the published release object
    back asset-by-asset against the frozen manifest. A leg that attaches
    two of the three publishes fine and then observes as a digest
    mismatch, which reads like a tampered artifact rather than a missing
    upload -- so the shape is asserted here, on the source, rather than
    discovered on a live tag.

    The prerelease flag is the same class of defect one level up: a
    source host advertises the newest non-prerelease release as *the*
    release, so a dev or rc checkpoint published without the flag lands
    on the stable default channel exactly as an npm ``latest`` would.

    Args:
        workflow: The parsed plugin-release workflow.

    Returns:
        One human-readable problem per violation; empty when the job
        exists, runs on a tag push, assembles and uploads all three
        declared assets, and sets the prerelease flag off the channel.
    """
    job = workflow.get("jobs", {}).get("publish-source-host")
    if job is None:
        return ["plugin-release.yaml declares no 'publish-source-host' job"]
    problems = _source_host_job_shape(job)
    source = "\n".join(str(step.get("run", "")) for step in job.get("steps", []))
    problems += [
        f"the source-host leg never names the {filename!r} asset"
        for filename in _expected_source_host_assets()
        if filename not in source
    ]
    return problems + _source_host_release_step(job)


def _source_host_job_shape(job: dict[str, Any]) -> list[str]:
    """Report the ways the job could exist yet never run, or never fail."""
    problems: list[str] = []
    if job.get("runs-on") != "ubuntu-24.04":
        problems.append("the publish-source-host job does not run on ubuntu-24.04")
    condition = str(job.get("if", ""))
    if condition and "refs/tags/v" not in condition:
        problems.append("the publish-source-host job's condition can skip it on a tag push")
    if any(step.get("continue-on-error") for step in job.get("steps", [])):
        problems.append("a publish-source-host step is continue-on-error, so a failure passes")
    return problems


def _source_host_release_step(job: dict[str, Any]) -> list[str]:
    """Report the ways the release-writing step could publish a bare release."""
    upload = _find_step(job, "tag release")
    if upload is None:
        return ["publish-source-host has no step that creates or edits the tag release"]
    problems: list[str] = []
    run = str(upload.get("run", ""))
    if "gh release create" not in run or "gh release edit" not in run:
        problems.append("the source-host leg does not create-or-edit the tag release")
    if "gh release upload" not in run:
        problems.append("the source-host leg attaches no assets to the tag release")
    if "--prerelease" not in run:
        problems.append("the source-host leg never flags a prerelease checkpoint")
    if "needs.package-validate.outputs.prerelease" not in str(upload.get("env", {})):
        problems.append("the prerelease flag is not wired from the resolved channel")
    return problems


def test_source_host_assets_are_attached_to_the_tag_release() -> None:
    """The live workflow attaches all three declared assets under the flag."""
    assert source_host_asset_violations(_load_plugin_release()) == []


def test_source_host_assets_gate_reds_on_a_notes_only_release() -> None:
    """The gate fires on the real defect: notes published, artifacts dropped.

    The synthetic job also drops the prerelease flag, swallows the exit
    code and pins the wrong runner -- the other ways a job can look like
    this gate's subject while publishing an unverifiable release.
    """
    defective = yaml.safe_load(
        """
        jobs:
          publish-source-host:
            runs-on: macos-26
            if: github.event_name == 'pull_request'
            steps:
              - name: Publish the tag release
                continue-on-error: true
                run: |
                  echo notes > RELEASE_NOTES.md
                  gh release create "$TAG" --notes-file RELEASE_NOTES.md
        """
    )
    problems = source_host_asset_violations(defective)
    assert any("'SHA256SUMS' asset" in problem for problem in problems), problems
    assert any("eawf-plugin-" in problem for problem in problems), problems
    assert any("never flags a prerelease" in problem for problem in problems), problems
    assert any("attaches no assets" in problem for problem in problems), problems
    assert any("continue-on-error" in problem for problem in problems), problems
    assert any("ubuntu-24.04" in problem for problem in problems), problems


def test_source_host_assets_gate_reds_on_a_missing_job() -> None:
    """A plugin-release workflow with no source-host leg is the violation."""
    assert source_host_asset_violations(yaml.safe_load("jobs: {}\n")) == [
        "plugin-release.yaml declares no 'publish-source-host' job"
    ]


def test_source_host_assets_cover_every_kind_the_github_target_declares() -> None:
    """The three names the gate checks are the three kinds the config asserts."""
    assets = source_host_assets("0.7.0.dev1")
    assert {kind.value for kind in assets} == {"release_notes", "checksums", "plugin_bundle"}
    assert set(assets.values()) == {
        "RELEASE_NOTES.md",
        "SHA256SUMS",
        "eawf-plugin-0.7.0.dev1.tar.gz",
    }


# --- publication receipts ----------------------------------------------------


#: Publish job -> the workflow it lives in and the leg it publishes.
_PUBLISH_JOBS: dict[str, tuple[Path, str]] = {
    "publish-pypi": (_RELEASE, "pypi"),
    "publish-claude-npm": (_PLUGIN_RELEASE, "npm"),
    "publish-source-host": (_PLUGIN_RELEASE, "github"),
}


def publication_receipt_violations(
    workflow: dict[str, Any],
    job_name: str,
    target_id: str,
) -> list[str]:
    """Report every way *job_name* could publish without leaving a receipt.

    The publisher is the pipeline, not the daemon: the runner is gone by
    the time the ledger asks what happened, so a leg that does not write
    its own word down settles as ``unknown`` forever and stays retryable
    against a publication that may well have succeeded.

    Args:
        workflow: The parsed workflow carrying the job.
        job_name: The publish job under inspection.
        target_id: The configured leg it publishes.

    Returns:
        One human-readable problem per violation; empty when the job
        writes the receipt the library names, uploads it under the same
        name, and does both even when the publish itself failed.
    """
    problems: list[str] = []
    job = workflow.get("jobs", {}).get(job_name)
    if job is None:
        return [f"the workflow declares no {job_name!r} job"]

    filename = receipt_filename(target_id)
    steps: list[dict[str, Any]] = job.get("steps", [])
    writers = [step for step in steps if "publication-receipt-" in str(step.get("run", ""))]
    if not writers:
        problems.append(f"{job_name} writes no {filename!r}")
    elif not all(str(step.get("if", "")) == "always()" for step in writers):
        problems.append(f"{job_name} skips its receipt when the publish fails")

    for field in ("target_id", "artifact_digests", "job_conclusion", "run_id"):
        if not any(field in str(step.get("run", "")) for step in writers):
            problems.append(f"the {target_id!r} receipt records no {field!r}")

    uploads = {
        str((step.get("with") or {}).get("name", "")): step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    }
    artifact = f"publication-receipt-{target_id}"
    if artifact not in uploads:
        problems.append(f"{job_name} uploads no {artifact!r} artifact")
    else:
        step = uploads[artifact]
        if str((step.get("with") or {}).get("path", "")) != filename:
            problems.append(f"the {artifact!r} artifact does not upload {filename!r}")
        if str(step.get("if", "")) != "always()":
            problems.append(f"{job_name} skips the receipt upload when the publish fails")
    return problems


@pytest.mark.parametrize(
    ("job_name", "target_id"),
    [(job, leg) for job, (_path, leg) in _PUBLISH_JOBS.items()],
)
def test_every_publish_job_uploads_a_publication_receipt(job_name: str, target_id: str) -> None:
    """All three live publish jobs leave the receipt reconciliation reads."""
    path = _PUBLISH_JOBS[job_name][0]
    workflow: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert publication_receipt_violations(workflow, job_name, target_id) == []


def test_publication_receipt_gate_reds_on_a_success_only_receipt() -> None:
    """The gate fires on the real defect: a receipt written only when green.

    A publish that failed is exactly the case reconciliation exists for,
    so a receipt gated on success leaves the ledger blind where it most
    needs sight. The synthetic job also drops the run id and never
    uploads the file.
    """
    defective = yaml.safe_load(
        """
        jobs:
          publish-pypi:
            steps:
              - name: Write the receipt
                run: |
                  echo '{"target_id": "pypi", "artifact_digests": {},
                  "job_conclusion": "ok"}' > publication-receipt-pypi.json
        """
    )
    problems = publication_receipt_violations(defective, "publish-pypi", "pypi")
    assert any("skips its receipt when the publish fails" in p for p in problems), problems
    assert any("records no 'run_id'" in p for p in problems), problems
    assert any("uploads no 'publication-receipt-pypi'" in p for p in problems), problems


def test_publication_receipt_gate_reds_on_a_receiptless_publish() -> None:
    """A publish job that writes nothing down settles as unknown forever."""
    defective = yaml.safe_load(
        """
        jobs:
          publish-claude-npm:
            steps:
              - name: Publish to npm
                run: npm publish --tag next
        """
    )
    problems = publication_receipt_violations(defective, "publish-claude-npm", "npm")
    assert "publish-claude-npm writes no 'publication-receipt-npm.json'" in problems


def test_publication_receipt_gate_reds_on_a_missing_job() -> None:
    """A workflow with no publish job at all is itself the violation."""
    assert publication_receipt_violations(yaml.safe_load("jobs: {}\n"), "publish-pypi", "pypi") == [
        "the workflow declares no 'publish-pypi' job"
    ]


# --- phase-release annotation grammar ----------------------------------------


def _annotation_pattern() -> re.Pattern[str]:
    """Return the release-annotation regex phase-release.yaml actually runs.

    Extracted from the workflow source rather than restated here: a
    pattern the test spells for itself proves the test's grammar, not
    the pipeline's.

    Returns:
        The compiled pattern.

    Raises:
        AssertionError: When the annotation step carries no regex.
    """
    job = _load_phase_release()["jobs"]["phase-release"]
    step = _find_step(job, "release annotation")
    assert step is not None, "phase-release.yaml has no annotation-extracting step"
    match = re.search(r'r"(\\\(release=.+?)"', str(step.get("run", "")))
    assert match is not None, "the annotation step carries no release= regex"
    return re.compile(match.group(1))


@pytest.mark.parametrize(
    ("subject", "tag"),
    [
        ("[P31] state: close iter + phase (release=v0.7.0.dev1)", "v0.7.0.dev1"),
        ("[P31] state: close iter + phase (release=v0.7.0.dev12)", "v0.7.0.dev12"),
        ("[P32] state: close iter + phase (release=v0.7.0rc1)", "v0.7.0rc1"),
        ("[P33] state: close iter + phase (release=v0.7.0)", "v0.7.0"),
        ("[P30] state: close iter + phase (release=v0.6.9a1)", "v0.6.9a1"),
        ("[P30] state: close iter + phase (release=v0.6.9b2)", "v0.6.9b2"),
    ],
)
def test_release_annotation_dev_segments_are_accepted(subject: str, tag: str) -> None:
    """The live grammar tags every checkpoint the v0.7 train walks through."""
    match = _annotation_pattern().search(subject)
    assert match is not None, subject
    assert match.group(1) == tag


@pytest.mark.parametrize(
    "subject",
    [
        "[P31] state: close iter + phase",
        "[P31] state: close iter + phase (release=0.7.0.dev1)",
        "[P31] state: close iter + phase (release=v0.7.dev1)",
        "[P31] state: close iter + phase (release=v0.7.0.dev)",
    ],
)
def test_release_annotation_dev_grammar_rejects_a_malformed_annotation(subject: str) -> None:
    """A subject that names no well-formed tag must not tag anything."""
    assert _annotation_pattern().search(subject) is None


def test_release_annotation_dev_version_check_compares_normalised_forms() -> None:
    """The version-source step normalises both sides before comparing them."""
    job = _load_phase_release()["jobs"]["phase-release"]
    step = _find_step(job, "version source")
    assert step is not None, "phase-release.yaml has no version-source verification step"
    run = str(step.get("run", ""))
    assert "def normalise(" in run
    assert "normalise(actual) != normalise(expected)" in run


def test_release_annotation_dev_gate_reds_on_the_prerelease_only_grammar() -> None:
    """The gate fires on the real defect: the grammar that stopped at ``rcN``.

    That regex is what shipped before the v0.7 train needed dev
    checkpoints, and its failure mode is silent -- a dev-checkpoint merge
    reads as "no release annotation" and the phase ships untagged rather
    than reding anything.
    """
    stale = re.compile(r"\(release=(v\d+\.\d+\.\d+(?:a\d+|b\d+|rc\d+)?)\)")
    subject = "[P31] state: close iter + phase (release=v0.7.0.dev1)"
    assert stale.search(subject) is None
    assert _annotation_pattern().search(subject) is not None


# --- test paths named by the build config still exist -----------------------

#: Every config that hard-codes a ``tests/`` path: the CI workflow's pytest
#: legs, the local recipes that mirror them, ruff's per-file ignores and the
#: managed-golden globs, and the hook file filters.
_TEST_PATH_CONFIGS: tuple[str, ...] = (
    ".github/workflows/ci.yaml",
    "justfile",
    "pyproject.toml",
    ".pre-commit-config.yaml",
)

#: A ``tests/`` path as these configs spell one: slash-separated segments of
#: word characters, dots and dashes, stopping at the first quote, whitespace
#: or shell metacharacter. Backslashes are admitted because two of the four
#: configs spell a path inside a regex (``settings\.json``); the caller
#: strips them, along with a trailing glob slash.
_TEST_PATH_RE = re.compile(r"tests/[\w./\\-]*[\w/]")


def _configured_test_paths() -> dict[str, set[str]]:
    """Return the ``tests/`` paths each build config names, by config file."""
    found: dict[str, set[str]] = {}
    for name in _TEST_PATH_CONFIGS:
        text = (_REPO_ROOT / name).read_text(encoding="utf-8")
        found[name] = {
            match.group(0).replace("\\", "").rstrip("/") for match in _TEST_PATH_RE.finditer(text)
        }
    return found


def _move_commit_ci_orphans(configured: dict[str, set[str]]) -> list[str]:
    """Return one row per configured ``tests/`` path that is not on disk."""
    return sorted(
        f"{name} names {path}, which does not exist"
        for name, paths in configured.items()
        for path in paths
        if not (_REPO_ROOT / path).exists()
    )


def test_move_commit_ci_paths_all_resolve_on_disk() -> None:
    """Every ``tests/`` path the build config names exists.

    A directory move orphans a hard-coded path silently: the CI step still
    runs, pytest reports "no tests ran", and the job goes green having
    proved nothing. This gate is the reason a move commit must carry its
    path-rule updates rather than leave them to a follow-up.
    """
    assert _move_commit_ci_orphans(_configured_test_paths()) == []


def test_move_commit_ci_gate_reds_on_an_orphaned_path() -> None:
    """The gate fires on the real defect: a path left behind by a move."""
    orphans = _move_commit_ci_orphans({"justfile": {"tests/runtimes/test_metering.py"}})
    assert orphans == ["justfile names tests/runtimes/test_metering.py, which does not exist"]


def test_move_commit_ci_covers_every_config_that_hardcodes_a_test_path() -> None:
    """Each scanned config really does name at least one ``tests/`` path.

    Boundary: a config that stops naming test paths (or is renamed) would
    otherwise make this gate silently vacuous for that file.
    """
    configured = _configured_test_paths()
    assert sorted(name for name, paths in configured.items() if not paths) == []
