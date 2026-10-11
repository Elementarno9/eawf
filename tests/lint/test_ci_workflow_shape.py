"""The CI workflow runs each gate on the events its cost and tier allow.

The job conditions and the pull-request test selection are Actions
expressions, so a text match proves only that a string is present, not
which runs it selects. These tests evaluate the expressions the workflow
actually carries against each event, ref and runner, and pin the outcome:
twice-green on the release train, nightly and manual runs only; one lint
job instead of a lint pass inside every test leg; a pull request that runs
the wave and iter tiers only; a green-tree record that still lands on a
main push now that twice-green skips there; and an ubuntu leg split into
three pytest-split shards whose union is the unsharded suite, each leg
uploading its JUnit report, with one coverage job judging the combined
shard data.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from eawf.platform.lint.kind_taxonomy import KIND_GATE_TIERS, GateTier

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"
_TOOL_PATH = _REPO_ROOT / "tools" / "ci_green_tree.py"
_DURATION_TOOL_PATH = _REPO_ROOT / "tools" / "duration_budget_gate.py"
_BASELINE = _REPO_ROOT / "tests" / "duration-baseline.json"

_LIVE_CONSOLE_GLOB = "tests/tui/surfaces/tui/console/test_console_live_*.py"
_LINT_COMMANDS = ("pre-commit run --all-files", "deptry .", "mypy src/")
_EVENTS = ("push", "pull_request", "schedule", "workflow_dispatch")
_SHA = "a" * 40
_TREE = "c" * 40

#: A quoted Actions string literal; a doubled quote is an escaped one.
_LITERAL = re.compile(r"'(?:[^']|'')*'")
#: A context reference such as ``github.ref`` or ``needs.changes.result``.
_CONTEXT = re.compile(r"[A-Za-z_][\w-]*(?:\.[\w-]+)+")
_EMBEDDED = re.compile(r"\$\{\{(.*?)\}\}")
#: A ``{N}`` placeholder of the Actions ``format()`` function.
_PLACEHOLDER = re.compile(r"\{(\d+)\}")


def _format(template: object, *args: object) -> str:
    return _PLACEHOLDER.sub(lambda match: str(args[int(match.group(1))]), str(template))


def _starts_with(value: object, prefix: object) -> bool:
    return str(value).lower().startswith(str(prefix).lower())


def _evaluate(expression: str, context: Mapping[str, str]) -> Any:
    """Evaluate one Actions expression against *context*.

    Covers the subset ci.yaml uses: string literals, context references,
    ``==``/``!=``, ``&&``/``||`` (which return an operand, as Python's
    ``and``/``or`` do), ``!``, ``startsWith``, ``format`` and ``cancelled()``,
    read as a run nobody cancelled.

    Raises:
        KeyError: The expression names a context the caller did not supply.
    """
    pieces: list[str] = []
    cursor = 0
    for literal in _LITERAL.finditer(expression):
        pieces.append(_translate(expression[cursor : literal.start()]))
        pieces.append(repr(literal.group(0)[1:-1].replace("''", "'")))
        cursor = literal.end()
    pieces.append(_translate(expression[cursor:]))
    names = {
        "ctx": context,
        "starts_with": _starts_with,
        "format_": _format,
        "cancelled": lambda: False,
    }
    return eval("".join(pieces), {"__builtins__": {}}, names)


def _translate(code: str) -> str:
    code = code.replace("&&", " and ").replace("||", " or ")
    code = re.sub(r"!(?!=)", " not ", code).replace("startsWith(", "starts_with(")
    code = re.sub(r"\bformat\(", "format_(", code)
    return _CONTEXT.sub(lambda match: f"ctx[{match.group(0)!r}]", code)


def _render(text: str, context: Mapping[str, str]) -> str:
    """Substitute every ``${{ }}`` in *text* the way the runner does."""
    return _EMBEDDED.sub(lambda match: str(_evaluate(match.group(1), context)), text)


def _context(
    event: str,
    ref: str = "refs/heads/main",
    *,
    changes: str = "success",
    code: str = "true",
    os: str = "ubuntu-24.04",
    shard: str = "1",
) -> dict[str, str]:
    return {
        "github.event_name": event,
        "github.ref": ref,
        "needs.changes.result": changes,
        "needs.changes.outputs.code": code,
        "matrix.os": os,
        "matrix.shard": shard,
    }


def _jobs() -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    jobs: dict[str, Any] = workflow["jobs"]
    return jobs


def _step(job: str, name: str) -> dict[str, Any]:
    return next(step for step in _jobs()[job]["steps"] if step.get("name") == name)


def _runs(job: str) -> str:
    return "\n".join(str(step.get("run", "")) for step in _jobs()[job]["steps"])


# --- twice-green cadence -----------------------------------------------------


def _owes_twice_green(event: str, ref: str) -> bool:
    release = ref == "refs/heads/release" or ref.startswith("refs/heads/release/")
    return event in {"schedule", "workflow_dispatch"} or (event == "push" and release)


@pytest.mark.parametrize("event", _EVENTS)
@pytest.mark.parametrize(
    "ref",
    [
        "refs/heads/main",
        "refs/heads/release",
        "refs/heads/release/0.7",
        "refs/heads/releases",
        "refs/heads/feature/release",
        "",
    ],
)
def test_twice_green_runs_only_nightly_manual_or_on_a_release_push(event: str, ref: str) -> None:
    condition = _jobs()["twice-green"]["if"]
    assert bool(_evaluate(condition, _context(event, ref))) is _owes_twice_green(event, ref)


def test_twice_green_release_push_keeps_the_changes_gate() -> None:
    condition = _jobs()["twice-green"]["if"]
    proven = _context("push", "refs/heads/release", code="false")
    crashed = _context("push", "refs/heads/release", changes="failure", code="")
    assert not _evaluate(condition, proven)
    assert _evaluate(condition, crashed)


def test_twice_green_nightly_ignores_the_changes_verdict() -> None:
    condition = _jobs()["twice-green"]["if"]
    assert _evaluate(condition, _context("schedule", code="false"))


def test_evaluator_refuses_an_unknown_context() -> None:
    with pytest.raises(KeyError):
        _evaluate("github.actor == 'x'", _context("push"))


# --- the lint job ------------------------------------------------------------


@pytest.mark.parametrize("command", _LINT_COMMANDS)
def test_lint_job_runs_each_lint_and_the_test_job_none(command: str) -> None:
    assert f"uv run {command}" in _runs("lint")
    assert command not in _runs("test")


def test_lint_job_runs_on_every_event_beside_the_matrix() -> None:
    lint = _jobs()["lint"]
    assert "if" not in lint
    assert "needs" not in lint


def test_green_tree_needs_the_lint_job() -> None:
    assert "lint" in _jobs()["green-tree"]["needs"]


# --- pull-request tier selection ---------------------------------------------


def _pr_marker() -> str:
    release = [kind.value for kind, tier in KIND_GATE_TIERS.items() if tier is GateTier.RELEASE]
    return " and ".join(f"not {marker}" for marker in ["slow", *release])


def _parallel_argv(event: str, os: str = "ubuntu-24.04") -> list[str]:
    run = str(_step("test", "Pytest (parallel)")["run"])
    return shlex.split(_render(run, _context(event, os=os)))


def _option(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == flag] + [
        arg.split("=", 1)[1] for arg in argv if arg.startswith(f"{flag}=")
    ]


def test_pr_marker_is_the_documented_tier_selection() -> None:
    assert _pr_marker() == "not slow and not e2e and not perf"


@pytest.mark.parametrize("os", ["ubuntu-24.04", "macos-26"])
def test_pull_request_leg_deselects_release_tiers_slow_and_live_console(os: str) -> None:
    argv = _parallel_argv("pull_request", os)
    assert _option(argv, "-m") == [_pr_marker()]
    assert _option(argv, "--ignore-glob") == [_LIVE_CONSOLE_GLOB]
    assert "tests/eval" in _option(argv, "--ignore")
    assert sorted(_REPO_ROOT.glob(_LIVE_CONSOLE_GLOB))


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_push_and_nightly_legs_keep_the_full_set(event: str) -> None:
    argv = _parallel_argv(event)
    assert _option(argv, "-m") == []
    assert _option(argv, "--ignore-glob") == []
    assert "tests/eval" not in _option(argv, "--ignore")


@pytest.mark.parametrize("event", _EVENTS)
def test_serial_perf_step_skips_only_on_a_pull_request(event: str) -> None:
    condition = _step("test", "Pytest (serial perf timing)")["if"]
    assert bool(_evaluate(condition, _context(event))) is (event != "pull_request")


# --- the green-tree record without twice-green -------------------------------


@pytest.fixture()
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_green_tree", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_green_tree"] = module
    spec.loader.exec_module(module)
    return module


def _needs(*, test: str = "success", twice: str = "skipped") -> dict[str, Any]:
    return {
        "changes": {"result": "success", "outputs": {"code": "true", "green": ""}},
        "lint": {"result": "success", "outputs": {}},
        "test": {"result": test, "outputs": {}},
        "twice-green": {"result": twice, "outputs": {}},
    }


def _env(ref: str | None) -> dict[str, str]:
    env = {"GITHUB_EVENT_NAME": "push", "GITHUB_SHA": _SHA, "GITHUB_RUN_ID": "7"}
    if ref is not None:
        env["GITHUB_REF"] = ref
    return env


def _git(args: object) -> str:
    return f"{_TREE}\n"


def _no_fetch(url: str) -> object:
    raise AssertionError(f"a passed run fetches nothing, yet asked for {url}")


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/releases", None])
def test_green_tree_records_a_push_off_the_release_train_without_twice_green(
    tool: ModuleType, ref: str | None
) -> None:
    record = tool.build_record(_env(ref), needs=_needs(), git=_git, fetch=_no_fetch)
    assert (record.tree, record.heavy_jobs) == (_TREE, "passed")


@pytest.mark.parametrize("ref", ["refs/heads/release", "refs/heads/release/0.7"])
def test_green_tree_refuses_a_release_push_without_twice_green(tool: ModuleType, ref: str) -> None:
    with pytest.raises(tool.RecordRefusedError, match="twice-green=skipped"):
        tool.build_record(_env(ref), needs=_needs(), git=_git, fetch=_no_fetch)


def test_green_tree_records_a_release_push_with_twice_green(tool: ModuleType) -> None:
    needs = _needs(twice="success")
    assert tool.plan_record(needs, event="push", ref="refs/heads/release") == ""


def test_green_tree_still_refuses_a_main_push_whose_matrix_skipped(tool: ModuleType) -> None:
    with pytest.raises(tool.RecordRefusedError, match=r"did not all pass: test=skipped$"):
        tool.plan_record(_needs(test="skipped"), event="push", ref="refs/heads/main")


def test_green_tree_still_refuses_a_failed_twice_green_on_main(tool: ModuleType) -> None:
    with pytest.raises(tool.RecordRefusedError, match="needed jobs did not pass: twice-green"):
        tool.plan_record(_needs(twice="failure"), event="push", ref="refs/heads/main")


def test_green_tree_refuses_a_pull_request_run(tool: ModuleType) -> None:
    with pytest.raises(tool.RecordRefusedError, match="pull_request run records no tree"):
        tool.plan_record(_needs(), event="pull_request", ref="refs/heads/main")


# --- the sharded ubuntu leg --------------------------------------------------

_SHARDS = ("1", "2", "3")
_LEGS = [("ubuntu-24.04", shard) for shard in _SHARDS] + [("macos-26", "1")]
_SPLIT_FILE = "split-durations.json"
#: A small, real test tree the shard partition is proven on by collection.
_SPLIT_PROBE = "tests/unit/platform/lint"


def _matrix_legs() -> list[tuple[str, str]]:
    """Expand the test job's matrix the way Actions does: product minus excludes."""
    matrix = _jobs()["test"]["strategy"]["matrix"]
    excluded = [(entry["os"], entry["shard"]) for entry in matrix.get("exclude", [])]
    assert "include" not in matrix
    return [
        (os, str(shard))
        for os in matrix["os"]
        for shard in matrix["shard"]
        if (os, str(shard)) not in excluded
    ]


