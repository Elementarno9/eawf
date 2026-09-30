"""A Run that went quiet gets a typed stall fact from the daemon's own sweep, and ends nothing.

RUN-029: the sweep the daemon schedules raises one ``run_stall`` fact per quiet episode on
the run ledger, carrying the last activity, the elapsed silence, the interval and the
resume path, and ``runtime.run.stalls.read`` lists every stall still standing. A Run that
has produced nothing is measured from its start, and a Run started inside a host session
sends no hello, so it is measured against the runtime whose harness owns that session.
RUN-030:
the fact moves no status, so a stalled Run stays running -- lost, not failed -- until a
principal's control ends it. UI-008: the events read answers with the Run's timeline
groups, so a surface draws the daemon's grouping instead of making its own.

Everything is driven through the real verbs and the real sweep; the threshold is crossed
by the configured interval or by handing the sweep a later clock, never by waiting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.config.schema import DEFAULT_STALL_INTERVAL_SECONDS
from eawf.kernel.runtime.stall import SILENT_SINCE_START, RunStallFact
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.run import (
    RUN_CONTRACT_READ_METHOD,
    RUN_CONTROL_REQUEST_METHOD,
    RUN_EVENT_APPEND_METHOD,
    RUN_EVENTS_READ_METHOD,
)
from eawf.runtime.daemon.methods.run_liveness import RUN_STALLS_READ_METHOD, RunStallsAnswer
from eawf.runtime.daemon.stall_sweep import sweep_once
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_run_stall_interval import (
    ACTOR,
    LONG,
    RUN_KEY,
    RUN_URN,
    announce_as,
    configure,
)

pytestmark = pytest.mark.integration

#: A Run of the tree that is not running, which the sweep must pass over.
DONE_KEY: Final = "RUN-00000011"

#: When the seeded running Run started, which a Run with no activity is measured from.
STARTED_AT: Final = datetime.fromisoformat(seed_row("run", "RUNNING")["started_at"])


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one running Run and one completed one."""
    provisioned = provision(tmp_path / "repo")
    done = seed_row("run", "COMPLETED")
    done["urn"] = RUN_URN.replace(RUN_KEY, DONE_KEY)
    done["key"] = DONE_KEY
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING"), DONE_KEY: done}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(tmp_path / "runtime")


