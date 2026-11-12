"""The suite hides the launching agent session's markers from every test.

An agent runtime exports a marker variable into the commands it runs, and
the operator-identity guard refuses an operator principal named under one.
A suite launched from an agent session would otherwise fail its sealing
smokes only because of who launched it.
"""

from __future__ import annotations

import os

# The child run is the point: only a fresh session proves the suite fixture
# hides markers that the launching process exported.
import subprocess  # noqa: EAWF024
import sys
from pathlib import Path

from eawf.runtime.daemon.operator_identity import AGENT_SESSION_ENV_MARKERS, agent_session_runtime

_REPO = Path(__file__).resolve().parents[2]
_PROBE = "tests/unit/test_agent_session_isolation.py::test_no_agent_session_marker_is_visible"


def test_no_agent_session_marker_is_visible() -> None:
    """No marker variable reaches a test, so the guard sees no agent session."""
    assert agent_session_runtime(os.environ) is None
    assert [name for name, _ in AGENT_SESSION_ENV_MARKERS if name in os.environ] == []


def test_a_child_suite_launched_with_markers_set_sees_none() -> None:
    """A suite launched with every marker set still runs its tests without them."""
    env = os.environ | {
        "CLAUDECODE": "1",
        "CODEX_THREAD_ID": "probe-thread",
        "PYTHONPATH": os.pathsep.join(
            filter(None, [str(_REPO / "src"), os.environ.get("PYTHONPATH")])
        ),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-n0",
            "-q",
            "--rootdir",
            str(_REPO),
            _PROBE,
        ],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
