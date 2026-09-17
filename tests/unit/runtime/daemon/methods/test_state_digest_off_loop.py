"""Tests that ``state.digest`` parses off the loop and dedups under a lock.

The TUI polls ``state.digest`` on a timer, and the handler used to read,
parse and validate the whole ``state.json`` inline on the daemon event
loop: a multi-megabyte state stalled every other in-flight request for
the duration of the poll. These tests pin the two halves of the fix:

* the read + ``orjson`` parse + ``State.model_validate`` run through
  :func:`asyncio.to_thread`, so a coroutine sharing the loop keeps
  ticking while a deliberately slow validation runs, and the returned
  version is still the SHA256 prefix of the raw on-disk bytes;
* the elapsed dedup check-and-set is guarded by a lock, so concurrent
  digests over one claimed wave past a minute boundary append exactly
  one ``wave_elapsed_update`` event.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.state import digest
from eawf.runtime.daemon.methods.state_events import (
    _WAVE_ELAPSED_LAST_MINUTE,
    claim_elapsed_minute,
)

pytestmark = pytest.mark.unit

#: Wall-clock block injected into ``State.model_validate`` to make an
#: on-loop parse observable: a loop that stalls cannot tick.
_SLOW_VALIDATE_SECONDS = 0.5
#: Ticker cadence of the coroutine that shares the loop with the digest.
_TICK_SECONDS = 0.01
#: Conservative floor for the tick count: an off-loop parse yields roughly
#: ``_SLOW_VALIDATE_SECONDS / _TICK_SECONDS`` ticks, an on-loop parse one.
_MIN_TICKS = 10


def _now() -> datetime:
    return datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


def _build_state_payload(*, claimed_minutes_ago: float | None = 45.0) -> dict[str, Any]:
    """Build a minimal valid State payload with one claimed wave.

    Args:
        claimed_minutes_ago: Minutes before now that the wave was claimed,
            or ``None`` to omit ``claimed_at`` entirely.
    """
    phase_id = "P24"
    iter_id = "P24-I01"
    wave_id = "P24-I01-W09"
    wave: dict[str, Any] = {
        "id": wave_id,
        "iter_id": iter_id,
        "title": "test wave",
        "status": "claimed",
        "effort_bucket": "M",
        "success_criteria": [
            {
                "id": "CR-01",
                "text": "the digest poll keeps the daemon event loop responsive",
                "kind": "deterministic",
                "acceptance_style": "binary",
                "evidence_kind": "deterministic",
                "gate_ids": [],
                "required": True,
                "quality_dimension": "performance_efficiency",
                "measurable_signal": "the focused daemon test observes a ticking loop",
            }
        ],
        "claim_session_id": "session-abc",
        "opened_at": _now().isoformat(),
        "sessions": {},
    }
    if claimed_minutes_ago is not None:
        claimed_at = datetime.now(UTC) - timedelta(minutes=claimed_minutes_ago)
        wave["claimed_at"] = claimed_at.isoformat()
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {
            phase_id: {
                "id": phase_id,
                "scope_id": "ABC",
                "track_id": None,
                "title": "Digest hygiene",
                "status": "active",
                "iter_ids": [iter_id],
                "outcome_ids": [],
                "opened_at": _now().isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            iter_id: {
                "id": iter_id,
                "phase_id": phase_id,
                "title": "First iter",
                "status": "active",
                "wave_ids": [wave_id],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _now().isoformat(),
                "closed_at": None,
            }
        },
        "waves": {wave_id: wave},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _build_ctx(
    *,
    tmp_path: Path,
    raw: bytes | None,
) -> tuple[MethodContext, Path, Path]:
    """Wire a :class:`MethodContext` over *raw* state bytes.

    Args:
        tmp_path: Per-test directory.
        raw: Bytes to write to ``state.json``, or ``None`` to leave the
            file absent.

    Returns:
        The context plus the state and event paths.
    """
    state_path = tmp_path / "state.json"
    if raw is not None:
        state_path.write_bytes(raw)
    event_path = store_path(state_path, StoreKind.EVENT)
    ctx = MethodContext(
        started_at="2026-09-17T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
        wal_dir=tmp_path / "wal",
        idempotency_cache={},
    )
    return ctx, state_path, event_path


def _run(body: Callable[[], Awaitable[None]]) -> None:
    """Run an async test body without ``pytest-asyncio``."""
    asyncio.run(body())


def _event_rows(event_path: Path) -> list[dict[str, Any]]:
    """Return every appended event row, or an empty list when none exist."""
    if not event_path.exists():
        return []
    text = event_path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    rows: list[dict[str, Any]] = [orjson.loads(line) for line in text.splitlines()]
    return rows


@pytest.fixture(autouse=True)
def _clear_elapsed_cache() -> Iterator[None]:
    """Isolate the daemon-local elapsed cache between tests.

    The cache is keyed by ``id(ctx)``, which CPython recycles once a
    context is collected, so a stale entry could otherwise suppress a
    later test's elapsed update.
    """
    _WAVE_ELAPSED_LAST_MINUTE.clear()
    yield
    _WAVE_ELAPSED_LAST_MINUTE.clear()


# ---- Parse off the loop ------------------------------------------------------


def test_digest_parse_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow validation must not stop a co-scheduled coroutine ticking."""
    raw = orjson.dumps(_build_state_payload(claimed_minutes_ago=None))
    ctx, _state_path, _event_path = _build_ctx(tmp_path=tmp_path, raw=raw)
    real_validate = State.model_validate

    def slow_validate(obj: Any, *args: Any, **kwargs: Any) -> State:
        time.sleep(_SLOW_VALIDATE_SECONDS)
        return real_validate(obj, *args, **kwargs)

    monkeypatch.setattr(State, "model_validate", staticmethod(slow_validate))

    async def body() -> None:
        ticks = 0
        task = asyncio.ensure_future(digest(ctx, {}))
        while not task.done():
            await asyncio.sleep(_TICK_SECONDS)
            ticks += 1
        result: dict[str, Any] = await task
        assert ticks >= _MIN_TICKS
        assert result["version"] == hashlib.sha256(raw).hexdigest()[:16]

    _run(body)


