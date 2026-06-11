"""Tests for the ``sync_track_outcomes`` reducer + wave-close hook (P30-I11-W07).

Covers the lifecycle reducer that recomputes a Track's measured outcome
statuses from their samples (via the I11-W06 ``compute_outcome_status``
comparator) and the wave-close hook that fires it:

* The pure reducer re-derives every reachable outcome's persisted status from
  its recorded sample, flips a stale ``met``/``missed`` when the comparator
  disagrees, and returns the ids that moved.
* The reducer is a no-op for an unmeasured (PENDING) outcome, a non-comparable
  direction (EQUAL/RANGE), and an unknown Track id.
* The daemon WAVE_CLOSE mutation fires the hook: closing a wave whose owning
  Track holds an outcome with a now-stale status updates that status on disk.
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
from eawf.kernel.state.enums import OutcomeDirection, OutcomeStatus, StoreKind
from eawf.kernel.state.models import Goal, Outcome, State, Track
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.state import mutate
from eawf.workflow.evidence.outcome import sync_track_outcomes

pytestmark = pytest.mark.integration


def _now() -> datetime:
    return datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


# --- shared builders ---------------------------------------------------------


def _outcome(
    *,
    outcome_id: str = "OUT-1",
    direction: OutcomeDirection = OutcomeDirection.MAX,
    threshold: float = 1.0,
    sample: float | None = 1.5,
    best_value: float | None = 1.5,
    status: OutcomeStatus = OutcomeStatus.MET,
) -> Outcome:
    """Build a measured Outcome whose persisted status may be stale.

    The model invariant forbids a measured outcome (terminal status + sample)
    with no evidence ref, so a measured one always carries an evidence ref.
    """
    evidence = [] if status is OutcomeStatus.PENDING and sample is None else ["repo:.ea/x.md"]
    return Outcome(
        id=outcome_id,
        scope_id="QR-X",
        metric="sharpe",
        threshold=threshold,
        direction=direction,
        value=sample,
        sample=sample,
        best_value=best_value,
        status=status,
        audit_id=None if sample is None else "AUD-1",
        evidence_refs=evidence,
        updated_at=_now(),
    )


def _state_with_track(outcome: Outcome, *, track_id: str = "QR-X") -> State:
    """Build a valid State carrying a Track -> Goal -> Outcome chain."""
    goal = Goal(
        id="GOAL-1",
        scope_id=track_id,
        title="Beat the sharpe floor",
        summary="The strategy must clear its sharpe threshold on the eval set.",
        status="open",
        outcome_ids=[outcome.id],
        created_at=_now(),
    )
    track = Track(
        id=track_id,
        code="QR",
        slug="x-track",
        title="X Strategy",
        kind="strategy",
        domains=["quant"],
        status="active",
        goal_ids=[goal.id],
    )
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "QR",
            "slug": "quant-research",
            "title": "Quant Research",
            "description": None,
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
            "track_ids": [track_id],
        },
        "current": {
            "project_code": "QR",
            "track_id": track_id,
            "phase_id": "P01",
            "iter_id": "P01-I01",
            "active_wave_ids": ["P01-I01-W01"],
            "active_session_ids": [],
        },
        "workspace": None,
        "tracks": {track_id: track.model_dump(mode="json")},
        "goals": {goal.id: goal.model_dump(mode="json")},
        "outcomes": {outcome.id: outcome.model_dump(mode="json")},
        "phases": {
            "P01": {
                "id": "P01",
                "scope_id": "QR",
                "track_id": track_id,
                "title": "Bootstrap",
                "status": "active",
                "iter_ids": ["P01-I01"],
                "outcome_ids": [],
                "opened_at": _now().isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P01-I01": {
                "id": "P01-I01",
                "phase_id": "P01",
                "title": "First iter",
                "status": "active",
                "wave_ids": ["P01-I01-W01"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _now().isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            "P01-I01-W01": {
                "id": "P01-I01-W01",
                "iter_id": "P01-I01",
                "title": "Move the sharpe metric",
                "status": "in_progress",
                "file_scopes": ["src/x/"],
                "opened_at": _now().isoformat(),
                "outcome": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


# --- pure reducer ------------------------------------------------------------


def test_sync_flips_stale_met_to_missed() -> None:
    """A persisted MET whose sample no longer clears the threshold flips to MISSED.

    The recorded sample (0.4) is below the MAX threshold (1.0), so the
    comparator derives UNMET -> persisted MISSED; the stale MET is corrected.
    """
    state = _state_with_track(
        _outcome(threshold=1.0, sample=0.4, best_value=0.4, status=OutcomeStatus.MET)
    )
    changed = sync_track_outcomes(state, track_id="QR-X")
    assert changed == ["OUT-1"]
    assert state.outcomes["OUT-1"].status is OutcomeStatus.MISSED


def test_sync_flips_stale_missed_to_met() -> None:
    """A persisted MISSED whose sample now clears the threshold flips to MET."""
    state = _state_with_track(
        _outcome(threshold=1.0, sample=1.5, best_value=1.5, status=OutcomeStatus.MISSED)
    )
    changed = sync_track_outcomes(state, track_id="QR-X")
    assert changed == ["OUT-1"]
    assert state.outcomes["OUT-1"].status is OutcomeStatus.MET


def test_sync_noop_when_status_already_current() -> None:
    """A correct persisted status is not rewritten and is not reported changed."""
    state = _state_with_track(
        _outcome(threshold=1.0, sample=1.5, best_value=1.5, status=OutcomeStatus.MET)
    )
    assert sync_track_outcomes(state, track_id="QR-X") == []
    assert state.outcomes["OUT-1"].status is OutcomeStatus.MET


def test_sync_skips_unmeasured_pending_outcome() -> None:
    """A PENDING (no-sample) outcome is left untouched -- the reducer invents nothing."""
    state = _state_with_track(
        _outcome(sample=None, best_value=None, status=OutcomeStatus.PENDING)
    )
    assert sync_track_outcomes(state, track_id="QR-X") == []
    assert state.outcomes["OUT-1"].status is OutcomeStatus.PENDING


def test_sync_skips_non_comparable_direction() -> None:
    """An EQUAL-direction outcome is skipped rather than crashing the comparator."""
    state = _state_with_track(
        _outcome(direction=OutcomeDirection.EQUAL, status=OutcomeStatus.MET)
    )
    assert sync_track_outcomes(state, track_id="QR-X") == []
    assert state.outcomes["OUT-1"].status is OutcomeStatus.MET


def test_sync_unknown_track_is_noop() -> None:
    """An unknown Track id is a no-op (no Track -> no outcomes to sync)."""
    state = _state_with_track(_outcome())
    assert sync_track_outcomes(state, track_id="NOPE-Z") == []


def test_sync_skips_outcome_unreachable_from_track() -> None:
    """An outcome not linked under the Track's goals is left untouched.

    Boundary: only outcomes reachable via ``Track -> Goal -> Outcome`` are
    synced, so an orphan outcome (not in any of the Track's goals) is skipped
    even when its persisted status is stale.
    """
    state = _state_with_track(
        _outcome(threshold=1.0, sample=0.4, best_value=0.4, status=OutcomeStatus.MET)
    )
    # Drop the goal->outcome link so the outcome is no longer reachable.
    goals = dict(state.goals or {})
    goals["GOAL-1"] = goals["GOAL-1"].model_copy(update={"outcome_ids": []})
    state.goals = goals
    assert sync_track_outcomes(state, track_id="QR-X") == []
    assert state.outcomes["OUT-1"].status is OutcomeStatus.MET


# --- wave-close hook (end-to-end through the daemon mutate RPC) ---------------


def _write_state(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.model_dump(mode="json")
    path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
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


def _close_mutation(wave_id: str) -> Mutation:
    # ``no_runtime_waiver`` waives the zero-EU close guard so the advisory
    # close proceeds without a captured runtime (the test is about the Track
    # sync, not runtime capture).
    return Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=wave_id,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": wave_id, "outcome": "ok", "no_runtime_waiver": True},
    )


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def test_wave_close_syncs_owning_track_outcome(tmp_path: Path) -> None:
    """Closing a wave updates its owning Track's stale outcome status on disk.

    The wave's iter -> phase is tagged ``track_id=QR-X`` (the P30-I11-W03
    silent phase-tag), and that Track holds an outcome whose persisted MET no
    longer matches its sub-threshold sample. Closing the wave fires the
    wave-close hook, which recomputes the outcome status to MISSED and persists
    it through the canonical daemon writer.
    """
    state = _state_with_track(
        _outcome(threshold=1.0, sample=0.4, best_value=0.4, status=OutcomeStatus.MET)
    )
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, state)
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await mutate(
            ctx,
            {
                "mutation": _close_mutation("P01-I01-W01").model_dump(mode="json"),
                "repo_root": str(tmp_path),
            },
        )
        assert result["after_version"] != result["before_version"]
        written = orjson.loads(state_path.read_bytes())
        # The wave closed AND the owning Track's outcome status was re-derived.
        assert written["waves"]["P01-I01-W01"]["status"] == "closed"
        assert written["outcomes"]["OUT-1"]["status"] == OutcomeStatus.MISSED.value

    _run(body)


def test_wave_close_leaves_current_outcome_unchanged(tmp_path: Path) -> None:
    """A wave close does not move an already-correct Track outcome status.

    Boundary: when the persisted status already matches the comparator verdict,
    the close-time sync is a no-op on the outcome -- the close still lands but
    the outcome status (and its sample) are unchanged.
    """
    state = _state_with_track(
        _outcome(threshold=1.0, sample=1.5, best_value=1.5, status=OutcomeStatus.MET)
    )
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, state)
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        await mutate(
            ctx,
            {
                "mutation": _close_mutation("P01-I01-W01").model_dump(mode="json"),
                "repo_root": str(tmp_path),
            },
        )
        written = orjson.loads(state_path.read_bytes())
        assert written["waves"]["P01-I01-W01"]["status"] == "closed"
        assert written["outcomes"]["OUT-1"]["status"] == OutcomeStatus.MET.value

    _run(body)
