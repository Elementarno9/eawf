"""CI-only probe: report who sends SIGINT to a pytest worker, then keep running."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading


def _watch() -> None:
    """Wait for each SIGINT and write its sender and the running test to stderr."""
    while True:
        info = signal.sigwaitinfo({signal.SIGINT})
        test = os.environ.get("PYTEST_CURRENT_TEST", "?")
        try:
            sender = subprocess.run(
                ["ps", "-o", "pid,ppid,pgid,etimes,args", "-p", str(info.si_pid)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            group = subprocess.run(
                ["ps", "-o", "pid,ppid,pgid,args", "-g", str(os.getpgrp())],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        except OSError as exc:
            sender, group = repr(exc), ""
        sys.__stderr__.write(
            f"\nSIGINT-PROBE worker={os.getpid()} pgid={os.getpgrp()} si_pid={info.si_pid} "
            f"si_code={info.si_code} si_uid={info.si_uid} test={test}\n"
            f"SIGINT-PROBE sender:\n{sender}\nSIGINT-PROBE group:\n{group}\n"
        )
        sys.__stderr__.flush()


if os.environ.get("PYTEST_XDIST_WORKER"):
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    threading.Thread(target=_watch, name="sigint-probe", daemon=True).start()


def _dump_stuck_feed_tests() -> None:
    """Dump every thread's stack once a console-feed test runs past 8 s."""
    import faulthandler
    import time

    dumped: set[str] = set()
    started: dict[str, float] = {}
    while True:
        time.sleep(0.5)
        test = os.environ.get("PYTEST_CURRENT_TEST", "")
        if "test_console_feed_continuity" not in test:
            continue
        started.setdefault(test, time.monotonic())
        if test not in dumped and time.monotonic() - started[test] > 8:
            dumped.add(test)
            sys.__stderr__.write(f"\nFEED-PROBE stacks worker={os.getpid()} test={test}\n")
            faulthandler.dump_traceback(file=sys.__stderr__, all_threads=True)
            sys.__stderr__.flush()


if os.environ.get("PYTEST_XDIST_WORKER"):
    import logging

    for name in ("eawf.surfaces.tui.chassis.state_binding", "eawf.runtime.daemon.server"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    threading.Thread(target=_dump_stuck_feed_tests, name="feed-probe", daemon=True).start()


def _watch_slow_subscribe() -> None:
    """Dump every thread's stack while a subscribe call waits past 0.5 s."""
    import faulthandler

    from eawf.surfaces.cli import _daemon_client

    real = _daemon_client.DaemonClient._call_over_socket

    def timed(self: _daemon_client.DaemonClient, line: bytes, *, method: str) -> bytes:
        def dump() -> None:
            sys.__stderr__.write(
                f"\nSLOW-CALL-PROBE method={method} worker={os.getpid()} still waiting at 0.5 s\n"
            )
            faulthandler.dump_traceback(file=sys.__stderr__, all_threads=True)
            sys.__stderr__.flush()

        timer = threading.Timer(0.5, dump)
        timer.daemon = True
        timer.start()
        try:
            return real(self, line, method=method)
        finally:
            timer.cancel()

    _daemon_client.DaemonClient._call_over_socket = timed  # type: ignore[method-assign]


if os.environ.get("PYTEST_XDIST_WORKER"):
    _watch_slow_subscribe()