def _leg_argv(os: str, shard: str, event: str = "push") -> list[str]:
    run = str(_step("test", "Pytest (parallel)")["run"])
    return shlex.split(_render(run, _context(event, os=os, shard=shard)))


def _runs_on(step: str, os: str, shard: str, event: str = "push") -> bool:
    condition = _step("test", step).get("if", "true")
    return bool(_evaluate(str(condition), _context(event, os=os, shard=shard)))


def test_matrix_runs_three_ubuntu_shards_and_one_macos_leg() -> None:
    assert sorted(_matrix_legs()) == sorted(_LEGS)


@pytest.mark.parametrize("shard", _SHARDS)
def test_each_ubuntu_shard_selects_its_own_group_by_the_split_durations(shard: str) -> None:
    argv = _leg_argv("ubuntu-24.04", shard)
    assert _option(argv, "--splits") == ["3"]
    assert _option(argv, "--group") == [shard]
    assert _option(argv, "--durations-path") == [_SPLIT_FILE]
    assert _option(argv, "--cov") == ["eawf"]


def test_macos_leg_runs_the_whole_suite_without_coverage() -> None:
    argv = _leg_argv("macos-26", "1")
    for flag in ("--splits", "--group", "--durations-path", "--cov"):
        assert _option(argv, flag) == []


