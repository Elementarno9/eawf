"""Import-time budgets for ``eawf --version`` and the ``eawf hook run`` command path.

Each hook call and each version probe is a fresh process, so its import cost
is paid every time. The measure is the summed self time ``python -X
importtime`` reports, the minimum of seven interpreters started after one
discarded warm-up: summed self time counts each module once and is far
steadier than wall time on a busy runner, and since load on the machine only
ever adds time, the fastest warm sample is the closest to the true cost.

The hook budget covers a whole ``eawf hook run`` process for each tool event,
the most frequent hooks, as the rendered hook wrapper starts it,
``--target-epoch`` included, inside a tree at that epoch: from the console
entry point through the epoch check and the hook command module to the end of
its dispatch, with a stand-in daemon client that answers at once, so the
measure holds the imports the command itself takes and no daemon start-up.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from eawf.workflow.skills.publication import BUNDLE_TARGET_EPOCH
from tests._epoch2_helpers import lay_epoch2_tree

#: Summed self import microseconds allowed for ``eawf --version``.
VERSION_BUDGET_US = 60_000

#: Summed self import microseconds allowed for one ``eawf hook run`` process.
HOOK_COMMAND_BUDGET_US = 150_000

_SAMPLES = 7

#: The serial perf lane enforces the budgets; a parallel run sets this to skip them.
_skip_perf = pytest.mark.skipif(
    os.environ.get("EAWF_SKIP_PERF") == "1",
    reason="EAWF_SKIP_PERF=1 - perf gate skipped",
)

#: One ``eawf hook run <argv[1]>`` whose daemon client answers every call with ``{}``.
_HOOK_RUN = """
import io, json, sys, types

class DaemonClient:
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return None
    def call(self, method, params):
        return {}

stand_in = types.ModuleType("eawf.surfaces.cli._daemon_client")
stand_in.DaemonClient = DaemonClient
sys.modules["eawf.surfaces.cli._daemon_client"] = stand_in
sys.stdin = io.StringIO(json.dumps({
    "session_id": "s1",
    "tool_name": "Bash",
    "tool_input": {"command": "ls"},
    "tool_response": {"stdout": "ok"},
}))
sys.argv = [
    "eawf", "hook", "run", sys.argv[1], "--runtime", "claude", "--target-epoch", sys.argv[2]
]
from eawf.__main__ import main
main()
"""


def _summed_self_us(args: list[str], cwd: Path) -> int:
    """Return the summed self import time one fresh interpreter reports for *args*.

    Args:
        args: Interpreter arguments after ``-X importtime``.
        cwd: The directory the interpreter runs in.

    Returns:
        The sum of the self column, in microseconds.

    Raises:
        AssertionError: The interpreter exited non-zero.
    """
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )
    assert proc.returncode == 0, proc.stderr
    return sum(
        int(line.removeprefix("import time:").split("|", 1)[0])
        for line in proc.stderr.splitlines()
        if line.startswith("import time:") and "self [us]" not in line
    )


def _warm_minimum_us(measure: Callable[[], int]) -> int:
    """Return the smallest of ``_SAMPLES`` measurements taken after one discarded warm-up.

    The warm-up fills the file system and bytecode caches, which a cold first
    interpreter pays for; the minimum then drops the time other load added.

    Args:
        measure: Takes one measurement, in microseconds.

    Returns:
        The minimum of the measurements after the warm-up, in microseconds.
    """
    measure()
    return min(measure() for _ in range(_SAMPLES))


@pytest.mark.parametrize(
    ("args", "budget_us"),
    [
        pytest.param(["-m", "eawf", "--version"], VERSION_BUDGET_US, id="version"),
        *(
            pytest.param(
                ["-c", _HOOK_RUN, event, str(BUNDLE_TARGET_EPOCH)],
                HOOK_COMMAND_BUDGET_US,
                id=f"hook-{event}",
            )
            for event in ("pre_tool_use", "post_tool_use")
        ),
    ],
)
@_skip_perf
def test_summed_import_time_within_budget(args: list[str], budget_us: int, tmp_path: Path) -> None:
    lay_epoch2_tree(tmp_path)
    minimum = _warm_minimum_us(lambda: _summed_self_us(args, tmp_path))
    assert minimum <= budget_us, (
        f"minimum summed self import time {minimum / 1000:.1f} ms over {budget_us / 1000:.0f} ms; "
        "run the same command with -X importtime to see what loads"
    )


def test_summed_self_us_refuses_a_failing_interpreter(tmp_path: Path) -> None:
    with pytest.raises(AssertionError):
        _summed_self_us(["-c", "raise SystemExit(3)"], tmp_path)


def test_sampler_discards_the_warm_up_and_takes_the_minimum() -> None:
    answers = iter([50, 150, 120, 170, 130, 140, 160, 125])
    calls: list[int] = []

    def measure() -> int:
        calls.append(1)
        return next(answers)

    assert _warm_minimum_us(measure) == 120
    assert len(calls) == 8


def test_sampler_reports_the_value_when_every_sample_is_equal() -> None:
    calls: list[int] = []

    def measure() -> int:
        calls.append(1)
        return 140

    assert _warm_minimum_us(measure) == 140
    assert len(calls) == 8
