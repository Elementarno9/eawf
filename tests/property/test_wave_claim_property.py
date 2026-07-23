"""Hypothesis property test: concurrent ``wave claim`` is exactly-once.

We spin N (2-8) threads against a shared ``state.json``. Each thread invokes
the lifecycle handler body in-process (CliRunner is not thread-safe; it
captures stdout globally), acquiring the sibling lock before reading +
mutating state.

Invariants asserted:

1. **Data-level exactly-once.** After all N threads complete, the on-disk
   state must show the wave in ``CLAIMED`` status with exactly one
   ``claim_session_id`` drawn from the candidate set, and the active-wave
   pointer must contain exactly one entry.
2. **At least one success.** ``successes >= 1`` — the lock + status-machine
   combination must always produce *some* winner; otherwise the system has
   livelocked.
3. **Status-machine guard rejects late claimers.** Among the threads that
   *did not* win the lock-and-status race, every rejection must be either
   :class:`portalock.LockTimeout` (mapped to exit 5) or
   :class:`LifecycleError` raised by ``claim_wave`` because the wave is no
   longer ``PENDING`` (mapped to exit 3).

The lockfile inode is persistent across releases, so every contender locks the
same inode. The property therefore asserts exactly one successful transition.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from eawf.kernel.state.models import State
from eawf.runtime.lock import portalock
from eawf.surfaces.cli.commands import lifecycle as lc
from eawf.workflow.lifecycle._claim_guards import CLAIM_PARALLEL_LIMIT_REACHED
from eawf.workflow.lifecycle._errors import LifecycleGuardError
from eawf.workflow.lifecycle.transitions import LifecycleError, claim_wave


def _seed_state(state_path: Path) -> None:
    """Write a minimal state.json with one PENDING wave."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    session_ids = [*(f"SES-{i}" for i in range(8)), "SES-A", "SES-B"]
    now = datetime.now(UTC).isoformat()
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": datetime.now(UTC).isoformat(),
        "project": {
            "code": "QR",
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": "P01",
            "iter_id": "P01-I01",
            "active_wave_ids": [],
            "active_session_ids": session_ids,
        },
        "workspace": None,
        "phases": {
            "P01": {
                "id": "P01",
                "scope_id": "QR",
                "track_id": None,
                "title": "Bootstrap",
                "status": "active",
                "iter_ids": ["P01-I01"],
                "outcome_ids": [],
                "opened_at": datetime.now(UTC).isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P01-I01": {
                "id": "P01-I01",
                "phase_id": "P01",
                "title": "Iter1",
                "status": "active",
                "wave_ids": ["P01-I01-W01"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": datetime.now(UTC).isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            "P01-I01-W01": {
                "id": "P01-I01-W01",
                "iter_id": "P01-I01",
                "title": "W1",
                "status": "pending",
                "effort_bucket": "M",
                "deps": [],
                "file_scopes": ["src/"],
                "success_criteria": [
                    {
                        "id": "CR-01",
                        "text": "one claimant wins the concurrent transition",
                        "kind": "deterministic",
                        "acceptance_style": "binary",
                        "evidence_kind": "deterministic",
                        "gate_ids": [],
                        "required": True,
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": "the final persisted claim has one winning session",
                    }
                ],
                "claim_session_id": None,
                "worktree_id": None,
                "outcome": None,
                "opened_at": datetime.now(UTC).isoformat(),
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {
            session_id: {
                "id": session_id,
                "role": "executor",
                "runtime": "test",
                "scope_id": "QR",
                "status": "active",
                "started_at": now,
            }
            for session_id in session_ids
        },
        "plugins": {},
        "indexes": {},
    }
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _claim_in_thread(
    state_path: Path,
    session_id: str,
    barrier: threading.Barrier,
    *,
    wave_id: str = "P01-I01-W01",
    max_parallel_waves: int = 4,
    out_of_order: bool = False,
) -> int:
    """Mirror the ``wave claim`` handler body in-process.

    Returns the canonical exit code each thread would have produced if
    invoked via the CLI:
    - ``0``  on successful CLAIMED-state write
    - ``3``  when the status-machine refused (wave already CLAIMED to other)
    - ``5``  when the sibling lock timed out
    """
    barrier.wait()
    try:
        with portalock.acquire(state_path, timeout=2.0):
            payload = orjson.loads(state_path.read_bytes())
            state = State.model_validate(payload)
            try:
                claim_wave(
                    state,
                    wave_id=wave_id,
                    session_id=session_id,
                    max_parallel_waves=max_parallel_waves,
                    out_of_order=out_of_order,
                )
            except LifecycleError:
                return 3
            state.updated_at = datetime.now(UTC)
            new_payload = state.model_dump(mode="json")
            lc._write_state_unlocked(state_path, new_payload)  # type: ignore[attr-defined]
            return 0
    except portalock.LockTimeout:
        return 5


def _seed_final_slot_state(state_path: Path) -> None:
    """Seed one occupied slot plus two PENDING waves competing for the last slot."""
    _seed_state(state_path)
    payload = orjson.loads(state_path.read_bytes())
    base_wave = payload["waves"]["P01-I01-W01"]
    occupied = dict(base_wave)
    occupied.update(
        {
            "id": "P01-I01-W00",
            "title": "occupied lane",
            "status": "in_progress",
            "claim_session_id": "SES-A",
            "claimed_at": datetime.now(UTC).isoformat(),
        }
    )
    contender = dict(base_wave)
    contender.update({"id": "P01-I01-W02", "title": "second contender"})
    payload["waves"]["P01-I01-W00"] = occupied
    payload["waves"]["P01-I01-W02"] = contender
    payload["iters"]["P01-I01"]["wave_ids"] = [
        "P01-I01-W00",
        "P01-I01-W01",
        "P01-I01-W02",
    ]
    payload["current"]["active_wave_ids"] = ["P01-I01-W00"]
    payload["agent_sessions"]["SES-A"]["claimed_wave_ids"] = ["P01-I01-W00"]
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _claim_final_slot_in_thread(
    state_path: Path,
    wave_id: str,
    session_id: str,
    barrier: threading.Barrier,
) -> tuple[int, str | None]:
    """Claim one of two contenders and return its exit code plus guard code."""
    barrier.wait()
    try:
        with portalock.acquire(state_path, timeout=2.0):
            state = State.model_validate(orjson.loads(state_path.read_bytes()))
            try:
                claim_wave(
                    state,
                    wave_id=wave_id,
                    session_id=session_id,
                    out_of_order=True,
                    max_parallel_waves=2,
                )
            except LifecycleGuardError as exc:
                return 3, exc.code
            except LifecycleError:
                return 3, None
            state.updated_at = datetime.now(UTC)
            lc._write_state_unlocked(  # type: ignore[attr-defined]
                state_path,
                state.model_dump(mode="json"),
            )
            return 0, None
    except portalock.LockTimeout:
        return 5, None


@pytest.mark.slow
@pytest.mark.property
@given(claimer_count=st.integers(min_value=2, max_value=8))
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_concurrent_wave_claim_exactly_once(
    claimer_count: int, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """N threads claiming the same wave: state-level exactly-once."""
    work = tmp_path_factory.mktemp("wave_claim_property")
    state_path = work / ".ea" / "state.json"
    _seed_state(state_path)

    barrier = threading.Barrier(claimer_count)
    sessions = [f"SES-{i}" for i in range(claimer_count)]
    with ThreadPoolExecutor(max_workers=claimer_count) as pool:
        futures = [pool.submit(_claim_in_thread, state_path, s, barrier) for s in sessions]
        codes = [f.result() for f in as_completed(futures)]

    successes = sum(1 for c in codes if c == 0)
    assert successes == 1, f"expected exactly one success, got {successes} (codes={codes})"
    assert all(c in (0, 3, 5) for c in codes), f"unexpected exit code in {codes}"

    # Data-level exactly-once: state on disk must show the wave in CLAIMED
    # status with exactly one session_id from the candidate set, and the
    # active_wave_ids pointer must list it exactly once.
    final = orjson.loads(state_path.read_bytes())
    wave = final["waves"]["P01-I01-W01"]
    assert wave["status"] == "claimed"
    assert wave["claim_session_id"] in sessions
    active = final["current"]["active_wave_ids"]
    assert active.count("P01-I01-W01") == 1, (
        f"active_wave_ids should hold exactly one entry, got {active}"
    )
    bound_sessions = [
        session_id
        for session_id, session in final["agent_sessions"].items()
        if session["claimed_wave_ids"] == ["P01-I01-W01"]
    ]
    assert bound_sessions == [wave["claim_session_id"]]


def test_two_waves_competing_for_final_capacity_slot_have_one_winner(tmp_path: Path) -> None:
    """The state lock serialises cap resolution: one final slot, one winner."""
    state_path = tmp_path / ".ea" / "state.json"
    _seed_final_slot_state(state_path)
    barrier = threading.Barrier(2)
    contenders = [("P01-I01-W01", "SES-0"), ("P01-I01-W02", "SES-1")]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_claim_final_slot_in_thread, state_path, wave_id, session_id, barrier)
            for wave_id, session_id in contenders
        ]
        results = [future.result() for future in as_completed(futures)]

    assert [code for code, _guard in results].count(0) == 1
    assert [guard for _code, guard in results].count(CLAIM_PARALLEL_LIMIT_REACHED) == 1
    final = State.model_validate(orjson.loads(state_path.read_bytes()))
    active = [
        wave.id for wave in final.waves.values() if wave.status.value in {"claimed", "in_progress"}
    ]
    assert len(active) == 2
    assert "P01-I01-W00" in active


def test_seeded_state_validates(tmp_path: Path) -> None:
    """Sanity: the seed payload itself passes schema validation."""
    state_path = tmp_path / ".ea" / "state.json"
    _seed_state(state_path)
    payload = orjson.loads(state_path.read_bytes())
    State.model_validate(payload)


def test_serial_claim_attempts_record_one_session(tmp_path: Path) -> None:
    """Serial calls (no contention) must show exactly-one semantics with no race."""
    state_path = tmp_path / ".ea" / "state.json"
    _seed_state(state_path)
    barrier = threading.Barrier(1)  # single-thread "barrier" is a no-op
    code1 = _claim_in_thread(state_path, "SES-A", barrier)
    code2 = _claim_in_thread(state_path, "SES-B", barrier)
    assert code1 == 0
    assert code2 == 3, "second claimer must hit the status-machine guard"
    final = orjson.loads(state_path.read_bytes())
    assert final["waves"]["P01-I01-W01"]["claim_session_id"] == "SES-A"
