"""A dispatched Run's turn is awaited in the background and ends the Run with it.

The suite drives ``runtime.run.dispatch`` over the registered verb against a canary
with a real git repository under it, with a fake launcher whose turn blocks on an
event the test releases. So "the dispatch answered before the turn ended" is a fact
the test holds open rather than infers: the answer, the running Run, the in-flight
refusal and the active lease are all read while the turn is still parked. The report
the turn binds goes through ``submit_report`` on the semantic gateway, the verb a
worker reaches, and every status is read back through ``projection.run.detail.read``.

A restart is a fresh daemon context over the same tree: its supervisor registry is
empty, which is exactly what a new process has.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal
from unittest import mock

import pytest
from pydantic import ValidationError

import eawf.runtime.daemon.methods.projection
import eawf.runtime.daemon.methods.run
import eawf.runtime.daemon.methods.semantic
import eawf.runtime.daemon.methods.spend  # noqa: F401  (registers the verbs the suite reaches)
from eawf.kernel.projection.connection import read_method
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.events import (
    ErrorPayload,
    ProviderLossPayload,
    RunEventKind,
    RunEventRecord,
)
from eawf.kernel.runtime.lease import LeaseStatus, WorkLease
from eawf.kernel.runtime.semantic import SemanticCall
from eawf.kernel.state.epoch2.measurement import VendorSessionRef
from eawf.kernel.store.ledger import read_ledger_records
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods, native_dispatch
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.semantic import SEMANTIC_CALL_METHOD
from eawf.runtime.daemon.methods.spend import RUN_USAGE_READ_METHOD
from eawf.runtime.daemon.native_dispatch import (
    RUN_DISPATCH_METHOD,
    RUN_RETRY_METHOD,
    DispatchRefusal,
    compile_launchers,
)
from eawf.runtime.daemon.run_events import run_events_of
from eawf.runtime.daemon.run_supervisor import (
    RunEndCause,
    RunSupervisor,
    end_cause,
    supervisor_of,
    sweep_lost_runs,
)
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    TurnExitKind,
    TurnOutcome,
    compose_worker_hello,
)
from eawf.runtime.runtimes.metering import UsageSample
from eawf.runtime.workspace.lease import root_leases, workspace_path
from tests.integration.runtime.daemon.test_native_dispatch import (
    ACTOR,
    PROVIDER_KIND,
    capsule_request,
    dispatch_params,
    ledger_records,
    make_canary,
    method_ctx,
    root_ctx,
    run_urn,
)

pytestmark = pytest.mark.integration

#: The provider's own handle for the fake worker's session.
SESSION_REF: Final = "codex-session-supervised"

#: The cumulative usage the fake worker discloses mid-turn.
SAMPLE: Final = UsageSample(input_tokens=120, output_tokens=30)

#: The Run detail read the console opens a Run on.
RUN_DETAIL_READ: Final = read_method("run.detail")


@pytest.fixture(autouse=True)
def _empty_host_homes(isolated_host_homes: Path) -> Iterator[None]:
    """Read the Run's vendor session against empty host homes, never the operator's."""
    yield


# ---- the fake worker ----------------------------------------------------------


class ParkedTurnLauncher:
    """A launcher whose worker announces itself, then parks its turn on an event.

    The turn discloses one usage reading, waits for the test to release it, files
    the Run's report through the semantic gateway when asked to, and ends the way
    the test said it would.

    Attributes:
        provider_kind: The provider the fake stands in for.
        release: Set by the test to let the turn end.
        capsule: The capsule the Run was sealed under, as the launch carried it.
    """

    def __init__(
        self,
        ctx: MethodContext,
        canary: CanaryProvision,
        *,
        exit_kind: TurnExitKind = "completed",
        report: bool = True,
        error_text: str | None = None,
        raises: bool = False,
    ) -> None:
        """Bind the fake to the daemon its worker reaches and the ending it plays."""
        self.provider_kind = PROVIDER_KIND
        self.release = asyncio.Event()
        self.capsule: AuthorityCapsule | None = None
        self.launches = 0
        self._ctx = ctx
        self._canary = canary
        self._exit_kind: TurnExitKind = exit_kind
        self._report = report
        self._error_text = error_text
        self._raises = raises

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Announce the worker and hand the parked turn back."""
        self.launches += 1
        self.capsule = request.capsule
        turn = asyncio.get_running_loop().create_task(self._turn(request))
        return NativeLaunchOutcome(
            provider_session_ref=SESSION_REF,
            subprocess_pid=4242,
            hello=compose_worker_hello(
                spec=request.spec,
                capsule=request.capsule,
                provider_session_ref=SESSION_REF,
                sdk_version="1.0.0",
                worker_protocol_version="1.0.0",
                event_codec_version="1.0.0",
                hello_sequence=request.hello_sequence,
            ),
            turn=turn,
        )

    async def _turn(self, request: NativeLaunchRequest) -> TurnOutcome:
        """Disclose usage, wait for the release, report, and end."""
        assert request.usage_sink is not None, "a dispatch always meters its turn"
        await request.usage_sink(SAMPLE, None)
        await self.release.wait()
        if self._report:
            answer = await self.submit_report(request.capsule)
            assert answer["result"]["bounded_output"]["accepted"] is True, answer
        if self._raises:
            raise RuntimeError("the provider stream broke mid-turn")
        return TurnOutcome(exit_kind=self._exit_kind, error_text=self._error_text)

    async def submit_report(self, capsule: AuthorityCapsule) -> dict[str, Any]:
        """File the Run's terminal report through the gateway, as a worker does."""
        call = SemanticCall.seal(
            {
                "call_id": f"call-{1:016x}",
                "run_ref": str(run_urn()),
                "contract_digest": capsule.contract_digest,
                "idempotency_key": "report-01",
                "tool_id": "submit_report",
                "tool_schema_version": "1.0.0",
                "payload": {
                    "tool_id": "submit_report",
                    "verdict": "pass",
                    "summary": "the supervised turn finished its work",
                },
                "requested_at": datetime.now(UTC),
            }
        )
        return await methods.dispatch(
            SEMANTIC_CALL_METHOD,
            self._ctx,
            {
                "repo_root": str(self._canary.root),
                "call": call.model_dump(mode="json"),
                "capsule": capsule.model_dump(mode="json"),
            },
        )


