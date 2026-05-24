"""Real-process E2E harness: real ``eawf`` binary + real ``eawfd`` daemon.

The rest of the suite is ~95% in-process (Typer ``CliRunner``) with
``auto_spawn_daemon`` monkeypatched, so the process-spawn / fd-IO /
real-output layer never executes under test. This harness drives the
*actual* console entry points in fresh subprocesses against a fully
isolated temp repo + runtime directory, then spawns and reaps a real
detached daemon.

Isolation contract (so these tests never touch the developer's live
daemon at ``~/.eawfd`` nor this repo's real ``.ea/``):

* ``EAWF_RUNTIME_DIR`` points at a short per-test temp directory, so the
  daemon binds its socket / PID file / log there — never under
  ``~/.eawfd``. The path is kept short on purpose: the AF_UNIX bind
  address has a 104-byte cap on macOS, and ``$TMPDIR`` there already
  eats ~50 bytes.
* ``EA_STATE`` points at a temp ``.ea/state.json`` seeded from the
  committed empty-repo fixture, so the daemon's ``state.*`` resolver and
  the session-TTL sweep operate on throwaway state.
* Every subprocess runs with ``cwd`` set to the temp repo and the env
  above, so no pwd-upward resolver can climb into the real worktree.
* The :func:`running_daemon` fixture always reaps its child in a
  finalizer (SIGTERM then SIGKILL), so a failing assertion never leaks a
  detached daemon.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

#: The committed empty-repo state fixture, seeded into each E2E temp repo
#: so the daemon's state resolver + session-TTL sweep have a schema-valid
#: ``state.json`` to load (an invalid one floods the log with a
#: validation traceback that embeds source paths).
_EMPTY_REPO_STATE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)

#: Wall-clock budget for the daemon to expose its socket after spawn.
#: Mirrors :data:`eawf.runtime.daemon.spawn.SPAWN_POLL_TIMEOUT_SECONDS` with a
#: little extra headroom for a cold interpreter start under ``uv run``.
_DAEMON_READY_TIMEOUT_S: float = 8.0

#: Poll cadence while waiting for the socket / PID file to appear.
_POLL_INTERVAL_S: float = 0.05

#: Grace period for a SIGTERM'd daemon to exit before we escalate to
#: SIGKILL in the fixture finalizer.
_TERM_GRACE_S: float = 5.0

#: Default per-subprocess timeout. The daemon CLI verbs are short-lived
#: (ping / status / stop round-trip a single JSON-RPC frame).
_CLI_TIMEOUT_S: float = 30.0


def _eawf_argv() -> list[str]:
    """Return the argv prefix that runs the real ``eawf`` CLI.

    ``[sys.executable, "-m", "eawf"]`` invokes the same console entry as
    the ``eawf`` script (``[project.scripts] eawf = "eawf.surfaces.cli.app:main"``)
    while pinning the interpreter to the active (uv-managed) venv, so the
    subprocess resolves the in-tree package rather than a globally
    installed one.
    """
    return [sys.executable, "-m", "eawf"]


def _eawfd_argv() -> list[str]:
    """Return the argv prefix that runs the real ``eawfd`` daemon.

    Matches the ``eawfd`` console entry
    (``[project.scripts] eawfd = "eawf.runtime.daemon.main:main"``); ``main()``
    defaults to non-foreground, so the child logs to
    ``<runtime_dir>/eawfd.log``.
    """
    return [sys.executable, "-m", "eawf.runtime.daemon.main"]


@dataclass
class E2EEnv:
    """Isolated repo + runtime sandbox for one E2E test.

    Attributes:
        repo: Temp repository root (holds ``.ea/state.json``).
        runtime_dir: Temp daemon runtime dir (socket / PID / log / WAL).
        env: Process environment with the isolation overrides applied.
        state_path: Path to the seeded ``.ea/state.json``.
    """

    repo: Path
    runtime_dir: Path
    env: dict[str, str]
    state_path: Path
    _spawned: list[subprocess.Popen[bytes]] = field(default_factory=list)

    @property
    def daemon_proc(self) -> subprocess.Popen[bytes]:
        """Return the most-recently spawned daemon handle.

        Raises:
            AssertionError: When no daemon has been spawned yet.
        """
        assert self._spawned, "no daemon spawned in this sandbox"
        return self._spawned[-1]

    @property
    def pid_file(self) -> Path:
        """Path to ``<runtime_dir>/eawfd.pid``."""
        return self.runtime_dir / "eawfd.pid"

    @property
    def sock_file(self) -> Path:
        """Path to ``<runtime_dir>/eawfd.sock``."""
        return self.runtime_dir / "eawfd.sock"

    @property
    def log_file(self) -> Path:
        """Path to ``<runtime_dir>/eawfd.log``."""
        return self.runtime_dir / "eawfd.log"

    def run_eawf(
        self, *args: str, timeout: float = _CLI_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str]:
        """Run the real ``eawf`` CLI with *args* against this sandbox.

        Args:
            args: Sub-command argv (e.g. ``"daemon", "ping"``).
            timeout: Per-call wall-clock budget in seconds.

        Returns:
            The completed process with captured text stdout / stderr.
        """
        return subprocess.run(
            [*_eawf_argv(), *args],
            cwd=str(self.repo),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def spawn_daemon(self) -> subprocess.Popen[bytes]:
        """Spawn a real detached ``eawfd`` child bound to this sandbox.

        The child is registered for teardown so the owning fixture reaps
        it even when the test body raises before an explicit stop.

        Returns:
            The live :class:`subprocess.Popen` handle.
        """
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            _eawfd_argv(),
            cwd=str(self.repo),
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._spawned.append(proc)
        return proc

    def reap_all(self) -> None:
        """Terminate every spawned daemon (SIGTERM, then SIGKILL).

        Idempotent and exception-safe: a process that already exited is
        skipped. Always escalates to SIGKILL so a hung child can never
        outlive the test session.
        """
        # Process-control races (already-exited child, reparented PID) are
        # benign on teardown; suppress them so a finalizer never masks a
        # test assertion.
        for proc in self._spawned:
            if proc.poll() is not None:
                continue
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=_TERM_GRACE_S)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                with contextlib.suppress(ProcessLookupError, OSError, subprocess.TimeoutExpired):
                    proc.wait(timeout=_TERM_GRACE_S)


def _short_runtime_dir() -> Path:
    """Return a unique, short runtime dir under ``$TMPDIR`` (AF_UNIX cap).

    ``tmp_path`` lives under ``/var/folders/...`` on macOS which routinely
    pushes ``<dir>/eawfd.sock`` past the 104-byte AF_UNIX bind cap, so the
    runtime dir is placed directly under ``$TMPDIR`` with a short stem
    (mirrors ``tests/daemon/test_scaffolding._short_sock_path``).
    """
    base = Path(tempfile.gettempdir())
    return base / f"eawf-e2e-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def e2e_env() -> Iterator[E2EEnv]:
    """Yield an isolated repo + runtime sandbox; reap daemons on teardown.

    The temp repo gets a schema-valid ``.ea/state.json`` (the committed
    empty-repo fixture). ``EAWF_RUNTIME_DIR`` + ``EA_STATE`` redirect the
    daemon entirely into the sandbox so neither the developer's live
    ``~/.eawfd`` nor this repo's real ``.ea/`` is touched.
    """
    repo = Path(tempfile.mkdtemp(prefix="eawf-e2e-repo-"))
    runtime_dir = _short_runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    ea = repo / ".ea"
    ea.mkdir(parents=True, exist_ok=True)
    state_path = ea / "state.json"
    shutil.copy(_EMPTY_REPO_STATE, state_path)

    env = dict(os.environ)
    env["EAWF_RUNTIME_DIR"] = str(runtime_dir)
    env["EA_STATE"] = str(state_path)
    # Keep the daemon alive long enough for a multi-verb test, but bounded
    # so a leaked child still self-exits.
    env["EAWF_DAEMON_IDLE_TIMEOUT"] = "120"
    env["EAWF_DAEMON_SESSION_TTL"] = "120"
    # Force quiet spawn parity with production (no stray stderr noise).
    env.pop("EAWF_VERBOSE", None)

    sandbox = E2EEnv(repo=repo, runtime_dir=runtime_dir, env=env, state_path=state_path)
    try:
        yield sandbox
    finally:
        sandbox.reap_all()
        shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _wait_for_socket(sandbox: E2EEnv, deadline: float) -> bool:
    """Poll until the daemon socket accepts a connection or *deadline* passes."""
    while time.monotonic() < deadline:
        if sandbox.sock_file.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    probe.connect(str(sandbox.sock_file))
                    return True
            except OSError:
                pass
        time.sleep(_POLL_INTERVAL_S)
    return False


@pytest.fixture
def running_daemon(e2e_env: E2EEnv) -> Iterator[E2EEnv]:
    """Yield a sandbox with a live real ``eawfd`` already accepting connections.

    Spawns the daemon as a detached subprocess, waits for the socket +
    PID file, and hands back the same :class:`E2EEnv`. Teardown is owned
    by :func:`e2e_env`'s finalizer, which reaps the child unconditionally.

    Raises:
        AssertionError: When the daemon never exposes its socket / PID
            file within :data:`_DAEMON_READY_TIMEOUT_S`; the captured log
            tail is surfaced to aid diagnosis.
    """
    e2e_env.spawn_daemon()
    deadline = time.monotonic() + _DAEMON_READY_TIMEOUT_S
    ready = _wait_for_socket(e2e_env, deadline)
    if not ready or not e2e_env.pid_file.exists():
        log_tail = (
            e2e_env.log_file.read_text(encoding="utf-8", errors="replace")
            if e2e_env.log_file.exists()
            else "(no log file written)"
        )
        raise AssertionError(
            f"daemon did not become ready within {_DAEMON_READY_TIMEOUT_S}s "
            f"(sock={e2e_env.sock_file.exists()} pid={e2e_env.pid_file.exists()})\n"
            f"--- eawfd.log ---\n{log_tail}"
        )
    yield e2e_env


def daemon_socket_path_fits_afunix(runtime_dir: Path) -> bool:
    """Return True when ``<runtime_dir>/eawfd.sock`` fits the AF_UNIX cap.

    Used by tests that deliberately nest the runtime dir under a longer
    path (e.g. a planted ``Users/`` segment for the scrub assertion) to
    skip rather than spuriously fail when the bind address would exceed
    the ~104-byte platform limit.
    """
    sock = runtime_dir / "eawfd.sock"
    # 104 is the macOS sun_path cap (Linux is 108); use the tighter bound.
    return len(str(sock).encode()) < 104


def spawn_daemon_in(
    *,
    repo: Path,
    runtime_dir: Path,
    extra_env: Sequence[tuple[str, str]] = (),
) -> tuple[subprocess.Popen[bytes], dict[str, str]]:
    """Spawn a real detached daemon under an arbitrary repo + runtime dir.

    A thin escape hatch for tests that need a runtime dir the standard
    :func:`e2e_env` fixture would not produce (e.g. one nested under a
    planted ``Users/`` path segment to drive the scrubber). Callers own
    teardown.

    Args:
        repo: Repository root (must contain a valid ``.ea/state.json``).
        runtime_dir: Daemon runtime directory (created if absent).
        extra_env: Additional ``(key, value)`` env overrides.

    Returns:
        The live :class:`subprocess.Popen` and the resolved env dict.
    """
    runtime_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["EAWF_RUNTIME_DIR"] = str(runtime_dir)
    env["EA_STATE"] = str(repo / ".ea" / "state.json")
    env["EAWF_DAEMON_IDLE_TIMEOUT"] = "120"
    env["EAWF_DAEMON_SESSION_TTL"] = "120"
    env.pop("EAWF_VERBOSE", None)
    for key, value in extra_env:
        env[key] = value
    proc: subprocess.Popen[bytes] = subprocess.Popen(
        _eawfd_argv(),
        cwd=str(repo),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, env
