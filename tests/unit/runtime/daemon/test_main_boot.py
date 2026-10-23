"""Boot-path tests for :mod:`eawf.runtime.daemon.main`.

``daemon.main.run()`` invokes :func:`eawf.runtime.daemon.recovery.replay_wal`
after the WAL directory is created and before the asyncio server accepts
connections, so a record that crashed mid-append is replayed at startup.

The tests exercise the boot path without ever opening a socket: the
asyncio body is monkeypatched into a no-op so the replay side effects can
be checked without standing up a real daemon. The daemon's logging sinks
are checked for the secret scrubber alongside.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.observability.logging.scrub import REDACTION, SensitiveScrubber
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon.wal import WalRecord, mark_applied, write_pending

pytestmark = pytest.mark.unit

_BOOT_SKIP_REASON = "daemon boot-path tests require POSIX runtime resolver"


def _envelope(env_id: str) -> Envelope:
    return Envelope(
        id=env_id,
        kind=StoreKind.EVENT,
        scope_id=None,
        created_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        summary="boot-replay test envelope",
        payload={"action": "noop"},
    )


def _record(record_id: str, envelope_id: str) -> WalRecord:
    return WalRecord(
        record_id=record_id,
        envelope=_envelope(envelope_id),
        idempotency_key=None,
        written_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        before_state_version="sha:before",
        after_state_version="sha:after",
    )


def _read_event_ids(event_path: Path) -> list[str]:
    if not event_path.exists():
        return []
    ids: list[str] = []
    for line in event_path.read_bytes().splitlines():
        if not line.strip():
            continue
        ids.append(orjson.loads(line)["id"])
    return ids


# ---------------------------------------------------------------------------
# Startup WAL replay
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_run_replays_wal_before_serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``.applied.json`` record missing from event.jsonl is replayed at boot."""
    # Stand up a synthetic project layout: state.json + an empty event log.
    ea_dir = tmp_path / "project" / ".ea"
    ea_dir.mkdir(parents=True)
    state_path = ea_dir / "state.json"
    state_path.write_bytes(orjson.dumps({"placeholder": True}))
    event_path = ea_dir / "store" / "event.jsonl"

    # Point the resolver at our synthetic state.
    monkeypatch.setenv("EA_STATE", str(state_path))

    # Redirect the daemon runtime directory at a fresh tmp tree so the
    # boot path materialises wal/ + pid + log without touching the
    # operator's real ~/.eawf/runtime/.
    rt_dir = tmp_path / "runtime"
    wal_dir = rt_dir / "wal"
    wal_dir.mkdir(parents=True)
    log_file = rt_dir / "eawfd.log"
    pid_file = rt_dir / "eawfd.pid"
    monkeypatch.setattr(daemon_main, "ensure_runtime_dir", lambda: rt_dir)
    monkeypatch.setattr(daemon_main, "pid_path", lambda: pid_file)
    monkeypatch.setattr(daemon_main, "log_path", lambda: log_file)
    monkeypatch.setattr(daemon_main, "socket_path", lambda: rt_dir / "eawfd.sock")

    # Seed a WAL record that crashed between event.jsonl append and
    # fsync rename — replay should append the envelope row + rename
    # the WAL record to .fsynced.json.
    rec = _record("rec-boot-001", "env-boot-001")
    write_pending(wal_dir, rec)
    mark_applied(wal_dir, rec.record_id)
    assert (wal_dir / "rec-boot-001.applied.json").exists()
    assert not event_path.exists()

    # Short-circuit the asyncio body so the test never tries to bind a
    # real socket. The replay runs in the synchronous prologue of run().
    def _close_coro(coro: object) -> None:
        # Close the coroutine the daemon would have run so the test
        # avoids a RuntimeWarning about a never-awaited coroutine.
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(daemon_main.asyncio, "run", _close_coro)

    exit_code = daemon_main.run(foreground=True)
    assert exit_code == 0

    # The boot-time replay must have appended the missing event row and
    # renamed the WAL record so subsequent boots are no-ops.
    assert _read_event_ids(event_path) == ["env-boot-001"]
    assert (wal_dir / "rec-boot-001.fsynced.json").exists()
    assert not (wal_dir / "rec-boot-001.applied.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_run_replay_clean_boot_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty WAL directory + missing event log → boot succeeds without writes."""
    ea_dir = tmp_path / "project" / ".ea"
    ea_dir.mkdir(parents=True)
    state_path = ea_dir / "state.json"
    state_path.write_bytes(orjson.dumps({"placeholder": True}))
    event_path = ea_dir / "store" / "event.jsonl"

    monkeypatch.setenv("EA_STATE", str(state_path))
    rt_dir = tmp_path / "runtime"
    monkeypatch.setattr(daemon_main, "ensure_runtime_dir", lambda: rt_dir)
    monkeypatch.setattr(daemon_main, "pid_path", lambda: rt_dir / "eawfd.pid")
    monkeypatch.setattr(daemon_main, "log_path", lambda: rt_dir / "eawfd.log")
    monkeypatch.setattr(daemon_main, "socket_path", lambda: rt_dir / "eawfd.sock")

    def _close_coro(coro: object) -> None:
        # Close the coroutine the daemon would have run so the test
        # avoids a RuntimeWarning about a never-awaited coroutine.
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(daemon_main.asyncio, "run", _close_coro)

    exit_code = daemon_main.run(foreground=True)
    assert exit_code == 0
    # No records → no event row.
    assert not event_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_run_acquires_singleton_before_pid_wal_and_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon boot owns the singleton before PID, WAL, or socket work."""
    ea_dir = tmp_path / "project" / ".ea"
    ea_dir.mkdir(parents=True)
    state_path = ea_dir / "state.json"
    state_path.write_bytes(orjson.dumps({"placeholder": True}))
    monkeypatch.setenv("EA_STATE", str(state_path))

    rt_dir = tmp_path / "runtime"
    pid_file = rt_dir / "eawfd.pid"
    events: list[str] = []

    @contextlib.contextmanager
    def _fake_singleton(_runtime_dir: Path) -> Iterator[None]:
        events.append("singleton")
        yield

    class _ReplayReport:
        pending_count = 0
        applied_count = 0
        fsynced_count = 0
        poisoned_count = 0
        replayed_event_count = 0

    def _fake_write_pid(_path: Path, _pid: int, _started_at: str) -> None:
        events.append("pid")

    def _fake_replay_wal(*_args: object, **_kwargs: object) -> _ReplayReport:
        events.append("wal")
        return _ReplayReport()

    def _close_coro(coro: object) -> None:
        events.append("socket")
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(daemon_main, "ensure_runtime_dir", lambda: rt_dir)
    monkeypatch.setattr(daemon_main, "pid_path", lambda: pid_file)
    monkeypatch.setattr(daemon_main, "socket_path", lambda: rt_dir / "eawfd.sock")
    monkeypatch.setattr(daemon_main, "acquire_daemon_singleton", _fake_singleton)
    monkeypatch.setattr(daemon_main, "_write_pid_file", _fake_write_pid)
    monkeypatch.setattr(daemon_main, "replay_wal", _fake_replay_wal)
    monkeypatch.setattr(daemon_main.asyncio, "run", _close_coro)

    assert daemon_main.run(foreground=True) == 0
    assert events == ["singleton", "pid", "wal", "socket"]


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_run_duplicate_boot_exits_without_runtime_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate daemon attempts do not overwrite PID or unlink sockets."""
    rt_dir = tmp_path / "runtime"
    rt_dir.mkdir()
    pid_file = rt_dir / "eawfd.pid"
    sock_file = rt_dir / "eawfd.sock"
    pid_file.write_text("1234\n1\nold\n", encoding="utf-8")
    sock_file.write_text("socket sentinel", encoding="utf-8")

    def _locked(_runtime_dir: Path) -> Iterator[None]:
        raise daemon_main.DaemonAlreadyRunningError("locked")

    def _unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("duplicate daemon mutated runtime")

    monkeypatch.setattr(daemon_main, "ensure_runtime_dir", lambda: rt_dir)
    monkeypatch.setattr(daemon_main, "pid_path", lambda: pid_file)
    monkeypatch.setattr(daemon_main, "socket_path", lambda: sock_file)
    monkeypatch.setattr(daemon_main, "acquire_daemon_singleton", _locked)
    monkeypatch.setattr(daemon_main, "_write_pid_file", _unexpected)
    monkeypatch.setattr(daemon_main, "replay_wal", _unexpected)
    monkeypatch.setattr(daemon_main.asyncio, "run", _unexpected)

    assert daemon_main.run(foreground=True) == 0
    assert pid_file.read_text(encoding="utf-8") == "1234\n1\nold\n"
    assert sock_file.read_text(encoding="utf-8") == "socket sentinel"


# ---------------------------------------------------------------------------
# Logging wiring — SensitiveScrubber attached to every sink
# ---------------------------------------------------------------------------


@pytest.fixture
def _restore_root_logging() -> Iterator[None]:
    """Snapshot + restore the root logger so wiring tests stay isolated."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def _has_scrubber(handler: logging.Handler) -> bool:
    """Return True when *handler* carries a :class:`SensitiveScrubber`."""
    return any(isinstance(f, SensitiveScrubber) for f in handler.filters)


def test_configure_logging_foreground_attaches_scrubber(_restore_root_logging: None) -> None:
    """The foreground stderr handler carries the scrubber filter."""
    logging.getLogger().handlers = []
    daemon_main._configure_logging(foreground=True)
    handlers = logging.getLogger().handlers
    assert handlers
    assert all(_has_scrubber(h) for h in handlers)


def test_configure_logging_file_handler_attaches_scrubber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _restore_root_logging: None
) -> None:
    """The non-foreground ``eawfd.log`` file handler carries the scrubber."""
    log_file = tmp_path / "runtime" / "eawfd.log"
    monkeypatch.setattr(daemon_main, "log_path", lambda: log_file)
    logging.getLogger().handlers = []
    daemon_main._configure_logging(foreground=False)
    file_handlers = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
    assert file_handlers
    assert all(_has_scrubber(h) for h in file_handlers)


def test_configure_logging_file_handler_scrubs_seeded_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _restore_root_logging: None
) -> None:
    """A seeded absolute-path leak never reaches ``eawfd.log`` on disk."""
    log_file = tmp_path / "runtime" / "eawfd.log"
    monkeypatch.setattr(daemon_main, "log_path", lambda: log_file)
    logging.getLogger().handlers = []
    daemon_main._configure_logging(foreground=False)

    log = logging.getLogger("eawf.test.daemon.leak")
    leak_line = (
        "error_detail session_log_path=/Users/operator/.ea/state.json"  # pragma: allowlist secret
    )
    log.info(leak_line)
    for handler in logging.getLogger().handlers:
        handler.flush()

    written = log_file.read_text(encoding="utf-8")
    assert "/Users/" not in written  # pragma: allowlist secret
    assert REDACTION in written


def test_cli_main_log_sink_attaches_scrubber(_restore_root_logging: None) -> None:
    """The CLI's root log sink carries the scrubber filter."""
    from eawf.observability.logging.scrub import configure_cli_logging as cli_configure_logging

    logging.getLogger().handlers = []
    cli_configure_logging()
    handlers = logging.getLogger().handlers
    assert handlers
    assert all(_has_scrubber(h) for h in handlers)


def test_cli_main_log_sink_does_not_clobber_existing_handlers(
    _restore_root_logging: None,
) -> None:
    """A pre-configured root logger is left untouched by the CLI sink."""
    from eawf.observability.logging.scrub import configure_cli_logging as cli_configure_logging

    sentinel = logging.NullHandler()
    logging.getLogger().handlers = [sentinel]
    cli_configure_logging()
    assert logging.getLogger().handlers == [sentinel]