def test_digest_returns_sha256_prefix_of_raw_bytes(tmp_path: Path) -> None:
    """The off-loop path still digests the raw bytes, not the parsed model."""
    raw = orjson.dumps(_build_state_payload(claimed_minutes_ago=None))
    ctx, _state_path, _event_path = _build_ctx(tmp_path=tmp_path, raw=raw)

    async def body() -> None:
        result: dict[str, Any] = await digest(ctx, {})
        assert result["version"] == hashlib.sha256(raw).hexdigest()[:16]
        assert len(result["version"]) == 16

    _run(body)


def test_malformed_state_digests_without_publishing(tmp_path: Path) -> None:
    """A state file that cannot parse yields a version and no elapsed event."""
    raw = b"{not json"
    ctx, _state_path, event_path = _build_ctx(tmp_path=tmp_path, raw=raw)

    async def body() -> None:
        result: dict[str, Any] = await digest(ctx, {})
        assert result["version"] == hashlib.sha256(raw).hexdigest()[:16]
        assert _event_rows(event_path) == []

    _run(body)


def test_schema_invalid_state_digests_without_publishing(tmp_path: Path) -> None:
    """A payload that parses but fails validation publishes nothing."""
    payload = _build_state_payload()
    payload.pop("project")
    raw = orjson.dumps(payload)
    ctx, _state_path, event_path = _build_ctx(tmp_path=tmp_path, raw=raw)

    async def body() -> None:
        result: dict[str, Any] = await digest(ctx, {})
        assert result["version"] == hashlib.sha256(raw).hexdigest()[:16]
        assert _event_rows(event_path) == []

    _run(body)


def test_absent_state_returns_empty_digest(tmp_path: Path) -> None:
    """An uninitialised project digests as empty bytes instead of faulting."""
    ctx, state_path, event_path = _build_ctx(tmp_path=tmp_path, raw=None)
    assert not state_path.exists()

    async def body() -> None:
        result: dict[str, Any] = await digest(ctx, {})
        assert result["version"] == hashlib.sha256(b"").hexdigest()[:16]
        assert _event_rows(event_path) == []

    _run(body)


# ---- One elapsed update per minute boundary ----------------------------------


def test_concurrent_digests_emit_one_elapsed_update_per_minute(tmp_path: Path) -> None:
    """Eight concurrent digests over one claimed wave append one event."""
    raw = orjson.dumps(_build_state_payload(claimed_minutes_ago=45.0))
    ctx, _state_path, event_path = _build_ctx(tmp_path=tmp_path, raw=raw)
    bus = ctx.bus
    assert isinstance(bus, EventBus)
    sub = bus.register(connection_id="elapsed-sub")

    async def body() -> None:
        results = await asyncio.gather(*(digest(ctx, {}) for _ in range(8)))
        assert {row["version"] for row in results} == {hashlib.sha256(raw).hexdigest()[:16]}
        rows = _event_rows(event_path)
        assert len(rows) == 1
        payload = rows[0]["payload"]
        assert payload["event_type"] == "wave_elapsed_update"
        assert payload["event_kind"] == "wave_elapsed_update"
        assert payload["extras"]["wave_id"] == "P24-I01-W09"
        assert payload["extras"]["elapsed_minute"] >= 45
        assert len(sub.queue) == 1

    _run(body)


def test_digest_below_one_minute_emits_no_elapsed_update(tmp_path: Path) -> None:
    """A wave claimed seconds ago has not crossed a minute boundary."""
    raw = orjson.dumps(_build_state_payload(claimed_minutes_ago=0.25))
    ctx, _state_path, event_path = _build_ctx(tmp_path=tmp_path, raw=raw)

    async def body() -> None:
        await digest(ctx, {})
        assert _event_rows(event_path) == []

    _run(body)


def test_claim_elapsed_minute_claims_each_boundary_once(tmp_path: Path) -> None:
    """The check-and-set admits the first caller per minute and no other."""
    ctx, _state_path, _event_path = _build_ctx(tmp_path=tmp_path, raw=None)
    key = "/repo/.ea/state.json:P24-I01-W09"

    assert claim_elapsed_minute(ctx=ctx, cache_key=key, elapsed_minute=1) is True
    assert claim_elapsed_minute(ctx=ctx, cache_key=key, elapsed_minute=1) is False
    assert claim_elapsed_minute(ctx=ctx, cache_key=key, elapsed_minute=2) is True
    assert claim_elapsed_minute(ctx=ctx, cache_key=f"{key}-other", elapsed_minute=1) is True


def test_claim_elapsed_minute_admits_one_of_eight_threads(tmp_path: Path) -> None:
    """Eight threads racing the same minute boundary: exactly one wins.

    Exercises the lock directly, since the handler-level concurrency test
    can only interleave at the loop's await points.
    """
    ctx, _state_path, _event_path = _build_ctx(tmp_path=tmp_path, raw=None)
    key = "/repo/.ea/state.json:P24-I01-W09"
    barrier = threading.Barrier(8)
    claims: list[bool] = []
    claims_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        won = claim_elapsed_minute(ctx=ctx, cache_key=key, elapsed_minute=7)
        with claims_lock:
            claims.append(won)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert len(claims) == 8
    assert sum(claims) == 1