# ---- driving the daemon -------------------------------------------------------


def params_of(canary: CanaryProvision, *, key: str = "dispatch-01") -> dict[str, Any]:
    """Return a dispatch whose capsule grants the report the turn files."""
    return dispatch_params(
        canary, key=key, capsule=capsule_request(tool_grants=["budget_status", "submit_report"])
    )


async def call(ctx: MethodContext, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one registered verb the way the socket listener does."""
    return await methods.dispatch(method, ctx, params)


def detail(ctx: MethodContext, canary: CanaryProvision) -> dict[str, Any]:
    """Return the Run as ``projection.run.detail.read`` answers for it."""
    answer = asyncio.run(
        call(ctx, RUN_DETAIL_READ, {"repo_root": str(canary.root), "key": str(run_urn())})
    )
    rows: list[dict[str, Any]] = answer["rows"]
    (row,) = [row for row in rows if row["urn"] == str(run_urn())]
    return row


def status_of(row: dict[str, Any]) -> str:
    """Return the status a detail row states."""
    return str(row["status"]["value"])


def stream(canary: CanaryProvision, runtime: Path) -> tuple[RunEventRecord, ...]:
    """Return the Run's own event lines, in ledger order."""
    return run_events_of(ledger_records(canary, runtime), run_urn())


def lease_of(canary: CanaryProvision, runtime: Path) -> WorkLease:
    """Return the one lease the Run was issued."""
    (lease,) = root_leases(root_ctx(canary, runtime))
    return lease


def stored_run(canary: CanaryProvision, runtime: Path) -> dict[str, Any]:
    """Return the Run's stored record, whichever tier holds it."""
    context = root_ctx(canary, runtime)
    with context.session([run_urn()]) as session:
        records = read_ledger_records(native_dispatch.run_ledger(session))
        return native_dispatch.stored_run(session, records, run_urn()).model_dump(mode="json")


def drive_to_end(
    tmp_path: Path, **ending: Any
) -> tuple[CanaryProvision, Path, MethodContext, dict[str, Any]]:
    """Dispatch, release the parked turn, and wait for the supervisor to end the Run."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    ctx = method_ctx(runtime)
    launcher = ParkedTurnLauncher(ctx, canary, **ending)

    async def scenario() -> dict[str, Any]:
        answer = await call(ctx, RUN_DISPATCH_METHOD, params_of(canary))
        context = ctx.native_root_context(canary.root / ".ea")
        task = supervisor_of(context).task_of(str(run_urn()))
        assert task is not None, "the dispatch handed its turn to no supervisor"
        launcher.release.set()
        await task
        return answer

    with mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
    ):
        answer = asyncio.run(scenario())
    return canary, runtime, ctx, answer


# ---- CR-1, CR-2 start, CR-3: the answer comes while the turn still runs -------


def test_dispatch_answers_announced_while_the_turn_is_parked(tmp_path: Path) -> None:
    """The verb returns at the announcement; a supervisor holds the turn meanwhile."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    ctx = method_ctx(runtime)
    launcher = ParkedTurnLauncher(ctx, canary)
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        answer = await call(ctx, RUN_DISPATCH_METHOD, params_of(canary))
        context = ctx.native_root_context(canary.root / ".ea")
        task = supervisor_of(context).task_of(str(run_urn()))
        seen["answer"] = answer
        seen["supervised"] = task is not None and not task.done()
        seen["running"] = await asyncio.to_thread(stored_run, canary, runtime)
        lease = lease_of(canary, runtime)
        seen["lease"] = lease.status
        seen["workspace"] = workspace_path(context, handle=lease.workspace_handle).is_dir()
        with pytest.raises(DaemonValidationError, match=DispatchRefusal.ATTEMPT_IN_FLIGHT.value):
            await call(ctx, RUN_DISPATCH_METHOD, params_of(canary))
        with pytest.raises(DaemonValidationError, match=DispatchRefusal.ATTEMPT_IN_FLIGHT.value):
            await call(ctx, RUN_DISPATCH_METHOD, params_of(canary, key="dispatch-02"))
        seen["launches"] = launcher.launches
        assert task is not None
        launcher.release.set()
        await task

    with mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
    ):
        asyncio.run(scenario())

    assert seen["answer"]["stage"] == "announced"
    assert seen["supervised"] is True
    running = seen["running"]
    assert running["status"] == "RUNNING"
    assert running["started_at"] is not None
    assert running["vendor_session"] == VendorSessionRef(
        harness="codex", session_digest=SESSION_REF
    ).model_dump(mode="json")
    assert seen["lease"] is LeaseStatus.ACTIVE
    assert seen["workspace"] is True
    assert seen["launches"] == 1


