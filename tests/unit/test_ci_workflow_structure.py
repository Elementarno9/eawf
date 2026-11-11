"""A draft pull request runs the fast CI lane; a ready one runs the full configuration.

A draft runs four ubuntu shards, one serial lane for the end-to-end and perf
tiers, one combined job for the small gates and one macOS job over the
affected tests. A pull request ready for review runs what a push to main or
dev runs, the whole-suite macOS shards and the coverage gate included, plus
the console replay ci.yaml calls. The job conditions are Actions expressions,
so each lane is read by evaluating each job's condition against an event,
not by matching text. The workflow files the fast lane retired stay gone,
and the green-tree record treats a dev push as it treats a main push.
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
    'tests/eval/test_skill_eval.py -m "eval and not repo_state"',
    "tools/snapshot_pairing_gate.py",
    "tools/commit_prefix_lint.py --check-history",
    "eawf hook ledger-history",
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


def _runs_on_event(job: dict[str, Any], event: str, *, draft: bool = True) -> bool:
    """Evaluate *job*'s condition for an *event* run whose changes job said code=true.

    A pull request is a draft unless *draft* is false; other events carry no pull
    request, and every condition reads the draft flag only behind an event check.
    """
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
        "github.event.pull_request.draft": draft,
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


def _pull_request_jobs(*, draft: bool = True) -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = _workflow()["jobs"]
    return {
        name: job for name, job in jobs.items() if _runs_on_event(job, "pull_request", draft=draft)
    }


def _runs(job: dict[str, Any], event: str = "pull_request", *, draft: bool = True) -> str:
    """Return the run scripts of the steps of *job* that an *event* run starts."""
    scripts = []
    for step in job.get("steps", []):
        condition = str(step.get("if", "true")).strip()
        embedded = _EMBEDDED.fullmatch(condition)
        condition = embedded.group(1) if embedded else condition
        context = {
            "github.event_name": event,
            "github.event.pull_request.draft": draft,
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
    """A draft pull request runs the fast lane and a dev push runs the full matrix."""
    triggers = _triggers(_workflow())
    assert {"main", "dev"} <= set(triggers["push"]["branches"])

    jobs = _pull_request_jobs()
    assert sorted(jobs) == [
        "changes",
        "ci-ok",
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

    e2e = [
        name
        for name, runs in gate_runs.items()
        if 'pytest -m "not eval and not repo_state" -n0 tests/e2e' in runs
    ]
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


def test_full_configuration_at_ready() -> None:
    """A ready pull request runs what a push runs; a draft runs the fast lane only."""
    jobs: dict[str, dict[str, Any]] = _workflow()["jobs"]
    ready = set(_pull_request_jobs(draft=False))
    draft = set(_pull_request_jobs(draft=True))
    push = {name for name, job in jobs.items() if _runs_on_event(job, "push")}

    # The record job stays push-only: a pull request run tests a merge tree nobody tags.
    assert ready == (push - {"green-tree"}) | {"console-replay"}
    assert {"coverage", "e2e", "test-macos", "console-replay"} <= ready
    assert "macos-affected" not in ready
    assert _legs(jobs["test-macos"]) == ["macos-26"] * 3
    assert "--cov=eawf" in _runs(jobs["test"], draft=False)
    assert "--cov=eawf" in _runs(jobs["e2e"], draft=False)

    assert draft & {"coverage", "test-macos", "console-replay"} == set()
    assert "macos-affected" in draft
    assert "--cov" not in _runs(jobs["test"]) + _runs(jobs["e2e"])


@pytest.mark.parametrize("draft", [True, False])
def test_full_configuration_at_ready_skips_no_job_on_a_non_pull_request_event(
    draft: bool,
) -> None:
    """A push carries no pull request, so the draft flag never changes what it runs."""
    jobs: dict[str, dict[str, Any]] = _workflow()["jobs"]
    for event in ("push", "schedule", "workflow_dispatch"):
        running = {name for name, job in jobs.items() if _runs_on_event(job, event, draft=draft)}
        assert running == {name for name, job in jobs.items() if _runs_on_event(job, event)}


def test_console_replay_folded() -> None:
    """The replay runs inside ci.yaml at ready, under the ci-ok verdict, and nowhere else."""
    replay_file = _WORKFLOWS / "console-replay.yaml"
    assert "pull_request" not in _triggers(_workflow(replay_file))

    jobs: dict[str, dict[str, Any]] = _workflow()["jobs"]
    replay = jobs["console-replay"]
    assert replay["uses"] == "./.github/workflows/console-replay.yaml"
    assert _runs_on_event(replay, "pull_request", draft=False)
    assert not _runs_on_event(replay, "pull_request", draft=True)
    assert not [e for e in ("push", "schedule", "workflow_dispatch") if _runs_on_event(replay, e)]
    assert "console-replay" in jobs["ci-ok"]["needs"]
    assert "console-replay" in jobs["green-tree"]["needs"]

    callers = [
        path.name
        for path in sorted(_WORKFLOWS.iterdir())
        if path != replay_file and "console-replay.yaml" in path.read_text(encoding="utf-8")
    ]
    assert callers == ["ci.yaml"]


def test_pull_request_types_unfiltered() -> None:
    """Every pull request runs the workflow, so a required check never stays pending."""
    pull_request = _triggers(_workflow())["pull_request"]
    assert pull_request == {"types": ["opened", "synchronize", "reopened", "ready_for_review"]}
    assert "paths-ignore" not in pull_request
    assert "paths" not in pull_request


#: The result string GitHub hands the verdict step, one result per needed job.
_RESULTS_EXPRESSION = "${{ join(needs.*.result, ' ') }}"


#: The verdict script's one passing ``case`` arm and its failing catch-all.
_PASS_ARM = re.compile(r"^\s*([a-z| ]+)\)\s*;;\s*$", re.MULTILINE)
_FAIL_ARM = re.compile(r"^\s*\*\)\s*$(?:\n(?!\s*;;).*)*\n\s*exit 1\s*$", re.MULTILINE)


def _verdict(results: list[str]) -> int:
    """Return the exit status the ci-ok verdict step's script gives *results*.

    The script loops over the space-separated results and runs one ``case``:
    the results it lists in its passing arm continue, and its catch-all exits 1.
    """
    step = _workflow()["jobs"]["ci-ok"]["steps"][-1]
    assert step["env"] == {"RESULTS": _RESULTS_EXPRESSION}
    script = str(step["run"])
    assert 'for result in ${RESULTS}; do\n  case "${result}" in' in script
    assert _FAIL_ARM.search(script) is not None
    arms = _PASS_ARM.findall(script)
    assert len(arms) == 1
    passing = {pattern.strip() for pattern in arms[0].split("|")}
    return 0 if all(result in passing for result in results) else 1


#: Status functions that replace the implicit ``success()`` a job condition
#: otherwise carries.
_STATUS_FUNCTIONS = ("always()", "cancelled()", "success()", "failure()")


def _simulate(
    jobs: dict[str, dict[str, Any]], names: list[str], context: dict[str, object]
) -> dict[str, str]:
    """Return ``success`` or ``skipped`` per job of *names*, in workflow order.

    A job runs when its condition holds and, unless the condition names a
    status function, every job it needs succeeded: GitHub skips a job whose
    need was skipped under the implicit ``success()``.
    """
    results: dict[str, str] = {}
    for name in (job for job in jobs if job in names):
        job = jobs[name]
        condition = str(job.get("if", "true"))
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else list(needs)
        implicit = not any(status in condition for status in _STATUS_FUNCTIONS)
        blocked = implicit and any(results.get(need) != "success" for need in needs)
        results[name] = "success" if not blocked and _evaluate(condition, context) else "skipped"
    return results


def test_simulate_skips_a_job_whose_need_skipped() -> None:
    jobs: dict[str, dict[str, Any]] = {
        "a": {"if": "false"},
        "b": {"needs": "a"},
        "c": {"needs": ["a"], "if": "always()"},
    }
    assert _simulate(jobs, ["a", "b", "c"], {}) == {"a": "skipped", "b": "skipped", "c": "success"}


def test_ci_ok_result_job() -> None:
    """ci-ok always runs, needs every other job but the record, and passes on success or skip."""
    jobs: dict[str, dict[str, Any]] = _workflow()["jobs"]
    ci_ok = jobs["ci-ok"]
    assert ci_ok["if"] == "always()"
    assert set(ci_ok["needs"]) == set(jobs) - {"ci-ok", "green-tree"}
    assert len(ci_ok["needs"]) == len(set(ci_ok["needs"]))
    assert _runs_on_event(ci_ok, "pull_request")

    # A code=false change set on a draft and on pushes: every job that runs
    # tests skips, only the classifier and the small gates run, and ci-ok passes.
    for event, ref, draft in (
        ("pull_request", "refs/pull/1/merge", True),
        ("pull_request", "refs/pull/1/merge", False),
        ("push", "refs/heads/dev", False),
        ("push", "refs/heads/main", False),
        ("push", "refs/heads/release/0.7", False),
    ):
        proven = {
            "github.event_name": event,
            "github.ref": ref,
            "github.event.pull_request.draft": draft,
            "needs.changes.result": "success",
            "needs.changes.outputs.code": "false",
        }
        results = _simulate(jobs, ci_ok["needs"], proven)
        assert [name for name, result in results.items() if result == "success"] == [
            "changes",
            "gates",
        ], (event, ref, draft)
        assert _verdict(list(results.values())) == 0

    assert _verdict(["success"] * len(ci_ok["needs"])) == 0
    assert _verdict(["skipped"]) == 0
    assert _verdict(["success", "failure"]) == 1
    assert _verdict(["cancelled", "success"]) == 1
    assert _verdict(["success", "skipped", "failure"]) == 1


def test_concurrency_cancels_superseded_runs() -> None:
    """Two pushes to one pull request share a group, so the later run cancels the earlier."""
    concurrency = _workflow()["concurrency"]
    group = (
        "ci-${{ github.event.pull_request.number"
        " || format('{0}-{1}', github.event_name, github.ref) }}"
    )
    assert concurrency == {"group": group, "cancel-in-progress": True}

    def rendered(event: str, number: str, ref: str) -> str:
        context = {
            "github.event_name": event,
            "github.event.pull_request.number": number,
            "github.ref": ref,
        }
        return _render(group, context)

    first = rendered("pull_request", "7", "refs/pull/7/merge")
    assert first == rendered("pull_request", "7", "refs/pull/7/merge") == "ci-7"
    assert rendered("pull_request", "8", "refs/pull/8/merge") != first
    push = rendered("push", "", "refs/heads/main")
    assert push == "ci-push-refs/heads/main"
    assert rendered("schedule", "", "refs/heads/main") != push


def test_workflow_files() -> None:
    """The retired workflows are gone and the release workflows keep their names."""
    present = {path.name for path in _WORKFLOWS.iterdir()}
    assert "feature-gates.yaml" not in present
    assert "reviewdog.yml" not in present
    assert {"release.yaml", "plugin-release.yaml", "console-replay.yaml"} <= present

    replay = _triggers(_workflow(_WORKFLOWS / "console-replay.yaml"))
    assert replay == {"workflow_call": None}


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
    for job in ("gates", "test", "e2e", "test-macos", "coverage", "linux-jail", "windows"):
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
