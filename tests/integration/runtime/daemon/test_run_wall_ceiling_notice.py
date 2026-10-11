"""A running Run at its sealed wall ceiling opens exactly one notice.

The daemon's own sweep measures each running Run from its start against the wall-clock
ceiling its binding sealed. Short of the ceiling nothing is written; at it, one
non-blocking hard-limit notice lands on the notice ledger, whether or not the Run is
still active, because the daemon ends the Run there. Restarting the daemon three times
keeps that one row, delivers it once, and opens no pause for anyone to answer.

A restart is a fresh daemon context attached to the same tree, which is all a restart is
to a sweep that keeps nothing in memory. Thresholds are crossed by handing the sweep a
later clock, never by waiting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.budget.notice_inbox import deliver_pending
from eawf.runtime.budget.notices import LOCAL_OPERATOR, load_notice_ledger, notices_path
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.pause import latest_pauses, stall_of
from eawf.runtime.daemon.methods.run import RUN_EVENT_APPEND_METHOD
from eawf.runtime.daemon.methods.run_liveness import detect_wall_ceiling_crossings
from eawf.runtime.daemon.stall_sweep import sweep_once
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)
from tests.integration.runtime.daemon.test_run_stall_interval import ACTOR, RUN_KEY, RUN_URN
from tests.unit.kernel.runtime.test_authority_capsule import review_fields

pytestmark = pytest.mark.integration

#: When the seeded running Run started.
STARTED: Final = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)

#: How far past the ceiling a later reading is taken.
LATER: Final = timedelta(seconds=600)

#: The wall-clock ceiling the Run's binding seals.
WALL: Final = timedelta(seconds=2400)

#: A Run of the tree that is not running, which the sweep must pass over.
DONE_KEY: Final = "RUN-00000011"


def _bind(canary: CanaryProvision) -> str:
    """File the running Run's binding with its sealed wall ceiling; return its contract."""
    capsule = AuthorityCapsule.seal(
        review_fields(
            run_ref=RUN_URN,
            budget={
                "tokens": 10_000,
                "wall_seconds": int(WALL.total_seconds()),
                "output_bytes": 1_048_576,
            },
        )
    )
    binding = RunBinding(
        run_ref=parse_qualified_urn(RUN_URN),
        compiled_spec_digest=capsule.compiled_spec_digest,
        authority_capsule_digest=capsule.contract_digest,
        route_policy_revision=1,
        bound_at=AT,
        capsule=capsule,
    )
    append_ledger_record(
        ledger_path(document_path(canary), Epoch2Collection.RUN),
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"BND-{RUN_KEY}",
            status="recorded",
            recorded_at=AT,
            payload=binding.model_dump(mode="json"),
        ),
    )
    return capsule.compiled_spec_digest


def bound_canary(
    root: Path, *, task: dict[str, Any] | None = None, bound: bool = True
) -> CanaryProvision:
    """Provision a canary holding one running Run, one completed Run and *task*."""
    provisioned = provision(root)
    done = seed_row("run", "COMPLETED")
    done["urn"] = RUN_URN.replace(RUN_KEY, DONE_KEY)
    done["key"] = DONE_KEY
    rows: dict[str, dict[str, Any]] = {"run": {RUN_KEY: seed_row("run", "RUNNING"), DONE_KEY: done}}
    if task is not None:
        rows["task"] = {task["key"]: task}
    seed(provisioned, rows)
    if bound:
        _bind(provisioned)
    return provisioned


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary whose one running Run is bound and scopes no seeded Task."""
    return bound_canary(tmp_path / "repo")


def daemon_for(canary: CanaryProvision, runtime_root: Path) -> MethodContext:
    """Start a daemon context and attach the canary, as a native request would."""
    ctx = method_context(runtime_root)
    ctx.native_root_context(tree_root(canary))
    return ctx


def attached(ctx: MethodContext) -> Epoch2RootContext:
    """Return the one tree the daemon context has attached."""
    (context,) = ctx.native_roots.values()
    return context


def _ledger(canary: CanaryProvision) -> Path:
    return notices_path(tree_root(canary) / "state.json")


def act(ctx: MethodContext, canary: CanaryProvision, sequence: int) -> None:
    """Record one activity on the running Run at the daemon's clock."""
    asyncio.run(
        methods.dispatch(
            RUN_EVENT_APPEND_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": RUN_URN,
                "event_ref": f"EVT-{sequence:08x}",
                "run_sequence": sequence,
                "event_kind": "reasoning_started",
                "payload": {"payload_kind": "reasoning_summary", "phase": "started"},
                "actor": ACTOR,
            },
        )
    )


