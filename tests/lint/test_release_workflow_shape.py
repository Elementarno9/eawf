"""The release workflows dry-run nightly, publish idempotently and fail fast.

The tag-time gates used to fire only on a tag, so a red receipt, audit or
plugin validate surfaced when the release was already being cut. These
tests pin the shape that moves them earlier: a nightly release.yaml run
that builds, produces the receipts and packages and validates the plugin
while every publish job stays skipped; a PyPI upload a re-run can finish;
a plugin-release dist wait that stops as soon as the release run it waits
on has completed without a dist; and the prose and skill-eval gates on
feature-branch pushes without the heavy test matrix.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.lint.test_ci_workflow_shape import _context, _evaluate

pytestmark = pytest.mark.unit

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_RELEASE = _WORKFLOWS / "release.yaml"
_PLUGIN_RELEASE = _WORKFLOWS / "plugin-release.yaml"
_CI = _WORKFLOWS / "ci.yaml"
_FEATURE = _WORKFLOWS / "feature-gates.yaml"

#: The jobs a nightly dry run must execute: build, receipts, plugin gate.
_DRY_RUN_JOBS = (
    "inventory-and-reproducibility",
    "build-wheel",
    "install-smoke",
    "plugin-package-validate",
)
#: The steps plugin-package-validate shares verbatim with plugin-release.
_SHARED_PLUGIN_STEPS = (
    "Render published plugin trees (claude=npm, codex=git-subdir)",
    "Validate gate (blocking) — claude + codex trees",
)
_DIST_WAIT = "Download the dist artifact from the Release run"


def _load(path: Path) -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return workflow


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the bare key ``on`` as the boolean True.
    triggers: dict[str, Any] = workflow[True]
    return triggers


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


def _runs_on(job: dict[str, Any], event: str, ref: str) -> bool:
    condition = job.get("if")
    return condition is None or bool(_evaluate(str(condition), _context(event, ref)))


# --- the nightly dry run -----------------------------------------------------


def test_release_has_a_nightly_schedule() -> None:
    schedule = _triggers(_load(_RELEASE))["schedule"]
    assert len(schedule) == 1
    fields = schedule[0]["cron"].split()
    assert len(fields) == 5
    assert fields[0].isdigit() and fields[1].isdigit()
    assert fields[2:] == ["*", "*", "*"]


@pytest.mark.parametrize("job_name", _DRY_RUN_JOBS)
@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_dry_run_executes_build_receipts_and_plugin_gate(job_name: str, event: str) -> None:
    job = _load(_RELEASE)["jobs"][job_name]
    assert _runs_on(job, event, "refs/heads/main")


def test_dry_run_receipts_include_the_pip_audit_report() -> None:
    job = _load(_RELEASE)["jobs"]["inventory-and-reproducibility"]
    runs = "\n".join(str(step.get("run", "")) for step in job["steps"])
    uploads = {str(step.get("with", {}).get("name")) for step in job["steps"]}
    assert "python -m eawf.workflow.release.produce" in runs
    assert "vulnerability-report" in uploads


@pytest.mark.parametrize("step_name", _SHARED_PLUGIN_STEPS)
def test_plugin_gate_runs_the_plugin_release_steps_verbatim(step_name: str) -> None:
    ours = _step(_load(_RELEASE)["jobs"]["plugin-package-validate"], step_name)
    theirs = _step(_load(_PLUGIN_RELEASE)["jobs"]["package-validate"], step_name)
    assert ours["run"] == theirs["run"]


def test_plugin_gate_holds_no_publish_capability() -> None:
    job = _load(_RELEASE)["jobs"]["plugin-package-validate"]
    assert "permissions" not in job
    assert "environment" not in job
    assert not any(str(step.get("uses", "")).startswith("actions/upload") for step in job["steps"])


def _publish_jobs() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path.name, name, job)
        for path in (_RELEASE, _PLUGIN_RELEASE)
        for name, job in _load(path)["jobs"].items()
        if name.startswith("publish-")
    ]


def test_both_release_workflows_declare_publish_jobs() -> None:
    names = {(workflow, name) for workflow, name, _ in _publish_jobs()}
    assert ("release.yaml", "publish-pypi") in names
    assert ("plugin-release.yaml", "publish-claude-npm") in names


@pytest.mark.parametrize(
    ("event", "ref", "publishes"),
    [
        ("schedule", "refs/heads/main", False),
        ("workflow_dispatch", "refs/heads/main", False),
        ("workflow_dispatch", "refs/tags/v0.7.0", False),
        ("push", "refs/heads/main", False),
        ("push", "refs/heads/v0.7.0", False),
        ("push", "refs/tags/x0.7.0", False),
        ("push", "refs/tags/v", True),
        ("push", "refs/tags/v0.7.0rc2", True),
    ],
)
def test_publish_jobs_run_only_on_a_version_tag_push(event: str, ref: str, publishes: bool) -> None:
    for workflow, name, job in _publish_jobs():
        assert "if" in job, f"{workflow} {name} is unconditional"
        assert _runs_on(job, event, ref) is publishes, (workflow, name, event, ref)


def test_publish_condition_refuses_an_unknown_context() -> None:
    with pytest.raises(KeyError):
        _evaluate("github.actor == 'x'", _context("schedule"))


# --- idempotent PyPI publish -------------------------------------------------


def test_pypi_publish_skips_files_already_uploaded() -> None:
    job = _load(_RELEASE)["jobs"]["publish-pypi"]
    publish = next(
        step for step in job["steps"] if "gh-action-pypi-publish" in str(step.get("uses"))
    )
    assert publish["with"]["skip-existing"] is True


# --- the plugin-release dist wait --------------------------------------------


def _write_tool(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _wait(
    tmp_path: Path, runs: list[str], *, dist_on_poll: int | None
) -> subprocess.CompletedProcess[str]:
    """Run the dist-wait script against a fake ``gh`` and ``sleep``.

    Args:
        tmp_path: Scratch directory used as the working directory.
        runs: The ``id status conclusion`` line ``gh api`` prints on each
            poll; the last one repeats once the list runs out.
        dist_on_poll: The 1-based poll whose download succeeds, or None.

    Returns:
        The finished process; a fake ``sleep`` exits 99 on its third call so
        a wait that never ends still terminates.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "runs").write_text("\n".join(runs) + "\n", encoding="utf-8")
    _write_tool(
        bin_dir,
        "gh",
        f"""state="{tmp_path}/polls"
if [ "$1" = api ]; then
  n=$(( $(cat "$state" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$state"
  total=$(wc -l < "{tmp_path}/runs")
  [ "$n" -gt "$total" ] && n=$total
  sed -n "${{n}}p" "{tmp_path}/runs"
  exit 0
fi
if [ "$1" = run ] && [ "$(cat "$state")" = "{dist_on_poll}" ]; then
  mkdir -p dist && touch dist/eawf.whl; exit 0
fi
exit 1""",
    )
    _write_tool(
        bin_dir,
        "sleep",
        f"""n=$(( $(cat "{tmp_path}/sleeps" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "{tmp_path}/sleeps"
[ "$n" -ge 3 ] && exit 99
exit 0""",
    )
    script = _step(_load(_PLUGIN_RELEASE)["jobs"]["render-release-assets"], _DIST_WAIT)["run"]
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "REPO": "owner/repo",
        "SHA": "a" * 40,
    }
    return subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )


