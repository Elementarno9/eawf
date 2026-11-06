"""Import-time budgets for ``eawf --version`` and the ``eawf hook run`` command path.

Each hook call and each version probe is a fresh process, so its import cost
is paid every time. The measure is the summed self time ``python -X
importtime`` reports, the median of five cold interpreters: summed self time
counts each module once and is far steadier than wall time on a busy runner.

The hook budget covers a whole ``eawf hook run`` process for each tool event,
the most frequent hooks, as the rendered hook wrapper starts it,
``--target-epoch`` included, inside a tree at that epoch: from the console
entry point through the epoch check and the hook command module to the end of
its dispatch, with a stand-in daemon client that answers at once, so the
measure holds the imports the command itself takes and no daemon start-up.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

from eawf.workflow.skills.publication import BUNDLE_TARGET_EPOCH
from tests._epoch2_helpers import lay_epoch2_tree

#: Summed self import microseconds allowed for ``eawf --version``.
VERSION_BUDGET_US = 60_000

#: Summed self import microseconds allowed for one ``eawf hook run`` process.
HOOK_COMMAND_BUDGET_US = 150_000

_SAMPLES = 5

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
    median = statistics.median(_summed_self_us(args, tmp_path) for _ in range(_SAMPLES))
    assert median <= budget_us, (
        f"median summed self import time {median / 1000:.1f} ms over {budget_us / 1000:.0f} ms; "
        "run the same command with -X importtime to see what loads"
    )


def test_summed_self_us_refuses_a_failing_interpreter(tmp_path: Path) -> None:
    with pytest.raises(AssertionError):
        _summed_self_us(["-c", "raise SystemExit(3)"], tmp_path)
