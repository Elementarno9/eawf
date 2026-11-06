"""An open Run that recorded nothing for a day, and that nothing awaits, is ended by the daemon.

Each test seeds a provisioned canary with open Runs whose last event or transition sits a
known distance before the sweep's clock, then sweeps it the way the daemon does: through
the stall loop's pass or the bound tree's attach on start. Statuses are read back from the
tree's document, or from the run ledger once a terminal Run is compacted out of it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.events import RunEventKind
from eawf.kernel.state.epoch2.run import Run, RunStatus
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.task_status import TaskStatus
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon import run_reaper, run_supervisor
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusalCode,
    TransactionRefusedError,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.permission import DAEMON_ACTOR
from eawf.runtime.daemon.methods.run import append_run_event
from eawf.runtime.daemon.run_events import RunEventAppend, run_events_of
from eawf.runtime.daemon.run_reaper import ABANDON_AFTER, end_abandoned_runs
from eawf.runtime.daemon.run_supervisor import RunSupervisor, supervisor_of
from eawf.runtime.daemon.stall_sweep import run_stall_loop, sweep_once
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    rekeyed,
    seed,
    seed_row,
    tree_root,
)

pytestmark = pytest.mark.integration

#: The sweep's clock in the tests that fix it.
NOW: Final = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

#: The Run the seeded Task is RUNNING under, and that Task.
RUN_KEY: Final = "RUN-00000010"
TASK_KEY: Final = "EAWF-0042"

#: The failure code an abandoned Run ends with.
ABANDONED: Final = "stalled-abandoned"


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary with nothing seeded yet."""
    return provision(tmp_path / "repo")


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(tmp_path / "runtime")


@pytest.fixture
def context(canary: CanaryProvision, ctx: MethodContext) -> Epoch2RootContext:
    """The canary's native context, attached to ``ctx`` as a native request attaches it."""
    return ctx.native_root_context(tree_root(canary))


def open_run(
    key: str, status: str, *, created: datetime, updated: datetime | None = None
) -> dict[str, Any]:
    """Return an open Run row created at *created* whose last transition was at *updated*."""
    row = rekeyed(seed_row("run", status), key=key)
    row["created_at"] = created.isoformat()
    row["updated_at"] = (updated or created).isoformat()
    if status == "RUNNING":
        row["started_at"] = created.isoformat()
    return row


def running_task() -> dict[str, Any]:
    """Return the seeded Task, RUNNING under :data:`RUN_KEY` and claimed."""
    task = seed_row("task", "RUNNING")
    task["claimed_by"] = "OP-0001"
    return task


def act(context: Epoch2RootContext, run: dict[str, Any], at: datetime, sequence: int = 1) -> None:
    """Record one run event on *run* at *at*, the daemon's recording clock."""
    append_run_event(
        context,
        RunEventAppend(
            urn=run["urn"],
            event_ref=f"EVT-{run['key'][-4:]}{sequence:04x}",
            run_sequence=sequence,
            event_kind="reasoning_started",
            payload={"payload_kind": "reasoning_summary", "phase": "started"},
            actor="OP-0001",
        ),
        now=at,
    )


def run_row(canary: CanaryProvision, key: str = RUN_KEY) -> dict[str, Any]:
    """Return one Run, from the document or, once compacted out, from its ledger."""
    row: dict[str, Any] | None = document_rows(
        read_document(document_path(canary)), Epoch2Collection.RUN
    ).get(key)
    if row is not None:
        return row
    for item in effective_records(read_ledger_records(run_ledger(canary))):
        if item.record_key == key and "payload_kind" not in item.payload:
            return item.payload
    raise AssertionError(f"run {key} is in neither tier")


def run_ledger(canary: CanaryProvision) -> Path:
    """Return the canary's run ledger."""
    return ledger_path(document_path(canary), Epoch2Collection.RUN)


def task_now(canary: CanaryProvision) -> Task:
    """Return the seeded Task as the document holds it now."""
    rows = document_rows(read_document(document_path(canary)), Epoch2Collection.TASK)
    return Task.model_validate(rows[TASK_KEY])