def _sleeps(tmp_path: Path) -> int:
    marker = tmp_path / "sleeps"
    return int(marker.read_text(encoding="utf-8")) if marker.exists() else 0


def test_dist_wait_reads_the_run_status_and_conclusion() -> None:
    script = _step(_load(_PLUGIN_RELEASE)["jobs"]["render-release-assets"], _DIST_WAIT)["run"]
    assert ".status" in script
    assert ".conclusion" in script


@pytest.mark.parametrize("conclusion", ["failure", "success", "cancelled"])
def test_dist_wait_exits_within_one_poll_when_the_run_completed_without_dist(
    tmp_path: Path, conclusion: str
) -> None:
    result = _wait(tmp_path, [f"7 completed {conclusion}"], dist_on_poll=None)
    assert result.returncode == 1
    assert f"completed ({conclusion}) without a dist artifact" in result.stderr
    assert _sleeps(tmp_path) == 0


def test_dist_wait_downloads_from_a_completed_run_that_has_dist(tmp_path: Path) -> None:
    result = _wait(tmp_path, ["7 completed success"], dist_on_poll=1)
    assert result.returncode == 0, result.stderr
    assert "downloaded dist from release.yaml run 7" in result.stdout
    assert _sleeps(tmp_path) == 0


def test_dist_wait_polls_an_in_flight_run_until_its_dist_lands(tmp_path: Path) -> None:
    runs = ["7 in_progress null", "7 in_progress null"]
    result = _wait(tmp_path, runs, dist_on_poll=2)
    assert result.returncode == 0, result.stderr
    assert _sleeps(tmp_path) == 1


def test_dist_wait_fails_fast_once_an_in_flight_run_completes_without_dist(
    tmp_path: Path,
) -> None:
    result = _wait(tmp_path, ["7 in_progress null", "7 completed failure"], dist_on_poll=None)
    assert result.returncode == 1
    assert "completed (failure)" in result.stderr
    assert _sleeps(tmp_path) == 1


def test_dist_wait_keeps_waiting_while_no_release_run_exists(tmp_path: Path) -> None:
    result = _wait(tmp_path, [""], dist_on_poll=None)
    assert result.returncode == 99
    assert _sleeps(tmp_path) == 3


# --- prose and eval gates on feature pushes ----------------------------------


def test_feature_gates_fire_on_feature_pushes_only() -> None:
    triggers = _triggers(_load(_FEATURE))
    assert set(triggers) == {"push"}
    assert triggers["push"] == {"branches": ["feature/**"]}


def test_feature_gates_run_prose_and_eval_without_the_test_matrix() -> None:
    jobs = _load(_FEATURE)["jobs"]
    assert set(jobs) == {"prose-gate", "skill-eval"}
    assert all("if" not in job for job in jobs.values())


def test_ci_push_trigger_names_no_feature_branch() -> None:
    branches = _triggers(_load(_CI))["push"]["branches"]
    assert not any(branch.startswith("feature") for branch in branches)


def test_feature_prose_gate_diffs_against_the_merge_base_with_main() -> None:
    job = _load(_FEATURE)["jobs"]["prose-gate"]
    checkout = _step(job, "Checkout")
    run = _step(job, "Validate prose (strict)")["run"]
    assert checkout["with"]["fetch-depth"] == 0
    assert 'git merge-base origin/main "${SHA}"' in run
    assert 'validate-prose --strict --base "${base}"' in run


def test_feature_skill_eval_runs_the_ci_command() -> None:
    ours = _step(_load(_FEATURE)["jobs"]["skill-eval"], "Skill envelope evals")["run"]
    theirs = _step(_load(_CI)["jobs"]["skill-eval"], "Skill envelope evals")["run"]
    assert ours == theirs
