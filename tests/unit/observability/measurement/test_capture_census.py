"""Both halves of the capture path have production callers, found by census.

The existence of a tested function proves nothing about whether anything
calls it. The census parses every module under ``src/eawf``, maps each call
to the function that makes it, and walks callers upward from each half of
the capture producer until it reaches a registered daemon method. A half
with no such chain is the defect this test exists to catch.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import pytest

import eawf

_SOURCE = Path(eawf.__file__).parent
_DAEMON_METHODS = "runtime/daemon/methods/"


def _callers(root: Path) -> dict[str, set[tuple[str, str]]]:
    """Map each called name to the ``(module, enclosing function)`` pairs that call it."""
    found: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for path in root.rglob("*.py"):
        module = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for scope in ast.walk(tree):
            if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                # A function handed to a thread runner is called by it.
                handed = [arg.id for arg in node.args if isinstance(arg, ast.Name)]
                for called in (name, *handed):
                    if called is not None and called != scope.name:
                        found[called].add((module, scope.name))
    return found


def _reaches_daemon_method(name: str, callers: dict[str, set[tuple[str, str]]]) -> bool:
    """Return whether a caller chain climbs from *name* into a daemon method module."""
    seen: set[str] = set()
    frontier: list[str] = [name]
    while frontier:
        current = frontier.pop()
        for module, function in callers.get(current, ()):
            if module.startswith(_DAEMON_METHODS):
                return True
            if function not in seen:
                seen.add(function)
                frontier.append(function)
    return False


@pytest.fixture(scope="module")
def callers() -> dict[str, set[tuple[str, str]]]:
    return _callers(_SOURCE)


@pytest.mark.parametrize("half", ["capture_run_start", "capture_run_terminal"])
def test_meas_046_each_capture_half_has_a_production_caller(
    half: str, callers: dict[str, set[tuple[str, str]]]
) -> None:
    production = {
        (module, function)
        for module, function in callers[half]
        if module != "observability/measurement/capture.py"
    }

    assert production, f"{half} has no production caller outside its own module"
    assert _reaches_daemon_method(half, callers), f"no daemon method reaches {half}"


def test_meas_046_every_terminal_edge_writer_takes_the_stop_reading(
    callers: dict[str, set[tuple[str, str]]],
) -> None:
    modules = {module for module, _ in callers["terminal_capture_updates"]}

    assert {
        "runtime/daemon/methods/run.py",
        "runtime/daemon/methods/run_budget.py",
        "runtime/daemon/run_capture_updates.py",
    } <= modules


def _write(root: Path, files: Iterable[tuple[str, str]]) -> Path:
    for name, body in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def test_meas_046_census_flags_a_tested_function_nothing_calls(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        [
            ("observability/measurement/capture.py", "def capture_run_start():\n    return 1\n"),
            ("runtime/daemon/methods/run.py", "def handler():\n    return 2\n"),
        ],
    )

    census = _callers(root)

    assert "capture_run_start" not in census
    assert not _reaches_daemon_method("capture_run_start", census)


def test_meas_046_census_follows_a_function_handed_to_a_thread_runner(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        [
            ("a.py", "def bind():\n    capture_run_start()\n"),
            (
                "runtime/daemon/methods/domain.py",
                "async def verb():\n    await asyncio.to_thread(bind, 1)\n",
            ),
        ],
    )

    assert _reaches_daemon_method("capture_run_start", _callers(root))