def assert_abandoned(canary: CanaryProvision, key: str) -> None:
    """Assert the Run failed as abandoned, with its ending stamped."""
    run = run_row(canary, key)
    assert run["status"] == "FAILED", key
    assert run["failure"]["code"] == ABANDONED
    assert run["ended_at"] is not None


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the subjects of every document-locked session a root opens from now on."""
    opened: list[str] = []
    original = Epoch2RootContext.session

    def counted(self: Epoch2RootContext, urns: Any) -> Any:
        urns = tuple(urns)
        opened.extend(str(urn) for urn in urns[:1])
        return original(self, urns)

    monkeypatch.setattr(Epoch2RootContext, "session", counted)
    return opened


# ---------------------------------------------------------------------------
# The threshold: a day of silence ends an open Run, anything less leaves it
# ---------------------------------------------------------------------------


def test_sweep_threshold(
    canary: CanaryProvision, ctx: MethodContext, context: Epoch2RootContext
) -> None:
    long_ago = NOW - timedelta(hours=48)
    runs = {
        # silent 25 h since its last event, with the Task running under it
        RUN_KEY: open_run(RUN_KEY, "RUNNING", created=long_ago),
        # its last event 23 h ago
        "RUN-00000011": open_run("RUN-00000011", "RUNNING", created=long_ago),
        # queued 25 h ago, no event: measured from its creation
        "RUN-00000012": open_run("RUN-00000012", "QUEUED", created=NOW - timedelta(hours=25)),
        # queued 23 h ago, no event
        "RUN-00000013": open_run("RUN-00000013", "QUEUED", created=NOW - timedelta(hours=23)),
        # created long ago but it took a transition 23 h ago
        "RUN-00000014": open_run(
            "RUN-00000014", "RUNNING", created=long_ago, updated=NOW - timedelta(hours=23)
        ),
    }
    seed(canary, {"run": runs, "task": {TASK_KEY: running_task()}})
    act(context, runs[RUN_KEY], NOW - timedelta(hours=25))
    act(context, runs["RUN-00000011"], NOW - timedelta(hours=23))

    sweep_once(ctx, now=NOW)

    assert_abandoned(canary, RUN_KEY)
    assert_abandoned(canary, "RUN-00000012")
    assert run_row(canary, "RUN-00000011")["status"] == "RUNNING"
    assert run_row(canary, "RUN-00000013")["status"] == "QUEUED"
    assert run_row(canary, "RUN-00000014")["status"] == "RUNNING"
    # the Task is handed back to PLANNED, as a reaped Run's Task is, never left stranded
    task = task_now(canary)
    assert task.status is TaskStatus.PLANNED
    assert task.active_run_ref is None
    assert task.claimed_by is None


def test_a_run_silent_exactly_the_threshold_stays_open(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    seed(canary, {"run": {RUN_KEY: open_run(RUN_KEY, "QUEUED", created=NOW - ABANDON_AFTER)}})

    assert end_abandoned_runs(context, RunSupervisor(), actor=DAEMON_ACTOR, now=NOW) == ()
    assert run_row(canary)["status"] == "QUEUED"


def test_a_supervised_run_is_never_abandoned(
    canary: CanaryProvision, ctx: MethodContext, context: Epoch2RootContext
) -> None:
    run = open_run(RUN_KEY, "RUNNING", created=NOW - timedelta(hours=48))
    seed(canary, {"run": {RUN_KEY: run}})

    async def scenario() -> None:
        parked = asyncio.Event()

        async def park() -> None:
            await parked.wait()

        supervisor_of(context).supervise(run["urn"], park())
        try:
            sweep_once(ctx, now=NOW)
        finally:
            parked.set()

    asyncio.run(scenario())

    assert run_row(canary)["status"] == "RUNNING"


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "CANCELLED", "SUSPENDED"])
def test_a_run_not_open_is_untouched(
    canary: CanaryProvision, context: Epoch2RootContext, status: str
) -> None:
    seed(canary, {"run": {RUN_KEY: seed_row("run", status)}})
    before = run_row(canary)

    assert end_abandoned_runs(context, RunSupervisor(), actor=DAEMON_ACTOR, now=NOW) == ()
    assert run_row(canary) == before


def test_a_tree_with_no_runs_is_a_no_op(context: Epoch2RootContext, sessions: list[str]) -> None:
    assert end_abandoned_runs(context, RunSupervisor(), actor=DAEMON_ACTOR, now=NOW) == ()
    assert sessions == []


def test_an_unreadable_run_is_passed_over_and_the_others_still_ended(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    created = NOW - timedelta(hours=48)
    unreadable = {**open_run("RUN-00000001", "RUNNING", created=created), "unreadable": True}
    seed(
        canary,
        {
            "run": {
                "RUN-00000001": unreadable,
                RUN_KEY: open_run(RUN_KEY, "RUNNING", created=created),
            }
        },
    )

    end_abandoned_runs(context, RunSupervisor(), actor=DAEMON_ACTOR, now=NOW)

    assert run_row(canary, "RUN-00000001")["status"] == "RUNNING"
    assert_abandoned(canary, RUN_KEY)


def test_a_refused_run_is_kept_and_the_others_still_ended(
    canary: CanaryProvision, context: Epoch2RootContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = NOW - timedelta(hours=48)
    seed(
        canary,
        {
            "run": {
                RUN_KEY: open_run(RUN_KEY, "RUNNING", created=created),
                "RUN-00000011": open_run("RUN-00000011", "RUNNING", created=created),
            }
        },
    )
    reap_one = run_reaper._reap

    def refuse_first(context: Epoch2RootContext, run: Any, **kwargs: Any) -> tuple[Any, ...]:
        if run.key == RUN_KEY:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.REVISION_CONFLICT,
                detail="the Run moved",
                entity_ref=str(run.urn),
                remediation="read the Run again",
            )
        return reap_one(context, run, **kwargs)

    monkeypatch.setattr(run_reaper, "_reap", refuse_first)

    end_abandoned_runs(context, RunSupervisor(), actor=DAEMON_ACTOR, now=NOW)

    assert run_row(canary)["status"] == "RUNNING"
    assert_abandoned(canary, "RUN-00000011")


def _in_the_gap(monkeypatch: pytest.MonkeyPatch, act_on: Callable[[], None]) -> None:
    """Call *act_on* once, right after the unlocked read decided a Run is abandoned."""
    decide = run_reaper._abandoned
    acted: list[bool] = []

    def deciding(*args: Any, **kwargs: Any) -> bool:
        abandoned = decide(*args, **kwargs)
        if abandoned and not acted:
            acted.append(True)
            act_on()
        return abandoned

    monkeypatch.setattr(run_reaper, "_abandoned", deciding)


def error_lines(canary: CanaryProvision, run: dict[str, Any]) -> list[str]:
    """Return the event ids of every error line on *run*'s stream."""
    records = read_ledger_records(run_ledger(canary))
    urn = parse_qualified_urn(run["urn"])
    return [
        event.event_ref
        for event in run_events_of(records, urn)
        if event.event_kind is RunEventKind.ERROR_OBSERVED
    ]


