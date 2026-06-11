"""Tests for the ``track.*`` JSON-RPC mutators (P30-I11-W02).

Covers the daemon-owned Track lifecycle mutator pair end-to-end:

* :func:`track.add` adds a Track row onto :attr:`State.tracks`, links it
  under the owning :attr:`Project.track_ids`, and moves the
  :attr:`CurrentPointers.track_id` cursor onto it (so an add doubles as a
  switch); the state write + event append + WAL fsync all land.
* :func:`track.switch` moves :attr:`CurrentPointers.track_id` onto an
  existing Track.
* :func:`track.switch` on an unknown id raises a typed
  :class:`TrackMutationError` (a ``ValueError`` subclass) and leaves
  ``state.json`` untouched.
* :func:`track.add` honours the ``PLANNED -> ACTIVE`` status lifecycle:
  the persisted Track carries the supplied :class:`TrackStatus`.

The handlers are driven through the module-level coroutines, matching the
in-process harness in :mod:`tests.daemon.test_state_methods`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.state import (
    TrackMutationError,
    track_add_rpc,
    track_switch_rpc,
)

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _build_state_payload() -> dict[str, object]:
    """Minimal valid State payload with a project + no tracks yet."""
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
        "phases": {},
        "iters": {},
        "waves": {},
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
    """Build a wired :class:`MethodContext` for the track mutator tests."""
    state_path = tmp_path / "state.json"
    if state_payload is None:
        state_payload = _build_state_payload()
    _write_state(state_path, state_payload)
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    ctx = MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
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
    asyncio.run(body())


def _add_params(
    *,
    track_id: str = "ABC-X",
    status: str = "planned",
) -> dict[str, Any]:
    return {
        "track_id": track_id,
        "code": "ABC",
        "slug": "x-track",
        "title": "X Track",
        "kind": "strategy",
        "domains": ["quant"],
        "status": status,
    }


# ---- track.add --------------------------------------------------------------


def test_track_add_persists_state_event_wal_and_moves_cursor(tmp_path: Path) -> None:
    """track.add adds the row, links the project, moves the cursor, fsyncs WAL."""
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        result: dict[str, Any] = await track_add_rpc(ctx, _add_params())
        assert result["track_id"] == "ABC-X"
        assert result["current_track_id"] == "ABC-X"
        assert result["added"] is True

        written = orjson.loads(state_path.read_bytes())
        assert written["tracks"]["ABC-X"]["id"] == "ABC-X"
        assert written["tracks"]["ABC-X"]["kind"] == "strategy"
        # Project -> Track containment edge linked.
        assert written["project"]["track_ids"] == ["ABC-X"]
        # current.track_id cursor moved onto the new track.
        assert written["current"]["track_id"] == "ABC-X"

        # event.jsonl carries exactly one envelope scoped to the track.
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1
        on_disk = orjson.loads(rows[0])
        assert on_disk["kind"] == StoreKind.EVENT.value
        assert on_disk["scope_id"] == "ABC-X"

        # WAL fully fsynced.
        assert list(wal_dir.glob("*.pending.json")) == []
        assert list(wal_dir.glob("*.applied.json")) == []
        assert len(list(wal_dir.glob("*.fsynced.json"))) == 1

    _run(body)


def test_track_add_honours_status_lifecycle(tmp_path: Path) -> None:
    """The persisted Track carries the supplied PLANNED/ACTIVE status."""
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        await track_add_rpc(ctx, _add_params(track_id="ABC-A", status="active"))
        written = orjson.loads(state_path.read_bytes())
        assert written["tracks"]["ABC-A"]["status"] == "active"

    _run(body)


def test_track_add_rejects_duplicate_id(tmp_path: Path) -> None:
    """A second add with the same id raises a typed TrackMutationError."""
    ctx, _state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        await track_add_rpc(ctx, _add_params(track_id="ABC-X"))
        with pytest.raises(ValueError, match="track already exists: 'ABC-X'"):
            await track_add_rpc(ctx, _add_params(track_id="ABC-X"))

    _run(body)


# ---- track.switch -----------------------------------------------------------


def test_track_switch_moves_current_track_id(tmp_path: Path) -> None:
    """add two tracks, then switch back to the first and watch the cursor follow."""
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        await track_add_rpc(ctx, _add_params(track_id="ABC-X"))
        await track_add_rpc(ctx, _add_params(track_id="ABC-Y"))
        # The second add left the cursor on ABC-Y.
        mid = orjson.loads(state_path.read_bytes())
        assert mid["current"]["track_id"] == "ABC-Y"

        result: dict[str, Any] = await track_switch_rpc(ctx, {"track_id": "ABC-X"})
        assert result["track_id"] == "ABC-X"
        assert result["current_track_id"] == "ABC-X"
        assert result["added"] is False

        after = orjson.loads(state_path.read_bytes())
        assert after["current"]["track_id"] == "ABC-X"

    _run(body)


def test_track_switch_unknown_track_raises_typed_error(tmp_path: Path) -> None:
    """Switching to an unknown Track raises a typed error and leaves state untouched."""
    ctx, state_path, event_path, _wal_dir = _build_ctx(tmp_path=tmp_path)
    before = state_path.read_bytes()

    async def body() -> None:
        with pytest.raises(TrackMutationError, match="unknown track: 'NOPE-Z'"):
            await _apply_unknown_switch(ctx)
        # state.json untouched on the rejected switch.
        assert state_path.read_bytes() == before
        # No event row emitted for a rejected mutation.
        assert not event_path.exists() or event_path.read_text().strip() == ""

    _run(body)


async def _apply_unknown_switch(ctx: MethodContext) -> None:
    """Drive the switch apply directly so the raw TrackMutationError surfaces.

    The handler's :func:`_commit_worktree_state` remaps the typed error to a
    plain ``ValueError`` for the wire; this exercises the underlying typed
    raise so the error class itself is pinned, while
    :func:`test_track_switch_unknown_via_rpc_raises_valueerror` covers the
    remapped wire shape.
    """
    from eawf.runtime.daemon.methods.state import (
        TrackSwitchParams,
        _apply_track_switch,
        _read_state,
    )

    state_path = ctx.state_path
    assert isinstance(state_path, Path)
    state, _payload = _read_state(state_path)
    _apply_track_switch(state, TrackSwitchParams(track_id="NOPE-Z"))


def test_track_switch_unknown_via_rpc_raises_valueerror(tmp_path: Path) -> None:
    """Through the RPC, the unknown-track rejection surfaces as a ValueError."""
    ctx, _state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown track: 'NOPE-Z'"):
            await track_switch_rpc(ctx, {"track_id": "NOPE-Z"})

    _run(body)
