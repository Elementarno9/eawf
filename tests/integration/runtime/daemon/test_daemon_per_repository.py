"""One daemon per repository: its main checkout and linked worktrees share one daemon.

Every daemon here is a real subprocess over a tmp repository under a tmp ``HOME``,
so none can dial or be dialled by the operator's own, and each is stopped at teardown.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.daemon.runtime_dir import tree_key
from eawf.runtime.daemon.spawn import daemon_pid_if_ready, request_daemon_shutdown
from tests._linked_worktree import repo_with_linked_worktree

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX socket runtime dirs"),
]

#: Upper bound on one client run, cold spawn included.
CLIENT_TIMEOUT_SECONDS = 60.0

#: Dials the daemon its working directory resolves to, spawning it when absent, and
#: reports which daemon answered, the tree it serves and the runtime directory dialled.
_CLIENT = """
import json
from pathlib import Path
from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.surfaces.cli._daemon_client import DaemonClient

with DaemonClient() as client:
    result = client.call("config.read")
print(json.dumps({
    "pid": client.pid,
    "tree": str(Path(result["layer_path"]).parent.parent),
    "runtime": str(runtime_dir()),
}))
"""


def _env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("EAWF_RUNTIME_DIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "EA_STATE"):
        env.pop(key, None)
    env["HOME"] = str(home)
    # A daemon the teardown misses still exits on its own.
    env["EAWF_DAEMON_IDLE_TIMEOUT"] = "120"
    return env


def _run(home: Path, cwd: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *argv],
        cwd=cwd,
        env=_env(home),
        capture_output=True,
        text=True,
        check=False,
        timeout=CLIENT_TIMEOUT_SECONDS,
    )


def _client(home: Path, cwd: Path) -> dict[str, Any]:
    completed = _run(home, cwd, ["-c", _CLIENT])
    assert completed.returncode == 0, completed.stderr
    reply: dict[str, Any] = json.loads(completed.stdout.strip().splitlines()[-1])
    return reply


def _daemon_status(home: Path, cwd: Path) -> dict[str, Any]:
    completed = _run(home, cwd, ["-m", "eawf", "--json", "daemon", "status"])
    assert completed.returncode == 0, completed.stderr
    envelope: dict[str, Any] = json.loads(completed.stdout)
    result: dict[str, Any] = envelope["result"]
    return result


def _stop(runtime: Path) -> None:
    pid = daemon_pid_if_ready(runtime)
    with contextlib.suppress(OSError, RuntimeError):
        request_daemon_shutdown(runtime, drain=False, timeout_seconds=5)
    if pid is None:
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


@pytest.fixture
def home() -> Iterator[Path]:
    # Rooted at /tmp: ``<home>/.eawfd/trees/<key>/eawfd.sock`` must stay inside
    # the 104-byte AF_UNIX path cap.
    root = Path(tempfile.mkdtemp(prefix="eawf-r-", dir="/tmp")).resolve()
    try:
        yield root
    finally:
        trees = root / ".eawfd" / "trees"
        for runtime in trees.iterdir() if trees.is_dir() else ():
            _stop(runtime)
        shutil.rmtree(root, ignore_errors=True)


def test_one_runtime_dir_per_repository(home: Path) -> None:
    main, linked = repo_with_linked_worktree(home)
    expected = home / ".eawfd" / "trees" / tree_key(main / ".ea" / "state.json")

    from_linked = _client(home, linked)
    from_main = _client(home, main)

    assert Path(from_linked["runtime"]) == Path(from_main["runtime"]) == expected
    assert from_linked["pid"] == from_main["pid"]
    assert Path(from_linked["tree"]) == main
    assert [path.name for path in expected.parent.iterdir()] == [expected.name]

    status_linked = _daemon_status(home, linked)
    status_main = _daemon_status(home, main)
    assert status_linked["pid"] == status_main["pid"] == from_main["pid"]

    second = _run(home, linked, ["-m", "eawf.runtime.daemon.main", "--foreground"])
    assert second.returncode == 0, second.stderr
    assert f"run duplicate-daemon runtime={expected.name!r}" in second.stderr
    assert daemon_pid_if_ready(expected) == from_main["pid"]
