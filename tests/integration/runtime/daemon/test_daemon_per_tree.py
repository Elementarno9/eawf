"""One daemon per tree: a client reaches the daemon of its own tree only.

Every test runs real subprocess daemons over tmp trees under a tmp ``HOME``,
so no daemon here can ever dial or be dialled by the operator's own. The
clients run with ``EAWF_RUNTIME_DIR`` unset, which is the path an operator's
shell takes: the runtime dir is derived from the tree.
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
from dataclasses import dataclass
from pathlib import Path

import pytest

from eawf.kernel.state.models import State
from eawf.observability.doctor import daemon_strays
from eawf.runtime.daemon.runtime_dir import runtime_base_dir
from eawf.runtime.daemon.spawn import daemon_pid_if_ready, request_daemon_shutdown
from tests.integration.runtime.daemon.test_close_lock_split import _state_payload

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX socket runtime dirs"),
]

#: Upper bound on one client run, cold spawn included.
CLIENT_TIMEOUT_SECONDS = 60.0

#: Reads the bound tree's state through the daemon the client's own tree
#: resolves to, spawning it when absent, and reports which daemon answered.
_CLIENT = """
import json
from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.surfaces.cli._daemon_client import DaemonClient

with DaemonClient() as client:
    result = client.call("state.read")
print(json.dumps({
    "pid": client.pid,
    "title": result["state"]["project"]["title"],
    "runtime": str(runtime_dir()),
}))
"""


@dataclass(frozen=True)
class ClientReply:
    """What one client run saw."""

    pid: int
    title: str
    runtime: Path


def _write_tree(root: Path, title: str) -> Path:
    payload = _state_payload()
    payload["project"]["title"] = title
    state_path = root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(State.model_validate(payload).model_dump_json(), encoding="utf-8")
    return root


@dataclass
class Sandbox:
    """A tmp ``HOME`` holding two trees, and every runtime dir a daemon used."""

    home: Path
    tree_a: Path
    tree_b: Path
    runtimes: set[Path]

    def env(self, runtime_override: Path | None = None) -> dict[str, str]:
        env = dict(os.environ)
        for key in ("EAWF_RUNTIME_DIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "EA_STATE"):
            env.pop(key, None)
        env["HOME"] = str(self.home)
        # A daemon the teardown misses still exits on its own.
        env["EAWF_DAEMON_IDLE_TIMEOUT"] = "120"
        if runtime_override is not None:
            env["EAWF_RUNTIME_DIR"] = str(runtime_override)
        return env

    def client(self, tree: Path, runtime_override: Path | None = None) -> ClientReply:
        completed = subprocess.run(
            [sys.executable, "-c", _CLIENT],
            cwd=tree,
            env=self.env(runtime_override),
            capture_output=True,
            text=True,
            check=False,
            timeout=CLIENT_TIMEOUT_SECONDS,
        )
        assert completed.returncode == 0, completed.stderr
        raw = json.loads(completed.stdout.strip().splitlines()[-1])
        reply = ClientReply(pid=raw["pid"], title=raw["title"], runtime=Path(raw["runtime"]))
        self.runtimes.add(reply.runtime)
        return reply


def _stop(runtime: Path) -> None:
    pid = daemon_pid_if_ready(runtime)
    with contextlib.suppress(OSError, RuntimeError):
        request_daemon_shutdown(runtime, drain=False, timeout_seconds=5)
    if pid is not None and not _wait_gone(pid, 5.0):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    # Rooted at /tmp: ``<home>/.eawfd/trees/<key>/eawfd.sock`` under the macOS
    # per-user temp dir runs past the 104-byte AF_UNIX path cap.
    home = Path(tempfile.mkdtemp(prefix="eawf-h-", dir="/tmp"))
    box = Sandbox(
        home=home,
        tree_a=_write_tree(home / "ta", "tree-a"),
        tree_b=_write_tree(home / "tb", "tree-b"),
        runtimes=set(),
    )
    try:
        yield box
    finally:
        for runtime in box.runtimes:
            _stop(runtime)
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def host_view(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    """Make this process resolve runtime dirs as a client of tree A would."""
    monkeypatch.setenv("HOME", str(sandbox.home))
    for key in ("EAWF_RUNTIME_DIR", "XDG_RUNTIME_DIR", "EA_STATE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(sandbox.tree_a)
    return sandbox


def _scan_only(monkeypatch: pytest.MonkeyPatch, pids: set[int]) -> None:
    """Limit the stray scan to *pids*, so no host daemon is pinged or stopped."""
    real = daemon_strays._daemon_processes

    def scoped() -> list[daemon_strays._DaemonProcess] | None:
        rows = real()
        return None if rows is None else [row for row in rows if row.pid in pids]

    monkeypatch.setattr(daemon_strays, "_daemon_processes", scoped)


def test_client_of_each_tree_reaches_its_own_tree_daemon(sandbox: Sandbox) -> None:
    """FU-21: with one HOME, tree B's client never gets tree A's state."""
    first_a = sandbox.client(sandbox.tree_a)
    first_b = sandbox.client(sandbox.tree_b)

    assert first_a.title == "tree-a"
    assert first_b.title == "tree-b"
    assert first_a.pid != first_b.pid
    assert first_a.runtime != first_b.runtime
    assert {first_a.runtime.parent, first_b.runtime.parent} == {sandbox.home / ".eawfd" / "trees"}


def test_repeat_client_of_one_tree_reuses_its_daemon(sandbox: Sandbox) -> None:
    """A single-repo user keeps one daemon: a second command attaches to it."""
    first = sandbox.client(sandbox.tree_a)
    nested = sandbox.tree_a / "src"
    nested.mkdir()
    again = sandbox.client(nested)

    assert again.pid == first.pid
    assert again.runtime == first.runtime
    assert again.title == "tree-a"


def test_daemon_on_the_per_user_address_is_bypassed_then_listed(
    host_view: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration: a daemon on the old per-HOME address never answers the new
    client, keeps answering its own address, and the doctor lists it."""
    legacy_dir = host_view.home / ".eawfd"
    legacy = host_view.client(host_view.tree_a, runtime_override=legacy_dir)
    assert legacy.runtime == legacy_dir == runtime_base_dir()

    fresh = host_view.client(host_view.tree_a)

    assert fresh.pid != legacy.pid
    assert fresh.title == "tree-a"
    assert daemon_pid_if_ready(legacy_dir) == legacy.pid
    _scan_only(monkeypatch, {legacy.pid, fresh.pid})
    strays = daemon_strays.find_stray_daemons()
    assert strays is not None
    assert [(stray.pid, stray.reason) for stray in strays] == [(legacy.pid, "superseded_address")]