def test_split_durations_are_written_before_the_sharded_run() -> None:
    names = [step.get("name") for step in _jobs()["test"]["steps"]]
    assert names.index("Shard durations") < names.index("Pytest (parallel)")
    run = str(_step("test", "Shard durations")["run"])
    assert shlex.split(run)[-3:] == ["split", "tests/duration-baseline.json", _SPLIT_FILE]
    assert all(_runs_on("Shard durations", "ubuntu-24.04", shard) for shard in _SHARDS)


@pytest.mark.parametrize(("os", "shard"), _LEGS)
def test_every_leg_uploads_its_junit_report_under_its_own_name(os: str, shard: str) -> None:
    step = _step("test", "Upload JUnit report")
    assert str(step["uses"]).startswith("actions/upload-artifact@")
    assert step["with"]["path"] == "pytest-junit.xml"
    assert _option(_leg_argv(os, shard), "--junitxml") == ["pytest-junit.xml"]
    condition = _EMBEDDED.fullmatch(str(step["if"]))
    assert condition is not None
    assert _evaluate(condition.group(1), _context("push", os=os, shard=shard))


def test_junit_artifact_names_are_unique_per_leg() -> None:
    name = str(_step("test", "Upload JUnit report")["with"]["name"])
    rendered = {_render(name, _context("push", os=os, shard=shard)) for os, shard in _LEGS}
    assert len(rendered) == len(_LEGS)


