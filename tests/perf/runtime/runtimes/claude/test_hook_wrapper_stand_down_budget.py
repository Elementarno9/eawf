"""The plugin hook wrapper stands down outside a managed repository within 50 ms at p99.

The blocking test of the same stand-down bounds it against an empty bash start
measured in the same run; this one holds the absolute budget, over 20 runs in
an unmanaged git repository and 20 outside git, on Linux.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.runtime.hooks.test_hook_wrapper_fail_open import (
    _env,
    stand_down_p99,
    wrapper,
)

__all__ = ["wrapper"]

#: The p99 a stand-down must stay under, in seconds.
STAND_DOWN_BUDGET_S = 0.050


@pytest.mark.skipif(
    os.environ.get("EAWF_SKIP_PERF") == "1", reason="EAWF_SKIP_PERF=1 - perf gate skipped"
)
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="timed on Linux only")
def test_unmanaged_stand_down_stays_under_its_budget(wrapper: Path, tmp_path: Path) -> None:
    repo = tmp_path / "scratch"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    env = _env(tmp_path, os.environ["PATH"])
    for cwd in (repo, plain):
        p99 = stand_down_p99(wrapper, cwd, env)
        assert p99 < STAND_DOWN_BUDGET_S, f"{cwd.name}: p99 {p99 * 1000:.1f} ms over 20 runs"
