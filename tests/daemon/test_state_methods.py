"""Tests for the ``state.*`` JSON-RPC handlers (P24-W09).

Covers the W09 mutator path end-to-end:

* :func:`state.read` returns the full state + a stable digest version.
* :func:`state.digest` returns the SHA256 of the on-disk bytes.
* :func:`state.mutate` applies one :class:`MutationKind.WAVE_CLOSE`
  payload, writes the WAL ``.fsynced.json`` record, atomic-writes
  ``state.json``, appends the canonical event envelope to
  ``event.jsonl``, and publishes the envelope on the subscription
  bus.
* :func:`state.mutate` idempotency: a repeat call with the same
  ``idempotency_key`` returns the cached envelope verbatim with
  ``idempotent_replay=True``.
* :func:`state.mutate` rejects an invalid mutation with
  ``ValueError("validation_failed: ...")`` (mapped to ``-32002``).
* :func:`state.mutate` WAL replay path: simulate a crash between
  ``.applied`` and ``.fsynced`` → restart →
  :func:`eawf.daemon.recovery.replay_wal` runs → ``event.jsonl``
  carries the envelope (or stays consistent if it was already
  written) without re-executing the mutator.

The handlers are driven through the module-level coroutines — JSON-RPC
framing is exercised in :mod:`tests.daemon.test_scaffolding`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.daemon import PROTOCOL_VERSION, recovery, wal
from eawf.daemon.bus import EventBus
from eawf.daemon.methods import MethodContext
from eawf.daemon.methods.state import digest, mutate, read
from eawf.daemon.wal import WalStatus
from eawf.state.enums import StoreKind
from eawf.state.mutations import Mutation, MutationKind
from eawf.store.paths import store_path

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _build_state_payload(
    *,
    wave_id: str = "P24-I01-W09",
    wave_status: str = "claimed",
) -> dict[str, object]:
    """Construct a minimal valid State payload with one claimed wave."""
    iter_id = "P24-I01"
    phase_id = "P24"
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
                "subproject_id": None,
                "title": "P24",
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
                "title": "I01",
                "status": "active",
                "wave_ids": [wave_id],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _now().isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            wave_id: {
                "id": wave_id,
                "iter_id": iter_id,
                "title": "test wave",
                "status": wave_status,
                "claim_session_id": "session-abc" if wave_status == "claimed" else None,
                "opened_at": _now().isoformat(),
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _build_ctx(
    *,
    tmp_path: Path,
    state_payload: dict[str, object] | None = None,
) -> tuple[MethodContext, Path, Path, Path]:
    """Build a wired :class:`MethodContext` for the W09 mutator tests.

    Returns the context plus the state / event / wal paths so tests
    can poke at the on-disk artefacts directly.
    """
    state_path = tmp_path / "state.json"
    if state_payload is None:
        state_payload = _build_state_payload()
    _write_state(state_path, state_payload)
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    ctx = MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )
    return ctx, state_path, event_path, wal_dir


def _run(body: Callable[[], Awaitable[None]]) -> None:
    """Run an async test body without ``pytest-asyncio``."""
    asyncio.run(body())


# ---- state.read -------------------------------------------------------------


def test_state_read_returns_state_and_version(tmp_path: Path) -> None:
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        result: dict[str, Any] = await read(ctx, {})
        assert "state" in result and "version" in result
        assert result["state"]["project"]["code"] == "ABC"
        assert isinstance(result["version"], str)
        assert len(result["version"]) == 16

    _run(body)


def test_state_read_rejects_unknown_field(tmp_path: Path) -> None:
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        with pytest.raises(Exception, match=r"(unexpected|extra)"):
            await read(ctx, {"unexpected": "field"})

    _run(body)


# ---- state.digest -----------------------------------------------------------


def test_state_digest_returns_sha256(tmp_path: Path) -> None:
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        first: dict[str, Any] = await digest(ctx, {})
        second: dict[str, Any] = await digest(ctx, {})
        assert first["version"] == second["version"]
        assert len(first["version"]) == 16

    _run(body)


# ---- state.mutate (happy path) ----------------------------------------------


def test_mutate_wave_close_persists_state_event_wal(tmp_path: Path) -> None:
    """End-to-end wave_close: state updated + event appended + WAL fsynced."""
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "test-close"},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert result["idempotent_replay"] is False
        assert len(result["before_version"]) == 16
        assert len(result["after_version"]) == 16
        assert result["before_version"] != result["after_version"]
        envelope = result["event"]
        assert envelope["kind"] == StoreKind.EVENT.value
        assert envelope["scope_id"] == "P24-I01-W09"

        # state.json reflects the close.
        new_state = orjson.loads(state_path.read_bytes())
        assert new_state["waves"]["P24-I01-W09"]["status"] == "closed"
        assert new_state["waves"]["P24-I01-W09"]["outcome"] == "test-close"

        # event.jsonl carries exactly one envelope with the same id.
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1
        on_disk = orjson.loads(rows[0])
        assert on_disk["id"] == envelope["id"]
        assert on_disk["kind"] == StoreKind.EVENT.value

        # WAL record fully fsynced; no pending / applied lingering.
        assert list(wal_dir.glob("*.pending.json")) == []
        assert list(wal_dir.glob("*.applied.json")) == []
        fsynced = list(wal_dir.glob("*.fsynced.json"))
        assert len(fsynced) == 1

    _run(body)


def test_mutate_publishes_to_bus(tmp_path: Path) -> None:
    """The mutator publishes the post-apply envelope on the subscription bus."""
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)
    bus = ctx.bus
    assert isinstance(bus, EventBus)
    sub = bus.register(connection_id="test-sub")

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "test"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        # Subscriber should have received exactly one envelope.
        assert len(sub.queue) == 1

    _run(body)


def test_mutate_last_event_id_updated(tmp_path: Path) -> None:
    """``ctx.last_event_id`` reflects the most recent envelope id."""
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)
    assert ctx.last_event_id == ""
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert ctx.last_event_id == result["event"]["id"]

    _run(body)


# ---- state.mutate (idempotency) ---------------------------------------------


def test_mutate_idempotency_returns_cached_replay(tmp_path: Path) -> None:
    """Repeat call with the same idempotency_key returns the cached result."""
    ctx, _state_path, event_path, _wal_dir = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        idempotency_key="key-1",
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        first: dict[str, Any] = await mutate(
            ctx, {"mutation": mutation.model_dump(mode="json"), "idempotency_key": "key-1"}
        )
        # Build a SECOND mutation with the same key + new mutation_id.
        mutation_two = Mutation(
            kind=MutationKind.WAVE_CLOSE,
            scope_id="P24-I01-W09",
            mutation_id=uuid.uuid4().hex,
            idempotency_key="key-1",
            params={"wave_id": "P24-I01-W09", "outcome": "ok"},
        )
        second: dict[str, Any] = await mutate(
            ctx, {"mutation": mutation_two.model_dump(mode="json"), "idempotency_key": "key-1"}
        )
        assert first["idempotent_replay"] is False
        assert second["idempotent_replay"] is True
        assert first["event"]["id"] == second["event"]["id"]

        # event.jsonl still carries exactly one row (replay did not re-append).
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1

    _run(body)


# ---- state.mutate (validation rejection) -----------------------------------


def test_mutate_unknown_wave_id_rejected(tmp_path: Path) -> None:
    """An unknown wave id raises ``validation_failed``; state.json unchanged."""
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)
    before_bytes = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W99",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W99", "outcome": "nope"},
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        # state.json untouched.
        assert state_path.read_bytes() == before_bytes
        # No event row appended.
        assert not event_path.exists() or event_path.read_text() == ""
        # The WAL pending record SHOULD also be absent — the rejection
        # short-circuited BEFORE write_pending, so no file lingers.
        assert list(wal_dir.glob("*.pending.json")) == []

    _run(body)


def test_mutate_missing_required_param_rejected(tmp_path: Path) -> None:
    """An apply that needs ``outcome`` rejects when the key is missing."""
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09"},  # outcome missing
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})

    _run(body)


def test_mutate_reserved_kind_raises_not_implemented(tmp_path: Path) -> None:
    """A MutationKind whose apply is reserved for C03-IMPL raises cleanly."""
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P24",
        mutation_id=uuid.uuid4().hex,
        params={},
    )

    async def body() -> None:
        with pytest.raises(NotImplementedError, match="not yet wired"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})

    _run(body)


# ---- state.mutate (WAL replay) ---------------------------------------------


def test_replay_wal_applied_record_is_promoted_to_fsynced(tmp_path: Path) -> None:
    """An ``.applied.json`` record left behind on restart is replayed to fsynced.

    Simulates the crash window between :func:`wal.mark_applied` and
    :func:`wal.mark_fsynced`: the event row is already on disk, but the
    WAL still says ``.applied``. Recovery should rename to
    ``.fsynced`` without re-appending the envelope.
    """
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)

    # Run a successful mutate so we have a fsynced WAL record + event row.
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id="record-1",
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        # Roll the WAL state back to ``.applied`` to simulate a crash
        # between mark_applied + mark_fsynced.
        fsynced = list(wal_dir.glob("*.fsynced.json"))
        assert len(fsynced) == 1
        os.replace(fsynced[0], wal_dir / fsynced[0].name.replace(".fsynced.", ".applied."))

        # Now run recovery — should detect the existing event row and
        # promote applied → fsynced without re-appending.
        rows_before = event_path.read_text().strip().splitlines()
        report = recovery.replay_wal(wal_dir, state_path, event_path)
        rows_after = event_path.read_text().strip().splitlines()

        assert report.applied_count == 1
        assert report.replayed_event_count == 0  # envelope already present
        assert rows_before == rows_after
        assert list(wal_dir.glob("*.applied.json")) == []
        assert len(list(wal_dir.glob("*.fsynced.json"))) == 1

    _run(body)


def test_replay_wal_applied_record_replays_missing_event(tmp_path: Path) -> None:
    """An ``.applied.json`` whose envelope is NOT in event.jsonl is replayed.

    Simulates the crash window between :func:`atomic_write_json_locked`
    (state.json fsynced) and :func:`append_envelope` (event.jsonl).
    Recovery appends the envelope from the WAL record to keep state +
    event consistent.
    """
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id="record-2",
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        # Manually:
        # 1. Truncate event.jsonl.
        # 2. Roll the WAL back to ``.applied``.
        # This simulates the crash between state-write and event-append.
        event_path.write_text("")
        fsynced = list(wal_dir.glob("*.fsynced.json"))
        assert len(fsynced) == 1
        os.replace(fsynced[0], wal_dir / fsynced[0].name.replace(".fsynced.", ".applied."))

        report = recovery.replay_wal(wal_dir, state_path, event_path)
        assert report.applied_count == 1
        assert report.replayed_event_count == 1
        # The envelope from the WAL record landed in event.jsonl.
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1
        env = orjson.loads(rows[0])
        assert env["kind"] == StoreKind.EVENT.value
        assert env["scope_id"] == "P24-I01-W09"
        # Record promoted to fsynced.
        assert len(list(wal_dir.glob("*.fsynced.json"))) == 1

    _run(body)


def test_replay_wal_pending_is_poisoned(tmp_path: Path) -> None:
    """A leftover ``.pending.json`` is moved to ``poisoned/`` (never re-run).

    Crash before state.json was written; the mutator outcome was never
    observed, so replay must NOT execute the mutator (the outcome-WAL
    by design captures POST-apply envelopes, so a pending record means
    "apply was attempted but state was not written").
    """
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)

    # Write a synthetic pending record (we don't have a fully-built
    # WalRecord on hand without running mutate; use the wal helper).
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id="record-3",
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        # Run mutate then roll the wal back to .pending.
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        fsynced = list(wal_dir.glob("*.fsynced.json"))
        os.replace(fsynced[0], wal_dir / fsynced[0].name.replace(".fsynced.", ".pending."))
        report = recovery.replay_wal(wal_dir, state_path, event_path)
        assert report.pending_count == 1
        assert (wal_dir / "poisoned").exists()
        poisoned = list((wal_dir / "poisoned").glob("*.poisoned.json"))
        assert len(poisoned) == 1

    _run(body)


# ---- state.mutate (param shape errors) -------------------------------------


def test_mutate_rejects_missing_state_path(tmp_path: Path) -> None:
    """Daemon misconfiguration (no state_path) raises RuntimeError."""
    ctx = MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
    )
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        with pytest.raises(RuntimeError, match="state_path / event_path not configured"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})

    _run(body)


def test_mutate_rejects_unknown_param_field(tmp_path: Path) -> None:
    """An unknown top-level params field is rejected by the schema."""
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await mutate(
                ctx,
                {
                    "mutation": mutation.model_dump(mode="json"),
                    "unexpected": "field",
                },
            )

    _run(body)


# ---- WAL record schema sanity ----------------------------------------------


def test_mutate_wal_record_carries_post_apply_envelope(tmp_path: Path) -> None:
    """The WAL pending → applied → fsynced lifecycle captures the canonical envelope."""
    ctx, _, _, wal_dir = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id="record-x",
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        fsynced = list(wal.list_records(wal_dir, status=WalStatus.FSYNCED))
        assert len(fsynced) == 1
        record = wal.read_record(fsynced[0])
        assert record.record_id == "record-x"
        assert record.envelope.id == result["event"]["id"]
        assert record.before_state_version == result["before_version"]
        assert record.after_state_version == result["after_version"]

    _run(body)
