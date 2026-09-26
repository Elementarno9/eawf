"""The autopilot no-daemon test cannot see a daemon another test left running.

Every test in an xdist worker shares the worker's runtime dir, so a daemon
an earlier test booted and never stopped answers the socket probe of every
later test. The multi-select commit test asserts the no-daemon line, so it
pins a runtime dir of its own. These tests plant a live listener on the
worker dir and show the pinned test still passes while the same flow
without the pin sees the planted daemon, and that the suite names a test
that leaves a daemon reachable at session end.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf.surfaces.tui.modes.autopilot import BATCH_NO_DAEMON
from tests.conftest import REPO_ROOT, RuntimeDirIsolation
from tests.tui import test_modes_autopilot as autopilot_tests

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the daemon socket is POSIX-only")


@contextlib.contextmanager
def planted_daemon(rt_dir: Path) -> Iterator[None]:
    """Serve a socket on *rt_dir* that accepts and drops every connection.

    The socket probe only asks whether a connect succeeds, so an accepting
    listener is a live daemon to it; dropping each connection makes any
    RPC that follows fail fast instead of hanging.
    """
    sock_path = rt_dir / "eawfd.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen()
    listener.settimeout(0.1)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)
        listener.close()
        sock_path.unlink(missing_ok=True)


@pytest.fixture
def worker_daemon(runtime_dir_isolation: RuntimeDirIsolation) -> Iterator[Path]:
    """Plant a live daemon on the worker session runtime dir for one test."""
    with planted_daemon(runtime_dir_isolation.runtime_dir):
        yield runtime_dir_isolation.runtime_dir


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty home, as the autopilot suite does."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def test_pinned_commit_reports_no_daemon_despite_a_worker_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_daemon: Path
) -> None:
    """The real test passes with a daemon up on the worker runtime dir."""
    assert os.environ["EAWF_RUNTIME_DIR"] == str(worker_daemon)
    autopilot_tests.test_autopilot_multi_select_commit_stages_batch_and_tears_down(
        tmp_path, monkeypatch
    )


def _refuse_spawn(_runtime_dir: Path) -> int:
    """Stand in for the client's cold spawn, which would fork a real daemon."""
    raise ConnectionRefusedError("planted daemon drops every connection")


def test_unpinned_commit_sees_the_worker_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_daemon: Path
) -> None:
    """Without the pin the planted daemon answers, so the no-daemon line is gone.

    Every daemon client here (the batch dispatch and the state binding's
    push subscription) would otherwise answer the dropped connection by
    forking a real daemon into the worker runtime dir, the very leak this
    module guards against.
    """
    from eawf.surfaces.cli import _daemon_client as dc

    monkeypatch.setattr(dc, "auto_spawn_daemon", _refuse_spawn)
    assert os.environ["EAWF_RUNTIME_DIR"] == str(worker_daemon)
    toasts = autopilot_tests.commit_two_wave_batch(tmp_path)
    assert BATCH_NO_DAEMON not in toasts


def test_session_end_check_names_the_test_that_left_a_daemon(tmp_path: Path) -> None:
    """A nested session whose test leaves a daemon up fails naming that test."""
    leaky = tmp_path / "test_leaky.py"
    leaky.write_text(
        textwrap.dedent(
            f"""
            import os
            import sys
            sys.path.insert(0, {str(Path(__file__).parent)!r})
            from test_autopilot_daemon_isolation import planted_daemon
            from pathlib import Path

            LEFT_UP = []


            def test_quiet():
                pass


            def test_leaves_daemon_up():
                planted = planted_daemon(Path(os.environ["EAWF_RUNTIME_DIR"]))
                planted.__enter__()
                LEFT_UP.append(planted)
            """
        ),
        encoding="utf-8",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_ADDOPTS"}
    }
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            "-c",
            os.devnull,
            "--rootdir",
            str(tmp_path),
            "-q",
            str(leaky),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "DaemonLeakError" in output, output
    assert "left reachable by: test_leaky.py::test_leaves_daemon_up" in output, output
    assert "test_quiet" not in output.split("left reachable by:")[1].splitlines()[0]