def call(method: str, ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb the way the server does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


def act(ctx: MethodContext, canary: CanaryProvision, sequence: int) -> None:
    """Record one activity at ``sequence``, so there is silence to measure from."""
    call(
        RUN_EVENT_APPEND_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        event_ref=f"EVT-{sequence:08x}",
        run_sequence=sequence,
        event_kind="reasoning_started",
        payload={"payload_kind": "reasoning_summary", "phase": "started"},
        actor=ACTOR,
    )


def stalls(ctx: MethodContext, canary: CanaryProvision) -> RunStallsAnswer:
    """Read every standing stall through the daemon verb."""
    return RunStallsAnswer.model_validate(
        call(RUN_STALLS_READ_METHOD, ctx, repo_root=str(canary.root))
    )


def quiet(ctx: MethodContext, canary: CanaryProvision) -> None:
    """Make the Run's runtime one whose interval any silence crosses, then let it act."""
    configure(canary, {"codex": {"stall_interval_s": 0}})
    announce_as(ctx, canary, "codex")
    act(ctx, canary, 1)


# ---------------------------------------------------------------------------
# RUN-029: the sweep raises a typed stall fact, once per quiet episode
# ---------------------------------------------------------------------------


def test_run_029_the_sweep_raises_one_typed_stall_fact_per_quiet_episode(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    quiet(ctx, canary)
    now = datetime.now(UTC)

    assert sweep_once(ctx, now=now) == (RUN_KEY,)
    assert sweep_once(ctx, now=now + timedelta(seconds=30)) == ()

    (fact,) = stalls(ctx, canary).stalls
    assert fact.run_ref.entity_key == RUN_KEY
    assert fact.anchor_sequence == 1
    assert fact.last_activity_kind.value == "reasoning_started"
    assert fact.interval_seconds == 0
    assert fact.elapsed_seconds >= 0
    assert fact.resume_method == RUN_CONTROL_REQUEST_METHOD
    assert fact.resume_control.value == "resume"
    read = call(RUN_EVENTS_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    assert RunStallFact.model_validate(read["stall"]["raised"]) == fact


def test_run_029_activity_after_a_stall_moves_the_run_past_it(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    quiet(ctx, canary)
    sweep_once(ctx, now=datetime.now(UTC))
    act(ctx, canary, 2)

    assert stalls(ctx, canary).stalls == ()
    assert sweep_once(ctx, now=datetime.now(UTC)) == (RUN_KEY,)
    (fact,) = stalls(ctx, canary).stalls
    assert fact.anchor_sequence == 2


def test_run_029_a_run_inside_its_interval_is_not_stalled(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    configure(canary, {"codex": {"stall_interval_s": LONG}})
    announce_as(ctx, canary, "codex")
    act(ctx, canary, 1)

    assert sweep_once(ctx, now=datetime.now(UTC)) == ()
    assert stalls(ctx, canary).stalls == ()


def test_run_029_the_default_interval_is_crossed_by_a_later_clock_alone(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    act(ctx, canary, 1)

    assert sweep_once(ctx, now=datetime.now(UTC)) == ()
    assert sweep_once(ctx, now=datetime.now(UTC) + timedelta(hours=1)) == (RUN_KEY,)


def test_run_029_a_run_with_no_activity_is_measured_from_its_start(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    configure(canary, {"codex": {"stall_interval_s": 0}})
    announce_as(ctx, canary, "codex")
    now = datetime.now(UTC)

    assert sweep_once(ctx, now=now) == (RUN_KEY,)
    assert sweep_once(ctx, now=now + timedelta(seconds=30)) == ()
    (fact,) = stalls(ctx, canary).stalls
    assert fact.anchor_sequence == SILENT_SINCE_START
    assert fact.last_activity_kind is None
    assert fact.last_activity_at == STARTED_AT
    assert fact.elapsed_seconds == pytest.approx((now - STARTED_AT).total_seconds())

    act(ctx, canary, 1)
    assert stalls(ctx, canary).stalls == ()


def test_run_029_a_run_with_no_activity_inside_its_interval_is_not_stalled(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    announce_as(ctx, canary, "codex")
    inside = STARTED_AT + timedelta(seconds=DEFAULT_STALL_INTERVAL_SECONDS - 1)

    assert sweep_once(ctx, now=inside) == ()
    assert sweep_once(ctx, now=inside + timedelta(seconds=1)) == (RUN_KEY,)


def _hosted(tmp_path: Path, harness: str) -> CanaryProvision:
    """A canary whose one running Run carries a *harness* session and sends no hello."""
    provisioned = provision(tmp_path / "repo")
    row = seed_row("run", "RUNNING")
    row["vendor_session"] = {"harness": harness, "session_digest": "host-session-1"}
    seed(provisioned, {"run": {RUN_KEY: row}})
    return provisioned


@pytest.mark.parametrize(("harness", "runtime"), [("claude-code", "claude"), ("codex", "codex")])
def test_run_029_a_host_session_run_is_measured_against_its_harness_runtime(
    tmp_path: Path, ctx: MethodContext, harness: str, runtime: str
) -> None:
    hosted = _hosted(tmp_path, harness)
    configure(hosted, {runtime: {"stall_interval_s": 1}})
    assert stalls(ctx, hosted).stalls == ()  # the read attaches the tree to the sweep

    assert sweep_once(ctx, now=STARTED_AT + timedelta(seconds=1)) == (RUN_KEY,)
    (fact,) = stalls(ctx, hosted).stalls
    assert fact.interval_seconds == 1


def test_run_029_a_host_session_run_takes_no_other_runtime_s_interval(
    tmp_path: Path, ctx: MethodContext
) -> None:
    hosted = _hosted(tmp_path, "claude-code")
    configure(hosted, {"codex": {"stall_interval_s": 1}})
    assert stalls(ctx, hosted).stalls == ()  # the read attaches the tree to the sweep

    assert sweep_once(ctx, now=STARTED_AT + timedelta(seconds=1)) == ()
    after = STARTED_AT + timedelta(seconds=DEFAULT_STALL_INTERVAL_SECONDS)
    assert sweep_once(ctx, now=after) == (RUN_KEY,)


def test_run_029_a_broken_tree_is_passed_over_and_the_sweep_goes_on(
    canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet(ctx, canary)

    def refuse(*_args: Any, **_kwargs: Any) -> tuple[str, ...]:
        raise RuntimeError("the select is not whole")

    monkeypatch.setattr("eawf.runtime.daemon.stall_sweep.detect_stalls", refuse)
    assert sweep_once(ctx, now=datetime.now(UTC)) == ()


def test_run_029_the_daemon_schedules_the_sweep_and_stops_it_on_shutdown(
    ctx: MethodContext,
) -> None:
    async def scenario() -> bool:
        ctx.shutdown_event = asyncio.Event()
        task = daemon_main._schedule_stall_sweep(ctx)
        await asyncio.sleep(0)
        ctx.shutdown_event.set()
        await asyncio.wait_for(task, timeout=10)
        return task.done()

    assert asyncio.run(scenario())


# ---------------------------------------------------------------------------
# RUN-030: a stall never terminates the Run
# ---------------------------------------------------------------------------


def test_run_030_a_stall_moves_no_status_and_reads_as_lost(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    quiet(ctx, canary)
    sweep_once(ctx, now=datetime.now(UTC))

    read = call(RUN_EVENTS_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    assert read["run_status"] == "RUNNING"
    assert read["stall"]["ambiguity"] == "lost"
    contract = call(RUN_CONTRACT_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    assert contract["status"] == "RUNNING"


def test_run_030_a_run_that_is_not_running_is_never_swept(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    quiet(ctx, canary)
    raised = sweep_once(ctx, now=datetime.now(UTC) + timedelta(hours=1))
    assert DONE_KEY not in raised


# ---------------------------------------------------------------------------
# UI-008: the events read answers with the daemon's own grouping
# ---------------------------------------------------------------------------


def test_ui_008_the_events_read_answers_with_its_timeline_groups(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    for sequence in range(1, 5):
        act(ctx, canary, sequence)

    read = call(RUN_EVENTS_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    timeline = read["timeline"]
    assert timeline["event_count"] == 4
    assert [g["count"] for g in timeline["groups"]] == [4]
    assert timeline["groups"][0]["first_sequence"] == 1
    assert timeline["groups"][0]["last_sequence"] == 4
    assert timeline["p0_coalesced"] == 0
    again = call(RUN_EVENTS_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    assert again["timeline"]["digest"] == timeline["digest"]
