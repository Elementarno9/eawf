"""A dispatched Run never stays QUEUED or RUNNING, nor keeps its lease, after a failure.

Each case drives ``runtime.task.dispatch``, or the driver beneath it, against a
canary over a real git repository, with a fake launcher standing in for the
worker. The failure is injected where production meets it -- the launcher, the
start of the Run, the supervisor's ending, the stream -- and the Run, its lease
and its Task are then read back off the tree, so "nothing is left stale" is a
claim about the tree rather than about the answer.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from unittest import mock

import pytest

from eawf.kernel.identity import QualifiedUrn, parse_qualified_urn
from eawf.kernel.runtime.events import MessageSummaryPayload
from eawf.kernel.state.epoch2.task_status import TaskStatus
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods, native_dispatch, run_supervisor
from eawf.runtime.daemon.dispatch_stream import message_sink
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusalCode, TransactionRefusedError
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, task_dispatch
from eawf.runtime.daemon.methods.domain_envelope import DOMAIN_TRANSITION_METHOD
from eawf.runtime.daemon.methods.task_dispatch import TASK_DISPATCH_METHOD
from eawf.runtime.daemon.native_dispatch import DispatchParams, DispatchRefusal, compile_launchers
from eawf.runtime.daemon.run_supervisor import supervisor_of
from eawf.runtime.daemon.transcript_store import (
    TranscriptEntry,
    keep_message,
    transcript_dir,
    transcript_ref,
)
from eawf.runtime.runtimes.adapter import NativeLaunchOutcome, NativeLaunchRequest
from eawf.runtime.runtimes.host_transcript import SpokenMessage
from tests import _provider_helpers as fx
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    rekeyed,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_native_dispatch import ACTOR, method_ctx
from tests.integration.runtime.daemon.test_task_dispatch import (
    BUDGET,
    FIRST_RUN,
    TASK_KEY,
    TASK_URN,
    ParkedLauncher,
    active_leases,
    make_tree,
    run_now,
    task_now,
    task_row,
)

#: A second Task, so two dispatches can race for the next Run key.
OTHER_TASK: Final = "EAWF-0043"


@pytest.fixture(autouse=True)
def _private_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_homes: Path
) -> Iterator[None]:
    """Read the global config layer and every host home from empty directories."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    yield


def run_urn(key: str = FIRST_RUN) -> QualifiedUrn:
    """Return the URN of the canary's Run keyed *key*."""
    return parse_qualified_urn(fx.RUN_URN.rsplit("/", 1)[0] + f"/{key}")


def root_of(ctx: MethodContext, canary: CanaryProvision) -> Epoch2RootContext:
    """Return the canary's native context, as the daemon holds it."""
    return ctx.native_root_context(canary.root / ".ea")


def task_params(canary: CanaryProvision, task: str = TASK_KEY) -> dict[str, Any]:
    """Return the params of one Task dispatch."""
    return {"repo_root": str(canary.root), "actor": ACTOR, "budget": BUDGET, "task": task}


async def settle(context: Epoch2RootContext, *keys: str) -> None:
    """Await the supervised turn of every Run keyed in *keys*, if one is supervised."""
    for key in keys:
        turn = supervisor_of(context).task_of(str(run_urn(key)))
        if turn is not None:
            await turn


def launching(launcher: ParkedLauncher) -> Any:
    """Return the patch that makes *launcher* the only one a dispatch reaches."""
    return mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
    )


def assert_settled(ctx: MethodContext, canary: CanaryProvision, code: str) -> None:
    """Assert the first Run failed under *code* and holds no lease any more."""
    run = run_now(ctx, canary)
    assert run.status.value == "FAILED", run.status
    assert run.failure is not None
    assert run.failure.code == code
    assert active_leases(ctx, canary) == []


