"""Branch-coverage floor for the lock package, measured by its own tests.

CI's per-package gate reads the lock package's branch rate off the
full-suite ``coverage.xml``, where several arcs used to be reached only when a
real ticker thread or a real contender lined up with the host clock, so the
rate sat one arc above the floor on a fast host and one below it on a slow one.
This test measures the same rate in a nested ``coverage run`` over the
deterministic lock tests only and holds it to the ``lock`` floor in
``[tool.eawf.coverage.gates]``. ``tests/unit/test_lock_portalock.py`` is left
out on purpose: its hold and heartbeat cases still drive real threads, so an
arc they happen to reach must not count toward a floor that has to hold on
every host.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCK_PACKAGE = "src/eawf/runtime/lock/"
_DETERMINISTIC_LOCK_TESTS = (
    "tests/unit/test_lock_portalock_branches.py",
    "tests/unit/test_lock_stale.py",
    "tests/unit/test_lock_sibling.py",
)

#: Environment prefixes an outer coverage run exports for its subprocesses.
#: Left in place they make the nested run start a second tracer (or write into
#: the outer data file) instead of measuring on its own.
_OUTER_COVERAGE_ENV_PREFIXES = ("COV_CORE_", "COVERAGE_")


def _lock_branch_floor() -> float:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as handle:
        gates = tomllib.load(handle)["tool"]["eawf"]["coverage"]["gates"]
    return float(gates["lock"]["branch"])


def _nested_coverage_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_OUTER_COVERAGE_ENV_PREFIXES)
    }


def _run_coverage(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "coverage", *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def test_lock_package_branch_rate_meets_floor_from_deterministic_tests(tmp_path: Path) -> None:
    data_file = tmp_path / ".coverage"
    report = tmp_path / "coverage.json"
    env = _nested_coverage_env()

    run = _run_coverage(
        [
            "run",
            "--branch",
            f"--data-file={data_file}",
            f"--source={_LOCK_PACKAGE}",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *_DETERMINISTIC_LOCK_TESTS,
        ],
        env=env,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    export = _run_coverage(["json", f"--data-file={data_file}", "-o", str(report)], env=env)
    assert export.returncode == 0, export.stdout + export.stderr

    files = json.loads(report.read_text(encoding="utf-8"))["files"]
    lock_files = {
        name: body
        for name, body in files.items()
        if Path(name).as_posix().startswith(_LOCK_PACKAGE)
    }
    assert lock_files, f"no {_LOCK_PACKAGE} file in the nested report: {sorted(files)}"
    covered = sum(int(body["summary"]["covered_branches"]) for body in lock_files.values())
    total = sum(int(body["summary"]["num_branches"]) for body in lock_files.values())
    assert total > 0, f"{_LOCK_PACKAGE} reported no branches"
    rate = covered / total * 100
    floor = _lock_branch_floor()
    assert rate >= floor, f"lock branch rate {rate:.2f}% ({covered}/{total}) < floor {floor}%"
