"""Hypothesis property test: concurrent ``goal define`` preserves invariants.

N threads (2-8) race to define distinct goals against a shared
``state.json`` through :func:`eawf.surfaces.cli._mutation.state_transaction`.

Mirroring ``test_wave_claim_property.py``, this test asserts only the
**data-level** invariants because the macOS portalock-with-unlink
behaviour admits a known in-process race in which two contemporaneous
threads can both pass the lock check on different inodes. The
user-visible contract for evidence mutations is:

1. *Some* goal must persist (no total wipe).
2. Every goal that does persist must have the correct shape (title /
   scope / status drawn from the claimed set).
3. Exit codes outside ``{0, 5}`` (which would indicate spurious
   schema-validation rejection) never appear.

A subprocess-based race (where flock semantics hold cross-process) is
covered by the manual smoke check in the PR description.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from eawf.kernel.state.enums import StoreKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._mutation import state_transaction
from eawf.workflow.evidence import _io
from eawf.workflow.evidence import goal as goal_evi


def _seed_state(state_path: Path) -> None:
    """Write a minimal, valid state.json with project=QR (no waves seeded)."""
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
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _define_goal_in_thread(
    state_path: Path,
    goal_id: str,
    scope_id: str,
    barrier: threading.Barrier,
) -> int:
    """Mirror the ``goal define`` handler body in-process.

    Returns the canonical exit code each thread would have produced if
    invoked via the CLI:
    - ``0``  on successful state write
    - ``3``  when the bare mutator raised :class:`InvalidInput`
    - ``4``  when post-mutation validation failed
    - ``5``  when the sibling lock timed out
    """
    barrier.wait()
    try:
        with state_transaction(state_path, timeout=2.0) as state:
            event = goal_evi.define_goal(
                state,
                goal_id=goal_id,
                title=goal_id,
                summary=goal_id,
                scope_id=scope_id,
            )
            _io.append_jsonl(_io.store_paths(state_path)[StoreKind.EVENT], event)
        return 0
    except cli_errors.StateConflict as exc:
        if exc.kind != "LockConflict":
            raise
        return 5
    except cli_errors.UserError as exc:
        if exc.kind != "InvalidInput":
            raise
        return 3
    except cli_errors.ValidationError:
        return 4


@pytest.mark.property
@given(claimer_count=st.integers(min_value=2, max_value=8))
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_concurrent_goal_define_data_invariants(
    claimer_count: int, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """N threads defining distinct goal IDs: data-level invariants hold."""
    work = tmp_path_factory.mktemp("goal_define_property")
    state_path = work / ".ea" / "state.json"
    _seed_state(state_path)

    barrier = threading.Barrier(claimer_count)
    goal_ids = [f"G{i:02d}" for i in range(claimer_count)]
    with ThreadPoolExecutor(max_workers=claimer_count) as pool:
        futures = [
            pool.submit(_define_goal_in_thread, state_path, gid, "QR", barrier) for gid in goal_ids
        ]
        codes_by_goal = {gid: f.result() for gid, f in zip(goal_ids, futures, strict=True)}

    codes = list(codes_by_goal.values())
    successes = [gid for gid, c in codes_by_goal.items() if c == 0]
    assert successes, f"expected at least one success, got 0 (codes={codes})"
    # Only ``0`` (success) or ``5`` (lock-conflict) codes are admissible
    # since the candidate IDs are pairwise distinct — a ``3``
    # (InvalidInput "already exists") or ``4`` (ValidationFailed) here would
    # indicate the mutator is seeing inconsistent state.
    assert all(c in (0, 5) for c in codes), (
        f"unexpected exit code (only 0 or 5 expected since IDs are unique): {codes}"
    )

    final = orjson.loads(state_path.read_bytes())
    persisted = final.get("goals") or {}
    assert persisted, f"expected at least one goal in final state, got none (codes={codes})"
    # Every persisted goal must be one of the candidates and carry the
    # correct shape.  This guards against partial writes / schema
    # corruption rather than mandating exactly-once delivery, which the
    # current macOS portalock layer cannot guarantee for in-process
    # threads (cross-process flock semantics are intact).
    for gid, body in persisted.items():
        assert gid in goal_ids, f"persisted goal {gid!r} not in candidate set"
        assert body["title"] == gid
        assert body["summary"] == gid
        assert body["scope_id"] == "QR"
        assert body["status"] == "open"


def test_seeded_state_validates(tmp_path: Path) -> None:
    """Sanity: the seed payload passes schema validation."""
    from eawf.kernel.state.models import State

    state_path = tmp_path / ".ea" / "state.json"
    _seed_state(state_path)
    payload = orjson.loads(state_path.read_bytes())
    State.model_validate(payload)