@pytest.mark.parametrize(
    "step", ["Quarantine lane (reports, never blocks)", "Pytest (serial perf timing)"]
)
def test_once_per_run_steps_run_on_one_ubuntu_shard(step: str) -> None:
    ran = [shard for shard in _SHARDS if _runs_on(step, "ubuntu-24.04", shard)]
    assert ran == ["1"]


def test_telemetry_sync_runs_once_per_leg() -> None:
    ran = [leg for leg in _LEGS if _runs_on("Telemetry sync (REL-009)", *leg)]
    assert ran == [("ubuntu-24.04", "1"), ("macos-26", "1")]


def test_every_ubuntu_shard_uploads_its_raw_coverage_data() -> None:
    step = _step("test", "Upload coverage data")
    names = {_render(str(step["with"]["name"]), _context("push", shard=shard)) for shard in _SHARDS}
    assert len(names) == len(_SHARDS)
    assert step["with"]["path"] == ".coverage"
    assert step["with"]["include-hidden-files"] is True
    assert all(_runs_on("Upload coverage data", "ubuntu-24.04", shard) for shard in _SHARDS)
    assert not _runs_on("Upload coverage data", "macos-26", "1")


def test_no_shard_judges_a_coverage_floor() -> None:
    assert "coverage_gate.py" not in _runs("test")
    assert "coverage report" not in _runs("test")
    assert "--cov-report=xml" not in _runs("test")


