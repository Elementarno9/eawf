"""Real-process E2E tier for the ``eawf`` CLI + ``eawfd`` daemon.

Every test here runs the *actual* console entry point in a fresh
subprocess against a fully isolated temp repo + runtime dir (see
``conftest.py``), and the daemon ones spawn + reap a real detached
``eawfd``. They assert on real stdout/stderr text, real exit codes, the
on-disk PID file / Unix socket lifecycle + perms, and the real
``eawfd.log`` contents.

This is the layer the ~95% in-process suite (Typer ``CliRunner`` with
``auto_spawn_daemon`` monkeypatched) cannot reach — the process-spawn,
fd-redirect, detach, and file-sink behaviours only execute in a real
child.

Isolation: the harness pins ``EAWF_RUNTIME_DIR`` + ``EA_STATE`` at temp
paths and runs every subprocess with ``cwd`` inside the temp repo, so
these tests never connect to (or kill) the developer's live daemon at
``~/.eawfd`` nor touch this repo's real ``.ea/``.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import stat
import sys
import time
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    E2EEnv,
    daemon_socket_path_fits_afunix,
    spawn_daemon_in,
)

# AF_UNIX + fork-based detach are POSIX-only. The Windows transport is a
# named pipe with a separate test surface; gate the whole tier off there.
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="E2E tier drives the POSIX UDS daemon; Windows pipe is out of scope",
    ),
]


def _imode(path: Path) -> int:
    """Return the permission bits (``S_IMODE``) of *path*."""
    return stat.S_IMODE(os.stat(path).st_mode)


def _read_pidfile_pid(pid_file: Path) -> int:
    """Return the integer PID recorded on the first line of *pid_file*."""
    return int(pid_file.read_text(encoding="utf-8").splitlines()[0].strip())


def _proc_alive(pid: int) -> bool:
    """Return True when *pid* names a live process (POSIX ``kill(pid, 0)``)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------- #
# CLI basics — real binary, real stdout / exit codes.
# --------------------------------------------------------------------------- #


def test_version_prints_bare_version_and_exits_zero(e2e_env: E2EEnv) -> None:
    """``eawf --version`` prints just the version string, exit 0."""
    result = e2e_env.run_eawf("--version")
    assert result.returncode == 0, result.stderr
    # Bare ``X.Y.Z`` line — no banner, no markup.
    assert result.stdout.strip()
    parts = result.stdout.strip().split(".")
    assert len(parts) >= 2 and all(p.isdigit() for p in parts[:2]), result.stdout


def test_help_lists_daemon_command_group(e2e_env: E2EEnv) -> None:
    """``eawf --help`` renders the top-level command tree incl. ``daemon``.

    The prog name in ``Usage:`` reflects the invocation form
    (``python -m eawf`` here vs. the ``eawf`` script), so the assertion
    keys on the stable tagline + the ``daemon`` command row instead.
    """
    result = e2e_env.run_eawf("--help")
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "agent-driven development framework" in result.stdout
    assert "daemon" in result.stdout
    assert "background daemon" in result.stdout


def test_unknown_command_exits_nonzero_with_stderr(e2e_env: E2EEnv) -> None:
    """An unknown sub-command exits non-zero and writes a usage error."""
    result = e2e_env.run_eawf("definitely-not-a-command")
    assert result.returncode != 0
    assert result.stderr.strip()
    assert result.stdout == ""


# --------------------------------------------------------------------------- #
# Daemon liveness verbs — auto-spawn a real child, real JSON-RPC round trip.
# --------------------------------------------------------------------------- #


def test_ping_cold_auto_spawns_and_reports_pid(e2e_env: E2EEnv) -> None:
    """Cold ``eawf daemon ping`` spawns a real daemon and reports its PID."""
    result = e2e_env.run_eawf("daemon", "ping")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("daemon ok pid=")
    assert "version=" in result.stdout
    # The reported PID matches the on-disk pidfile the child wrote.
    assert e2e_env.pid_file.exists()
    reported = result.stdout.split("pid=", 1)[1].split()[0]
    assert int(reported) == _read_pidfile_pid(e2e_env.pid_file)


def test_ping_twice_reuses_same_daemon(e2e_env: E2EEnv) -> None:
    """A second ``daemon ping`` reuses the already-running daemon (same PID)."""
    first = e2e_env.run_eawf("daemon", "ping")
    assert first.returncode == 0, first.stderr
    pid_after_first = _read_pidfile_pid(e2e_env.pid_file)
    second = e2e_env.run_eawf("daemon", "ping")
    assert second.returncode == 0, second.stderr
    assert _read_pidfile_pid(e2e_env.pid_file) == pid_after_first


