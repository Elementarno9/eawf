"""The pull-request CI lane is the fast one; dev pushes run the full matrix.

A pull request runs four ubuntu shards, one serial lane for the end-to-end
and perf tiers, one combined job for the small gates and one macOS job over
the affected tests; the whole-suite macOS shards and the coverage gate run
on pushes to main and dev. The job conditions are Actions expressions, so
the lane is read by evaluating each job's condition against a pull-request
event, not by matching text. The workflow files the fast lane retired stay
gone, and the green-tree record treats a dev push as it treats a main push.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from tests.lint.test_ci_workflow_shape import _evaluate, _render

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_CI = _WORKFLOWS / "ci.yaml"
_TOOL_PATH = _REPO_ROOT / "tools" / "ci_green_tree.py"

#: Every command the combined small-gates job owns on a pull request.
_SMALL_GATES = (
    "uv run pre-commit run --all-files",
    "uv run deptry .",
    "uv run mypy src/",
    "uv build --wheel",
    "tools/requirement_trace.py check --require-owned",
    "tests/eval/test_skill_eval.py -m eval",
    "tools/snapshot_pairing_gate.py",
    "tools/commit_prefix_lint.py --check-history",
    "eawf hook ledger-history",
    "eawf hook validate-prose --strict",
    "eawf hook eawf027-citation-scope",
)

#: The single-gate jobs the combined job replaced.
_RETIRED_GATE_JOBS = (
    "lint",
    "tool-install-smoke",
    "snapshot-pairing",
    "commit-grammar",
    "requirement-trace",
    "skill-eval",
    "prose-gate",
)

_SHA = "a" * 40
_TREE = "c" * 40
_EMBEDDED = re.compile(r"^\$\{\{(.*)\}\}$", re.DOTALL)


def _workflow(path: Path = _CI) -> dict[Any, Any]:
    document: dict[Any, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    """Return the trigger table; YAML 1.1 reads a bare ``on`` key as ``True``."""
    raw = workflow.get(True, workflow.get("on"))
    return raw if isinstance(raw, dict) else {}


def _runs_on_event(job: dict[str, Any], event: str) -> bool:
    """Evaluate *job*'s condition for an *event* run whose changes job said code=true."""
    condition = str(job.get("if", "true")).strip()
    embedded = _EMBEDDED.fullmatch(condition)
    if embedded is not None:
        condition = embedded.group(1)
    if "needs.*.result" in condition:
        condition = condition.replace("contains(needs.*.result, 'failure')", "false")
        condition = condition.replace("contains(needs.*.result, 'cancelled')", "false")
    context = {
        "github.event_name": event,
        "github.ref": "refs/pull/1/merge",
        "needs.changes.result": "success",
        "needs.changes.outputs.code": "true",
    }
    return bool(_evaluate(condition, context))


def _legs(job: dict[str, Any]) -> list[str]:
    """Return one runner label per matrix leg *job* starts."""
    runs_on = str(job["runs-on"])
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    legs = [runs_on]
    for axis, values in matrix.items():
        legs = [
            leg.replace(f"${{{{ matrix.{axis} }}}}", str(value)) for leg in legs for value in values
        ]
    return legs


def _pull_request_jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = _workflow()["jobs"]
    return {name: job for name, job in jobs.items() if _runs_on_event(job, "pull_request")}


def _runs(job: dict[str, Any], event: str = "pull_request") -> str:
    """Return the run scripts of the steps of *job* that an *event* run starts."""
    scripts = []
    for step in job.get("steps", []):
        condition = str(step.get("if", "true")).strip()
        embedded = _EMBEDDED.fullmatch(condition)
        condition = embedded.group(1) if embedded else condition
        context = {
            "github.event_name": event,
            "steps.sync.outcome": "success",
            "steps.wheel.outcome": "success",
            "matrix.shard": "1",
            "steps.resvg-cache.outputs.cache-hit": "false",
            "steps.scope.outputs.scope": "tests/unit",
            "github.event.pull_request.base.sha": _SHA,
            "github.event.pull_request.head.sha": _SHA,
        }
        if bool(_evaluate(condition, context)):
            scripts.append(" ".join(_render(str(step.get("run", "")), context).split()))
    return "\n".join(scripts)