# ---- CR-2 ends, CR-3 release, CR-5 usage ---------------------------------------


def test_a_turn_ending_with_its_report_bound_completes_the_run(tmp_path: Path) -> None:
    """The bound report completes the Run, and its lease leaves ACTIVE after."""
    canary, runtime, ctx, answer = drive_to_end(tmp_path)

    assert answer["stage"] == "announced"
    assert status_of(detail(ctx, canary)) == "COMPLETED"
    kinds = [line.event_kind for line in stream(canary, runtime)]
    assert RunEventKind.ERROR_OBSERVED not in kinds
    assert RunEventKind.USAGE_OBSERVED in kinds
    assert lease_of(canary, runtime).status in {LeaseStatus.RELEASED, LeaseStatus.QUARANTINED}


def test_the_turns_usage_is_measured_on_the_runs_stream(tmp_path: Path) -> None:
    """The reading the worker disclosed mid-turn is the Run's measured spend."""
    canary, _runtime, ctx, _answer = drive_to_end(tmp_path)

    usage = asyncio.run(
        call(ctx, RUN_USAGE_READ_METHOD, {"repo_root": str(canary.root), "urn": str(run_urn())})
    )

    assert usage["tokens"] > 0
    assert usage["quality"] == "measured"


def test_a_clean_workspace_is_released_once_the_run_is_terminal(tmp_path: Path) -> None:
    """A worker that left nothing behind gives its worktree back."""
    canary, runtime, _ctx, _answer = drive_to_end(tmp_path)

    lease = lease_of(canary, runtime)
    assert lease.status is LeaseStatus.RELEASED
    assert not workspace_path(root_ctx(canary, runtime), handle=lease.workspace_handle).exists()


@pytest.mark.parametrize(
    ("ending", "cause"),
    [
        ({"exit_kind": "failed", "error_text": "the provider refused the turn"}, "turn-failed"),
        ({"exit_kind": "interrupted"}, "turn-interrupted"),
        ({"exit_kind": "budget_terminated"}, "budget-terminated"),
        ({"report": False}, "report-unbound"),
        ({"raises": True, "report": False}, "turn-failed"),
    ],
    ids=["failed", "interrupted", "budget-terminated", "no-report", "turn-raised"],
)
def test_a_turn_that_does_not_complete_with_a_report_fails_the_run(
    tmp_path: Path, ending: dict[str, Any], cause: str
) -> None:
    """Every other ending is FAILED, with an ``error_observed`` line naming why."""
    canary, runtime, ctx, _answer = drive_to_end(tmp_path, **ending)

    assert status_of(detail(ctx, canary)) == "FAILED"
    errors = [
        line for line in stream(canary, runtime) if line.event_kind is RunEventKind.ERROR_OBSERVED
    ]
    assert len(errors) == 1
    payload = errors[0].payload
    assert isinstance(payload, ErrorPayload)
    assert payload.code == cause
    assert payload.retry_class == "new_linked_run"
    assert stored_run(canary, runtime)["failure"]["code"] == cause
    assert lease_of(canary, runtime).status in {LeaseStatus.RELEASED, LeaseStatus.QUARANTINED}


