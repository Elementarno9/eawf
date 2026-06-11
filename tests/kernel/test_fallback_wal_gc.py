"""Tests for the opportunistic fallback-WAL GC (P30-I14-W02).

The daemonless fallback writer (the V1 carve-out: CI / one-shot / recovery
shell) has no background sweep loop, so the repo-local WAL under
``.ea/locks/wal/`` would accrete unbounded. :func:`_maybe_gc_fallback_wal`
piggybacks a single :func:`eawf.runtime.daemon.wal.gc_done_records` sweep onto a
fallback write, throttled by a process-level sentinel to at most once per
interval. The three concerns mirror the wave success criterion:

- A fallback sweep removes aged ``.fsynced.json`` records (reusing the W01 GC
  helper).
- A second sweep WITHIN the throttle interval does NOT re-sweep, even when a
  freshly-aged record is now eligible.
- A sweep AFTER the interval has elapsed re-fires.
- Error / boundary paths: a missing WAL directory is a no-op, and an OSError
  from the underlying sweep is swallowed (GC must not sink the mutation).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.kernel.state import io
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.io import _maybe_gc_fallback_wal
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.wal import (
    WalRecord,
    mark_applied,
    mark_fsynced,
    write_pending,
)

pytestmark = pytest.mark.unit

_INTERVAL_SECONDS = 3600
_RETENTION_SECONDS = 3600


@pytest.fixture(autouse=True)
def _reset_sentinel() -> Iterator[None]:
    """Reset the process-level throttle sentinel around each test.

    The sentinel is module state shared across the whole process; without a
    reset, the first test's sweep would suppress every later test's sweep.
    """
    io._LAST_FALLBACK_WAL_GC_AT = None
    yield
    io._LAST_FALLBACK_WAL_GC_AT = None


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


def _write_fsynced(wal_dir: Path, record_id: str, *, mtime: datetime) -> Path:
    """Persist a fsynced WAL record and pin its mtime to *mtime*."""
    write_pending(wal_dir, _build_record(record_id))
    mark_applied(wal_dir, record_id)
    fsynced = mark_fsynced(wal_dir, record_id)
    stamp = mtime.timestamp()
    os.utime(fsynced, (stamp, stamp))
    return fsynced


def test_maybe_gc_fallback_wal_removes_aged_record(tmp_path: Path) -> None:
    """A first sweep retires an aged fsynced record and keeps a recent one.

    The aged record's mtime sits a full retention window plus a margin behind
    the injected ``now``, so it is past the cutoff; the recent record sits
    inside the window and survives.
    """
    wal_dir = tmp_path / "wal"
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
    aged = _write_fsynced(
        wal_dir, "rec-aged", mtime=now - timedelta(seconds=_RETENTION_SECONDS + 600)
    )
    recent = _write_fsynced(wal_dir, "rec-recent", mtime=now - timedelta(seconds=60))

    removed = _maybe_gc_fallback_wal(
        wal_dir,
        now=now,
        interval_seconds=_INTERVAL_SECONDS,
        retention_seconds=_RETENTION_SECONDS,
    )

    assert removed == [aged]
    assert not aged.exists()
    assert recent.exists()


def test_maybe_gc_fallback_wal_second_write_within_interval_does_not_resweep(
    tmp_path: Path,
) -> None:
    """A second sweep inside the throttle interval is suppressed.

    After the first sweep arms the sentinel, a record aged past the retention
    window between the two calls would be eligible -- but the throttle elapsed
    check (``now - last < interval``) short-circuits before any sweep runs, so
    the newly-aged record survives until the interval passes.
    """
    wal_dir = tmp_path / "wal"
    first_now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
    # First sweep: nothing aged yet, but it arms the sentinel at ``first_now``.
    assert (
        _maybe_gc_fallback_wal(
            wal_dir,
            now=first_now,
            interval_seconds=_INTERVAL_SECONDS,
            retention_seconds=_RETENTION_SECONDS,
        )
        == []
    )

    # A record that is now well past the retention window.
    aged = _write_fsynced(
        wal_dir, "rec-aged", mtime=first_now - timedelta(seconds=_RETENTION_SECONDS + 600)
    )

    # Second call is only half an interval later -- the throttle suppresses it.
    second_now = first_now + timedelta(seconds=_INTERVAL_SECONDS // 2)
    removed = _maybe_gc_fallback_wal(
        wal_dir,
        now=second_now,
        interval_seconds=_INTERVAL_SECONDS,
        retention_seconds=_RETENTION_SECONDS,
    )

    assert removed == []
    assert aged.exists()


def test_maybe_gc_fallback_wal_resweeps_after_interval(tmp_path: Path) -> None:
    """A sweep after the interval has elapsed re-fires and retires the record."""
    wal_dir = tmp_path / "wal"
    first_now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
    _maybe_gc_fallback_wal(
        wal_dir,
        now=first_now,
        interval_seconds=_INTERVAL_SECONDS,
        retention_seconds=_RETENTION_SECONDS,
    )

    aged = _write_fsynced(
        wal_dir, "rec-aged", mtime=first_now - timedelta(seconds=_RETENTION_SECONDS + 600)
    )

    # One full interval plus a second later -- the throttle window has elapsed.
    later_now = first_now + timedelta(seconds=_INTERVAL_SECONDS + 1)
    removed = _maybe_gc_fallback_wal(
        wal_dir,
        now=later_now,
        interval_seconds=_INTERVAL_SECONDS,
        retention_seconds=_RETENTION_SECONDS,
    )

    assert removed == [aged]
    assert not aged.exists()


def test_maybe_gc_fallback_wal_missing_dir_is_noop(tmp_path: Path) -> None:
    """A missing WAL directory sweeps nothing and does not raise.

    Boundary case: the fallback path can fire before any WAL record exists
    (a fresh repo's first mutation). ``gc_done_records`` returns ``[]`` on a
    missing directory, so the opportunistic call is a clean no-op.
    """
    wal_dir = tmp_path / "never-created" / "wal"
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)

    removed = _maybe_gc_fallback_wal(
        wal_dir,
        now=now,
        interval_seconds=_INTERVAL_SECONDS,
        retention_seconds=_RETENTION_SECONDS,
    )

    assert removed == []
    assert not wal_dir.exists()


def test_maybe_gc_fallback_wal_swallows_sweep_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError from the underlying sweep is swallowed, not propagated.

    Error path: GC is a janitor, not part of the durable write, so a sweep
    failure must never sink the mutation that triggered it. The throttle slot
    is still claimed so a persistently-failing sweep does not re-fire on every
    write.
    """
    wal_dir = tmp_path / "wal"
    _write_fsynced(wal_dir, "rec-aged", mtime=datetime(2026, 6, 11, 11, 0, 0, tzinfo=UTC))

    def _boom(*_args: object, **_kwargs: object) -> list[Path]:
        raise OSError("disk gone")

    monkeypatch.setattr("eawf.runtime.daemon.wal.gc_done_records", _boom)

    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
    removed = _maybe_gc_fallback_wal(
        wal_dir,
        now=now,
        interval_seconds=_INTERVAL_SECONDS,
        retention_seconds=_RETENTION_SECONDS,
    )

    assert removed == []
    # The throttle slot is armed even though the sweep failed.
    assert now.timestamp() == io._LAST_FALLBACK_WAL_GC_AT
