"""A running epoch-2 Run past its estimate with no progress opens exactly one notice.

PRX-034 over a native tree: the daemon's own sweep measures each running Run from its
start against the one effort constant. Short of the notify fraction, or with progress
inside the grace period, nothing is written; past both, one non-blocking notice lands on
the notice ledger. Restarting the daemon and the client three times keeps that one row,
delivers it once, and opens no pause for anyone to answer.

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

from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.budget.notice_inbox import deliver_pending
from eawf.runtime.budget.notices import LOCAL_OPERATOR, load_notice_ledger, notices_path
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.pause import latest_pauses
from eawf.runtime.daemon.methods.run import RUN_EVENT_APPEND_METHOD
from eawf.runtime.daemon.methods.run_liveness import detect_estimate_crossings
from eawf.runtime.daemon.stall_sweep import sweep_once
from eawf.workflow.estimation.buckets import EFFORT_DISPERSION_MINUTES, EFFORT_MINUTES
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)
from tests.integration.runtime.daemon.test_run_stall_interval import ACTOR, RUN_KEY, RUN_URN

pytestmark = pytest.mark.integration

#: When the seeded running Run started.
STARTED: Final = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)

#: The default notice policy's no-progress grace period.
GRACE: Final = timedelta(seconds=600)

#: The one effort constant, as the elapsed time that passes it.
ESTIMATE: Final = timedelta(minutes=EFFORT_MINUTES)

#: A Run of the tree that is not running, which the sweep must pass over.
DONE_KEY: Final = "RUN-00000011"


def _canary(root: Path, *, task: dict[str, Any] | None = None) -> CanaryProvision:
    """Provision a canary holding one running Run, one completed Run and *task*."""
    provisioned = provision(root)
    done = seed_row("run", "COMPLETED")
    done["urn"] = RUN_URN.replace(RUN_KEY, DONE_KEY)
    done["key"] = DONE_KEY
    rows: dict[str, dict[str, Any]] = {"run": {RUN_KEY: seed_row("run", "RUNNING"), DONE_KEY: done}}
    if task is not None:
        rows["task"] = {task["key"]: task}
    seed(provisioned, rows)
    return provisioned


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary whose one running Run scopes no seeded Task."""
    return _canary(tmp_path / "repo")


def _daemon(canary: CanaryProvision, runtime_root: Path) -> MethodContext:
    """Start a daemon context and attach the canary, as a native request would."""
    ctx = method_context(runtime_root)
    ctx.native_root_context(tree_root(canary))
    return ctx


def _attached(ctx: MethodContext) -> Epoch2RootContext:
    """Return the one tree the daemon context has attached."""
    (context,) = ctx.native_roots.values()
    return context


def _ledger(canary: CanaryProvision) -> Path:
    return notices_path(tree_root(canary) / "state.json")


def _act(ctx: MethodContext, canary: CanaryProvision, sequence: int) -> None:
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
    records = read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))
    return dict(latest_pauses(records))


def test_prx_034_an_epoch2_run_past_its_estimate_with_no_progress_opens_one_notice(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = _daemon(canary, tmp_path / "runtime")

    sweep_once(ctx, now=STARTED + ESTIMATE)

    (notice,) = load_notice_ledger(_ledger(canary)).notices.values()
    assert notice.scope_id == RUN_KEY
    assert (notice.basis, notice.axis, notice.status, notice.revision) == (
        "estimate",
        "wall_seconds",
        "OPEN",
        1,
    )
    assert notice.highest_band == "limit_reached"
    assert notice.blocking is False
    assert notice.budget_value == int(EFFORT_MINUTES * 60) == 1440
    assert notice.observed_value == 1440
    assert notice.audience == (LOCAL_OPERATOR,)
    assert _pauses(canary) == {}


def test_prx_034_one_second_short_of_the_estimate_writes_nothing(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = _daemon(canary, tmp_path / "runtime")

    sweep_once(ctx, now=STARTED + ESTIMATE - timedelta(seconds=1))

    assert not _ledger(canary).exists()


def test_prx_034_the_estimate_is_the_effort_constant_not_the_pessimistic_p90(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = _daemon(canary, tmp_path / "runtime")
    p90 = timedelta(minutes=EFFORT_DISPERSION_MINUTES["p90"])

    opened = detect_estimate_crossings(_attached(ctx), now=STARTED + ESTIMATE)

    assert opened == (RUN_KEY,)
    assert p90 > ESTIMATE


def test_prx_034_progress_inside_the_grace_holds_the_notice_back(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = _daemon(canary, tmp_path / "runtime")
    _act(ctx, canary, 1)
    acted = datetime.now(UTC)

    sweep_once(ctx, now=acted + GRACE - timedelta(seconds=30))
    assert not _ledger(canary).exists()

    sweep_once(ctx, now=acted + GRACE + timedelta(seconds=30))
    assert len(load_notice_ledger(_ledger(canary)).notices) == 1


def test_prx_034_three_restarts_keep_one_row_one_delivery_and_no_modal(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    now = STARTED + ESTIMATE + GRACE
    sweep_once(_daemon(canary, tmp_path / "runtime-0"), now=now)
    first = deliver_pending(_ledger(canary), principal=LOCAL_OPERATOR, now=now)
    settled = _ledger(canary).read_bytes()

    for restart in range(1, 4):
        later = now + timedelta(minutes=restart)
        daemon = _daemon(canary, tmp_path / f"runtime-{restart}")  # daemon restart
        opened = detect_estimate_crossings(_attached(daemon), now=later)
        sweep_once(daemon, now=later)
        again = deliver_pending(_ledger(canary), principal=LOCAL_OPERATOR, now=later)  # client
        assert opened == ()
        assert again == ()

    assert len(first) == 1
    assert _ledger(canary).read_bytes() == settled
    assert len(load_notice_ledger(_ledger(canary)).notices) == 1
    assert _pauses(canary) == {}


def test_prx_034_a_run_that_is_not_running_is_passed_over(tmp_path: Path) -> None:
    provisioned = provision(tmp_path / "repo")
    done = seed_row("run", "COMPLETED")
    seed(provisioned, {"run": {done["key"]: done}})
    ctx = _daemon(provisioned, tmp_path / "runtime")

    sweep_once(ctx, now=STARTED + ESTIMATE + GRACE)

    assert not _ledger(provisioned).exists()


def test_prx_034_the_notice_is_for_the_principal_holding_the_task_claim(tmp_path: Path) -> None:
    task = seed_row("task", "RUNNING")
    task["claimed_by"] = ACTOR
    task["first_claimed_at"] = "2026-09-08T00:30:00Z"
    provisioned = _canary(tmp_path / "repo", task=task)
    ctx = _daemon(provisioned, tmp_path / "runtime")

    sweep_once(ctx, now=STARTED + ESTIMATE)

    (notice,) = load_notice_ledger(_ledger(provisioned)).notices.values()
    assert notice.audience == (ACTOR,)
    (delivered,) = deliver_pending(_ledger(provisioned), principal=ACTOR, now=STARTED + ESTIMATE)
    assert delivered.scope_id == RUN_KEY
