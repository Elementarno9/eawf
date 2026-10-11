"""A Run that reaches its hard ceiling is ended by the daemon, and its worker is stopped.

Past its wall-clock ceiling the gateway refuses every call a Run makes, but a refused call
does not stop the worker making it, and each one spends provider tokens. So the sweep that
opens the Run's budget notice also fails the Run as ``budget-exhausted`` and signals the
worker's process group through the grace-then-kill ladder. The worker here is a real
``sleep`` in its own process group, so the stop is observed as the signal it is.

A campaign round's Run ended this way keeps that ending: the round returns its step to
pending, charges what it spent, and the drive pauses naming the exhausted budget, as for
any round that ended without a report. A soft token ceiling still stops nothing.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.projection.campaign import CampaignView
from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.kernel.state.enums import EffortBucket
from eawf.kernel.state.epoch2.run import Run, RunStatus
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.budget.notices import load_notice_ledger, notices_path
from eawf.runtime.budget.service import TerminationResult
from eawf.runtime.daemon.budget_interlock import guard_in_flight_budget
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import campaign_run
from eawf.runtime.daemon.methods.campaign_run import CAMPAIGN_START_METHOD, HostResearchAgent
from eawf.runtime.daemon.methods.run_liveness import detect_wall_ceiling_crossings
from eawf.runtime.daemon.native_dispatch import run_binding_of
from eawf.runtime.daemon.run_supervisor import RunEndCause, end_exhausted_run, supervisor_of
from eawf.runtime.daemon.semantic_gateway import (
    GuardInputs,
    PreHandlerCheck,
    evaluate_pre_handler_checks,
)
from eawf.runtime.daemon.stall_sweep import sweep_once
from eawf.runtime.runtimes.adapter import NativeLaunchOutcome, NativeLaunchRequest, TurnOutcome
from eawf.runtime.runtimes.metering import UsageSample, meter_stream
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    tree_root,
)
from tests.integration.runtime.daemon.methods.test_campaign_run import _runs, _start, served
from tests.integration.runtime.daemon.test_campaign_round_driver import (
    FakeDriver,
    _isolated,  # noqa: F401 -- the round harness keeps every home and checkout under tmp
    canary,  # noqa: F401 -- the git-backed canary the round harness compiles against
)
from tests.integration.runtime.daemon.test_run_stall_interval import ACTOR, RUN_KEY, RUN_URN
from tests.integration.runtime.daemon.test_run_wall_ceiling_notice import (
    STARTED,
    WALL,
    attached,
    bound_canary,
    daemon_for,
)
from tests.unit.kernel.runtime.test_semantic_transport_schema import call_for

pytestmark = pytest.mark.integration


@contextmanager
def sleeping_worker() -> Iterator[subprocess.Popen[bytes]]:
    """Yield a live process leading its own group, reaped the moment it exits."""
    worker = subprocess.Popen(["sleep", "60"], start_new_session=True)
    # an unreaped exit stays a zombie the ladder would read as alive
    threading.Thread(target=worker.wait, daemon=True).start()
    try:
        yield worker
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=10)


def stored(provisioned: CanaryProvision, key: str = RUN_KEY) -> Run:
    """Return the Run *key* as the document or its ledger holds it now."""
    return Run.model_validate(_runs(provisioned)[key])


def test_a_run_at_its_wall_ceiling_is_failed_and_its_worker_stopped(tmp_path: Path) -> None:
    provisioned = bound_canary(tmp_path / "repo")
    ctx = daemon_for(provisioned, tmp_path / "runtime")
    supervisor = supervisor_of(attached(ctx))

    with sleeping_worker() as worker, supervisor.worker(RUN_URN, worker.pid):
        sweep_once(ctx, now=STARTED + WALL)
        assert worker.wait(timeout=10) == -signal.SIGTERM

    run = stored(provisioned)
    assert run.status is RunStatus.FAILED
    assert run.failure is not None
    assert run.failure.code == RunEndCause.BUDGET_EXHAUSTED.value == "budget-exhausted"
    assert "2400s" in run.failure.message
    (notice,) = load_notice_ledger(
        notices_path(tree_root(provisioned) / "state.json")
    ).notices.values()
    assert (notice.scope_id, notice.axis, notice.basis) == (RUN_KEY, "wall_seconds", "hard_limit")


def test_one_second_short_of_the_ceiling_the_worker_runs_on(tmp_path: Path) -> None:
    provisioned = bound_canary(tmp_path / "repo")
    ctx = daemon_for(provisioned, tmp_path / "runtime")

    with sleeping_worker() as worker, supervisor_of(attached(ctx)).worker(RUN_URN, worker.pid):
        sweep_once(ctx, now=STARTED + WALL - timedelta(seconds=1))
        assert worker.poll() is None

    assert stored(provisioned).status is RunStatus.RUNNING


def test_an_ended_run_is_refused_its_next_tool_call_as_not_running(tmp_path: Path) -> None:
    provisioned = bound_canary(tmp_path / "repo")
    ctx = daemon_for(provisioned, tmp_path / "runtime")
    sweep_once(ctx, now=STARTED + WALL)
    records = read_ledger_records(ledger_path(document_path(provisioned), Epoch2Collection.RUN))
    binding = run_binding_of(records, parse_qualified_urn(RUN_URN))
    assert binding is not None and binding.capsule is not None

    outcome = evaluate_pre_handler_checks(
        GuardInputs(
            call=call_for(SemanticToolId.REPO_READ, run_ref=RUN_URN),
            capsule=binding.capsule,
            run=stored(provisioned),
            binding=binding,
            control_facts=(),
            lease=None,
            prior=None,
            calls_so_far=0,
            now=STARTED + WALL + timedelta(seconds=1),
        )
    )

    assert (outcome.verdict, outcome.check) == ("refused", PreHandlerCheck.RUN_STATE)


def test_a_run_no_longer_running_is_left_alone_and_its_worker_not_signalled(
    tmp_path: Path,
) -> None:
    provisioned = bound_canary(tmp_path / "repo")
    ctx = daemon_for(provisioned, tmp_path / "runtime")
    context = attached(ctx)
    sweep_once(ctx, now=STARTED + WALL)

    with sleeping_worker() as worker, supervisor_of(context).worker(RUN_URN, worker.pid):
        again = end_exhausted_run(
            context,
            parse_qualified_urn(RUN_URN),
            actor=ACTOR,
            detail="a second reading",
            now=STARTED + WALL + timedelta(minutes=1),
        )
        assert again == ()
        assert worker.poll() is None


def test_a_run_whose_worker_this_daemon_does_not_hold_is_still_failed(tmp_path: Path) -> None:
    provisioned = bound_canary(tmp_path / "repo")
    ctx = daemon_for(provisioned, tmp_path / "runtime")

    assert supervisor_of(attached(ctx)).stop_worker(RUN_URN) is None
    sweep_once(ctx, now=STARTED + WALL)

    assert stored(provisioned).status is RunStatus.FAILED


def test_the_worker_hold_ends_with_its_turn(tmp_path: Path) -> None:
    provisioned = bound_canary(tmp_path / "repo")
    ctx = daemon_for(provisioned, tmp_path / "runtime")
    supervisor = supervisor_of(attached(ctx))

    with sleeping_worker() as worker:
        with supervisor.worker(RUN_URN, worker.pid):
            pass
        assert supervisor.stop_worker(RUN_URN) is None
        assert worker.poll() is None


def test_a_soft_token_ceiling_stops_nothing() -> None:
    signalled: list[int] = []

    def cancel(pgid: int) -> TerminationResult:
        signalled.append(pgid)
        raise AssertionError("a soft ceiling signalled the worker")

    class _NoLedger:
        def open(self, notice: Any) -> bool:
            raise AssertionError("a soft ceiling opened a budget control")

        def confirm(self, notice: Any, termination: Any) -> None:
            raise AssertionError("a soft ceiling confirmed a reap")

    outcome = guard_in_flight_budget(
        reading=meter_stream((UsageSample(input_tokens=1_500, output_tokens=500),)),
        base_budget=1_000,
        enforce="soft",
        multiplier=1.0,
        run_ref=parse_qualified_urn(RUN_URN),
        control_request_ref="CTL-00000001",
        noticed_at=STARTED,
        pgid=4242,
        ledger=_NoLedger(),
        cancel=cancel,
    )

    assert (outcome.terminated, outcome.notice, signalled) == (False, None, [])
    assert outcome.decision.over_cap is True


class StoppableDriver(FakeDriver):
    """A driver whose worker is a real process, ended only by a signal to its group.

    While the turn runs it hands the round to *sweep*, which is where the daemon finds the
    Run past its ceiling; the turn then ends the way a provider's does when its process is
    killed under it, with no answer.
    """

    def __init__(self, sweep: Callable[[NativeLaunchRequest], None]) -> None:
        super().__init__("claude")
        self._sweep = sweep
        self.worker: subprocess.Popen[bytes] | None = None

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        self.worker = subprocess.Popen(["sleep", "60"], start_new_session=True)
        launched = await super().launch(request)
        return launched.model_copy(update={"subprocess_pid": self.worker.pid})

    async def _turn(self, request: NativeLaunchRequest) -> TurnOutcome:
        assert self.worker is not None
        exited = asyncio.create_task(asyncio.to_thread(self.worker.wait))
        await asyncio.to_thread(self._sweep, request)
        await exited
        return TurnOutcome(exit_kind="failed", error_text="claude ended the turn without a result")


def test_a_round_ended_at_its_ceiling_keeps_that_ending_and_pauses_the_drive(
    canary: CanaryProvision,  # noqa: F811 -- the imported round-harness fixture
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    contexts: list[Epoch2RootContext] = []

    def sweep(request: NativeLaunchRequest) -> None:
        later = datetime.now(UTC) + timedelta(seconds=request.capsule.budget.wall_seconds)
        detect_wall_ceiling_crossings(contexts[0], now=later)

    driver = StoppableDriver(sweep)

    def agent_for(runtime: str, context: Epoch2RootContext, _depth: Any) -> HostResearchAgent:
        contexts.append(context)
        return HostResearchAgent(runtime, context, EffortBucket.S, launchers={"claude": driver})

    monkeypatch.setattr(campaign_run, "research_agent_for", agent_for)

    async def body() -> CampaignView:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="shallow", agents=1)
            )
            key = started["record"]["key"]
            while campaign_run.campaign_drive_in_flight():
                await asyncio.sleep(0.05)
            return CampaignView.model_validate(
                await client.call(
                    "projection.campaign.view", repo_root=str(canary.root), campaign_key=key
                )
            )

    with caplog.at_level("INFO"):
        view = asyncio.run(body())

    assert driver.worker is not None
    assert driver.worker.returncode == -signal.SIGTERM
    first = view.steps[0].step
    assert first.state.value == "pending", "the ended round returns its step to pending"
    assert view.status.value == "active", "a round's ceiling stops the round, not the Campaign"
    run = stored(canary, first.run_refs[0].entity_key)
    assert run.status is RunStatus.FAILED, "the round keeps the daemon's ending, not a cancel"
    assert run.failure is not None and run.failure.code == "budget-exhausted"
    ended = [r.getMessage() for r in caplog.records if "campaign drive ended" in r.getMessage()]
    assert ended
    assert "disposition=paused" in ended[0]
    assert "budget-exhausted" in ended[0]