def _option(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == flag]


def test_pr_lane() -> None:
    """A pull request runs the fast lane and a dev push runs the full matrix."""
    triggers = _triggers(_workflow())
    pull_request = triggers["pull_request"] or {}
    assert pull_request.get("paths-ignore") or pull_request.get("paths")
    assert {"main", "dev"} <= set(triggers["push"]["branches"])

    jobs = _pull_request_jobs()
    assert sorted(jobs) == [
        "changes",
        "e2e",
        "gates",
        "linux-jail",
        "macos-affected",
        "test",
        "windows",
    ]

    ubuntu_shards = _legs(jobs["test"])
    assert ubuntu_shards == ["ubuntu-24.04"] * 4
    parallel = next(s for s in jobs["test"]["steps"] if s.get("name") == "Pytest (parallel)")
    argv = shlex.split(str(parallel["run"]))
    assert _option(argv, "--splits") == ["4"]
    assert "tests/e2e" in [arg.removeprefix("--ignore=") for arg in argv]

    gate_runs = {name: _runs(job) for name, job in jobs.items()}
    for command in _SMALL_GATES:
        assert [name for name, runs in gate_runs.items() if command in runs] == ["gates"], command
    assert not [name for name in _RETIRED_GATE_JOBS if name in _workflow()["jobs"]]

    e2e = [name for name, runs in gate_runs.items() if "pytest -n0 tests/e2e" in runs]
    assert e2e == ["e2e"]

    macos = [label for job in jobs.values() for label in _legs(job) if label.startswith("macos")]
    assert macos == ["macos-26"]

    assert "coverage" not in jobs
    assert not [name for name, runs in gate_runs.items() if "coverage_gate.py" in runs]
    assert "--cov" not in _runs(jobs["test"]) + _runs(jobs["e2e"])


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_pr_lane_moves_macos_and_coverage_off_pull_requests(event: str) -> None:
    """Every other event runs the whole-suite macOS shards and the coverage gate."""
    jobs: dict[str, dict[str, Any]] = _workflow()["jobs"]
    running = {name for name, job in jobs.items() if _runs_on_event(job, event)}
    assert {"test", "e2e", "test-macos", "coverage"} <= running
    assert "macos-affected" not in running
    assert _legs(jobs["test-macos"]) == ["macos-26"] * 3
    assert "--cov=eawf" in _runs(jobs["test"], event)


def test_pr_lane_reads_a_condition_wrapped_in_an_expression() -> None:
    assert _runs_on_event({"if": "${{ github.event_name == 'push' }}"}, "push")
    assert not _runs_on_event({"if": "${{ github.event_name == 'push' }}"}, "pull_request")
    assert _runs_on_event({}, "pull_request")


def test_pr_lane_expands_every_matrix_leg() -> None:
    job = {"runs-on": "${{ matrix.os }}", "strategy": {"matrix": {"os": ["a", "b"], "n": [1, 2]}}}
    assert _legs(job) == ["a", "a", "b", "b"]
    assert _legs({"runs-on": "macos-26"}) == ["macos-26"]


def test_workflow_files() -> None:
    """The retired workflows are gone and the release workflows keep their names."""
    present = {path.name for path in _WORKFLOWS.iterdir()}
    assert "feature-gates.yaml" not in present
    assert "reviewdog.yml" not in present
    assert {"release.yaml", "plugin-release.yaml", "console-replay.yaml"} <= present

    replay = _triggers(_workflow(_WORKFLOWS / "console-replay.yaml"))
    assert "push" not in replay
    assert "pull_request" in replay


def test_workflow_files_read_the_on_key_under_either_spelling() -> None:
    assert _triggers({True: {"push": None}}) == {"push": None}
    assert _triggers({"on": {"pull_request": None}}) == {"pull_request": None}
    assert _triggers({}) == {}
    assert _triggers({True: "push"}) == {}