def _pauses(canary: CanaryProvision) -> dict[str, Any]:
    """Return every pause but the stall's: the silent Run's own stall holds one of its own."""
    records = read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))
    return {key: pause for key, pause in latest_pauses(records).items() if stall_of(pause) is None}


def test_a_silent_run_at_its_wall_ceiling_opens_one_hard_limit_notice(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = daemon_for(canary, tmp_path / "runtime")

    sweep_once(ctx, now=STARTED + WALL)

    (notice,) = load_notice_ledger(_ledger(canary)).notices.values()
    assert notice.scope_id == RUN_KEY
    assert (notice.basis, notice.axis, notice.status, notice.revision) == (
        "hard_limit",
        "wall_seconds",
        "OPEN",
        1,
    )
    assert (notice.highest_band, notice.severity) == ("limit_reached", "critical")
    assert notice.blocking is False
    assert notice.budget_value == notice.observed_value == int(WALL.total_seconds())
    assert notice.contract_digest is not None
    assert notice.audience == (LOCAL_OPERATOR,)
    assert _pauses(canary) == {}


def test_one_second_short_of_the_wall_ceiling_writes_nothing(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = daemon_for(canary, tmp_path / "runtime")

    opened = detect_wall_ceiling_crossings(attached(ctx), now=STARTED + WALL - timedelta(seconds=1))

    assert opened == ()
    assert not _ledger(canary).exists()


def test_an_event_just_before_the_ceiling_holds_nothing_back(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = daemon_for(canary, tmp_path / "runtime")
    act(ctx, canary, 1)

    sweep_once(ctx, now=STARTED + WALL)

    assert len(load_notice_ledger(_ledger(canary)).notices) == 1


def test_three_restarts_keep_one_row_one_delivery_and_no_modal(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    now = STARTED + WALL + LATER
    sweep_once(daemon_for(canary, tmp_path / "runtime-0"), now=now)
    first = deliver_pending(_ledger(canary), principal=LOCAL_OPERATOR, now=now)
    settled = _ledger(canary).read_bytes()

    for restart in range(1, 4):
        later = now + timedelta(minutes=restart)
        daemon = daemon_for(canary, tmp_path / f"runtime-{restart}")  # daemon restart
        opened = detect_wall_ceiling_crossings(attached(daemon), now=later)
        sweep_once(daemon, now=later)
        again = deliver_pending(_ledger(canary), principal=LOCAL_OPERATOR, now=later)  # client
        assert opened == ()
        assert again == ()

    assert len(first) == 1
    assert _ledger(canary).read_bytes() == settled
    assert len(load_notice_ledger(_ledger(canary)).notices) == 1
    assert _pauses(canary) == {}


def test_a_run_that_is_not_running_is_passed_over(tmp_path: Path) -> None:
    provisioned = provision(tmp_path / "repo")
    done = seed_row("run", "COMPLETED")
    seed(provisioned, {"run": {done["key"]: done}})
    ctx = daemon_for(provisioned, tmp_path / "runtime")

    sweep_once(ctx, now=STARTED + WALL + LATER)

    assert not _ledger(provisioned).exists()


def test_a_run_bound_without_a_sealed_ceiling_is_not_measured(tmp_path: Path) -> None:
    unbound = bound_canary(tmp_path / "repo", bound=False)
    ctx = daemon_for(unbound, tmp_path / "runtime")

    assert detect_wall_ceiling_crossings(attached(ctx), now=STARTED + WALL * 10) == ()
    assert not _ledger(unbound).exists()


def test_the_notice_is_for_the_principal_holding_the_task_claim(tmp_path: Path) -> None:
    task = seed_row("task", "RUNNING")
    task["claimed_by"] = ACTOR
    task["first_claimed_at"] = "2026-09-08T00:30:00Z"
    provisioned = bound_canary(tmp_path / "repo", task=task)
    ctx = daemon_for(provisioned, tmp_path / "runtime")

    sweep_once(ctx, now=STARTED + WALL)

    (notice,) = load_notice_ledger(_ledger(provisioned)).notices.values()
    assert notice.audience == (ACTOR,)
    (delivered,) = deliver_pending(_ledger(provisioned), principal=ACTOR, now=STARTED + WALL)
    assert delivered.scope_id == RUN_KEY