def test_coverage_job_combines_the_shards_then_runs_the_gate_once() -> None:
    job = _jobs()["coverage"]
    assert job["needs"] == "test"
    assert "if" not in job
    download = next(step for step in job["steps"] if "download-artifact" in str(step.get("uses")))
    assert download["with"]["pattern"] == "coverage-data-shard-*"
    commands = [
        line.strip()
        for step in job["steps"]
        for line in str(step.get("run", "")).splitlines()
        if line.strip()
    ]
    combine = commands.index("uv run coverage combine coverage-data/*/.coverage")
    assert commands[combine + 1 :] == [
        "uv run coverage xml",
        (
            "uv run python tools/coverage_gate.py"
            " ${{ github.event_name == 'pull_request' && '--exclude eval' || '' }}"
        ),
        "uv run coverage report",
    ]


def test_green_tree_needs_the_coverage_job() -> None:
    assert "coverage" in _jobs()["green-tree"]["needs"]


# --- the shard partition, proven by collection ------------------------------


@pytest.fixture()
def duration_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("duration_budget_gate", _DURATION_TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["duration_budget_gate"] = module
    spec.loader.exec_module(module)
    return module


def _collect(*args: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    return [line for line in result.stdout.splitlines() if "::" in line]


def test_the_shards_partition_the_unsharded_collection(
    duration_tool: ModuleType, tmp_path: Path
) -> None:
    split_file = tmp_path / _SPLIT_FILE
    split = duration_tool.node_ids(
        json.loads(_BASELINE.read_text(encoding="utf-8"))["durations"], _REPO_ROOT
    )
    split_file.write_text(json.dumps(split), encoding="utf-8")
    algorithm = _option(_leg_argv("ubuntu-24.04", "1"), "--splitting-algorithm")
    whole = _collect(_SPLIT_PROBE)
    shards = [
        _collect(
            _SPLIT_PROBE,
            "--splits=3",
            f"--group={shard}",
            f"--durations-path={split_file}",
            *[f"--splitting-algorithm={name}" for name in algorithm],
        )
        for shard in _SHARDS
    ]
    assert all(shards)
    assert sorted(test for shard in shards for test in shard) == sorted(whole)
    timed = [test for test in whole if test in split]
    assert len(timed) >= len(whole) // 2, "the baseline keys no longer name collected node ids"


def test_split_rekeys_a_module_test_and_a_class_test(
    duration_tool: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_a.py").write_text("", encoding="utf-8")
    durations = {
        "tests.unit.test_a::test_x[1-2]": 0.5,
        "tests.unit.test_a.TestB.TestC::test_y": 1.0,
    }
    assert duration_tool.node_ids(durations, tmp_path) == {
        "tests/unit/test_a.py::test_x[1-2]": 0.5,
        "tests/unit/test_a.py::TestB::TestC::test_y": 1.0,
    }


def test_split_drops_a_test_whose_module_is_gone(duration_tool: ModuleType, tmp_path: Path) -> None:
    assert duration_tool.node_ids({"tests.unit.test_gone::test_x": 1.0}, tmp_path) == {}


def test_split_of_an_empty_baseline_is_empty(duration_tool: ModuleType, tmp_path: Path) -> None:
    assert duration_tool.node_ids({}, tmp_path) == {}


def test_split_refuses_a_key_that_is_not_a_junit_key(
    duration_tool: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="not a JUnit test key"):
        duration_tool.node_ids({"tests.unit.test_a": 1.0}, tmp_path)


def test_record_drops_only_keys_that_name_a_home_path(duration_tool: ModuleType) -> None:
    home = "/".join(["", "Users", "alice"])
    durations = {f"tests.unit.test_a::test_scrub[{home}]": 0.1, "tests.unit.test_a::test_x": 0.2}
    assert duration_tool.committable(durations) == {"tests.unit.test_a::test_x": 0.2}
    assert duration_tool.committable({}) == {}


def test_committed_baseline_names_no_home_path(duration_tool: ModuleType) -> None:
    durations = json.loads(_BASELINE.read_text(encoding="utf-8"))["durations"]
    assert duration_tool.committable(durations) == durations


def test_split_entry_point_writes_the_pytest_split_file(
    duration_tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "test_a.py").write_text("", encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"revision": "abc", "durations": {"test_a::test_x": 0.25}}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert duration_tool.main(["split", str(baseline), str(tmp_path / "out.json")]) == 0
    assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8")) == {
        "test_a.py::test_x": pytest.approx(0.25)
    }
