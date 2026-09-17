"""Daemon state writers refuse a ``state.json`` restored to an older copy.

A ``git checkout`` (or a pre-commit stash restore) over ``state.json`` puts
back a file whose ``updated_at`` predates the daemon's own latest write. A
daemon writer that built on that copy would silently discard every daemon
write the copy predates, so ``commit_worktree_state``, both ``state.mutate``
commit paths and the dispatch-pause toggle refuse it with ``state_regressed``
and write nothing: the state bytes, the WAL and the event log all stay as
they were. Successive daemon writes, CLI fallback writes landing in between,
and a restarted daemon (a fresh context) are all accepted.

Every test drives the real coroutines and writers against a
:class:`MethodContext` whose files live under ``tmp_path`` -- no live daemon,
no subprocess.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import (
    STATE_REGRESSED,
    DaemonValidationError,
    MethodContext,
    StateRegressedError,
)
from eawf.runtime.daemon.methods import state as daemon_state
from eawf.runtime.daemon.methods.agent import pause, resume
from eawf.runtime.daemon.methods.state import mutate
from eawf.runtime.daemon.methods.state_worktree import commit_worktree_state
from eawf.surfaces.cli import _mutation
from eawf.workflow.verify.preflight import ClosePreflight

pytestmark = pytest.mark.unit

_SEEDED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_WAVE = "P40-I01-W01"
_SIBLING = "P40-I01-W02"


def _wave_row(wave_id: str) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": "P40-I01",
        "title": f"probe daemon writers with wave {wave_id}",
        "status": "claimed",
        "file_scopes": ["src/x.py"],
        "success_criteria": [],
        "gates": [],
        "effort_bucket": "S",
        "agent_role": "executor",
        "opened_at": _SEEDED_AT.isoformat(),
        "sessions": {},
    }


def _state_payload() -> dict[str, Any]:
    """A minimal valid State with two CLAIMED waves, stamped in the past."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _SEEDED_AT.isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {
            "P40": {
                "id": "P40",
                "scope_id": "ABC",
                "track_id": None,
                "title": "P40",
                "status": "active",
                "iter_ids": ["P40-I01"],
                "outcome_ids": [],
                "opened_at": _SEEDED_AT.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P40-I01": {
                "id": "P40-I01",
                "phase_id": "P40",
                "title": "I01",
                "status": "active",
                "wave_ids": [_WAVE, _SIBLING],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _SEEDED_AT.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {_WAVE: _wave_row(_WAVE), _SIBLING: _wave_row(_SIBLING)},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _seed_state(repo_root: Path) -> Path:
    """Write the seeded state under *repo_root* and return its path."""
    state_path = repo_root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = State.model_validate(_state_payload())
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


def _build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    """A daemon context as a freshly (re)started daemon process holds it."""
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at="2026-09-01T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=store_path(state_path, StoreKind.EVENT),
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )


def _run(coro_fn: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    return asyncio.run(coro_fn())


def _mutation_params(kind: MutationKind, wave_id: str, **params: Any) -> dict[str, Any]:
    return {
        "mutation": Mutation(
            kind=kind,
            scope_id=wave_id,
            mutation_id=uuid.uuid4().hex,
            params={"wave_id": wave_id, **params},
        ).model_dump(mode="json")
    }


def _fail(ctx: MethodContext, wave_id: str) -> dict[str, Any]:
    """Drive the single-lock ``state.mutate`` commit path."""
    params = _mutation_params(MutationKind.WAVE_FAIL, wave_id, reason="a gate went red")
    return _run(lambda: mutate(ctx, params))


def _close(ctx: MethodContext, wave_id: str) -> dict[str, Any]:
    """Drive the split pre-flight / commit ``state.mutate`` close path."""
    params = _mutation_params(
        MutationKind.WAVE_CLOSE, wave_id, outcome="ok", no_runtime_waiver=True
    )
    return _run(lambda: mutate(ctx, params))


def _stub_close_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the lock-free close pre-flight with a no-op pass."""

    async def _preflight(*args: Any, **kwargs: Any) -> ClosePreflight:
        return ClosePreflight(evidence=[], readiness=None)

    monkeypatch.setattr(daemon_state, "run_close_preflight", _preflight)


def _commit(
    ctx: MethodContext,
    *,
    repo_root: Path | None = None,
    applied: list[str] | None = None,
) -> dict[str, Any]:
    """Run a no-op daemon mutator through ``commit_worktree_state``."""

    def _apply(state: State) -> dict[str, Any]:
        if applied is not None:
            applied.append(state.urn)
        return {"ok": True}

    return commit_worktree_state(
        ctx=ctx,
        repo_root=repo_root,
        params={},
        command="state.track_sync",
        scope_id=None,
        apply_func=_apply,
    )


def _snapshot(state_path: Path, wal_dir: Path) -> tuple[bytes, list[tuple[str, bytes]], bytes]:
    """Return the state bytes, every WAL file's bytes and the event-log bytes."""
    event_path = store_path(state_path, StoreKind.EVENT)
    events = event_path.read_bytes() if event_path.exists() else b""
    wal_files = sorted(
        (str(path.relative_to(wal_dir)), path.read_bytes())
        for path in wal_dir.rglob("*")
        if path.is_file()
    )
    return state_path.read_bytes(), wal_files, events


def _updated_at(state_path: Path) -> datetime:
    return State.model_validate_json(state_path.read_bytes()).updated_at


def _cli_write(state_path: Path, *, stamp: bool) -> None:
    """Write through the CLI fallback, optionally stamping a fresh ``updated_at``."""
    _mutation.set_daemonless_flag(False)
    with _mutation.state_transaction(state_path) as state:
        state.dispatch_paused = True
        if stamp:
            state.updated_at = datetime.now(UTC)


def _assert_regressed(excinfo: pytest.ExceptionInfo[StateRegressedError]) -> None:
    """Assert the refusal is the typed one and names the restart escape."""
    message = str(excinfo.value)
    assert excinfo.value.code == STATE_REGRESSED
    assert message.startswith(f"validation_failed: {STATE_REGRESSED}: ")
    assert "eawf daemon restart" in message


# ---- commit_worktree_state ---------------------------------------------------


def test_commit_worktree_state_refuses_regressed_state_file(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path)
    restored = state_path.read_bytes()
    ctx = _build_ctx(tmp_path, state_path)
    _commit(ctx)
    state_path.write_bytes(restored)
    before = _snapshot(state_path, tmp_path / "wal")
    applied: list[str] = []

    with pytest.raises(StateRegressedError) as excinfo:
        _commit(ctx, applied=applied)

    _assert_regressed(excinfo)
    assert applied == []
    assert _snapshot(state_path, tmp_path / "wal") == before


def test_commit_worktree_state_accepts_successive_writes(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path)
    ctx = _build_ctx(tmp_path, state_path)

    _commit(ctx)
    first = _updated_at(state_path)
    _commit(ctx)

    assert _updated_at(state_path) >= first > _SEEDED_AT
    assert ctx.state_written_at[state_path.resolve()] == _updated_at(state_path)


def test_commit_worktree_state_accepts_later_cli_write(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path)
    ctx = _build_ctx(tmp_path, state_path)
    _commit(ctx)
    daemon_written = _updated_at(state_path)

    _cli_write(state_path, stamp=True)
    cli_written = _updated_at(state_path)
    _commit(ctx)

    assert cli_written >= daemon_written
    assert _updated_at(state_path) >= cli_written
    assert State.model_validate_json(state_path.read_bytes()).dispatch_paused is True


def test_commit_worktree_state_accepts_cli_write_keeping_updated_at(tmp_path: Path) -> None:
    """The boundary case: an equal stamp lost nothing, so it passes."""
    state_path = _seed_state(tmp_path)
    ctx = _build_ctx(tmp_path, state_path)
    _commit(ctx)
    daemon_written = _updated_at(state_path)

    _cli_write(state_path, stamp=False)
    assert _updated_at(state_path) == daemon_written
    _commit(ctx)

    assert State.model_validate_json(state_path.read_bytes()).dispatch_paused is True


def test_commit_worktree_state_accepts_restored_file_after_daemon_restart(
    tmp_path: Path,
) -> None:
    state_path = _seed_state(tmp_path)
    restored = state_path.read_bytes()
    ctx = _build_ctx(tmp_path, state_path)
    _commit(ctx)
    state_path.write_bytes(restored)

    restarted = _build_ctx(tmp_path, state_path)
    _commit(restarted)

    assert _updated_at(state_path) > _SEEDED_AT


def test_commit_worktree_state_matches_repo_root_and_bound_path(tmp_path: Path) -> None:
    """A repo root spelled with ``.`` / ``..`` resolves onto the same entry."""
    state_path = _seed_state(tmp_path)
    restored = state_path.read_bytes()
    ctx = _build_ctx(tmp_path, state_path)
    _commit(ctx)
    state_path.write_bytes(restored)

    with pytest.raises(StateRegressedError) as excinfo:
        _commit(ctx, repo_root=tmp_path / "." / ".ea" / "..")

    _assert_regressed(excinfo)
    assert state_path.read_bytes() == restored


def test_commit_worktree_state_tracks_each_state_path_separately(tmp_path: Path) -> None:
    bound_path = _seed_state(tmp_path / "bound")
    other_root = tmp_path / "other"
    other_path = _seed_state(other_root)
    ctx = _build_ctx(tmp_path, bound_path)
    _commit(ctx)

    _commit(ctx, repo_root=other_root)

    assert _updated_at(other_path) > _SEEDED_AT
    assert set(ctx.state_written_at) == {bound_path.resolve(), other_path.resolve()}


# ---- state.mutate ------------------------------------------------------------


def test_mutate_single_lock_refuses_regressed_state_file(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path)
    restored = state_path.read_bytes()
    ctx = _build_ctx(tmp_path, state_path)
    _fail(ctx, _WAVE)
    state_path.write_bytes(restored)
    before = _snapshot(state_path, tmp_path / "wal")

    with pytest.raises(StateRegressedError) as excinfo:
        _fail(ctx, _SIBLING)

    _assert_regressed(excinfo)
    assert _snapshot(state_path, tmp_path / "wal") == before


def test_mutate_single_lock_accepts_successive_writes(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path)
    ctx = _build_ctx(tmp_path, state_path)

    _fail(ctx, _WAVE)
    _cli_write(state_path, stamp=True)
    _fail(ctx, _SIBLING)

    waves = State.model_validate_json(state_path.read_bytes()).waves
    assert (waves[_WAVE].status.value, waves[_SIBLING].status.value) == ("failed", "failed")


def test_mutate_wave_close_refuses_regressed_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _seed_state(tmp_path)
    restored = state_path.read_bytes()
    ctx = _build_ctx(tmp_path, state_path)
    _stub_close_preflight(monkeypatch)
    _fail(ctx, _SIBLING)
    state_path.write_bytes(restored)
    before = _snapshot(state_path, tmp_path / "wal")

    with pytest.raises(StateRegressedError) as excinfo:
        _close(ctx, _WAVE)

    _assert_regressed(excinfo)
    assert ctx.active_lock_handle is None
    assert _snapshot(state_path, tmp_path / "wal") == before


def test_mutate_wave_close_records_its_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _seed_state(tmp_path)
    restored = state_path.read_bytes()
    ctx = _build_ctx(tmp_path, state_path)
    _stub_close_preflight(monkeypatch)
    _close(ctx, _WAVE)
    assert State.model_validate_json(state_path.read_bytes()).waves[_WAVE].status.value == "closed"
    state_path.write_bytes(restored)

    with pytest.raises(StateRegressedError):
        _fail(ctx, _SIBLING)

    assert state_path.read_bytes() == restored


# ---- agent.pause / agent.resume ----------------------------------------------


def test_set_dispatch_paused_refuses_regressed_state_file(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path)
    restored = state_path.read_bytes()
    ctx = _build_ctx(tmp_path, state_path)
    _run(lambda: pause(ctx, {}))
    state_path.write_bytes(restored)
    before = _snapshot(state_path, tmp_path / "wal")
    last_event_id = ctx.last_event_id

    with pytest.raises(StateRegressedError) as excinfo:
        _run(lambda: resume(ctx, {"repo_root": str(tmp_path)}))

    _assert_regressed(excinfo)
    assert _snapshot(state_path, tmp_path / "wal") == before
    assert ctx.last_event_id == last_event_id


def test_set_dispatch_paused_accepts_successive_toggles(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path)
    ctx = _build_ctx(tmp_path, state_path)

    _run(lambda: pause(ctx, {}))
    _cli_write(state_path, stamp=True)
    result = _run(lambda: resume(ctx, {}))

    assert result == {"paused": False}
    assert State.model_validate_json(state_path.read_bytes()).dispatch_paused is False
    event_rows = store_path(state_path, StoreKind.EVENT).read_bytes().splitlines()
    assert len(event_rows) == 2


# ---- MethodContext guard -----------------------------------------------------


def test_refuse_regressed_state_passes_path_never_written(tmp_path: Path) -> None:
    """The empty case: nothing written yet, so nothing can have regressed."""
    state_path = tmp_path / ".ea" / "state.json"
    ctx = _build_ctx(tmp_path, state_path)

    ctx.refuse_regressed_state(state_path, updated_at=_SEEDED_AT)

    assert ctx.state_written_at == {}


def test_refuse_regressed_state_passes_equal_and_refuses_one_microsecond_older(
    tmp_path: Path,
) -> None:
    """The off-by-one case: equal passes, the smallest step back refuses."""
    state_path = tmp_path / ".ea" / "state.json"
    ctx = _build_ctx(tmp_path, state_path)
    ctx.note_state_written(state_path, updated_at=_SEEDED_AT)

    ctx.refuse_regressed_state(state_path, updated_at=_SEEDED_AT)
    with pytest.raises(StateRegressedError) as excinfo:
        ctx.refuse_regressed_state(state_path, updated_at=_SEEDED_AT - timedelta(microseconds=1))

    _assert_regressed(excinfo)


def test_refuse_regressed_state_rejects_a_naive_stamp(tmp_path: Path) -> None:
    """A tz-naive stamp is not comparable; the guard raises rather than pass."""
    state_path = tmp_path / ".ea" / "state.json"
    ctx = _build_ctx(tmp_path, state_path)
    ctx.note_state_written(state_path, updated_at=_SEEDED_AT)

    with pytest.raises(TypeError):
        ctx.refuse_regressed_state(state_path, updated_at=datetime(2026, 1, 1, 12, 0, 0))


def test_note_state_written_keeps_latest_write_when_clock_steps_back(tmp_path: Path) -> None:
    """A backwards clock step must not wedge the daemon into refusing forever."""
    state_path = tmp_path / ".ea" / "state.json"
    ctx = _build_ctx(tmp_path, state_path)
    ctx.note_state_written(state_path, updated_at=_SEEDED_AT)
    earlier = _SEEDED_AT - timedelta(seconds=30)

    ctx.note_state_written(state_path, updated_at=earlier)
    ctx.refuse_regressed_state(state_path, updated_at=earlier)

    assert ctx.state_written_at[state_path.resolve()] == earlier


def test_state_regressed_error_maps_to_validation_failure() -> None:
    error = StateRegressedError(f"validation_failed: {STATE_REGRESSED}: restored")

    assert isinstance(error, DaemonValidationError)
    assert StateRegressedError.code == "state_regressed"