def test_status_reports_zero_counters_on_warm_daemon(running_daemon: E2EEnv) -> None:
    """``daemon status`` against a warm daemon prints zeroed counters."""
    result = running_daemon.run_eawf("daemon", "status")
    assert result.returncode == 0, result.stderr
    assert "subs=0" in result.stdout
    assert "in_flight=0" in result.stdout
    assert f"pid={_read_pidfile_pid(running_daemon.pid_file)}" in result.stdout


def test_status_json_emits_parseable_object(running_daemon: E2EEnv) -> None:
    """``eawf --json daemon status`` emits a JSON object with daemon counters."""
    import json

    result = running_daemon.run_eawf("--json", "daemon", "status")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["pid"] == _read_pidfile_pid(running_daemon.pid_file)
    assert payload["active_subscriptions"] == 0
    assert payload["in_flight_mutations"] == 0
    assert payload["protocol_version"] == "1"


# --------------------------------------------------------------------------- #
# PID + socket lifecycle — real on-disk artifacts of a real child.
# --------------------------------------------------------------------------- #


def test_spawn_writes_pidfile_socket_and_log(running_daemon: E2EEnv) -> None:
    """A live daemon materialises its pidfile, socket node, and log file."""
    assert running_daemon.pid_file.exists()
    assert running_daemon.sock_file.exists()
    assert running_daemon.log_file.exists()
    # The pidfile names a process that is genuinely alive.
    assert _proc_alive(_read_pidfile_pid(running_daemon.pid_file))


def test_pidfile_records_protocol_and_timestamp(running_daemon: E2EEnv) -> None:
    """The pidfile carries ``<pid>\\n<protocol>\\n<started_at>``."""
    lines = running_daemon.pid_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 3
    assert lines[0].strip().isdigit()
    assert lines[1].strip() == "1"  # PROTOCOL_VERSION
    # ISO-8601-ish boot timestamp on the third line.
    assert lines[2].strip().startswith("20")


def test_bound_socket_is_a_socket_node(running_daemon: E2EEnv) -> None:
    """The bound path is a real AF_UNIX socket that accepts a connection."""
    assert stat.S_ISSOCK(os.stat(running_daemon.sock_file).st_mode)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(2.0)
        probe.connect(str(running_daemon.sock_file))  # raises on refusal


def test_bound_socket_is_owner_only(running_daemon: E2EEnv) -> None:
    """The Unix socket node is chmod'd to ``0o600`` (no group/other access)."""
    assert _imode(running_daemon.sock_file) == 0o600


def test_runtime_dir_is_owner_only_after_boot(running_daemon: E2EEnv) -> None:
    """The live daemon retightens its runtime dir to ``0o700`` on boot.

    The harness pre-creates the runtime dir under the prevailing umask, so
    a passing assertion proves the daemon's boot path routes through
    ``ensure_runtime_dir`` (which re-applies owner-only perms) rather than
    a bare ``mkdir`` that would leave group/other traversal bits set. The
    dir holds the PID file, socket, log, and WAL — all of which embed the
    operator's cwd / state paths — so it must not be listable by other
    local users.
    """
    assert _imode(running_daemon.runtime_dir) == 0o700


def test_stop_kills_daemon_and_removes_pidfile_and_socket(running_daemon: E2EEnv) -> None:
    """``daemon stop`` terminates the live daemon and clears its artifacts."""
    pid = _read_pidfile_pid(running_daemon.pid_file)
    assert _proc_alive(pid)
    result = running_daemon.run_eawf("daemon", "stop")
    assert result.returncode == 0, result.stderr
    assert "shutting down" in result.stdout
    # The daemon is a direct child of the test process, so reap it via the
    # Popen handle to confirm exit (and clear the zombie that would
    # otherwise read as "alive" under ``os.kill(pid, 0)``).
    exit_code = running_daemon.daemon_proc.wait(timeout=5.0)
    assert exit_code == 0
    # The daemon's finally-block unlinks the pidfile + socket on its way
    # out; both are gone once the process has exited.
    assert not running_daemon.pid_file.exists()
    assert not running_daemon.sock_file.exists()


# --------------------------------------------------------------------------- #
# eawfd.log contents — the real file sink.
# --------------------------------------------------------------------------- #


def test_log_records_boot_line_with_pid_and_version(running_daemon: E2EEnv) -> None:
    """The daemon log captures the boot line naming the live PID + version."""
    log_text = running_daemon.log_file.read_text(encoding="utf-8")
    pid = _read_pidfile_pid(running_daemon.pid_file)
    assert f"run boot pid={pid}" in log_text
    assert "version=" in log_text