class GatedLauncher(ParkedLauncher):
    """A launcher whose launch waits, inside the provider call, until the test opens it."""

    def __init__(self, provider_kind: str) -> None:
        """Stand in for *provider_kind*'s launcher, closed."""
        super().__init__(provider_kind)
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Wait to be opened, then announce the worker."""
        self.entered.set()
        await self.proceed.wait()
        return await super().launch(request)


class RaisingLauncher(ParkedLauncher):
    """A launcher whose provider call raises *error* rather than a spawn error."""

    def __init__(self, provider_kind: str, error: BaseException) -> None:
        """Stand in for *provider_kind*'s launcher, failing with *error*."""
        super().__init__(provider_kind)
        self.error = error

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Raise the configured error."""
        self.launches += 1
        raise self.error


# ---- a same-key retry during a slow launch ---------------------------------------


def test_a_same_key_retry_during_a_slow_launch_starts_no_second_worker(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    context = root_of(ctx, canary)
    launcher = GatedLauncher("claude")
    sent: list[DispatchParams] = []
    driver = native_dispatch.dispatch_run

    async def spy(context: Epoch2RootContext, args: DispatchParams, **kwargs: Any) -> Any:
        sent.append(args)
        return await driver(context, args, **kwargs)

    async def scenario() -> list[Any]:
        first = asyncio.create_task(
            methods.dispatch(TASK_DISPATCH_METHOD, ctx, task_params(canary))
        )
        await launcher.entered.wait()
        retry = asyncio.create_task(driver(context, sent[0], now=datetime.now(UTC)))
        await asyncio.wait({retry}, timeout=2)
        launcher.proceed.set()
        answers = await asyncio.gather(first, retry, return_exceptions=True)
        launcher.release.set()
        await settle(context, FIRST_RUN)
        return answers

    with launching(launcher), mock.patch.object(task_dispatch, "dispatch_run", spy):
        first, retry = asyncio.run(scenario())

    assert isinstance(first, dict), first
    assert isinstance(retry, DaemonValidationError), retry
    assert DispatchRefusal.ATTEMPT_IN_FLIGHT.value in str(retry)
    assert launcher.launches == 1


def test_the_launch_hold_is_released_once_the_dispatch_ends() -> None:
    supervisor = run_supervisor.RunSupervisor()

    with supervisor.launching("run-a"):
        assert supervisor.busy("run-a")
        assert not supervisor.busy("run-b")
        with (
            pytest.raises(DaemonValidationError, match=DispatchRefusal.ATTEMPT_IN_FLIGHT.value),
            supervisor.launching("run-a"),
        ):
            pass
    assert not supervisor.busy("run-a")

    with pytest.raises(ValueError, match="boom"), supervisor.launching("run-a"):
        raise ValueError("boom")
    assert not supervisor.busy("run-a")


# ---- parallel dispatch allocates distinct Runs ---------------------------------------


def test_parallel_dispatches_of_two_tasks_create_two_runs(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    other = rekeyed(task_row("PLANNED", write_claims=["docs"]), key=OTHER_TASK)
    seed(canary, {"task": {OTHER_TASK: other}})
    ctx = method_ctx(tmp_path / "runtime")
    context = root_of(ctx, canary)
    launcher = ParkedLauncher("claude")
    allocate = task_dispatch.next_run_urn

    def slow_allocation(context: Epoch2RootContext, task: Any) -> QualifiedUrn:
        # Hold the guessed key long enough for the sibling dispatch to guess too.
        guessed = allocate(context, task)
        time.sleep(0.5)
        return guessed

    async def scenario() -> list[Any]:
        answers = await asyncio.gather(
            methods.dispatch(TASK_DISPATCH_METHOD, ctx, task_params(canary)),
            methods.dispatch(TASK_DISPATCH_METHOD, ctx, task_params(canary, OTHER_TASK)),
            return_exceptions=True,
        )
        working.append(task_now(ctx, canary))
        launcher.release.set()
        await settle(context, FIRST_RUN, "RUN-00000002")
        return answers

    working: list[Any] = []
    with (
        launching(launcher),
        mock.patch.object(task_dispatch, "next_run_urn", slow_allocation),
    ):
        first, second = asyncio.run(scenario())

    assert isinstance(first, dict), first
    assert isinstance(second, dict), second
    assert {first["run_key"], second["run_key"]} == {FIRST_RUN, "RUN-00000002"}
    assert first["task_key"] == TASK_KEY
    assert second["task_key"] == OTHER_TASK
    assert launcher.launches == 2
    assert str(working[0].active_run_ref) == first["run_ref"]


# ---- any launch failure fails the Run and withdraws its lease --------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        pytest.param(OSError("the wrapper could not be written"), "dispatch-spawn-failed", id="os"),
        pytest.param(RuntimeError("the launcher broke"), "dispatch-failed", id="runtime"),
    ],
)
def test_a_launch_that_raises_fails_the_run_and_frees_the_task(
    tmp_path: Path, error: BaseException, code: str
) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")

    async def scenario() -> BaseException | None:
        try:
            await methods.dispatch(TASK_DISPATCH_METHOD, ctx, task_params(canary))
        except BaseException as raised:
            return raised
        return None

    with launching(RaisingLauncher("claude", error)):
        raised = asyncio.run(scenario())

    assert raised is not None
    assert_settled(ctx, canary, code)
    assert task_now(ctx, canary).status is TaskStatus.PLANNED

    launcher = ParkedLauncher("claude")

    async def again() -> dict[str, Any]:
        answer = await methods.dispatch(TASK_DISPATCH_METHOD, ctx, task_params(canary))
        launcher.release.set()
        await settle(root_of(ctx, canary), "RUN-00000002")
        return answer

    with launching(launcher):
        answer = asyncio.run(again())

    assert answer["run_key"] == "RUN-00000002"
    assert launcher.launches == 1


def test_a_cancelled_dispatch_fails_the_run_and_still_raises(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    launcher = GatedLauncher("claude")

    async def scenario() -> bool:
        dispatched = asyncio.create_task(
            methods.dispatch(TASK_DISPATCH_METHOD, ctx, task_params(canary))
        )
        await launcher.entered.wait()
        dispatched.cancel()
        try:
            await dispatched
        except asyncio.CancelledError:
            return True
        return False

    with launching(launcher):
        cancelled = asyncio.run(scenario())

    assert cancelled
    assert_settled(ctx, canary, "dispatch-cancelled")
    assert supervisor_of(root_of(ctx, canary)).busy(str(run_urn())) is False


# ---- a Run the dispatch could not start is ended by its supervisor -------------------


def test_a_refused_start_after_the_worker_is_supervised_still_ends_the_run(
    tmp_path: Path,
) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    context = root_of(ctx, canary)
    launcher = ParkedLauncher("claude")
    refusal = TransactionRefusedError(
        code=TransactionRefusalCode.REVISION_CONFLICT,
        detail="the Run moved under the start",
        entity_ref=str(run_urn()),
        remediation="Read the Run again.",
    )

    async def scenario() -> BaseException | None:
        try:
            await methods.dispatch(TASK_DISPATCH_METHOD, ctx, task_params(canary))
        except DaemonValidationError as raised:
            refused: BaseException | None = raised
        else:
            refused = None
        assert run_now(ctx, canary).status.value == "QUEUED"
        launcher.release.set()
        await settle(context, FIRST_RUN)
        return refused

    with launching(launcher), mock.patch.object(run_supervisor, "start_run", side_effect=refusal):
        refused = asyncio.run(scenario())

    assert refused is not None
    assert_settled(ctx, canary, "report-unbound")


def test_an_announcement_that_raises_leaves_the_run_to_its_supervisor(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    context = root_of(ctx, canary)
    launcher = ParkedLauncher("claude")

    async def scenario() -> BaseException | None:
        try:
            await methods.dispatch(TASK_DISPATCH_METHOD, ctx, task_params(canary))
        except OSError as raised:
            refused: BaseException | None = raised
        else:
            refused = None
        assert run_now(ctx, canary).status.value == "RUNNING"
        launcher.release.set()
        await settle(context, FIRST_RUN)
        return refused

    with (
        launching(launcher),
        mock.patch.object(
            native_dispatch, "accept_announcement", side_effect=OSError("the ledger was full")
        ),
    ):
        refused = asyncio.run(scenario())

    assert isinstance(refused, OSError)
    assert_settled(ctx, canary, "report-unbound")


def test_an_ending_that_raises_still_fails_the_run_and_withdraws_its_lease(
    tmp_path: Path,
) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    context = root_of(ctx, canary)
    launcher = ParkedLauncher("claude")
    judged = run_supervisor.end_cause
    calls = itertools.count()

    def flaky(turn: Any, *, reported: bool, stopped: bool) -> Any:
        if next(calls) == 0:
            raise RuntimeError("the report ledger could not be read")
        return judged(turn, reported=reported, stopped=stopped)

    async def scenario() -> dict[str, Any]:
        answer = await methods.dispatch(TASK_DISPATCH_METHOD, ctx, task_params(canary))
        launcher.release.set()
        await settle(context, FIRST_RUN)
        return answer

    with launching(launcher), mock.patch.object(run_supervisor, "end_cause", flaky):
        answer = asyncio.run(scenario())

    assert answer["stage"] == "announced"
    assert_settled(ctx, canary, "turn-failed")


def test_an_edit_record_that_raises_does_not_keep_the_run_running(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    context = root_of(ctx, canary)
    launcher = ParkedLauncher("claude")

    async def scenario() -> None:
        await methods.dispatch(TASK_DISPATCH_METHOD, ctx, task_params(canary))
        launcher.release.set()
        await settle(context, FIRST_RUN)

    with (
        launching(launcher),
        mock.patch.object(
            run_supervisor, "record_worker_edits", side_effect=OSError("git was gone")
        ),
    ):
        asyncio.run(scenario())

    assert_settled(ctx, canary, "report-unbound")


# ---- the stream never stops the worker -----------------------------------------------


def spoken(text: str) -> SpokenMessage:
    """Return *text* as a launcher hands a message."""
    return SpokenMessage(message_role="assistant", summary=text, truncated=False, text=text)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError("disk full"), id="os"),
        pytest.param(RuntimeError("lock lost"), id="runtime"),
        pytest.param(TimeoutError("lock timeout"), id="timeout"),
    ],
)
def test_a_stream_line_that_raises_is_dropped_not_raised(
    tmp_path: Path, error: BaseException
) -> None:
    canary = make_tree(tmp_path)
    context = root_of(method_ctx(tmp_path / "runtime"), canary)
    sink = message_sink(context, run_urn(), actor=ACTOR, attempt_ref="ATT-stream")
    item = MessageSummaryPayload(message_role="assistant", summary="working")

    with mock.patch("eawf.runtime.daemon.methods.run.append_run_event", side_effect=error):
        asyncio.run(sink(item))


def test_a_transcript_store_that_raises_costs_only_the_link(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    context = root_of(method_ctx(tmp_path / "runtime"), canary)
    sink = message_sink(context, run_urn(), actor=ACTOR, attempt_ref="ATT-store")
    stated: list[Any] = []

    def record(context: Epoch2RootContext, append: Any, **_: Any) -> None:
        stated.append(append.payload)

    with (
        mock.patch(
            "eawf.runtime.daemon.dispatch_stream.keep_message",
            side_effect=ValueError("the generation is not selected"),
        ),
        mock.patch("eawf.runtime.daemon.methods.run.append_run_event", side_effect=record),
    ):
        asyncio.run(sink(spoken("all done")))

    (payload,) = stated
    assert payload.summary == "all done"
    assert payload.content_artifact_ref is None


def transcript_file(context: Epoch2RootContext) -> Path:
    """Return the canary Run's transcript file, its directory created."""
    directory = transcript_dir(context)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{FIRST_RUN.lower()}.jsonl"


