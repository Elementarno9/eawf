"""The release gates accept a green push run on the tagged tree.

Both release workflows refuse to publish unless CI proved the tagged commit
green. Reading only the newest ``ci.yaml`` run for the commit let a
cancelled run hide a green one, and a green run on an identical tree never
counted. The gates now go through one shared checker,
``tools/ci_green_tree.py``, which accepts a successful push-event run on the
tagged commit, or one on the tagged tree that recorded that tree as green.
The record is written by the last ``ci.yaml`` job, only on a push, and only
when the heavy jobs passed or were skipped against a recorded green tree.

The workflows run only on a push or a tag push, so the checks read their
source, and each ships a companion that feeds the checker a defective shape
to prove it reds. The tool's decisions are exercised with injected git and
API answers, so no test spawns git or touches the network. ``tools/`` is not
a package, so the module is loaded by path.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import re
import sys
import urllib.error
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_CI = _WORKFLOWS / "ci.yaml"
_RELEASE = _WORKFLOWS / "release.yaml"
_PLUGIN_RELEASE = _WORKFLOWS / "plugin-release.yaml"
_TOOL_PATH = _REPO_ROOT / "tools" / "ci_green_tree.py"

_GREEN_TREE_JOB = "green-tree"
_GATE_JOB = "require-green-ci"
_GATE_COMMAND = "python3 tools/ci_green_tree.py gate"
_RECORD_COMMAND = "python3 tools/ci_green_tree.py record"
_RECORD_PREFIX = "ci-green-tree-"

#: The one condition the record job carries: a push run whose needed jobs
#: neither failed nor were cancelled, whether or not some were skipped.
_GREEN_TREE_CONDITION = (
    "!cancelled() && github.event_name == 'push'"
    " && !contains(needs.*.result, 'failure') && !contains(needs.*.result, 'cancelled')"
)
_TAG_PUSH_CONDITION = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"

_NEWEST_RUN = re.compile(r"per_page=1\b|workflow_runs\[0\]")
_INLINE_RUNS_QUERY = re.compile(r"workflows/ci\.yaml/runs")

_REPO = "owner/repo"
_SHA = "a" * 40
_OTHER_SHA = "b" * 40
_BASELINE = "e" * 40
_TREE = "c" * 40
_OTHER_TREE = "d" * 40
_BASELINE_TREE = "f" * 40
_API = "https://api.example.test"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_green_tree", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ci_green_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tool() -> ModuleType:
    return _load_tool()


def _load(path: Path) -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return workflow


def _needs_of(job: dict[str, Any]) -> list[str]:
    """Return the job names *job* needs, whichever YAML form it uses."""
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _upstream(jobs: dict[str, Any], name: str) -> set[str]:
    """Return every job *name* waits for, directly or through another job."""
    seen: set[str] = set()
    pending = _needs_of(jobs.get(name, {}))
    while pending:
        need = pending.pop()
        if need not in seen:
            seen.add(need)
            pending.extend(_needs_of(jobs.get(need, {})))
    return seen


# --- the green-tree record job ------------------------------------------------


def green_tree_violations(workflow: dict[str, Any], *, heavy_jobs: Sequence[str]) -> list[str]:
    """Report every way *workflow* could record a tree CI never proved green.

    The release gates accept a run on another commit when it recorded the
    tagged tree, so the record is only as sound as the job that writes it:
    a job that does not need every other job records a tree while one of
    them is red, a job that runs on a pull request records a merge tree
    nobody tags, and a record tool that checks other jobs than the ones the
    changes job skips can vouch for a tree the matrix never ran on.

    Args:
        workflow: The parsed CI workflow.
        heavy_jobs: The jobs the record tool requires to pass or to have
            been skipped against a recorded tree.

    Returns:
        One human-readable problem per violation; empty when ci.yaml ends
        with a push-only record job that needs every other job, the changes
        job exports its baseline, the heavy jobs are exactly the ones the
        changes job can skip, and the record upload is named for the tree
        and waits for the tool's decision.
    """
    jobs: dict[str, Any] = workflow.get("jobs", {})
    job = jobs.get(_GREEN_TREE_JOB)
    if job is None:
        return ["ci.yaml declares no 'green-tree' job"]
    problems: list[str] = []
    if list(jobs)[-1] != _GREEN_TREE_JOB:
        problems.append("ci.yaml does not end with the green-tree job")
    missing = [name for name in jobs if name != _GREEN_TREE_JOB and name not in _needs_of(job)]
    if missing:
        problems.append(
            f"the green-tree job does not need {', '.join(missing)}, "
            "so a red run of one of them can still record the tree"
        )
    if job.get("if") != _GREEN_TREE_CONDITION:
        problems.append(
            "the green-tree job is not gated to push runs whose needed jobs "
            "neither failed nor were cancelled"
        )
    permissions = job.get("permissions") or {}
    if permissions.get("actions") != "read":
        problems.append("the green-tree job cannot list the green run an inherited tree needs")
    gated = sorted(
        name
        for name, other in jobs.items()
        if "needs.changes.outputs.code" in str(other.get("if", ""))
    )
    if gated != sorted(heavy_jobs):
        problems.append(
            f"the jobs the changes job can skip ({', '.join(gated)}) are not the heavy "
            f"jobs the record tool checks ({', '.join(sorted(heavy_jobs))})"
        )
    return problems + _changes_baseline_output(jobs.get("changes", {})) + _record_steps(job)


def _changes_baseline_output(job: dict[str, Any]) -> list[str]:
    """Report a changes job that does not export the baseline it compared with."""
    steps: list[dict[str, Any]] = job.get("steps", [])
    classify = next(
        (step for step in steps if "tools/ci_changes.py" in str(step.get("run", ""))), None
    )
    wired = f"${{{{ steps.{(classify or {}).get('id')}.outputs.green }}}}"
    if classify is None or str((job.get("outputs") or {}).get("green", "")) != wired:
        return ["the changes job does not export the green baseline a skipped run inherits"]
    return []


def _record_steps(job: dict[str, Any]) -> list[str]:
    """Report the ways the record job's steps could upload the wrong record."""
    steps: list[dict[str, Any]] = job.get("steps", [])
    problems: list[str] = []
    checkout = next(
        (step for step in steps if str(step.get("uses", "")).startswith("actions/checkout")), None
    )
    if checkout is None or (checkout.get("with") or {}).get("fetch-depth") != 0:
        problems.append("the green-tree job checks out no history to read an inherited tree from")
    record = next(
        (step for step in steps if str(step.get("run", "")).startswith(_RECORD_COMMAND)), None
    )
    if record is None:
        return [*problems, "no green-tree step runs the tree record tool"]
    if (record.get("env") or {}).get("NEEDS") != "${{ toJSON(needs) }}":
        problems.append("the record step does not see the needed jobs' results")
    upload = next(
        (step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact")),
        None,
    )
    if upload is None:
        return [*problems, "no green-tree step uploads the tree record"]
    step_id = record.get("id")
    settings = upload.get("with") or {}
    if settings.get("name") != f"{_RECORD_PREFIX}${{{{ steps.{step_id}.outputs.tree }}}}":
        problems.append("the tree record is not named for the recorded tree")
    if upload.get("if") != f"steps.{step_id}.outputs.recorded == 'true'":
        problems.append("the tree record is uploaded whatever the record tool decided")
    if settings.get("if-no-files-found") != "error":
        problems.append("a missing tree record uploads nothing without failing the job")
    return problems


def test_green_tree_record_on_push(tool: ModuleType) -> None:
    """The live CI workflow records the green tree from its last job, on pushes."""
    assert green_tree_violations(_load(_CI), heavy_jobs=tool.HEAVY_JOBS) == []


def test_green_tree_gate_reds_on_a_partial_needs_list(tool: ModuleType) -> None:
    """The gate fires on the real defect: a record job that skips a needed job."""
    workflow = _load(_CI)
    workflow["jobs"][_GREEN_TREE_JOB]["needs"] = ["changes", "test", "twice-green"]
    assert green_tree_violations(workflow, heavy_jobs=tool.HEAVY_JOBS) == [
        "the green-tree job does not need tool-install-smoke, snapshot-pairing, requirement-trace, "
        "skill-eval, linux-jail, prose-gate, windows, "
        "so a red run of one of them can still record the tree"
    ]


def test_green_tree_gate_reds_on_a_single_string_need(tool: ModuleType) -> None:
    """A bare-string needs value is read as one job, not as its characters."""
    workflow = _load(_CI)
    workflow["jobs"][_GREEN_TREE_JOB]["needs"] = "changes"
    problems = green_tree_violations(workflow, heavy_jobs=tool.HEAVY_JOBS)
    assert len(problems) == 1
    assert problems[0].startswith("the green-tree job does not need test, tool-install-smoke")


def test_green_tree_gate_reds_on_an_unsound_record_job(tool: ModuleType) -> None:
    """Every other way the record job can vouch for an unproven tree reds.

    The synthetic job runs on any event and after any failure, is not the
    last job, reads a shallow clone, lacks the actions scope, hides the
    needed results from the tool, uploads a fixed name whatever the tool
    decided, and tolerates a missing file; the changes job exports no
    baseline, and a third job is gated on the changes verdict.
    """
    defective = yaml.safe_load(
        """
        jobs:
          changes:
            outputs:
              code: ${{ steps.classify.outputs.code }}
            steps:
              - id: classify
                run: python3 tools/ci_changes.py
          test:
            needs: changes
            if: needs.changes.outputs.code == 'true'
          twice-green:
            needs: changes
            if: github.event_name == 'push' && needs.changes.outputs.code == 'true'
          green-tree:
            needs: [changes, test, twice-green, docs]
            if: always()
            steps:
              - uses: actions/checkout@v4
              - id: record
                run: python3 tools/ci_green_tree.py record out.json
              - uses: actions/upload-artifact@v4
                with:
                  name: ci-green-tree
          docs:
            needs: changes
            if: needs.changes.outputs.code == 'true'
        """
    )
    assert green_tree_violations(defective, heavy_jobs=tool.HEAVY_JOBS) == [
        "ci.yaml does not end with the green-tree job",
        "the green-tree job is not gated to push runs whose needed jobs "
        "neither failed nor were cancelled",
        "the green-tree job cannot list the green run an inherited tree needs",
        "the jobs the changes job can skip (docs, test, twice-green) are not the heavy "
        "jobs the record tool checks (test, twice-green)",
        "the changes job does not export the green baseline a skipped run inherits",
        "the green-tree job checks out no history to read an inherited tree from",
        "the record step does not see the needed jobs' results",
        "the tree record is not named for the recorded tree",
        "the tree record is uploaded whatever the record tool decided",
        "a missing tree record uploads nothing without failing the job",
    ]


def test_green_tree_gate_reds_on_a_toolless_job(tool: ModuleType) -> None:
    """A record job that never runs the tool has no decision to upload."""
    workflow = _load(_CI)
    steps = workflow["jobs"][_GREEN_TREE_JOB]["steps"]
    workflow["jobs"][_GREEN_TREE_JOB]["steps"] = [steps[0], steps[2]]
    assert green_tree_violations(workflow, heavy_jobs=tool.HEAVY_JOBS) == [
        "no green-tree step runs the tree record tool"
    ]


def test_green_tree_gate_reds_on_an_uploadless_job(tool: ModuleType) -> None:
    """A record job that uploads nothing leaves the gates nothing to find."""
    workflow = _load(_CI)
    workflow["jobs"][_GREEN_TREE_JOB]["steps"].pop()
    assert green_tree_violations(workflow, heavy_jobs=tool.HEAVY_JOBS) == [
        "no green-tree step uploads the tree record"
    ]


def test_green_tree_gate_reds_on_a_missing_job(tool: ModuleType) -> None:
    """A CI workflow with no record job at all is itself the violation."""
    assert green_tree_violations(yaml.safe_load("jobs: {}\n"), heavy_jobs=tool.HEAVY_JOBS) == [
        "ci.yaml declares no 'green-tree' job"
    ]


# --- the record decision --------------------------------------------------------


def _needs(
    code: str, *, test: str = "success", twice: str = "success", green: str = ""
) -> dict[str, Any]:
    return {
        "changes": {"result": "success", "outputs": {"code": code, "green": green}},
        "test": {"result": test, "outputs": {}},
        "twice-green": {"result": twice, "outputs": {}},
        "snapshot-pairing": {"result": "skipped", "outputs": {}},
        "windows": {"result": "success", "outputs": {}},
    }


def _skipped_needs() -> dict[str, Any]:
    return _needs("false", test="skipped", twice="skipped", green=_BASELINE)


def test_green_tree_record_plan_accepts_passed_heavy_jobs(tool: ModuleType) -> None:
    assert tool.plan_record(_needs("true"), event="push") == ""


def test_green_tree_record_plan_inherits_a_skipped_push(tool: ModuleType) -> None:
    assert tool.plan_record(_skipped_needs(), event="push") == _BASELINE


def _without(needs: dict[str, Any], name: str) -> dict[str, Any]:
    trimmed = copy.deepcopy(needs)
    del trimmed[name]
    return trimmed


def _with_result(needs: dict[str, Any], name: str, result: object) -> dict[str, Any]:
    changed = copy.deepcopy(needs)
    changed[name]["result"] = result
    return changed


@pytest.mark.parametrize(
    ("needs", "event", "message"),
    [
        pytest.param(_needs("true"), "pull_request", "a pull_request run records", id="pr"),
        pytest.param(_needs("true"), "", "a unnamed run records no tree", id="no-event"),
        pytest.param({}, "push", "sees no needed jobs", id="empty"),
        pytest.param(None, "push", "sees no needed jobs", id="unreadable"),
        pytest.param(
            _with_result(_needs("true"), "windows", "failure"),
            "push",
            "needed jobs did not pass: windows",
            id="failed",
        ),
        pytest.param(
            _with_result(_needs("true"), "test", "cancelled"),
            "push",
            "needed jobs did not pass: test",
            id="cancelled",
        ),
        pytest.param(
            _with_result(_needs("true"), "windows", None),
            "push",
            "needed jobs did not pass: windows",
            id="no-result",
        ),
        pytest.param(
            _without(_needs("true"), "twice-green"),
            "push",
            "does not need twice-green",
            id="heavy-not-needed",
        ),
        pytest.param(
            _without(_needs("true"), "changes"),
            "push",
            "does not need changes",
            id="changes-not-needed",
        ),
        pytest.param(
            _with_result(_needs("true"), "changes", "skipped"),
            "push",
            "the changes job did not run",
            id="changes-skipped",
        ),
        pytest.param(
            _needs("true", test="skipped"),
            "push",
            "did not all pass: test=skipped, twice-green=success",
            id="code-true-heavy-skipped",
        ),
        pytest.param(
            _needs("false", test="skipped", green=_BASELINE),
            "push",
            "skipped the heavy jobs, yet test=skipped, twice-green=success",
            id="code-false-heavy-ran",
        ),
        pytest.param(
            _needs("false", test="skipped", twice="skipped"),
            "push",
            "named no green baseline: ''",
            id="no-baseline",
        ),
        pytest.param(
            _needs("false", test="skipped", twice="skipped", green="-rf"),
            "push",
            "named no green baseline: '-rf'",
            id="malformed-baseline",
        ),
        pytest.param(_needs(""), "push", "reported code=''", id="no-verdict"),
    ],
)
def test_green_tree_record_plan_refuses(
    tool: ModuleType, needs: object, event: str, message: str
) -> None:
    with pytest.raises(tool.RecordRefusedError, match=re.escape(message)):
        tool.plan_record(needs, event=event)


# --- fakes for git, the API and the clock ------------------------------------------


def _git(tool: ModuleType, trees: dict[str, str]) -> Callable[[Sequence[str]], str]:
    """Return a git runner that resolves ``<sha>^{tree}`` from *trees* only."""

    def run(args: Sequence[str]) -> str:
        argv = list(args)
        if argv[:2] == ["rev-parse", "--verify"] and len(argv) == 3:
            sha = argv[2].removesuffix("^{tree}")
            if sha in trees:
                return f"{trees[sha]}\n"
        raise tool.GreenTreeError(f"fatal: bad revision in {argv!r}")

    return run


def _run(
    run_id: int,
    sha: str,
    tree: str,
    *,
    event: str = "push",
    status: str = "completed",
    conclusion: str | None = "success",
    repo: str = _REPO,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "head_sha": sha,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "head_repository": {"full_name": repo},
        "head_commit": {"tree_id": tree},
    }


def _artifact(tree: str, *, expired: object = False) -> dict[str, Any]:
    return {"name": f"{_RECORD_PREFIX}{tree}", "expired": expired}


class FakeApi:
    """Answer the checker's three lookups from canned lists, recording the URLs.

    The runs lists are returned unfiltered, so a run the real API would
    have filtered out still reaches the checker's own filter.
    """

    def __init__(
        self,
        *,
        green: list[dict[str, Any]] | None = None,
        commit: list[dict[str, Any]] | None = None,
        records: dict[int, list[object]] | None = None,
    ) -> None:
        self.green = [] if green is None else green
        self.commit = [] if commit is None else commit
        self.records = {} if records is None else records
        self.urls: list[str] = []

    def __call__(self, url: str) -> object:
        self.urls.append(url)
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        if parts.path == f"/repos/{_REPO}/actions/workflows/ci.yaml/runs":
            return {"workflow_runs": self.green if query.get("status") else self.commit}
        match = re.fullmatch(rf"/repos/{_REPO}/actions/runs/(\d+)/artifacts", parts.path)
        if match is None:
            raise AssertionError(f"unexpected lookup {url}")
        return {"artifacts": self.records.get(int(match[1]), [])}


class FakeClock:
    """A monotonic clock that only moves when the checker sleeps."""

    def __init__(self, on_sleep: Callable[[], None] | None = None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._on_sleep = on_sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self._on_sleep is not None:
            self._on_sleep()


def _await(
    tool: ModuleType, fetch: Callable[[str], object], clock: FakeClock | None = None
) -> tuple[Any, FakeClock, list[str]]:
    clock = FakeClock() if clock is None else clock
    lines: list[str] = []
    outcome = tool.await_green_run(
        fetch=fetch,
        api_url=_API,
        repository=_REPO,
        sha=_SHA,
        tree=_TREE,
        clock=clock,
        sleep=clock.sleep,
        log=lines.append,
    )
    return outcome, clock, lines


# --- building and writing the record ----------------------------------------------


def _record_env(tmp_path: Path, needs: object, *, event: str = "push") -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REPOSITORY": _REPO,
        "GITHUB_SHA": _SHA,
        "GITHUB_RUN_ID": "777",
        "GITHUB_API_URL": _API,
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
        "NEEDS": json.dumps(needs),
    }


def _baseline_api() -> FakeApi:
    return FakeApi(
        green=[_run(41, _BASELINE, _BASELINE_TREE)], records={41: [_artifact(_BASELINE_TREE)]}
    )


def test_green_tree_record_build_for_passed_heavy_jobs(tool: ModuleType, tmp_path: Path) -> None:
    api = FakeApi()
    record = tool.build_record(
        _record_env(tmp_path, None), needs=_needs("true"), git=_git(tool, {_SHA: _TREE}), fetch=api
    )
    assert record == tool.TreeRecord(tree=_TREE, sha=_SHA, run_id="777", heavy_jobs="passed")
    assert api.urls == []


def test_green_tree_record_build_names_the_inherited_tree(tool: ModuleType, tmp_path: Path) -> None:
    git = _git(tool, {_SHA: _TREE, _BASELINE: _BASELINE_TREE})
    record = tool.build_record(
        _record_env(tmp_path, None), needs=_skipped_needs(), git=git, fetch=_baseline_api()
    )
    assert record == tool.TreeRecord(
        tree=_TREE,
        sha=_SHA,
        run_id="777",
        heavy_jobs="inherited",
        inherited_tree=_BASELINE_TREE,
        inherited_run_id="41",
    )


@pytest.mark.parametrize(
    "api",
    [
        pytest.param(FakeApi(green=[_run(41, _BASELINE, _BASELINE_TREE)]), id="unrecorded"),
        pytest.param(
            FakeApi(
                green=[_run(41, _BASELINE, _BASELINE_TREE, conclusion="failure")],
                records={41: [_artifact(_BASELINE_TREE)]},
            ),
            id="red-baseline-run",
        ),
        pytest.param(
            FakeApi(
                green=[_run(41, _BASELINE, _OTHER_TREE)], records={41: [_artifact(_OTHER_TREE)]}
            ),
            id="other-tree",
        ),
    ],
)
def test_green_tree_record_build_refuses_an_unrecorded_baseline(
    tool: ModuleType, tmp_path: Path, api: FakeApi
) -> None:
    git = _git(tool, {_SHA: _TREE, _BASELINE: _BASELINE_TREE})
    with pytest.raises(tool.RecordRefusedError, match="has no recorded tree"):
        tool.build_record(_record_env(tmp_path, None), needs=_skipped_needs(), git=git, fetch=api)


def test_green_tree_record_build_unknown_baseline_commit_raises(
    tool: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(tool.GreenTreeError, match="bad revision"):
        tool.build_record(
            _record_env(tmp_path, None),
            needs=_skipped_needs(),
            git=_git(tool, {_SHA: _TREE}),
            fetch=_baseline_api(),
        )


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("GITHUB_RUN_ID", "", "GITHUB_RUN_ID is unset"),
        ("GITHUB_SHA", "HEAD", "GITHUB_SHA is 'HEAD', not a full object name"),
    ],
)
def test_green_tree_record_build_bad_environment_raises(
    tool: ModuleType, tmp_path: Path, name: str, value: str, message: str
) -> None:
    env = _record_env(tmp_path, None)
    env[name] = value
    with pytest.raises(tool.GreenTreeError, match=re.escape(message)):
        tool.build_record(env, needs=_needs("true"), git=_git(tool, {_SHA: _TREE}), fetch=FakeApi())