def test_the_turns_own_words_are_filed_as_the_errors_trace(tmp_path: Path) -> None:
    """A failed turn's error text is the line's message and its stored diagnostic."""
    canary, runtime, _ctx, _answer = drive_to_end(
        tmp_path, exit_kind="failed", error_text="the provider refused the turn"
    )

    (line,) = [item for item in stream(canary, runtime) if isinstance(item.payload, ErrorPayload)]
    assert isinstance(line.payload, ErrorPayload)
    assert line.payload.message == "the provider refused the turn"
    assert line.payload.diagnostic_ref is not None


# ---- CR-4: a restart leaves no orphan -------------------------------------------


def park_and_lose(tmp_path: Path) -> tuple[CanaryProvision, Path]:
    """Dispatch with a parked turn, then end the daemon's loop with the turn still parked.

    Ending the loop cancels the supervisor and the turn without either ending the
    Run, which is what a daemon that died mid-turn leaves on the tree.
    """
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    ctx = method_ctx(runtime)
    launcher = ParkedTurnLauncher(ctx, canary)
    with mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
    ):
        answer = asyncio.run(call(ctx, RUN_DISPATCH_METHOD, params_of(canary)))
    assert answer["stage"] == "announced"
    assert stored_run(canary, runtime)["status"] == "RUNNING"
    return canary, runtime


def lost_lines(canary: CanaryProvision, runtime: Path) -> list[RunEventRecord]:
    """Return the Run's ``provider_lost`` lines."""
    return [
        line for line in stream(canary, runtime) if line.event_kind is RunEventKind.PROVIDER_LOST
    ]


def test_a_restarted_daemon_fails_a_run_nobody_awaits(tmp_path: Path) -> None:
    """The boot sweep states the loss once and moves the Run to FAILED."""
    canary, runtime = park_and_lose(tmp_path)
    context = root_ctx(canary, runtime)

    sweep_lost_runs(context, supervisor_of(context), actor=ACTOR, now=datetime.now(UTC))
    sweep_lost_runs(context, supervisor_of(context), actor=ACTOR, now=datetime.now(UTC))

    assert stored_run(canary, runtime)["status"] == "FAILED"
    assert stored_run(canary, runtime)["failure"]["code"] == RunEndCause.PROVIDER_LOST.value
    (lost,) = lost_lines(canary, runtime)
    assert isinstance(lost.payload, ProviderLossPayload)
    assert lease_of(canary, runtime).status is not LeaseStatus.ACTIVE


def test_the_retry_verb_sweeps_first_and_then_asks_for_a_successor(tmp_path: Path) -> None:
    """After a restart, retrying the lost Run is a linked retry, not a resume."""
    canary, runtime = park_and_lose(tmp_path)
    ctx = method_ctx(runtime)
    params = {
        "repo_root": str(canary.root),
        "urn": str(run_urn()),
        "actor": ACTOR,
        "idempotency_key": "retry-01",
    }

    with pytest.raises(DaemonValidationError, match=DispatchRefusal.SUCCESSOR_REQUIRED.value):
        asyncio.run(call(ctx, RUN_RETRY_METHOD, params))

    assert status_of(detail(ctx, canary)) == "FAILED"
    assert len(lost_lines(canary, runtime)) == 1


def test_a_redispatch_after_a_restart_is_refused_on_the_failed_run(tmp_path: Path) -> None:
    """The dispatch verb sweeps too, so it never resumes a Run nobody awaits."""
    canary, runtime = park_and_lose(tmp_path)
    ctx = method_ctx(runtime)
    launcher = ParkedTurnLauncher(ctx, canary)

    with (
        mock.patch.object(
            native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
        ),
        pytest.raises(DaemonValidationError, match=DispatchRefusal.RUN_NOT_DISPATCHABLE.value),
    ):
        asyncio.run(call(ctx, RUN_DISPATCH_METHOD, params_of(canary)))
    assert launcher.launches == 0
    assert len(lost_lines(canary, runtime)) == 1