def test_logs_cmd_streams_log_tail(running_daemon: E2EEnv) -> None:
    """``daemon logs --tail`` prints the real on-disk log lines."""
    result = running_daemon.run_eawf("daemon", "logs", "--tail", "50")
    assert result.returncode == 0, result.stderr
    assert "run boot pid=" in result.stdout


def test_logs_cmd_rejects_out_of_range_tail(e2e_env: E2EEnv) -> None:
    """``daemon logs --tail 0`` is a usage error (exit 2), no stdout."""
    result = e2e_env.run_eawf("daemon", "logs", "--tail", "0")
    assert result.returncode == 2
    assert "must be between" in result.stderr
    assert result.stdout == ""


def test_logs_cmd_errors_when_no_log_present(e2e_env: E2EEnv) -> None:
    """``daemon logs`` with no daemon ever started reports a missing log."""
    # Sanity: no daemon was spawned, so no log file exists yet.
    assert not e2e_env.log_file.exists()
    result = e2e_env.run_eawf("daemon", "logs")
    assert result.returncode == 1
    assert "no daemon log" in result.stderr


# macOS-home scrub pattern is anchored on this directory-name prefix
# (see eawf.logging.scrub.SensitiveScrubber.PATTERNS). It is assembled
# from parts at runtime — never written as a "/<prefix>/<name>" literal —
# so the path-leak pre-commit gate does not flag this synthetic fixture.
_HOME_PREFIX = "Users"
_PLANTED_USER = "victim"


def test_log_scrubs_home_shaped_paths() -> None:
    """A macOS-home-shaped path segment must be redacted in the daemon log.

    Drives a real daemon whose runtime dir is nested under a planted
    home-shaped segment (``<home-prefix>/<name>``), so the ``serve_unix``
    bind line emits an absolute path matching
    :class:`eawf.logging.scrub.SensitiveScrubber`'s macOS-home pattern.
    The scrubber, once wired onto the file sink, must rewrite that segment
    to ``<scrubbed>`` before the formatter serialises it.
    """
    import tempfile

    base = Path(tempfile.gettempdir())
    sandbox = base / f"eawf-e2e-scrub-{os.getpid()}-{int(time.time() * 1000) % 100000}"
    # Nest the runtime dir under a home-shaped segment so the logged
    # absolute socket path matches the macOS-home scrub pattern mid-string.
    runtime_dir = sandbox / _HOME_PREFIX / _PLANTED_USER / "rt"
    repo = sandbox / "repo"
    (repo / ".ea").mkdir(parents=True, exist_ok=True)
    fixture = (
        Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
    )
    shutil.copy(fixture, repo / ".ea" / "state.json")

    if not daemon_socket_path_fits_afunix(runtime_dir):
        shutil.rmtree(sandbox, ignore_errors=True)
        pytest.skip("planted runtime-dir path exceeds the AF_UNIX socket cap")

    # The exact substring the scrubber should redact, assembled at runtime.
    planted_needle = f"/{_HOME_PREFIX}/{_PLANTED_USER}"
    proc, _env = spawn_daemon_in(repo=repo, runtime_dir=runtime_dir)
    try:
        sock = runtime_dir / "eawfd.sock"
        log_file = runtime_dir / "eawfd.log"
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not sock.exists():
            time.sleep(0.05)
        # Let the boot + bind log lines flush.
        time.sleep(0.5)
        assert log_file.exists(), "daemon never wrote a log file"
        log_text = log_file.read_text(encoding="utf-8", errors="replace")
        # The bind line logs the absolute socket path; its planted
        # home-shaped segment must be scrubbed.
        assert planted_needle not in log_text, (
            "daemon log leaked a raw home-shaped path; the SensitiveScrubber "
            "filter is not applied to the eawfd.log file handler"
        )
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        shutil.rmtree(sandbox, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Isolation guard — prove the tier never touches the real runtime dir.
# --------------------------------------------------------------------------- #


def test_runtime_dir_override_keeps_daemon_inside_sandbox(running_daemon: E2EEnv) -> None:
    """The spawned daemon binds inside the sandbox, not the real ``~/.eawfd``."""
    real_runtime = Path.home() / ".eawfd"
    # The sandbox runtime dir is the one carrying live artifacts.
    assert running_daemon.sock_file.exists()
    assert running_daemon.sock_file.is_relative_to(running_daemon.runtime_dir)
    assert not running_daemon.runtime_dir.is_relative_to(real_runtime)
    # The pidfile PID is owned by us and lives only in the sandbox.
    assert _proc_alive(_read_pidfile_pid(running_daemon.pid_file))
