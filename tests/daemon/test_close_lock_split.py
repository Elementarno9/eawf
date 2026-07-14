"""D-LOCK-SPLIT: lock-free close pre-flight + optimistic commit (P30-I23-W09).

The root wedge (ZD-R1): the whole close pipeline ran inside one
``portalock.acquire`` hold, so a close whose deterministic gates shell
out for minutes starved every concurrent mutator into LockTimeout.
Post-split the WAVE_CLOSE path runs its pre-flight (deterministic tier +
floor pack + rollup reads) with NO lock and commits under a ms-scale
hold; a concurrent writer that mutates the TARGET WAVE ROW between
pre-flight and lock refuses the close with a typed stale error.

The suite drives the real ``mutate`` coroutine with the pre-flight
seams instrumented — no live daemon, no subprocess.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state import mutate

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
_WAVE = "P30-I23-W09"


def _state_payload() -> dict[str, Any]:
    """A minimal valid State with one CLAIMED wave and an open sibling row."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _T0.isoformat(),
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
            "P30": {
                "id": "P30",
                "scope_id": "ABC",
                "track_id": None,
                "title": "P30",
                "status": "active",
                "iter_ids": ["P30-I23"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I23": {
                "id": "P30-I23",
                "phase_id": "P30",
                "title": "I23",
                "status": "active",
                "wave_ids": [_WAVE, "P30-I23-W99"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE: {
                "id": _WAVE,
                "iter_id": "P30-I23",
                "title": "split wave close into pre-flight and commit",
                "status": "claimed",
                "file_scopes": ["src/x.py"],
                "success_criteria": [],
                "gates": [],
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "sessions": {},
            },
            "P30-I23-W99": {
                "id": "P30-I23-W99",
                "iter_id": "P30-I23",
                "title": "sibling wave for concurrent-mutation probes",
                "status": "pending",
                "file_scopes": ["src/y.py"],
                "success_criteria": [],
                "gates": [],
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "sessions": {},
            },
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(state_path: Path) -> None:
    state = State.model_validate(_state_payload())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")


def _build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at="2026-07-02T00:00:00+00:00",
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


def _close_mutation(wave_id: str = _WAVE) -> dict[str, Any]:
    return {
        "mutation": Mutation(
            kind=MutationKind.WAVE_CLOSE,
            scope_id=wave_id,
            mutation_id=uuid.uuid4().hex,
            params={"wave_id": wave_id, "outcome": "ok", "no_runtime_waiver": True},
        ).model_dump(mode="json")
    }


def _retitle_mutation() -> dict[str, Any]:
    return {
        "mutation": Mutation(
            kind=MutationKind.ROADMAP_REVISE,
            scope_id="P30",
            mutation_id=uuid.uuid4().hex,
            params={"op": "retitle", "wave_id": "P30-I23-W99", "title": "retitled sibling"},
        ).model_dump(mode="json")
    }


#: How long the stubbed pre-flight sleeps, standing in for the minutes-long
#: deterministic gates + floor pack. The assertions compare against THIS rather
#: than a hardcoded wall-clock budget, so the test states the invariant (the
#: gates run outside the lock) instead of a guess about how fast the host writes.
_PREFLIGHT_SLEEP_S = 0.8


def _run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(body())


class _LockHoldRecorder:
    """Wraps portalock.acquire to record each hold's duration AND when it began."""

    def __init__(self, real_acquire: Any) -> None:
        self._real = real_acquire
        self.holds: list[float] = []
        #: Monotonic timestamp of each ``acquire`` entry, so a caller can assert
        #: the lock was taken AFTER the pre-flight finished rather than inferring
        #: it from how long the hold lasted.
        self.acquired_at: list[float] = []

    def __call__(self, path: Path, timeout: float = 5.0) -> Any:
        recorder = self

        class _Ctx:
            def __enter__(self) -> Any:
                self._inner = recorder._real(path, timeout=timeout)
                self._entered = time.monotonic()
                recorder.acquired_at.append(self._entered)
                return self._inner.__enter__()

            def __exit__(self, *exc: Any) -> Any:
                recorder.holds.append(time.monotonic() - self._entered)
                return self._inner.__exit__(*exc)

        return _Ctx()


def test_close_lock_hold_bounded_by_commit_not_gate_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-01: a slow (0.8s) pre-flight never runs inside the state-lock hold.

    The property under test is an ORDER: the shell-outs finish BEFORE
    ``portalock.acquire`` is entered, so the lock hold covers only the commit
    tail. Asserting that order directly is what makes this test honest.

    It used to infer the order from a duration -- "the hold must be under 0.4s"
    -- which is a wall-clock budget on a disk write, not a statement about the
    split. A loaded runner that takes 0.6s to fsync then fails a test whose
    invariant is perfectly intact, and the message blames a regression that did
    not happen. The timing bound survives only as a loose backstop against the
    hold swallowing the pre-flight entirely.
    """
    from eawf.runtime.daemon.methods import state as daemon_state
    from eawf.runtime.lock import portalock
    from eawf.workflow.verify.preflight import ClosePreflight

    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)

    preflight_done: list[float] = []

    async def _slow_preflight(*args: Any, **kwargs: Any) -> ClosePreflight:
        await asyncio.sleep(_PREFLIGHT_SLEEP_S)
        preflight_done.append(time.monotonic())
        return ClosePreflight(evidence=[], readiness=None)

    monkeypatch.setattr(daemon_state, "run_close_preflight", _slow_preflight)
    recorder = _LockHoldRecorder(portalock.acquire)
    monkeypatch.setattr(portalock, "acquire", recorder)

    async def body() -> None:
        await mutate(ctx, _close_mutation())
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_WAVE]["status"] == "closed"

    _run(body)
    assert recorder.holds, "the commit phase never acquired the state lock"
    assert preflight_done, "the pre-flight seam never ran"
    assert min(recorder.acquired_at) >= preflight_done[0], (
        "the state lock was acquired BEFORE the pre-flight finished — the "
        "gates are running under the lock and the split regressed"
    )
    assert max(recorder.holds) < _PREFLIGHT_SLEEP_S, (
        f"lock hold {max(recorder.holds):.2f}s reaches the pre-flight sleep "
        f"({_PREFLIGHT_SLEEP_S}s) — the gate runtime is inside the hold"
    )


def test_close_refused_when_wave_row_changes_during_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-02: a concurrent writer mutating the TARGET WAVE ROW refuses the close.

    The pre-flight seam rewrites the target wave row on disk (standing in
    for a concurrent mutator landing mid-pre-flight); the optimistic
    re-check under the lock must refuse with the typed stale error and
    leave the wave un-closed — the caller's retry re-runs pre-flight.
    """
    from eawf.runtime.daemon.methods import state as daemon_state
    from eawf.workflow.verify.preflight import ClosePreflight

    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)

    async def _mutating_preflight(*args: Any, **kwargs: Any) -> ClosePreflight:
        payload = orjson.loads(state_path.read_bytes())
        payload["waves"][_WAVE]["title"] = "concurrently retitled during pre-flight"
        state_path.write_bytes(orjson.dumps(payload))
        return ClosePreflight(evidence=[], readiness=None)

    monkeypatch.setattr(daemon_state, "run_close_preflight", _mutating_preflight)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="close_preflight_stale"):
            await mutate(ctx, _close_mutation())
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_WAVE]["status"] == "claimed"

    _run(body)