def test_doctor_lists_and_fix_stops_a_daemon_whose_runtime_dir_is_gone(
    host_view: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FU-22: a daemon bound to a removed runtime dir is listed, and the
    ``--fix`` repair stops it after re-checking its digest."""
    from eawf.observability.doctor.repair import _stray_action
    from eawf.surfaces.doctor_repair import _stop_strays

    orphan_dir = Path(tempfile.mkdtemp(prefix="eawf-s-", dir="/tmp"))
    stray = host_view.client(host_view.tree_b, runtime_override=orphan_dir)
    live = host_view.client(host_view.tree_a)
    host_view.runtimes.discard(orphan_dir)
    shutil.rmtree(orphan_dir)
    _scan_only(monkeypatch, {stray.pid, live.pid})

    check = daemon_strays.check_stray_daemons()
    assert check.status == "warn"
    assert f"pid {stray.pid} " in (check.detail or "")
    assert f"pid {live.pid} " not in (check.detail or "")

    action = _stray_action()
    assert action is not None
    assert (action.action_id, action.mutation_class, action.record_count) == (
        "daemon.stop-strays",
        "user_process",
        1,
    )
    result = _stop_strays(action)

    assert (result["status"], result["record_count"]) == ("applied", 1)
    assert _wait_gone(stray.pid, 1.0)
    assert daemon_pid_if_ready(live.runtime) == live.pid
    assert daemon_strays.check_stray_daemons().status == "ok"
    with pytest.raises(ValueError, match="plan changed"):
        _stop_strays(action)