@pytest.fixture()
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_green_tree", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_green_tree"] = module
    spec.loader.exec_module(module)
    return module


def _needs(**results: str) -> dict[str, Any]:
    """Return the needs of a push run whose changes job said code=true."""
    needs: dict[str, Any] = {
        "changes": {"result": "success", "outputs": {"code": "true", "green": ""}},
    }
    for job in ("gates", "test", "e2e", "test-macos", "coverage", "windows"):
        needs[job] = {"result": results.get(job.replace("-", "_"), "success"), "outputs": {}}
    for job in ("macos-affected", "twice-green"):
        needs[job] = {"result": "skipped", "outputs": {}}
    return needs


def _env(ref: str) -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_SHA": _SHA,
        "GITHUB_RUN_ID": "7",
        "GITHUB_REF": ref,
    }


def _git(args: object) -> str:
    return f"{_TREE}\n"


def _no_fetch(url: str) -> object:
    raise AssertionError(f"a passed run fetches nothing, yet asked for {url}")


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/dev"])
def test_green_tree_dev(tool: ModuleType, ref: str) -> None:
    """A dev push records its tree exactly as a main push does, macOS shards included."""
    record = tool.build_record(_env(ref), needs=_needs(), git=_git, fetch=_no_fetch)
    assert (record.tree, record.sha, record.heavy_jobs) == (_TREE, _SHA, "passed")
    with pytest.raises(tool.RecordRefusedError, match=r"heavy jobs did not all pass: .*test-macos"):
        tool.build_record(_env(ref), needs=_needs(test_macos="skipped"), git=_git, fetch=_no_fetch)


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/dev"])
@pytest.mark.parametrize("job", ["test_macos", "e2e"])
def test_green_tree_dev_refuses_a_push_whose_heavy_job_skipped(
    tool: ModuleType, ref: str, job: str
) -> None:
    """The whole-suite macOS shards and the serial lane are heavy on every push."""
    with pytest.raises(tool.RecordRefusedError, match="heavy jobs did not all pass"):
        tool.plan_record(_needs(**{job: "skipped"}), event="push", ref=ref)


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/dev"])
def test_green_tree_dev_refuses_a_failed_job(tool: ModuleType, ref: str) -> None:
    with pytest.raises(tool.RecordRefusedError, match="needed jobs did not pass: test-macos"):
        tool.plan_record(_needs(test_macos="failure"), event="push", ref=ref)


def test_green_tree_dev_refuses_a_record_job_that_misses_a_heavy_job(tool: ModuleType) -> None:
    needs = _needs()
    del needs["test-macos"]
    with pytest.raises(tool.RecordRefusedError, match="does not need test-macos"):
        tool.plan_record(needs, event="push", ref="refs/heads/dev")


def test_green_tree_dev_gate_accepts_a_green_dev_push_run(tool: ModuleType) -> None:
    """The release gate reads push runs on any branch, so a dev run vouches too."""
    payload = {
        "workflow_runs": [
            {
                "id": 11,
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "head_branch": "dev",
                "head_sha": _SHA,
                "head_repository": {"full_name": "o/r"},
            }
        ]
    }
    green = tool.find_green_run(
        fetch=lambda url: payload,
        api_url="https://api.test",
        repository="o/r",
        sha=_SHA,
        tree=_TREE,
    )
    assert green == tool.GreenRun(run_id=11, head_sha=_SHA, basis="head sha")


def test_green_tree_dev_records_every_heavy_job_the_workflow_gates(tool: ModuleType) -> None:
    """Each job the changes job can skip on a push is one the record waits on."""
    gated = {
        name
        for name, job in _workflow()["jobs"].items()
        if "needs.changes.outputs.code" in str(job.get("if", "")) and _runs_on_event(job, "push")
    }
    release = {"twice-green"}
    assert gated | release == set(tool.HEAVY_JOBS)
    assert set(tool.HEAVY_JOBS) <= set(_workflow()["jobs"]["green-tree"]["needs"])
