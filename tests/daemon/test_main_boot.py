"""Boot-path tests for :mod:`eawf.daemon.main` (P24-I02-W01 / audit F1+F2).

Covers two wiring gaps surfaced by the P24 ship-gate audit:

- F1: ``daemon.main.run()`` invokes :func:`eawf.daemon.recovery.replay_wal`
  after the WAL directory is created and before the asyncio server
  accepts connections (C02 §5.6 startup-replay invariant).
- F2: ``daemon.main._schedule_session_ttl_sweep`` schedules
  :func:`eawf.daemon.session_ttl.run_sweep_loop` against the wired
  shutdown event so expired session rows prune on each tick (W07
  background-sweep criterion).

The tests exercise the boot path without ever opening a socket — the
asyncio body is monkeypatched into a no-op so we can verify the
side-effect surface (replay report + sweep task) without standing
up a real daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest

from eawf.daemon import main as daemon_main
from eawf.daemon.bus import EventBus
from eawf.daemon.methods import MethodContext
from eawf.daemon.session import reset_registry
from eawf.daemon.session_ttl import DEFAULT_TTL_SECONDS
from eawf.daemon.wal import WalRecord, mark_applied, write_pending
from eawf.state.enums import StoreKind
from eawf.state.models import SessionAttempt, Wave
from eawf.store.envelope import Envelope

pytestmark = pytest.mark.unit

_BOOT_SKIP_REASON = "daemon boot-path tests require POSIX runtime resolver"


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Reset the process-local session-handle registry between tests."""
    reset_registry()
    yield
    reset_registry()


def _run(body: Callable[[], Awaitable[None]]) -> None:
    """Run an async test body without ``pytest-asyncio``."""
    asyncio.run(body())


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


def _now() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _make_attempt(
    *,
    attempt: int = 1,
    runtime: str = "claude-code",
    ended_at: datetime | None = None,
) -> SessionAttempt:
    return SessionAttempt(
        attempt=attempt,
        runtime=runtime,
        session_id=f"sess-{attempt}",
        session_log_handle=f"urn:eawf:v1:session-log:{runtime}:{'a' * 32}",
        started_at=_now(),
        ended_at=ended_at,
    )


def _build_state_with_expired_session(*, wave_id: str) -> dict[str, object]:
    """Build a minimal state payload with one expired session attempt."""
    ended = _now() - timedelta(seconds=DEFAULT_TTL_SECONDS + 10)
    attempt = _make_attempt(ended_at=ended)
    wave = Wave.model_validate(
        {
            "id": wave_id,
            "iter_id": "P24-I02",
            "title": "boot-sweep-test",
            "status": "closed",
            "opened_at": _now().isoformat(),
            "sessions": {"1": attempt.model_dump(mode="json")},
        }
    )
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "QR",
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {"project_code": "QR"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {wave_id: wave.model_dump(mode="json")},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


# ---------------------------------------------------------------------------
# F1 — startup WAL replay
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
    monkeypatch.setattr(daemon_main, "runtime_dir", lambda: rt_dir)
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
    monkeypatch.setattr(daemon_main, "runtime_dir", lambda: rt_dir)
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


# ---------------------------------------------------------------------------
# F2 — schedule session_ttl sweep
# ---------------------------------------------------------------------------


def _build_ctx(*, state_path: Path | None, bus: EventBus | None) -> MethodContext:
    return MethodContext(
        started_at="2026-05-19T12:00:00+00:00",
        pid=42,
        protocol_version="1",
        version="0.3.0",
        shutdown_event=asyncio.Event(),
        bus=bus,
        state_path=state_path,
    )


def test_schedule_session_ttl_sweep_returns_none_without_state(
    tmp_path: Path,
) -> None:
    """Daemonless / unit-test contexts (state_path=None) skip the sweep."""

    async def body() -> None:
        ctx = _build_ctx(state_path=None, bus=None)
        task = daemon_main._schedule_session_ttl_sweep(ctx)
        assert task is None

    _run(body)


def test_schedule_session_ttl_sweep_prunes_expired_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scheduled sweep publishes a session_handle_pruned envelope on tick."""
    state_path = tmp_path / "state.json"
    payload = _build_state_with_expired_session(wave_id="P24-I02-W01")
    state_path.write_bytes(orjson.dumps(payload))

    async def body() -> None:
        published: list[Envelope] = []

        class _CapturingBus:
            """Minimal publish() stub matching EventBus's narrow API."""

            def publish(self, envelope: Envelope) -> None:
                published.append(envelope)

        ctx = _build_ctx(state_path=state_path, bus=_CapturingBus())
        task = daemon_main._schedule_session_ttl_sweep(ctx)
        assert task is not None
        # Yield to the loop so the sweep's first tick runs.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Trip the shutdown event so the loop exits before sleeping
        # the full interval.
        assert isinstance(ctx.shutdown_event, asyncio.Event)
        ctx.shutdown_event.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        assert len(published) == 1
        assert published[0].payload["event_type"] == "session_handle_pruned"

    _run(body)


def test_resolve_session_ttl_seconds_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing env var → DEFAULT_TTL_SECONDS (86_400, one day)."""
    monkeypatch.delenv("EAWF_DAEMON_SESSION_TTL", raising=False)
    assert daemon_main._resolve_session_ttl_seconds() == DEFAULT_TTL_SECONDS


def test_resolve_session_ttl_seconds_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive integer env var overrides the default."""
    monkeypatch.setenv("EAWF_DAEMON_SESSION_TTL", "120")
    assert daemon_main._resolve_session_ttl_seconds() == 120


def test_resolve_session_ttl_seconds_unparseable_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage env value → default + warning (verified by return value)."""
    monkeypatch.setenv("EAWF_DAEMON_SESSION_TTL", "not-a-number")
    assert daemon_main._resolve_session_ttl_seconds() == DEFAULT_TTL_SECONDS


def test_resolve_session_ttl_seconds_non_positive_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero / negative → default."""
    monkeypatch.setenv("EAWF_DAEMON_SESSION_TTL", "0")
    assert daemon_main._resolve_session_ttl_seconds() == DEFAULT_TTL_SECONDS
    monkeypatch.setenv("EAWF_DAEMON_SESSION_TTL", "-5")
    assert daemon_main._resolve_session_ttl_seconds() == DEFAULT_TTL_SECONDS
