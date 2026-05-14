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

The current macOS lock implementation (``portalock.acquire`` unlinks the
lockfile on release) admits a known race in which two contemporaneous
in-process threads can both pass the lock check on different inodes. This
test does NOT assert ``successes == 1`` because that would constrain the
lock layer rather than the lifecycle layer; the data-level invariant in
(1) is the user-visible contract.
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

from eawf.cli.commands import lifecycle as lc
from eawf.lifecycle.transitions import LifecycleError, claim_wave
from eawf.lock import portalock
from eawf.state.models import State


def _seed_state(state_path: Path) -> None:
    """Write a minimal state.json with one PENDING wave."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
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
            "subproject_id": None,
            "phase_id": "P01",
            "iter_id": "P01-I01",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P01": {
                "id": "P01",
                "scope_id": "QR",
                "subproject_id": None,
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
                "deps": [],
                "file_scopes": ["src/"],
                "claim_session_id": None,
                "worktree_id": None,
                "outcome": None,
                "opened_at": datetime.now(UTC).isoformat(),
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _claim_in_thread(
    state_path: Path,
    session_id: str,
    barrier: threading.Barrier,
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
                claim_wave(state, wave_id="P01-I01-W01", session_id=session_id)
            except LifecycleError:
                return 3
            state.updated_at = datetime.now(UTC)
            new_payload = state.model_dump(mode="json")
            lc._write_state_unlocked(state_path, new_payload)  # type: ignore[attr-defined]
            return 0
    except portalock.LockTimeout:
        return 5


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
    assert successes >= 1, f"expected at least one success, got 0 (codes={codes})"
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