def test_close_tolerates_unrelated_rows_moving_during_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version bump from an UNRELATED row does not refuse the close."""
    from eawf.runtime.daemon.methods import state as daemon_state
    from eawf.workflow.verify.preflight import ClosePreflight

    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)

    async def _sibling_mutating_preflight(*args: Any, **kwargs: Any) -> ClosePreflight:
        payload = orjson.loads(state_path.read_bytes())
        payload["waves"]["P30-I23-W99"]["title"] = "sibling moved mid-pre-flight"
        state_path.write_bytes(orjson.dumps(payload))
        return ClosePreflight(evidence=[], readiness=None)

    monkeypatch.setattr(daemon_state, "run_close_preflight", _sibling_mutating_preflight)

    async def body() -> None:
        await mutate(ctx, _close_mutation())
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_WAVE]["status"] == "closed"
        assert payload["waves"]["P30-I23-W99"]["title"] == "sibling moved mid-pre-flight"

    _run(body)


def test_concurrent_mutate_completes_while_close_preflight_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-03: a concurrent state.mutate lands within its window mid-pre-flight.

    While a close sits in a 1.0s pre-flight (standing in for a 30s gate),
    a concurrent ROADMAP_REVISE mutate must acquire the lock and complete
    well inside its 5s acquire window — pre-split it LockTimeouted.
    """
    from eawf.runtime.daemon.methods import state as daemon_state
    from eawf.workflow.verify.preflight import ClosePreflight

    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)
    sibling_wall: dict[str, float] = {}

    async def _slow_preflight(*args: Any, **kwargs: Any) -> ClosePreflight:
        await asyncio.sleep(1.0)
        return ClosePreflight(evidence=[], readiness=None)

    monkeypatch.setattr(daemon_state, "run_close_preflight", _slow_preflight)

    async def _sibling() -> None:
        await asyncio.sleep(0.2)  # land mid-pre-flight
        started = time.monotonic()
        await mutate(ctx, _retitle_mutation())
        sibling_wall["wall"] = time.monotonic() - started

    async def body() -> None:
        close_result, _ = await asyncio.gather(
            mutate(ctx, _close_mutation()),
            _sibling(),
        )
        assert close_result["idempotent_replay"] is False

    _run(body)
    assert sibling_wall["wall"] < 2.0, (
        f"concurrent mutate took {sibling_wall['wall']:.2f}s — starved by the close hold"
    )
