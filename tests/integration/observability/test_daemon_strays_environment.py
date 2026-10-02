"""The stray-daemon scan reads a live process's runtime-dir pin from its environment."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from eawf.observability.doctor import daemon_strays

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not sys.platform.startswith(("linux", "darwin")), reason="reads /proc or KERN_PROCARGS2"
    ),
]


def test_pinned_runtime_dir_is_read_from_the_process_environment(tmp_path: Path) -> None:
    pinned = tmp_path / "pinned"
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        env={**os.environ, "EAWF_RUNTIME_DIR": str(pinned)},
    )
    try:
        # the child is pinned once it has exec'd the interpreter
        deadline = time.monotonic() + 10
        while daemon_strays._pinned_runtime_dir(child.pid) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert daemon_strays._pinned_runtime_dir(child.pid) == pinned
    finally:
        child.communicate(b"")


def test_an_unpinned_process_has_no_pinned_runtime_dir() -> None:
    env = {key: value for key, value in os.environ.items() if key != "EAWF_RUNTIME_DIR"}
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE, env=env
    )
    try:
        time.sleep(0.3)
        assert daemon_strays._pinned_runtime_dir(child.pid) is None
    finally:
        child.communicate(b"")


def test_a_gone_process_has_no_pinned_runtime_dir() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()

    assert daemon_strays._pinned_runtime_dir(child.pid) is None