@pytest.mark.parametrize("status", ["RUNNING", "QUEUED"])
def test_sweep_threshold_repro_woke_after_the_read(
    canary: CanaryProvision,
    context: Epoch2RootContext,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """A Run that records an event after the unlocked read is left open, with no error line."""
    run = open_run(RUN_KEY, status, created=NOW - timedelta(hours=48))
    seed(canary, {"run": {RUN_KEY: run}})
    _in_the_gap(monkeypatch, lambda: act(context, run, NOW - timedelta(minutes=1)))

    assert end_abandoned_runs(context, RunSupervisor(), actor=DAEMON_ACTOR, now=NOW) == ()

    assert run_row(canary)["status"] == status
    assert error_lines(canary, run) == []


def test_sweep_threshold_repro_orphan_error_line(
    canary: CanaryProvision, context: Epoch2RootContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Run another writer moves after the unlocked read keeps no stalled error line."""
    run = open_run(RUN_KEY, "RUNNING", created=NOW - timedelta(hours=48))
    seed(canary, {"run": {RUN_KEY: run}})

    def suspend() -> None:
        run_supervisor._move(
            context,
            Run.model_validate(run),
            actor=DAEMON_ACTOR,
            to_status=RunStatus.SUSPENDED,
            idempotency_key="gap-suspend",
            updates={"suspension_reason": "AWAITING_PROVIDER_CAPACITY"},
            now=NOW - timedelta(minutes=1),
        )

    _in_the_gap(monkeypatch, suspend)

    assert end_abandoned_runs(context, RunSupervisor(), actor=DAEMON_ACTOR, now=NOW) == ()

    assert run_row(canary)["status"] == "SUSPENDED"
    assert error_lines(canary, run) == []


def test_an_abandoned_run_states_one_error_line_bound_by_its_failure(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    """The stalled line stands once on the stream of the Run it ended, queued or running."""
    created = NOW - timedelta(hours=48)
    runs = {
        RUN_KEY: open_run(RUN_KEY, "RUNNING", created=created),
        "RUN-00000011": open_run("RUN-00000011", "QUEUED", created=created),
    }
    seed(canary, {"run": runs})

    end_abandoned_runs(context, RunSupervisor(), actor=DAEMON_ACTOR, now=NOW)

    for key, run in runs.items():
        assert_abandoned(canary, key)
        assert len(error_lines(canary, run)) == 1


# ---------------------------------------------------------------------------
# The lock: a pass decides every Run from one read and locks only what it ends
# ---------------------------------------------------------------------------


def test_a_repeated_pass_over_many_open_runs_takes_no_lock(
    canary: CanaryProvision,
    ctx: MethodContext,
    context: Epoch2RootContext,
    sessions: list[str],
) -> None:
    keys = [f"RUN-{n:08d}" for n in range(20, 32)]
    runs = {key: open_run(key, "RUNNING", created=NOW - timedelta(hours=48)) for key in keys}
    seed(canary, {"run": runs})
    for key in keys:
        act(context, runs[key], NOW - timedelta(hours=2))
    # the first pass raises each Run's stall and opens its pause, which does take locks
    sweep_once(ctx, now=NOW)
    sessions.clear()

    sweep_once(ctx, now=NOW + timedelta(minutes=1))

    assert sessions == []
    assert all(run_row(canary, key)["status"] == "RUNNING" for key in keys)


def test_an_abandoned_run_is_locked_only_for_its_own_ending(
    canary: CanaryProvision, context: Epoch2RootContext, sessions: list[str]
) -> None:
    created = NOW - timedelta(hours=48)
    fresh = [f"RUN-{n:08d}" for n in range(20, 30)]
    runs = {key: open_run(key, "QUEUED", created=NOW - timedelta(hours=1)) for key in fresh}
    runs[RUN_KEY] = open_run(RUN_KEY, "RUNNING", created=created)
    seed(canary, {"run": runs})

    end_abandoned_runs(context, RunSupervisor(), actor=DAEMON_ACTOR, now=NOW)

    assert_abandoned(canary, RUN_KEY)
    locked_runs = {subject for subject in sessions if "/run/" in subject}
    assert locked_runs == {runs[RUN_KEY]["urn"]}


# ---------------------------------------------------------------------------
# Start and cadence: the daemon sweeps on attach and on every stall-loop pass
# ---------------------------------------------------------------------------


def _ended_counts(monkeypatch: pytest.MonkeyPatch, after: Callable[[], None]) -> list[int]:
    """Count the envelopes each abandonment pass commits, calling *after* once each returns."""
    counts: list[int] = []
    original = run_reaper.end_abandoned_runs

    def spied(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
        ended = original(*args, **kwargs)
        counts.append(len(ended))
        after()
        return ended

    monkeypatch.setattr(run_reaper, "end_abandoned_runs", spied)
    return counts


def _ledger_lines(canary: CanaryProvision) -> Iterator[str]:
    """Yield the run ledger's lines as they stand."""
    yield from run_ledger(canary).read_text(encoding="utf-8").splitlines()


def test_start_and_idempotence(
    canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = datetime.now(UTC)
    stale = started - timedelta(hours=48)
    seed(
        canary,
        {
            "run": {RUN_KEY: open_run(RUN_KEY, "RUNNING", created=stale)},
            "task": {TASK_KEY: running_task()},
        },
    )
    ctx.state_path = str(tree_root(canary) / "state.json")
    ctx.started_at = started.isoformat()
    stop: list[Callable[[], None]] = []
    counts = _ended_counts(monkeypatch, lambda: stop[-1]() if stop else None)

    asyncio.run(daemon_main._attach_bound_tree(ctx))

    assert counts[0] > 0
    assert_abandoned(canary, RUN_KEY)
    assert task_now(canary).status is TaskStatus.PLANNED

    async def one_loop_pass() -> None:
        event = asyncio.Event()
        loop = asyncio.get_running_loop()
        stop.append(lambda: loop.call_soon_threadsafe(event.set))
        await run_stall_loop(ctx, stop_event=event)

    # a Run that went stale after the start is ended on the loop's cadence
    seed(canary, {"run": {"RUN-00000011": open_run("RUN-00000011", "QUEUED", created=stale)}})
    asyncio.run(one_loop_pass())

    assert counts[1] > 0
    assert_abandoned(canary, "RUN-00000011")

    # and a pass over the same tree commits nothing more
    document_before = document_path(canary).read_bytes()
    ledger_before = list(_ledger_lines(canary))
    asyncio.run(one_loop_pass())

    assert counts[2] == 0
    assert document_path(canary).read_bytes() == document_before
    assert list(_ledger_lines(canary)) == ledger_before