def test_a_transcript_line_that_is_not_utf8_is_skipped(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    context = root_of(method_ctx(tmp_path / "runtime"), canary)
    path = transcript_file(context)
    path.write_bytes(b"\xff\xfe not text\n")

    ref = keep_message(context, run_urn(), event_ref="EVT-a", text="kept", now=datetime.now(UTC))

    assert ref == transcript_ref(run_urn(), "EVT-a")
    assert path.read_bytes().count(b"\n") == 2


# ---- keeping a message reads only what it has not read --------------------------------


def test_keeping_many_messages_does_not_reread_the_file(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    context = root_of(method_ctx(tmp_path / "runtime"), canary)
    parse = TranscriptEntry.model_validate_json
    count = 40

    with mock.patch.object(TranscriptEntry, "model_validate_json", side_effect=parse) as parsed:
        for index in range(count):
            keep_message(
                context, run_urn(), event_ref=f"EVT-{index}", text="w", now=datetime.now(UTC)
            )

    assert parsed.call_count < count
    lines = transcript_file(context).read_text(encoding="utf-8").splitlines()
    assert len(lines) == count


def test_a_replayed_message_is_not_kept_twice(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    context = root_of(method_ctx(tmp_path / "runtime"), canary)

    for _ in range(2):
        keep_message(context, run_urn(), event_ref="EVT-a", text="once", now=datetime.now(UTC))

    assert len(transcript_file(context).read_text(encoding="utf-8").splitlines()) == 1


def test_a_line_another_writer_appended_is_seen(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    context = root_of(method_ctx(tmp_path / "runtime"), canary)
    keep_message(context, run_urn(), event_ref="EVT-a", text="first", now=datetime.now(UTC))
    path = transcript_file(context)
    foreign = TranscriptEntry(
        ref=transcript_ref(run_urn(), "EVT-b"),
        lines=("second",),
        withheld_lines=0,
        recorded_at=datetime.now(UTC),
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{foreign.model_dump_json()}\n")

    keep_message(context, run_urn(), event_ref="EVT-b", text="second", now=datetime.now(UTC))

    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_a_pruned_file_is_written_afresh(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    context = root_of(method_ctx(tmp_path / "runtime"), canary)
    keep_message(context, run_urn(), event_ref="EVT-a", text="first", now=datetime.now(UTC))
    path = transcript_file(context)
    path.unlink()

    keep_message(context, run_urn(), event_ref="EVT-a", text="first", now=datetime.now(UTC))

    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


# ---- the generic transition verb evaluates the edge's guards ------------------------


def requeue(ctx: MethodContext, canary: CanaryProvision, **extra: Any) -> dict[str, Any]:
    """Ask the generic transition verb to hand the seeded Task back to PLANNED."""
    task = task_now(ctx, canary)
    answer: dict[str, Any] = asyncio.run(
        methods.dispatch(
            DOMAIN_TRANSITION_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": str(TASK_URN),
                "to_status": "PLANNED",
                "expected_revision": task.revision,
                "idempotency_key": "requeue-generic",
                "actor": "OP-0001",
                **extra,
            },
        )
    )
    return answer


def guarded_tree(tmp_path: Path, run_status: str) -> tuple[MethodContext, CanaryProvision]:
    """Provision a Task RUNNING under the seeded Run, which sits in *run_status*."""
    canary = provision(tmp_path / "repo")
    task = seed_row("task", "RUNNING")
    task["claimed_by"] = "OP-0001"
    seed(canary, {"run": {"RUN-00000010": seed_row("run", run_status)}, "task": {TASK_KEY: task}})
    return method_context(tmp_path / "rpc"), canary


@pytest.mark.parametrize(
    ("run_status", "extra", "guard"),
    [
        pytest.param("FAILED", {}, "reason_recorded", id="no-reason"),
        pytest.param("RUNNING", {"reason_code": "worker-lost"}, "no_active_run", id="open-run"),
    ],
)
def test_the_generic_verb_refuses_an_edge_whose_guard_is_false(
    tmp_path: Path, run_status: str, extra: dict[str, Any], guard: str
) -> None:
    ctx, canary = guarded_tree(tmp_path, run_status)

    answer = requeue(ctx, canary, **extra)

    assert answer["status"] == "error", answer
    (error,) = answer["errors"]
    assert error["code"] == "transition_guard_failed"
    assert error["guard"] == guard
    assert task_now(ctx, canary).status is TaskStatus.RUNNING


def test_the_generic_verb_admits_an_edge_whose_guards_hold(tmp_path: Path) -> None:
    ctx, canary = guarded_tree(tmp_path, "FAILED")

    answer = requeue(ctx, canary, reason_code="worker-lost")

    assert answer["status"] == "ok", answer
    assert task_now(ctx, canary).status is TaskStatus.PLANNED


def test_a_replayed_generic_request_skips_the_guards(tmp_path: Path) -> None:
    ctx, canary = guarded_tree(tmp_path, "FAILED")
    first = requeue(ctx, canary, reason_code="worker-lost")
    task = task_now(ctx, canary)

    replay: dict[str, Any] = asyncio.run(
        methods.dispatch(
            DOMAIN_TRANSITION_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": str(TASK_URN),
                "to_status": "PLANNED",
                "expected_revision": task.revision - 1,
                "idempotency_key": "requeue-generic",
                "actor": "OP-0001",
                "reason_code": "worker-lost",
            },
        )
    )

    assert first["status"] == "ok", first
    assert replay["status"] == "ok", replay
