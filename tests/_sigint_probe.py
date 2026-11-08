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
