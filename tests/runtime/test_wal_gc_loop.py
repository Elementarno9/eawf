"""Tests for the daemon WAL garbage-collection sweep loop (P30-I14-W01).

Three concerns, mirroring the wave success criteria:

- Fake-clock sweep: :func:`eawf.runtime.daemon.wal.gc_done_records` (driven
  by an injected ``now``) unlinks fsynced records older than the retention
  window and keeps recent ones, so the WAL stops growing unbounded.
- Loop scheduling: :func:`eawf.runtime.daemon.wal.run_wal_gc_loop` runs N
  sweeps under a fake clock then exits cleanly on the stop event, and
  :func:`eawf.runtime.daemon.main._schedule_wal_gc_sweep` is invoked +
  cancelled in the finally of BOTH server entrypoints.
- Env guards: the interval / retention resolvers reject a non-positive or
  unparseable value back to the module default so a misconfigured env can
  neither spin a zero-interval loop nor silently disable GC.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon.wal import (
    DEFAULT_WAL_GC_INTERVAL_SECONDS,
    WalRecord,
    mark_applied,
    mark_fsynced,
    resolve_wal_gc_interval_seconds,
    resolve_wal_retention_seconds,
    run_wal_gc_loop,
    write_pending,
)

pytestmark = pytest.mark.unit

_RETENTION_SECONDS = 3600
_DEFAULT_RETENTION_SECONDS = 3600


def _run(body: Callable[[], Coroutine[object, object, None]]) -> None:
    """Run an async test body without ``pytest-asyncio``."""
    asyncio.run(body())


def _build_envelope(envelope_id: str) -> Envelope:
    return Envelope(
        id=envelope_id,
        kind=StoreKind.EVENT,
        scope_id=None,
        created_at=datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC),
        summary="test envelope",
        payload={"action": "noop"},
    )


def _build_record(record_id: str) -> WalRecord:
    return WalRecord(
        record_id=record_id,
        envelope=_build_envelope(f"env-{record_id}"),
        idempotency_key=None,
        written_at=datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC),
        before_state_version="sha:before",
        after_state_version="sha:after",
    )


def _write_fsynced(wal_dir: Path, record_id: str, *, age_seconds: float) -> Path:
    """Persist a fsynced WAL record and back-date its mtime by *age_seconds*."""
    write_pending(wal_dir, _build_record(record_id))
    mark_applied(wal_dir, record_id)
    fsynced = mark_fsynced(wal_dir, record_id)
    aged = datetime.now(UTC).timestamp() - age_seconds
    os.utime(fsynced, (aged, aged))
    return fsynced


# ---------------------------------------------------------------------------
# Fake-clock sweep
# ---------------------------------------------------------------------------


def test_run_wal_gc_loop_one_sweep_removes_aged_keeps_recent(tmp_path: Path) -> None:
    """One sweep under a fake clock removes aged records, keeps recent ones.

    The injected ``now`` is fixed to wall-clock UTC: the aged record's mtime
    sits past the retention window so it is swept, the recent one stays. A
    pre-set stop event exits the loop right after that single sweep.
    """
    wal_dir = tmp_path / "wal"
    aged = _write_fsynced(wal_dir, "rec-aged", age_seconds=_RETENTION_SECONDS + 600)
    recent = _write_fsynced(wal_dir, "rec-recent", age_seconds=60)

    fixed_now = datetime.now(UTC)
    clock_calls = 0

    def _clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return fixed_now

    async def body() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_wal_gc_loop(
                wal_dir=wal_dir,
                retention_seconds=_RETENTION_SECONDS,
                interval_seconds=3600,
                stop_event=stop,
                now=_clock,
            )
        )
        # Yield so the loop body runs its first sweep before we stop it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    _run(body)
    assert clock_calls >= 1
    assert not aged.exists()
    assert recent.exists()


def test_run_wal_gc_loop_advanced_clock_ages_a_fresh_record(tmp_path: Path) -> None:
    """Advancing the injected clock makes a once-fresh record sweep-eligible.

    The record's mtime is "now", so under the real clock it is in-window.
    Driving :func:`gc_done_records` (the loop's per-tick body) with a clock
    far in the future moves it past retention and it is swept -- proving the
    sweep honours the injected clock rather than wall time.
    """
    from eawf.runtime.daemon.wal import gc_done_records

    wal_dir = tmp_path / "wal"
    record = _write_fsynced(wal_dir, "rec-fresh", age_seconds=0)

    base = datetime.now(UTC)
    # Under the base clock the fresh record is in-window -> kept.
    assert gc_done_records(wal_dir, _RETENTION_SECONDS, now=base) == []
    assert record.exists()
    # Advance the clock past retention -> the same record is swept.
    future = base + timedelta(seconds=_RETENTION_SECONDS + 1)
    removed = gc_done_records(wal_dir, _RETENTION_SECONDS, now=future)
    assert removed == [record]
    assert not record.exists()


def test_run_wal_gc_loop_runs_multiple_sweeps_then_cancels(tmp_path: Path) -> None:
    """The loop ticks N times under a fast injected sleep then cancels cleanly.

    The ``stop_event=None`` forever-loop is the daemon entrypoint shape: it
    only exits on cancellation. Here the injected sleep raises
    ``CancelledError`` on the third tick, proving the loop runs repeated
    sweeps and unwinds cleanly when the scheduling task is cancelled.
    """
    wal_dir = tmp_path / "wal"
    aged = _write_fsynced(wal_dir, "rec-aged", age_seconds=_RETENTION_SECONDS + 600)
    sweeps = 0

    async def body() -> None:
        nonlocal sweeps

        async def _fast_sleep(_seconds: float) -> None:
            nonlocal sweeps
            sweeps += 1
            if sweeps >= 3:
                raise asyncio.CancelledError
            await asyncio.sleep(0)

        with pytest.raises(asyncio.CancelledError):
            await run_wal_gc_loop(
                wal_dir=wal_dir,
                retention_seconds=_RETENTION_SECONDS,
                interval_seconds=1,
                stop_event=None,
                sleep=_fast_sleep,
            )

    _run(body)
    assert sweeps == 3
    assert not aged.exists()


# ---------------------------------------------------------------------------
# Loop scheduling in both server entrypoints
# ---------------------------------------------------------------------------


def test_schedule_wal_gc_sweep_returns_none_without_wal_dir() -> None:
    """Daemonless / unit-test contexts (wal_dir=None) skip the sweep."""
    from eawf.runtime.daemon.methods import MethodContext

    async def body() -> None:
        ctx = MethodContext(
            started_at="2026-06-11T12:00:00+00:00",
            pid=42,
            protocol_version="1",
            version="0.6.0",
            shutdown_event=asyncio.Event(),
        )
        assert daemon_main._schedule_wal_gc_sweep(ctx) is None

    _run(body)


def test_schedule_wal_gc_sweep_runs_and_cancels(tmp_path: Path) -> None:
    """A scheduled sweep ticks once then cancels cleanly on shutdown."""
    from eawf.runtime.daemon.methods import MethodContext

    wal_dir = tmp_path / "wal"
    aged = _write_fsynced(wal_dir, "rec-aged", age_seconds=_DEFAULT_RETENTION_SECONDS + 600)

    async def body() -> None:
        ctx = MethodContext(
            started_at="2026-06-11T12:00:00+00:00",
            pid=42,
            protocol_version="1",
            version="0.6.0",
            shutdown_event=asyncio.Event(),
            wal_dir=wal_dir,
        )
        task = daemon_main._schedule_wal_gc_sweep(ctx)
        assert task is not None
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert isinstance(ctx.shutdown_event, asyncio.Event)
        ctx.shutdown_event.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert not aged.exists()

    _run(body)


def test_both_entrypoints_schedule_and_cancel_wal_gc() -> None:
    """``_run_server`` AND ``_run_windows_server`` schedule + cancel the GC task."""
    for entrypoint in (daemon_main._run_server, daemon_main._run_windows_server):
        source = inspect.getsource(entrypoint)
        assert "_schedule_wal_gc_sweep(ctx)" in source, (
            f"{entrypoint.__name__} does not schedule the WAL-GC sweep"
        )
        assert "wal_gc_task.cancel()" in source, (
            f"{entrypoint.__name__} does not cancel the WAL-GC sweep in finally"
        )


# ---------------------------------------------------------------------------
# Loop guard rails — non-positive interval / retention raise
# ---------------------------------------------------------------------------


def test_run_wal_gc_loop_rejects_non_positive_interval(tmp_path: Path) -> None:
    """A non-positive interval raises rather than spinning a zero-sleep loop."""

    async def body() -> None:
        with pytest.raises(ValueError, match="interval_seconds must be positive"):
            await run_wal_gc_loop(wal_dir=tmp_path, interval_seconds=0)

    _run(body)


def test_run_wal_gc_loop_rejects_non_positive_retention(tmp_path: Path) -> None:
    """A non-positive retention raises rather than disabling GC."""

    async def body() -> None:
        with pytest.raises(ValueError, match="retention_seconds must be positive"):
            await run_wal_gc_loop(wal_dir=tmp_path, retention_seconds=-1)

    _run(body)


# ---------------------------------------------------------------------------
# Env-resolver guards
# ---------------------------------------------------------------------------


def test_resolve_wal_gc_interval_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing env var falls back to the module default."""
    monkeypatch.delenv("EAWF_DAEMON_WAL_GC_INTERVAL", raising=False)
    assert resolve_wal_gc_interval_seconds() == DEFAULT_WAL_GC_INTERVAL_SECONDS


def test_resolve_wal_gc_interval_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A positive integer env var overrides the default."""
    monkeypatch.setenv("EAWF_DAEMON_WAL_GC_INTERVAL", "120")
    assert resolve_wal_gc_interval_seconds() == 120


def test_resolve_wal_gc_interval_non_positive_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero / negative interval falls back so the loop cannot spin."""
    monkeypatch.setenv("EAWF_DAEMON_WAL_GC_INTERVAL", "0")
    assert resolve_wal_gc_interval_seconds() == DEFAULT_WAL_GC_INTERVAL_SECONDS
    monkeypatch.setenv("EAWF_DAEMON_WAL_GC_INTERVAL", "-30")
    assert resolve_wal_gc_interval_seconds() == DEFAULT_WAL_GC_INTERVAL_SECONDS


def test_resolve_wal_gc_interval_unparseable_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage interval value falls back to the default."""
    monkeypatch.setenv("EAWF_DAEMON_WAL_GC_INTERVAL", "not-a-number")
    assert resolve_wal_gc_interval_seconds() == DEFAULT_WAL_GC_INTERVAL_SECONDS


def test_resolve_wal_retention_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing env var falls back to the module default retention window."""
    monkeypatch.delenv("EAWF_DAEMON_WAL_RETENTION", raising=False)
    assert resolve_wal_retention_seconds() == _DEFAULT_RETENTION_SECONDS


def test_resolve_wal_retention_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A positive integer env var overrides the default retention."""
    monkeypatch.setenv("EAWF_DAEMON_WAL_RETENTION", "7200")
    assert resolve_wal_retention_seconds() == 7200


def test_resolve_wal_retention_non_positive_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero / negative retention falls back so GC is not silently disabled."""
    monkeypatch.setenv("EAWF_DAEMON_WAL_RETENTION", "0")
    assert resolve_wal_retention_seconds() == _DEFAULT_RETENTION_SECONDS
    monkeypatch.setenv("EAWF_DAEMON_WAL_RETENTION", "-1")
    assert resolve_wal_retention_seconds() == _DEFAULT_RETENTION_SECONDS


def test_resolve_wal_retention_unparseable_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage retention value falls back to the default."""
    monkeypatch.setenv("EAWF_DAEMON_WAL_RETENTION", "soon")
    assert resolve_wal_retention_seconds() == _DEFAULT_RETENTION_SECONDS
