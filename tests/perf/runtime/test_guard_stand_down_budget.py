"""The hook wrapper stands a pre-tool call down outside every managed tree in shell time.

The guard's on-or-off decision reads only the session's directories, so the
size and the words of a call cannot slow it: a 60 KB command stands down as
fast as ``ls``, without starting eawf.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from typing import Final

import pytest

from tests._linked_worktree import git
from tests.integration.runtime.hooks.test_hook_wrapper_fail_open import (
    _SYSTEM_PATH,
    _env,
    _run,
    _stub,
    wrapper,
)

__all__ = ["wrapper"]

pytestmark = pytest.mark.perf

#: Commands of every size and kind a session outside every managed tree runs.
_COMMANDS: Final = ("ls", "rm -rf build", "echo $HOME > out.txt", f"echo {'x' * 60_000}")

_RUNS: Final = 20


def _p99(script: Path, cwd: Path, env: dict[str, str], command: str) -> float:
    samples: list[float] = []
    payload = json.dumps({"cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": command}})
    for _ in range(_RUNS):
        started = time.perf_counter()
        done = _run(script, "pre_tool_use", payload, cwd=cwd, env=env)
        samples.append(time.perf_counter() - started)
        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    return sorted(samples)[math.ceil(0.99 * _RUNS) - 1]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="timing is asserted on Linux")
def test_guard_stand_down_budget(wrapper: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    plain = tmp_path / "plain"
    plain.mkdir()
    stubs = tmp_path / "stubs"
    _stub(stubs, "uv", '#!/bin/bash\necho started >>"$EAWF_TEST_LOG"\n')
    _stub(stubs, "eawf", "#!/bin/bash\n")
    env = _env(tmp_path, f"{stubs}:{_SYSTEM_PATH}")
    for cwd in (repo, plain):
        baseline = _p99(Path("/dev/null"), cwd, env, "ls")
        for command in _COMMANDS:
            elapsed = _p99(wrapper, cwd, env, command)
            # Load slows both alike; an interpreter start would cost tens of empty shells.
            assert elapsed < 10 * baseline, (cwd.name, command[:20], elapsed, baseline)
    assert not (tmp_path / "calls.log").exists(), "the wrapper started eawf"
