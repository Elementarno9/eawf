"""The CI workflow runs each gate on the events its cost and tier allow.

The job conditions and the test selection are Actions expressions, so a
text match proves only that a string is present, not which runs it
selects. These tests evaluate the expressions the workflow actually
carries against each event, ref and runner, and pin the outcome:
twice-green on the release train, nightly and manual runs only; one
combined gates job instead of a lint pass inside every test leg; four
ubuntu pytest-split shards selecting the same tests on a pull request as
on a push, beside one serial lane for the end-to-end and perf tiers;
three whole-suite macOS shards off pull requests and one affected-tests
macOS job on them; a coverage gate judging the combined ubuntu data off
pull requests; a green-tree record that lands on a main or dev push
without twice-green; and a resvg build restored from a cache keyed on its
pinned version.
"""

from __future__ import annotations

import fnmatch
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

from tests.lint.test_no_wall_clock_budget_asserts import PERF_LANE

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"
_TOOL_PATH = _REPO_ROOT / "tools" / "ci_green_tree.py"
_DURATION_TOOL_PATH = _REPO_ROOT / "tools" / "duration_budget_gate.py"
_BASELINE = _REPO_ROOT / "tests" / "duration-baseline.json"

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
    ``true``/``false``, ``==``/``!=``, ``&&``/``||`` (which return an operand, as Python's
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
        "true": True,
        "false": False,
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


# --- the combined gates job --------------------------------------------------


@pytest.mark.parametrize("command", _LINT_COMMANDS)
def test_gates_job_runs_each_lint_and_the_test_jobs_none(command: str) -> None:
    assert f"uv run {command}" in _runs("gates")
    assert command not in _runs("test") + _runs("test-macos") + _runs("e2e")


def test_gates_job_runs_on_every_event_beside_the_matrix() -> None:
    gates = _jobs()["gates"]
    assert "if" not in gates
    assert "needs" not in gates


@pytest.mark.parametrize("event", _EVENTS)
def test_gates_job_skips_no_gate_after_a_failed_one(event: str) -> None:
    """Every gate step after the sync runs on, so one red run lists every finding."""
    steps = _jobs()["gates"]["steps"]
    sync = next(index for index, step in enumerate(steps) if step.get("id") == "sync")
    for step in steps[sync + 1 :]:
        assert "!cancelled()" in str(step["if"]), step["name"]
    failed = {
        **_context(event),
        "steps.sync.outcome": "failure",
        "steps.wheel.outcome": "failure",
    }
    assert not [step["name"] for step in steps[sync + 1 :] if _evaluate(_bare(step), failed)]


def test_green_tree_needs_the_gates_job() -> None:
    assert "gates" in _jobs()["green-tree"]["needs"]


# --- one test shape on every event -------------------------------------------


_UBUNTU = "ubuntu-24.04"
_MACOS = "macos-26"
_OS = (_UBUNTU, _MACOS)
_SHARDS: dict[str, tuple[str, ...]] = {_UBUNTU: ("1", "2", "3", "4"), _MACOS: ("1", "2", "3")}
_LEGS = [(os, shard) for os in _OS for shard in _SHARDS[os]]
_TEST_JOB = {_UBUNTU: "test", _MACOS: "test-macos"}
_SERIAL_PREFIX = "Pytest (serial "
_EXCLUDE_FLAGS = ("--exclude", "--omit", "--ignore", "--deselect", "-m")
_COV_FLAGS = ("--cov=eawf", "--cov-report=", "--cov-fail-under=0")


def _bare(step: Mapping[str, Any]) -> str:
    """Return a step's condition without its ``${{ }}`` wrapper; no condition is ``true``."""
    condition = str(step.get("if", "true"))
    embedded = _EMBEDDED.fullmatch(condition)
    return embedded.group(1) if embedded else condition


def _parallel_argv(event: str, os: str = _UBUNTU, shard: str = "1") -> list[str]:
    run = str(_step(_TEST_JOB[os], "Pytest (parallel)")["run"])
    return shlex.split(_render(run, _context(event, os=os, shard=shard)))


def _option(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == flag] + [
        arg.split("=", 1)[1] for arg in argv if arg.startswith(f"{flag}=")
    ]


def _serial_steps_run(job: str, event: str, shard: str = "1") -> list[str]:
    context = _context(event, shard=shard)
    return [
        str(step["name"])
        for step in _jobs()[job]["steps"]
        if str(step.get("name", "")).startswith(_SERIAL_PREFIX) and _evaluate(_bare(step), context)
    ]


def _job_runs(job: str, event: str) -> bool:
    return bool(_evaluate(str(_jobs()[job].get("if", "true")), _context(event)))


def _coverage_gate_argv(event: str) -> list[str]:
    step = next(
        step
        for step in _jobs()["coverage"]["steps"]
        if "tools/coverage_gate.py" in str(step.get("run", ""))
    )
    return shlex.split(_render(str(step["run"]), _context(event)))


@pytest.mark.parametrize("shard", _SHARDS[_UBUNTU])
def test_pull_request_and_push_select_the_same_parallel_tests(shard: str) -> None:
    pull_request = _parallel_argv("pull_request", shard=shard)
    push = _parallel_argv("push", shard=shard)
    assert pull_request == [arg for arg in push if arg not in _COV_FLAGS]
    assert _option(pull_request, "-m") == []
    assert _option(pull_request, "--ignore-glob") == []
    assert "tests/eval" not in _option(pull_request, "--ignore")


@pytest.mark.parametrize(("os", "shard"), _LEGS)
def test_macos_shards_select_the_ubuntu_tests(os: str, shard: str) -> None:
    ubuntu = [arg for arg in _parallel_argv("push") if arg not in _COV_FLAGS]
    argv = _parallel_argv("push", os, shard)
    assert _option(argv, "--ignore") == _option(ubuntu, "--ignore")


@pytest.mark.parametrize("event", _EVENTS)
def test_every_event_keeps_the_default_marker_selection(event: str) -> None:
    assert _option(_parallel_argv(event), "-m") == []


@pytest.mark.parametrize("event", _EVENTS)
def test_serial_lane_runs_the_same_serial_steps_on_every_event(event: str) -> None:
    assert _job_runs("e2e", event)
    assert _serial_steps_run("e2e", event) == [
        "Pytest (serial e2e timing)",
        "Pytest (serial perf structure)",
    ]


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_macos_serial_steps_run_on_shard_one_off_pull_requests(event: str) -> None:
    assert _job_runs("test-macos", event)
    assert _serial_steps_run("test-macos", event, "1") == [
        "Pytest (serial e2e timing)",
        "Pytest (serial perf structure)",
    ]
    assert _serial_steps_run("test-macos", event, "2") == []


def test_pull_request_runs_the_affected_macos_tests_instead_of_the_shards() -> None:
    assert not _job_runs("test-macos", "pull_request")
    assert _job_runs("macos-affected", "pull_request")
    assert not [
        event for event in _EVENTS if event != "pull_request" and _job_runs("macos-affected", event)
    ]


def test_affected_macos_run_tolerates_an_empty_collection_only() -> None:
    """Exit 5 means the ignores left nothing to collect; every other status stands."""
    run = str(_step("macos-affected", "Pytest (affected, parallel)")["run"])
    assert '"${status}" -eq 5' in run
    assert run.rstrip().endswith('exit "${status}"')
    step = _step("macos-affected", "Pytest (affected, parallel)")
    assert step["env"]["EAWF_SKIP_PERF"] == "1"
    assert not step.get("continue-on-error", False)


@pytest.mark.parametrize("event", _EVENTS)
def test_coverage_gate_passes_no_exclude_flag(event: str) -> None:
    argv = _coverage_gate_argv(event)
    assert argv == ["uv", "run", "python", "tools/coverage_gate.py"]
    assert not [arg for arg in argv if arg.split("=", 1)[0] in _EXCLUDE_FLAGS]


def test_coverage_gate_runs_off_pull_requests_only() -> None:
    assert [event for event in _EVENTS if _job_runs("coverage", event)] == [
        "push",
        "schedule",
        "workflow_dispatch",
    ]


@pytest.mark.parametrize("event", _EVENTS)
def test_end_to_end_suite_runs_serially_in_its_own_job(event: str) -> None:
    for os in _OS:
        for shard in _SHARDS[os]:
            assert _option(_parallel_argv(event, os, shard), "--ignore").count("tests/e2e") == 1
    step = _step("e2e", "Pytest (serial e2e timing)")
    argv = shlex.split(_render(str(step["run"]), _context(event)))
    expected = ["uv", "run", "pytest", "-n0", "tests/e2e"]
    if event != "pull_request":
        expected.extend(_COV_FLAGS)
    assert argv == expected
    assert not step.get("continue-on-error", False)
    assert not _jobs()["e2e"].get("continue-on-error", False)
    steps = _jobs()["e2e"]["steps"]
    assert steps.index(step) < steps.index(_step("e2e", "Upload coverage data"))


# --- the perf lane: structure blocks, budgets report nightly -----------------

_PERF_STRUCTURE = "Pytest (serial perf structure)"
_PERF_LANE_STEP = "Perf lane (nightly, reports, never blocks)"
_SERIAL_JOB = {_UBUNTU: "e2e", _MACOS: "test-macos"}


@pytest.mark.parametrize("event", _EVENTS)
@pytest.mark.parametrize(("os", "shard"), _LEGS)
def test_parallel_legs_ignore_every_perf_lane_path_and_deselect_nothing(
    event: str, os: str, shard: str
) -> None:
    argv = _parallel_argv(event, os, shard)
    assert _option(argv, "--deselect") == []
    assert [path for path in PERF_LANE if path not in _option(argv, "--ignore")] == []


def test_affected_macos_run_ignores_every_perf_lane_path() -> None:
    run = " ".join(str(_step("macos-affected", "Pytest (affected, parallel)")["run"]).split())
    command = next(line for line in run.split(" uv ")[1:] if line.startswith("run pytest"))
    assert [path for path in PERF_LANE if f"--ignore={path}" not in command] == []


@pytest.mark.parametrize("event", _EVENTS)
@pytest.mark.parametrize("os", _OS)
def test_blocking_perf_step_runs_the_lane_with_its_budgets_skipped(event: str, os: str) -> None:
    step = _step(_SERIAL_JOB[os], _PERF_STRUCTURE)
    argv = shlex.split(_render(str(step["run"]), _context(event, os=os)))
    expected = ["uv", "run", "pytest", "-n0", *PERF_LANE]
    if os == _UBUNTU and event != "pull_request":
        expected.extend(["--cov=eawf", "--cov-append", "--cov-report=", "--cov-fail-under=0"])
    assert argv == expected
    assert step["env"] == {"EAWF_SKIP_PERF": "1"}
    assert not step.get("continue-on-error", False)


#: The serial lane is one unsharded job; the macOS leg keys its serial steps on shard 1.
_SERIAL_LEGS = [(_UBUNTU, "1"), *[(_MACOS, shard) for shard in _SHARDS[_MACOS]]]


@pytest.mark.parametrize(("os", "shard"), _SERIAL_LEGS)
def test_perf_lane_with_budgets_runs_nightly_once_per_os_and_never_blocks(
    os: str, shard: str
) -> None:
    step = _step(_SERIAL_JOB[os], _PERF_LANE_STEP)
    ran = {
        event for event in _EVENTS if _evaluate(_bare(step), _context(event, os=os, shard=shard))
    }
    assert ran == ({"schedule", "workflow_dispatch"} if shard == "1" else set())
    assert step["continue-on-error"] is True
    assert "env" not in step
    argv = shlex.split(_render(str(step["run"]), _context("schedule", os=os)))
    assert argv == ["uv", "run", "pytest", "-n0", *PERF_LANE]


def test_perf_lane_steps_finish_before_coverage_is_uploaded() -> None:
    names = [step.get("name") for step in _jobs()["e2e"]["steps"]]
    upload = names.index("Upload coverage data")
    assert names.index(_PERF_STRUCTURE) < upload
    assert names.index(_PERF_LANE_STEP) < upload


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
        "gates": {"result": "success", "outputs": {}},
        "test": {"result": test, "outputs": {}},
        "e2e": {"result": test, "outputs": {}},
        "test-macos": {"result": test, "outputs": {}},
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


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/dev", "refs/heads/releases", None])
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
    with pytest.raises(
        tool.RecordRefusedError,
        match=r"did not all pass: test=skipped, e2e=skipped, test-macos=skipped$",
    ):
        tool.plan_record(_needs(test="skipped"), event="push", ref="refs/heads/main")


def test_green_tree_still_refuses_a_failed_twice_green_on_main(tool: ModuleType) -> None:
    with pytest.raises(tool.RecordRefusedError, match="needed jobs did not pass: twice-green"):
        tool.plan_record(_needs(twice="failure"), event="push", ref="refs/heads/main")


def test_green_tree_refuses_a_pull_request_run(tool: ModuleType) -> None:
    with pytest.raises(tool.RecordRefusedError, match="pull_request run records no tree"):
        tool.plan_record(_needs(), event="pull_request", ref="refs/heads/main")


# --- the sharded legs ----------------------------------------------------------

_SPLIT_FILE = "split-durations.json"
#: A small, real test tree the shard partition is proven on by collection.
_SPLIT_PROBE = "tests/unit/platform/lint"


def _matrix_legs() -> list[tuple[str, str]]:
    """Expand both test jobs' matrices the way Actions does: product minus excludes."""
    legs: list[tuple[str, str]] = []
    for job in _TEST_JOB.values():
        matrix = _jobs()[job]["strategy"]["matrix"]
        excluded = [(entry["os"], entry["shard"]) for entry in matrix.get("exclude", [])]
        assert "include" not in matrix
        legs.extend(
            (os, str(shard))
            for os in matrix["os"]
            for shard in matrix["shard"]
            if (os, str(shard)) not in excluded
        )
    return legs


def _runs_on(step: str, os: str, shard: str, event: str = "push") -> bool:
    return bool(_evaluate(_bare(_step(_TEST_JOB[os], step)), _context(event, os=os, shard=shard)))


def test_matrix_runs_four_ubuntu_and_three_macos_shards() -> None:
    assert sorted(_matrix_legs()) == sorted(_LEGS)


@pytest.mark.parametrize(("os", "shard"), _LEGS)
def test_each_shard_selects_its_own_group_by_the_split_durations(os: str, shard: str) -> None:
    argv = _parallel_argv("push", os, shard)
    assert _option(argv, "--splits") == [str(len(_SHARDS[os]))]
    assert _option(argv, "--group") == [shard]
    assert _option(argv, "--durations-path") == [_SPLIT_FILE]
    assert _option(argv, "--splitting-algorithm") == ["least_duration"]


@pytest.mark.parametrize("event", _EVENTS)
@pytest.mark.parametrize("shard", _SHARDS[_UBUNTU])
def test_only_the_ubuntu_shards_off_pull_requests_collect_coverage(event: str, shard: str) -> None:
    expected = [] if event == "pull_request" else ["eawf"]
    assert _option(_parallel_argv(event, _UBUNTU, shard), "--cov") == expected
    assert _option(_parallel_argv(event, _MACOS, "1"), "--cov") == []


@pytest.mark.parametrize("os", _OS)
def test_split_durations_are_written_before_the_sharded_run(os: str) -> None:
    job = _TEST_JOB[os]
    names = [step.get("name") for step in _jobs()[job]["steps"]]
    assert names.index("Shard durations") < names.index("Pytest (parallel)")
    run = str(_step(job, "Shard durations")["run"])
    assert shlex.split(run)[-3:] == ["split", "tests/duration-baseline.json", _SPLIT_FILE]
    assert all(_runs_on("Shard durations", os, shard) for shard in _SHARDS[os])


@pytest.mark.parametrize(("os", "shard"), _LEGS)
def test_every_leg_uploads_its_junit_report_under_its_own_name(os: str, shard: str) -> None:
    step = _step(_TEST_JOB[os], "Upload JUnit report")
    assert str(step["uses"]).startswith("actions/upload-artifact@")
    assert step["with"]["path"] == "pytest-junit.xml"
    assert _option(_parallel_argv("push", os, shard), "--junitxml") == ["pytest-junit.xml"]
    assert _runs_on("Upload JUnit report", os, shard)


def test_junit_artifact_names_are_unique_per_leg() -> None:
    rendered = {
        _render(
            str(_step(_TEST_JOB[os], "Upload JUnit report")["with"]["name"]),
            _context("push", os=os, shard=shard),
        )
        for os, shard in _LEGS
    }
    assert len(rendered) == len(_LEGS)


def test_telemetry_sync_runs_once_per_os() -> None:
    assert "Telemetry sync (REL-009)" not in [step.get("name") for step in _jobs()["test"]["steps"]]
    assert _step("e2e", "Telemetry sync (REL-009)").get("if") is None
    ran = [
        shard for shard in _SHARDS[_MACOS] if _runs_on("Telemetry sync (REL-009)", _MACOS, shard)
    ]
    assert ran == ["1"]


def test_coverage_job_collects_the_ubuntu_shards_and_the_serial_lane() -> None:
    pattern = str(_step("coverage", "Download shard coverage data")["with"]["pattern"])
    shard_step = _step("test", "Upload coverage data")
    serial_step = _step("e2e", "Upload coverage data")
    uploads = [
        _render(str(shard_step["with"]["name"]), _context("push", shard=shard))
        for shard in _SHARDS[_UBUNTU]
    ] + [str(serial_step["with"]["name"])]
    assert len(set(uploads)) == len(_SHARDS[_UBUNTU]) + 1
    assert all(fnmatch.fnmatchcase(name, pattern) for name in uploads)
    for step in (shard_step, serial_step):
        assert step["with"]["path"] == ".coverage"
        assert step["with"]["include-hidden-files"] is True
        ran = [event for event in _EVENTS if _evaluate(_bare(step), _context(event))]
        assert ran == ["push", "schedule", "workflow_dispatch"]
    macos_steps = [step.get("name") for step in _jobs()["test-macos"]["steps"]]
    assert "Upload coverage data" not in macos_steps


# --- the cached resvg build ---------------------------------------------------

_RESVG_RESTORE = "Restore resvg (cached by pinned version)"
_RESVG_INSTALL = "Install resvg (pinned, T5 SVG pixel-diff oracle)"


def _resvg_context(shard: str, cache_hit: str) -> dict[str, str]:
    return {
        **_context("push", shard=shard),
        "env.RESVG_VERSION": str(_jobs()["test"]["env"]["RESVG_VERSION"]),
        "runner.os": "Linux",
        "runner.arch": "X64",
        "steps.resvg-cache.outputs.cache-hit": cache_hit,
    }


def test_resvg_install_builds_the_pinned_version() -> None:
    version = str(_jobs()["test"]["env"]["RESVG_VERSION"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    argv = shlex.split(str(_step("test", _RESVG_INSTALL)["run"]))
    assert argv[:2] == ["cargo", "install"]
    assert _option(argv, "--version") == ["$RESVG_VERSION"]
    assert "--locked" in argv


def test_resvg_cache_is_keyed_on_the_pinned_version_and_runner() -> None:
    names = [step.get("name") for step in _jobs()["test"]["steps"]]
    assert names.index(_RESVG_RESTORE) + 1 == names.index(_RESVG_INSTALL)
    restore = _step("test", _RESVG_RESTORE)
    assert restore["id"] == "resvg-cache"
    assert str(restore["uses"]).startswith("actions/cache@")
    assert restore["with"]["path"] == "~/.cargo/bin/resvg"
    context = _resvg_context("1", "")
    key = _render(str(restore["with"]["key"]), context)
    assert key == f"resvg-{context['env.RESVG_VERSION']}-Linux-X64"


@pytest.mark.parametrize("shard", _SHARDS[_UBUNTU])
@pytest.mark.parametrize("cache_hit", ["true", "false", ""])
def test_resvg_build_runs_only_on_a_cache_miss(shard: str, cache_hit: str) -> None:
    context = _resvg_context(shard, cache_hit)
    assert _evaluate(_bare(_step("test", _RESVG_RESTORE)), context)
    assert bool(_evaluate(_bare(_step("test", _RESVG_INSTALL)), context)) is (cache_hit != "true")


def test_no_shard_judges_a_coverage_floor() -> None:
    for job in ("test", "test-macos", "e2e"):
        assert "coverage_gate.py" not in _runs(job)
        assert "coverage report" not in _runs(job)
        assert "--cov-report=xml" not in _runs(job)


def test_coverage_job_combines_the_shards_then_runs_the_gate_once() -> None:
    job = _jobs()["coverage"]
    assert job["needs"] == ["test", "e2e"]
    assert job["if"] == "github.event_name != 'pull_request'"
    download = next(step for step in job["steps"] if "download-artifact" in str(step.get("uses")))
    assert download["with"]["pattern"] == "coverage-data-*"
    commands = [
        line.strip()
        for step in job["steps"]
        for line in str(step.get("run", "")).splitlines()
        if line.strip()
    ]
    combine = commands.index("uv run coverage combine coverage-data/*/.coverage")
    assert commands[combine + 1 :] == [
        "uv run coverage xml",
        "uv run python tools/coverage_gate.py",
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
    argv = _parallel_argv("push")
    algorithm = _option(argv, "--splitting-algorithm")
    whole = _collect(_SPLIT_PROBE)
    shards = [
        _collect(
            _SPLIT_PROBE,
            *[f"--splits={count}" for count in _option(argv, "--splits")],
            f"--group={shard}",
            f"--durations-path={split_file}",
            *[f"--splitting-algorithm={name}" for name in algorithm],
        )
        for shard in _SHARDS[_UBUNTU]
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