def test_green_tree_record_main_writes_the_record_and_outputs(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _record_env(tmp_path, _needs("true"))
    body = tmp_path / "ci-green-tree.json"
    code = tool.main(["record", str(body)], env, git=_git(tool, {_SHA: _TREE}), fetch=FakeApi())
    assert code == 0
    assert json.loads(body.read_text(encoding="utf-8")) == {
        "heavy_jobs": "passed",
        "inherited_run_id": "",
        "inherited_tree": "",
        "run_id": "777",
        "sha": _SHA,
        "tree": _TREE,
    }
    output = Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8")
    assert output == f"recorded=true\ntree={_TREE}\n"
    assert capsys.readouterr().out == f"recorded=true: {_RECORD_PREFIX}{_TREE} (passed)\n"


def test_green_tree_record_main_writes_an_inherited_record(
    tool: ModuleType, tmp_path: Path
) -> None:
    env = _record_env(tmp_path, _skipped_needs())
    body = tmp_path / "ci-green-tree.json"
    git = _git(tool, {_SHA: _TREE, _BASELINE: _BASELINE_TREE})
    assert tool.main(["record", str(body)], env, git=git, fetch=_baseline_api()) == 0
    written = json.loads(body.read_text(encoding="utf-8"))
    assert written["heavy_jobs"] == "inherited"
    assert written["inherited_tree"] == _BASELINE_TREE
    assert written["inherited_run_id"] == "41"


@pytest.mark.parametrize(
    ("needs_text", "fetch", "message"),
    [
        pytest.param(
            json.dumps(_with_result(_needs("true"), "windows", "failure")),
            FakeApi(),
            "needed jobs did not pass: windows",
            id="failed-need",
        ),
        pytest.param("", FakeApi(), "the record job sees no needed jobs", id="no-needs"),
        pytest.param("{not json", FakeApi(), "the record job sees no needed jobs", id="bad-json"),
        pytest.param(
            json.dumps(_skipped_needs()),
            FakeApi(green=None),
            "has no recorded tree",
            id="unrecorded-baseline",
        ),
    ],
)
def test_green_tree_record_main_refusal_writes_recorded_false(
    tool: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    needs_text: str,
    fetch: FakeApi,
    message: str,
) -> None:
    env = _record_env(tmp_path, None)
    env["NEEDS"] = needs_text
    body = tmp_path / "ci-green-tree.json"
    git = _git(tool, {_SHA: _TREE, _BASELINE: _BASELINE_TREE})
    assert tool.main(["record", str(body)], env, git=git, fetch=fetch) == 0
    assert not body.exists()
    assert Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == "recorded=false\n"
    out = capsys.readouterr().out
    assert out.startswith("recorded=false: ")
    assert message in out


def test_green_tree_record_main_lookup_failure_writes_recorded_false(
    tool: ModuleType, tmp_path: Path
) -> None:
    def offline(url: str) -> object:
        raise tool.GreenTreeError("the API lookup failed: offline")

    env = _record_env(tmp_path, _skipped_needs())
    git = _git(tool, {_SHA: _TREE, _BASELINE: _BASELINE_TREE})
    assert tool.main(["record", str(tmp_path / "r.json")], env, git=git, fetch=offline) == 0
    assert Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == "recorded=false\n"


def test_green_tree_record_main_without_github_output_exits_two(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _record_env(tmp_path, _needs("true"))
    del env["GITHUB_OUTPUT"]
    code = tool.main(["record", str(tmp_path / "r.json")], env, git=_git(tool, {}), fetch=FakeApi())
    assert code == 2
    assert "GITHUB_OUTPUT is unset" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv", [[], ["record"], ["gate", "extra"], ["record", "a", "b"], ["publish"]]
)
def test_green_tree_main_rejects_a_malformed_invocation(
    tool: ModuleType, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    assert tool.main(argv, {}, git=_git(tool, {}), fetch=FakeApi()) == 2
    assert capsys.readouterr().err.startswith("usage: ci_green_tree.py")


def test_green_tree_main_reads_sys_argv_and_os_environ(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tool.sys, "argv", ["ci_green_tree.py"])
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert tool.main() == 2
    assert capsys.readouterr().err.startswith("usage:")


# --- the release gates --------------------------------------------------------------


def require_green_ci_violations(workflow: dict[str, Any]) -> list[str]:
    """Report every way a release workflow could publish a tag CI never proved.

    The gate must go through the shared tree-keyed checker: an inline query
    for the newest run on the tagged commit lets a cancelled run hide a
    green one, and a second copy of the check drifts from the first. The
    checker needs a checkout to read itself and the tagged tree from, a
    token that can list runs, and every publish job must wait for it.

    Args:
        workflow: The parsed release workflow.

    Returns:
        One human-readable problem per violation; empty when a tag-push-only
        job checks the tree out, runs the shared checker with the workflow
        token and nothing swallowing its exit code, queries no run inline,
        and sits upstream of every publish job.
    """
    jobs: dict[str, Any] = workflow.get("jobs", {})
    job = jobs.get(_GATE_JOB)
    if job is None:
        return ["the workflow declares no 'require-green-ci' job"]
    problems: list[str] = []
    if job.get("if") != _TAG_PUSH_CONDITION:
        problems.append(
            "the require-green-ci job does not run on exactly the publishing tag pushes"
        )
    problems.extend(_gate_step_violations(job))
    permissions = job.get("permissions") or {}
    if permissions.get("actions") != "read" or permissions.get("contents") != "read":
        problems.append("the require-green-ci job cannot read both the CI runs and the tag")
    publishers = [name for name in jobs if name.startswith("publish-")]
    if not publishers:
        problems.append("the workflow declares no publish job for the gate to hold")
    problems.extend(
        f"{name} does not wait for the require-green-ci job"
        for name in publishers
        if _GATE_JOB not in _upstream(jobs, name)
    )
    return problems


def _gate_step_violations(job: dict[str, Any]) -> list[str]:
    """Report the ways the gate job's steps could bypass the shared checker."""
    problems: list[str] = []
    steps: list[dict[str, Any]] = job.get("steps", [])
    scripts = [str(step.get("run", "")) for step in steps]
    if any(_NEWEST_RUN.search(script) for script in scripts):
        problems.append(
            "the require-green-ci job reads only the newest ci.yaml run, "
            "so a cancelled run hides a green one"
        )
    if any(_INLINE_RUNS_QUERY.search(script) for script in scripts):
        problems.append("the require-green-ci job queries ci.yaml runs outside the shared checker")
    gate = next((step for step in steps if str(step.get("run", "")).strip() == _GATE_COMMAND), None)
    if gate is None:
        problems.append("no require-green-ci step runs the shared tree-keyed checker")
    else:
        if gate.get("continue-on-error") or job.get("continue-on-error"):
            problems.append("the checker is continue-on-error, so a refusal still publishes")
        if (gate.get("env") or {}).get("GITHUB_TOKEN") != "${{ github.token }}":
            problems.append("the checker gets no token to list the CI runs with")
    if not any(str(step.get("uses", "")).startswith("actions/checkout") for step in steps):
        problems.append("the require-green-ci job checks out neither the checker nor the tag")
    return problems


@pytest.mark.parametrize("path", [_RELEASE, _PLUGIN_RELEASE], ids=["release", "plugin-release"])
def test_require_green_ci_is_tree_keyed(path: Path) -> None:
    """Both live release workflows gate every publish on the shared checker."""
    assert require_green_ci_violations(_load(path)) == []


def test_require_green_ci_runs_one_shared_checker() -> None:
    """The two gates are one check: the same command, env and permissions."""

    def shape(path: Path) -> tuple[object, ...]:
        job = _load(path)["jobs"][_GATE_JOB]
        gate = next(step for step in job["steps"] if step.get("run") == _GATE_COMMAND)
        return job["if"], job["permissions"], job["timeout-minutes"], gate["env"], gate["run"]

    assert shape(_RELEASE) == shape(_PLUGIN_RELEASE)
    assert _TOOL_PATH.is_file()


_NEWEST_RUN_JOB = r"""
jobs:
  require-green-ci:
    if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')
    permissions:
      actions: read
    steps:
      - name: Assert ci.yaml concluded success
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          read -r status conclusion < <(
            gh api "repos/${REPO}/actions/workflows/ci.yaml/runs?head_sha=${SHA}&per_page=1" \
              --jq '.workflow_runs[0] | "\(.status) \(.conclusion)"'
          )
          [ "${conclusion}" = "success" ]
  publish-pypi:
    needs: [build-wheel, require-green-ci]
"""


def test_require_green_ci_reds_on_newest_run_query() -> None:
    """The gate fires on the real defect: the newest-run query it replaced."""
    assert require_green_ci_violations(yaml.safe_load(_NEWEST_RUN_JOB)) == [
        "the require-green-ci job reads only the newest ci.yaml run, "
        "so a cancelled run hides a green one",
        "the require-green-ci job queries ci.yaml runs outside the shared checker",
        "no require-green-ci step runs the shared tree-keyed checker",
        "the require-green-ci job checks out neither the checker nor the tag",
        "the require-green-ci job cannot read both the CI runs and the tag",
    ]


def test_require_green_ci_reds_on_a_forked_or_swallowed_checker() -> None:
    """A private copy of the check, or a swallowed refusal, is no shared gate."""
    workflow = _load(_RELEASE)
    job = workflow["jobs"][_GATE_JOB]
    gate = next(step for step in job["steps"] if step.get("run") == _GATE_COMMAND)
    gate["continue-on-error"] = True
    gate["env"] = {}
    job["if"] = "github.event_name == 'push'"
    assert require_green_ci_violations(workflow) == [
        "the require-green-ci job does not run on exactly the publishing tag pushes",
        "the checker is continue-on-error, so a refusal still publishes",
        "the checker gets no token to list the CI runs with",
    ]
    gate["run"] = "python3 tools/release_ci_gate.py"
    assert "no require-green-ci step runs the shared tree-keyed checker" in (
        require_green_ci_violations(workflow)
    )


def test_require_green_ci_reds_on_an_ungated_publish() -> None:
    """A publish job that stops waiting for the gate, even indirectly, reds."""
    workflow = _load(_PLUGIN_RELEASE)
    for name in ("publish-claude-npm", "publish-codex-branch"):
        job = workflow["jobs"][name]
        job["needs"] = [need for need in _needs_of(job) if need != _GATE_JOB]
    assert require_green_ci_violations(workflow) == [
        "publish-claude-npm does not wait for the require-green-ci job",
        "publish-codex-branch does not wait for the require-green-ci job",
        "publish-source-host does not wait for the require-green-ci job",
    ]


def test_require_green_ci_reds_on_a_missing_job() -> None:
    assert require_green_ci_violations(yaml.safe_load("jobs: {}\n")) == [
        "the workflow declares no 'require-green-ci' job"
    ]


def test_require_green_ci_reds_on_a_workflow_without_publish_jobs() -> None:
    workflow = _load(_RELEASE)
    workflow["jobs"] = {_GATE_JOB: workflow["jobs"][_GATE_JOB]}
    assert require_green_ci_violations(workflow) == [
        "the workflow declares no publish job for the gate to hold"
    ]


# --- the checker's decisions --------------------------------------------------------


def test_require_green_ci_accepts_same_tree(tool: ModuleType) -> None:
    """A green push run on another commit counts once it recorded the tagged tree."""
    api = FakeApi(
        green=[_run(9, _OTHER_SHA, _OTHER_TREE), _run(5, _OTHER_SHA, _TREE)],
        records={9: [_artifact(_OTHER_TREE)], 5: [_artifact(_OTHER_TREE), _artifact(_TREE)]},
    )
    outcome, clock, _ = _await(tool, api)
    assert outcome == tool.GateOutcome(
        passed=True, reason=f"ci.yaml run 5 on {_OTHER_SHA[:12]} is green by recorded tree"
    )
    assert clock.sleeps == []
    assert api.urls[-1] == (
        f"{_API}/repos/{_REPO}/actions/runs/5/artifacts?name={_RECORD_PREFIX}{_TREE}"
    )


def test_require_green_ci_accepts_exact_sha_without_record(tool: ModuleType) -> None:
    """A green push run on the tagged commit counts as it always has."""
    api = FakeApi(green=[_run(3, _SHA, _TREE)])
    outcome, _, _ = _await(tool, api)
    assert outcome.passed is True
    assert outcome.reason == f"ci.yaml run 3 on {_SHA[:12]} is green by head sha"
    assert len(api.urls) == 1


def test_require_green_ci_accepts_green_run_behind_a_cancelled_one(tool: ModuleType) -> None:
    """The newest run on the commit being cancelled no longer hides a green one."""
    api = FakeApi(
        green=[_run(8, _SHA, _TREE, conclusion="cancelled"), _run(7, _SHA, _TREE)],
        commit=[_run(8, _SHA, _TREE, conclusion="cancelled"), _run(7, _SHA, _TREE)],
    )
    outcome, _, _ = _await(tool, api)
    assert outcome.reason == f"ci.yaml run 7 on {_SHA[:12]} is green by head sha"


def test_require_green_ci_queries_only_successful_push_runs(tool: ModuleType) -> None:
    assert tool.green_runs_url(api_url=f"{_API}/", repository=_REPO) == (
        f"{_API}/repos/{_REPO}/actions/workflows/ci.yaml/runs"
        "?event=push&status=success&per_page=100"
    )
    assert tool.commit_runs_url(api_url=_API, repository=_REPO, sha=_SHA) == (
        f"{_API}/repos/{_REPO}/actions/workflows/ci.yaml/runs"
        f"?event=push&head_sha={_SHA}&per_page=100"
    )


def test_require_green_ci_reds_on_pr_run(tool: ModuleType) -> None:
    """A green pull request run proves a merge tree, so it never counts.

    The push run on the tagged commit was cancelled, and the only green runs
    are pull request runs on the same commit and on the same tree, one of
    them even carrying a record.
    """
    api = FakeApi(
        green=[
            _run(6, _SHA, _TREE, event="pull_request"),
            _run(4, _OTHER_SHA, _TREE, event="pull_request"),
        ],
        commit=[_run(8, _SHA, _TREE, conclusion="cancelled")],
        records={4: [_artifact(_TREE)], 6: [_artifact(_TREE)]},
    )
    outcome, clock, _ = _await(tool, api)
    assert outcome.passed is False
    assert outcome.reason == (
        f"every push-event ci.yaml run on {_SHA[:12]} finished without success, "
        f"and no green push run recorded tree {_TREE[:12]}"
    )
    assert clock.sleeps == []
    assert all("event=push" in url for url in api.urls if "/workflows/" in url)


@pytest.mark.parametrize(
    "green",
    [
        pytest.param(_run(4, _OTHER_SHA, _TREE, repo="someone/fork"), id="foreign-repo"),
        pytest.param(_run(4, _OTHER_SHA, _TREE, status="in_progress"), id="unfinished"),
        pytest.param(_run(4, _OTHER_SHA, _TREE, conclusion="failure"), id="red"),
        pytest.param({**_run(4, _OTHER_SHA, _TREE), "id": True}, id="bool-id"),
        pytest.param({**_run(4, _OTHER_SHA, _TREE), "head_commit": None}, id="no-head-commit"),
        pytest.param({**_run(4, _OTHER_SHA, _TREE), "head_sha": None}, id="no-head-sha"),
    ],
)
def test_require_green_ci_reds_on_an_unqualified_same_tree_run(
    tool: ModuleType, green: dict[str, Any]
) -> None:
    api = FakeApi(green=[green], records={4: [_artifact(_TREE)]})
    assert (
        tool.find_green_run(fetch=api, api_url=_API, repository=_REPO, sha=_SHA, tree=_TREE) is None
    )


@pytest.mark.parametrize(
    "artifacts",
    [
        pytest.param([], id="no-record"),
        pytest.param([_artifact(_TREE, expired=True)], id="expired"),
        pytest.param([_artifact(_TREE, expired=None)], id="expiry-unknown"),
        pytest.param([_artifact(_OTHER_TREE)], id="other-tree"),
        pytest.param(["ci-green-tree"], id="malformed-entry"),
    ],
)
def test_require_green_ci_reds_on_same_tree_without_live_record(
    tool: ModuleType, artifacts: list[object]
) -> None:
    api = FakeApi(green=[_run(4, _OTHER_SHA, _TREE)], records={4: artifacts})
    assert (
        tool.find_green_run(fetch=api, api_url=_API, repository=_REPO, sha=_SHA, tree=_TREE) is None
    )


def test_require_green_ci_waits_for_an_in_flight_run(tool: ModuleType) -> None:
    api = FakeApi(commit=[_run(8, _SHA, _TREE, status="in_progress", conclusion=None)])

    def finish() -> None:
        if len(clock.sleeps) == 2:
            api.green.append(_run(8, _SHA, _TREE))

    clock = FakeClock(on_sleep=finish)
    outcome, _, lines = _await(tool, api, clock)
    assert outcome.passed is True
    assert clock.sleeps == [30, 30]
    assert lines == [f"no green push-event ci.yaml run on {_SHA[:12]} or tree {_TREE[:12]} yet"] * 2


def test_require_green_ci_waits_for_an_uncreated_run(tool: ModuleType) -> None:
    api = FakeApi(commit=[_run(8, _SHA, _TREE, event="pull_request")])
    clock = FakeClock(on_sleep=lambda: api.green.append(_run(9, _SHA, _TREE)))
    outcome, _, _ = _await(tool, api, clock)
    assert outcome.passed is True
    assert clock.sleeps == [30]


def test_require_green_ci_times_out(tool: ModuleType) -> None:
    api = FakeApi()
    outcome, clock, _ = _await(tool, api)
    assert outcome == tool.GateOutcome(
        passed=False,
        reason=f"timed out after 1800s waiting for a green push-event ci.yaml run on {_SHA[:12]}",
    )
    assert clock.sleeps == [30] * 60


def test_require_green_ci_retries_a_failed_lookup(tool: ModuleType) -> None:
    api = FakeApi(green=[_run(3, _SHA, _TREE)])
    calls: list[str] = []

    def flaky(url: str) -> object:
        calls.append(url)
        if len(calls) == 1:
            raise tool.GreenTreeError("the API lookup failed: HTTP Error 502")
        return api(url)

    outcome, clock, lines = _await(tool, flaky)
    assert outcome.passed is True
    assert clock.sleeps == [30]
    assert lines == ["the CI lookup failed, retrying: the API lookup failed: HTTP Error 502"]


@pytest.mark.parametrize("payload", [None, [], {"runs": []}, {"workflow_runs": "none"}])
def test_require_green_ci_malformed_runs_payload_raises(tool: ModuleType, payload: object) -> None:
    with pytest.raises(tool.GreenTreeError, match="no 'workflow_runs' list"):
        tool.green_push_runs(payload, repository=_REPO)
    with pytest.raises(tool.GreenTreeError, match="no 'workflow_runs' list"):
        tool.commit_settled(payload, repository=_REPO)


@pytest.mark.parametrize("payload", [None, [], {"artifacts": None}])
def test_require_green_ci_malformed_artifacts_payload_raises(
    tool: ModuleType, payload: object
) -> None:
    with pytest.raises(tool.GreenTreeError, match="no 'artifacts' list"):
        tool.has_record(payload, tree=_TREE)


def test_require_green_ci_green_push_runs_boundaries(tool: ModuleType) -> None:
    assert tool.green_push_runs({"workflow_runs": []}, repository=_REPO) == []
    kept = _run(1, _SHA, _TREE)
    payload = {"workflow_runs": ["junk", kept, _run(2, _SHA, _TREE, conclusion=None)]}
    assert tool.green_push_runs(payload, repository=_REPO) == [kept]


@pytest.mark.parametrize(
    ("runs", "settled"),
    [
        pytest.param([], False, id="none-yet"),
        pytest.param([_run(1, _SHA, _TREE, conclusion="failure")], True, id="one-finished"),
        pytest.param(
            [_run(1, _SHA, _TREE, conclusion="failure"), _run(2, _SHA, _TREE, status="queued")],
            False,
            id="one-in-flight",
        ),
        pytest.param([_run(1, _SHA, _TREE, repo="someone/fork")], False, id="foreign-only"),
        pytest.param([_run(1, _SHA, _TREE, event="pull_request")], False, id="pr-only"),
    ],
)
def test_require_green_ci_commit_settled(
    tool: ModuleType, runs: list[dict[str, Any]], settled: bool
) -> None:
    assert tool.commit_settled({"workflow_runs": runs}, repository=_REPO) is settled


def test_require_green_ci_tree_of_resolves_the_commit_tree(tool: ModuleType) -> None:
    assert tool.tree_of(_git(tool, {_SHA: _TREE}), _SHA) == _TREE
    with pytest.raises(tool.GreenTreeError, match="bad revision"):
        tool.tree_of(_git(tool, {}), _SHA)
    with pytest.raises(tool.GreenTreeError, match="not an object name"):
        tool.tree_of(_git(tool, {_SHA: "--output=x"}), _SHA)


# --- the gate entry point -----------------------------------------------------------


def _gate_env() -> dict[str, str]:
    return {"GITHUB_REPOSITORY": _REPO, "GITHUB_SHA": _SHA, "GITHUB_API_URL": _API}


def _gate(tool: ModuleType, env: dict[str, str], api: FakeApi, trees: dict[str, str]) -> int:
    clock = FakeClock()
    code: int = tool.main(
        ["gate"], env, git=_git(tool, trees), fetch=api, clock=clock, sleep=clock.sleep
    )
    return code


def test_require_green_ci_main_passes_on_a_green_tree(
    tool: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi(green=[_run(5, _OTHER_SHA, _TREE)], records={5: [_artifact(_TREE)]})
    assert _gate(tool, _gate_env(), api, {_SHA: _TREE}) == 0
    assert capsys.readouterr().out == (
        "CI green, release may proceed: "
        f"ci.yaml run 5 on {_OTHER_SHA[:12]} is green by recorded tree\n"
    )


def test_require_green_ci_main_refuses_a_settled_red_commit(
    tool: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi(commit=[_run(8, _SHA, _TREE, conclusion="failure")])
    assert _gate(tool, _gate_env(), api, {_SHA: _TREE}) == 1
    assert capsys.readouterr().err.startswith("refusing to publish: every push-event")


def test_require_green_ci_main_defaults_the_api_root(tool: ModuleType) -> None:
    env = _gate_env()
    del env["GITHUB_API_URL"]
    seen: list[str] = []

    def fetch(url: str) -> object:
        seen.append(url)
        return {"workflow_runs": [_run(3, _SHA, _TREE)]}

    clock = FakeClock()
    code = tool.main(
        ["gate"], env, git=_git(tool, {_SHA: _TREE}), fetch=fetch, clock=clock, sleep=clock.sleep
    )
    assert code == 0
    assert seen[0].startswith("https://api.github.com/repos/")


@pytest.mark.parametrize(
    ("name", "value", "trees", "message"),
    [
        pytest.param(
            "GITHUB_REPOSITORY", "", {_SHA: _TREE}, "GITHUB_REPOSITORY is unset", id="repo"
        ),
        pytest.param("GITHUB_SHA", "", {_SHA: _TREE}, "GITHUB_SHA is unset", id="no-sha"),
        pytest.param("GITHUB_SHA", "-x", {_SHA: _TREE}, "not a full object name", id="bad-sha"),
        pytest.param("GITHUB_SHA", _SHA, {}, "bad revision", id="no-tree"),
    ],
)
def test_require_green_ci_main_unusable_environment_exits_two(
    tool: ModuleType,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
    trees: dict[str, str],
    message: str,
) -> None:
    env = _gate_env()
    env[name] = value
    api = FakeApi()
    assert _gate(tool, env, api, trees) == 2
    assert message in capsys.readouterr().err
    assert api.urls == []


# --- default adapters ---------------------------------------------------------------


class _Completed:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_run_git_returns_stdout(tool: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> _Completed:
        seen.append(argv)
        assert kwargs["check"] is True
        return _Completed(f"{_TREE}\n")

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    assert tool.run_git(["rev-parse", "HEAD^{tree}"]) == f"{_TREE}\n"
    assert seen == [["git", "rev-parse", "HEAD^{tree}"]]


def test_run_git_nonzero_exit_raises_green_tree_error(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> _Completed:
        raise tool.subprocess.CalledProcessError(128, argv, stderr="fatal: bad object\n")

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    with pytest.raises(tool.GreenTreeError, match="git rev-parse failed: fatal: bad object"):
        tool.run_git(["rev-parse", _SHA])


def test_run_git_missing_binary_raises_green_tree_error(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> _Completed:
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    with pytest.raises(tool.GreenTreeError, match="git could not start"):
        tool.run_git(["rev-parse", _SHA])


def _capture_urlopen(tool: ModuleType, monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[Any]:
    requests: list[Any] = []

    def fake_urlopen(request: Any, timeout: float) -> io.BytesIO:
        requests.append(request)
        return io.BytesIO(body)

    monkeypatch.setattr(tool.urllib.request, "urlopen", fake_urlopen)
    return requests


def test_github_fetcher_decodes_json_with_a_bearer_token(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _capture_urlopen(tool, monkeypatch, b'{"artifacts": []}')
    assert tool.github_fetcher("token-value")(f"{_API}/runs") == {"artifacts": []}
    assert requests[0].full_url == f"{_API}/runs"
    assert requests[0].get_header("Authorization") == "Bearer token-value"


def test_github_fetcher_without_token_sends_no_authorization(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _capture_urlopen(tool, monkeypatch, b"{}")
    tool.github_fetcher("")(f"{_API}/runs")
    assert requests[0].get_header("Authorization") is None


def test_github_fetcher_bad_json_raises_green_tree_error(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture_urlopen(tool, monkeypatch, b"<html>")
    with pytest.raises(tool.GreenTreeError, match="API lookup failed"):
        tool.github_fetcher("token-value")(f"{_API}/runs")


def test_github_fetcher_transport_error_raises_green_tree_error(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def offline(request: Any, timeout: float) -> io.BytesIO:
        raise urllib.error.URLError("network is unreachable")

    monkeypatch.setattr(tool.urllib.request, "urlopen", offline)
    with pytest.raises(tool.GreenTreeError, match="network is unreachable"):
        tool.github_fetcher("token-value")(f"{_API}/runs")