def test_the_sweep_leaves_a_supervised_run_alone(tmp_path: Path) -> None:
    """A Run whose turn this daemon awaits is not lost, whatever its status."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    ctx = method_ctx(runtime)
    launcher = ParkedTurnLauncher(ctx, canary)

    async def scenario() -> None:
        await call(ctx, RUN_DISPATCH_METHOD, params_of(canary))
        context = ctx.native_root_context(canary.root / ".ea")
        supervisor = supervisor_of(context)
        envelopes = await asyncio.to_thread(
            sweep_lost_runs, context, supervisor, actor=ACTOR, now=datetime.now(UTC)
        )
        assert envelopes == ()
        task = supervisor.task_of(str(run_urn()))
        assert task is not None
        launcher.release.set()
        await task

    with mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
    ):
        asyncio.run(scenario())
    assert lost_lines(canary, runtime) == []
    assert stored_run(canary, runtime)["status"] == "COMPLETED"


def test_the_sweep_finds_nothing_on_a_tree_with_no_lease(tmp_path: Path) -> None:
    """An undispatched tree has no Run that could have lost its worker."""
    canary = make_canary(tmp_path / "repo")
    context = root_ctx(canary, tmp_path / "runtime")

    assert sweep_lost_runs(context, RunSupervisor(), actor=ACTOR, now=datetime.now(UTC)) == ()
    assert stored_run(canary, tmp_path / "runtime")["status"] == "QUEUED"


# ---- the supervisor's own rules ---------------------------------------------------


@pytest.mark.parametrize(
    ("exit_kind", "reported", "cause"),
    [
        ("completed", True, None),
        ("completed", False, RunEndCause.REPORT_UNBOUND),
        ("failed", True, RunEndCause.TURN_FAILED),
        ("interrupted", True, RunEndCause.TURN_INTERRUPTED),
        ("budget_terminated", True, RunEndCause.BUDGET_TERMINATED),
    ],
)
def test_end_cause_reads_the_turn_before_the_report(
    exit_kind: Literal["completed", "failed", "interrupted", "budget_terminated"],
    reported: bool,
    cause: RunEndCause | None,
) -> None:
    """Only a completed turn with a bound report completes; a report does not mend a failure."""
    assert end_cause(TurnOutcome(exit_kind=exit_kind), reported=reported) is cause


def test_turn_outcome_refuses_an_ending_outside_the_four() -> None:
    """The ending vocabulary is closed, so the supervisor never meets a fifth."""
    with pytest.raises(ValidationError):
        TurnOutcome.model_validate({"exit_kind": "vanished"})


def test_a_supervisor_awaits_one_turn_per_run() -> None:
    """A second supervisor for one Run is refused; a finished one is forgotten."""

    async def scenario() -> tuple[bool, bool]:
        supervisor = RunSupervisor()
        gate = asyncio.Event()

        async def parked() -> None:
            await gate.wait()

        async def other() -> None:
            return None

        task = supervisor.supervise("RUN-A", parked())
        with pytest.raises(RuntimeError, match="already supervised"):
            supervisor.supervise("RUN-A", other())
        held = supervisor.task_of("RUN-A") is task
        gate.set()
        await task
        return held, supervisor.task_of("RUN-A") is None

    held, forgotten = asyncio.run(scenario())
    assert held is True
    assert forgotten is True


def test_a_supervisor_is_swept_exactly_once() -> None:
    """The first verb after a start sweeps; every later one does not."""
    supervisor = RunSupervisor()

    assert supervisor.claim_sweep() is True
    assert supervisor.claim_sweep() is False


def test_a_fresh_daemon_context_starts_with_an_empty_registry(tmp_path: Path) -> None:
    """Two daemon processes over one tree never share a supervisor."""
    canary = make_canary(tmp_path / "repo")
    first = root_ctx(canary, tmp_path / "runtime")
    second = root_ctx(canary, tmp_path / "runtime")

    assert supervisor_of(first) is supervisor_of(first)
    assert supervisor_of(first) is not supervisor_of(second)


@pytest.mark.parametrize("message", ["", "x" * 501])
def test_provider_loss_refuses_a_message_outside_its_bound(message: str) -> None:
    """The loss line carries one bounded sentence, never nothing and never prose."""
    with pytest.raises(ValidationError):
        ProviderLossPayload(message=message)


def test_provider_loss_refuses_an_unknown_field() -> None:
    """The payload is closed, so a drifted writer is refused at the line."""
    with pytest.raises(ValidationError):
        ProviderLossPayload.model_validate({"message": "lost", "pid": 1})
