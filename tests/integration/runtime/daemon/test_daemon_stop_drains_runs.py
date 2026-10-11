"""Stopping the daemon ends every dispatched Run it awaits before it answers.

The suite dispatches a Task against a canary with a real git repository, with a
fake worker whose turn stays parked, then sends ``daemon.shutdown`` the way
``eawf daemon stop`` does. A Run whose worker already bound its report survives
the stop as ``COMPLETED``; a Run with no report is failed ``daemon-stopped`` and
its Task handed back. The answer is read only after, so "drained" is a fact
about the stored Runs rather than about the answer. Signalling the worker group
is patched out: the fake's process group is not a real one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import orjson
import pytest

import eawf.runtime.daemon.methods.daemon
import eawf.runtime.daemon.methods.semantic  # noqa: F401  (registers the verbs the suite reaches)
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import lifecycle, methods, native_dispatch, run_supervisor
from eawf.runtime.daemon.lifecycle import DaemonLifecycleError, start_daemon, wait_for_predecessor
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.task_dispatch import TASK_DISPATCH_METHOD
from eawf.runtime.daemon.native_dispatch import DispatchRefusal, compile_launchers
from eawf.runtime.daemon.run_supervisor import RunEndCause, end_cause, supervisor_of
from eawf.runtime.daemon.server import DAEMON_SHUTTING_DOWN, _process_frame
from eawf.runtime.daemon.spawn import DaemonSpawnTimeoutError
from eawf.runtime.runtimes.adapter import TurnOutcome
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import firehose_path
from tests.integration.runtime.daemon.test_native_dispatch import ACTOR, method_ctx
from tests.integration.runtime.daemon.test_run_completion_task import (
    ReportingLauncher,
    make_tree,
)
from tests.integration.runtime.daemon.test_task_dispatch import (  # noqa: F401  (autouse homes)
    BUDGET,
    TASK_KEY,
    TASK_URN,
    _private_homes,
    run_now,
    task_now,
)

pytestmark = pytest.mark.integration


class LingeringLauncher(ReportingLauncher):
    """A worker that binds its report, when it has one, and then keeps running.

    Attributes:
        reported: Set once the worker's report was accepted, or at once when it
            files none.
    """

    def __init__(self, ctx: MethodContext, canary: CanaryProvision, *, verdict: str | None):
        """Bind the fake to the daemon its worker reaches and the report it files."""
        super().__init__(ctx, canary, verdict=verdict)
        self.reported = asyncio.Event()

    async def _turn(self) -> TurnOutcome:
        assert self._request is not None
        if self._verdict is not None:
            answer = await self._submit(self._request.capsule)
            assert answer["result"]["bounded_output"]["accepted"] is True, answer
        self.reported.set()
        await self.release.wait()
        return TurnOutcome(exit_kind="completed")


@pytest.fixture
def stopped_groups() -> Iterator[mock.MagicMock]:
    """Record every worker group the daemon signals instead of signalling it."""
    with mock.patch.object(run_supervisor, "cancel_with_grace") as cancel:
        yield cancel


def _stop(
    tmp_path: Path,
    *,
    verdict: str | None,
    timeout_seconds: int = 0,
    release_after: float | None = None,
) -> tuple[MethodContext, CanaryProvision, dict[str, Any], dict[str, Any]]:
    """Dispatch the Task, wait for its worker, stop the daemon and read back the Run.

    Returns:
        The daemon context, the canary, the shutdown answer, and what was read
        inside the loop right after the answer.
    """
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    ctx.shutdown_event = asyncio.Event()
    launcher = LingeringLauncher(ctx, canary, verdict=verdict)
    seen: dict[str, Any] = {}

    async def scenario() -> dict[str, Any]:
        params = {"repo_root": str(canary.root), "actor": ACTOR, "budget": BUDGET, "task": TASK_KEY}
        answer = await methods.dispatch(TASK_DISPATCH_METHOD, ctx, params)
        await launcher.reported.wait()
        if release_after is not None:
            asyncio.get_running_loop().call_later(release_after, launcher.release.set)
        shutdown = await methods.dispatch(
            "daemon.shutdown", ctx, {"drain": True, "timeout_seconds": timeout_seconds}
        )
        supervisor = supervisor_of(ctx.native_root_context(canary.root / ".ea"))
        seen["supervised"] = supervisor.task_of(answer["run_ref"])
        seen["run"] = await asyncio.to_thread(run_now, ctx, canary)
        seen["shutdown_set"] = ctx.shutdown_event.is_set()
        with pytest.raises(DaemonValidationError) as refusal, supervisor.launching("RUN-next"):
            pass
        seen["dispatch_after"] = str(refusal.value)
        ping = orjson.dumps({"jsonrpc": "2.0", "id": "p", "method": "daemon.ping", "params": {}})
        seen["ping"] = await _process_frame(ping, ctx)
        return shutdown

    with mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
    ):
        shutdown = asyncio.run(scenario())
    return ctx, canary, shutdown, seen


def _requeue_reason(canary: CanaryProvision) -> str | None:
    """Return the reason the Task's move back to PLANNED was recorded under."""
    for line in firehose_path(canary).read_text(encoding="utf-8").splitlines():
        payload = json.loads(line).get("payload", {})
        if payload.get("entity_ref") == str(TASK_URN) and payload.get("to_status") == "PLANNED":
            reason: str | None = payload.get("reason_code")
            return reason
    return None


# ---- a reported Run survives the stop -------------------------------------------


def test_a_run_whose_report_was_accepted_is_completed_before_the_stop_answers(
    tmp_path: Path, stopped_groups: mock.MagicMock
) -> None:
    ctx, canary, shutdown, seen = _stop(tmp_path, verdict="pass")

    assert shutdown["drained"] is True
    assert seen["supervised"] is None
    assert seen["run"].status.value == "COMPLETED"
    assert seen["shutdown_set"] is True
    assert task_now(ctx, canary).status.value == "READY_TO_INTEGRATE"
    stopped_groups.assert_called_once_with(4242)


# ---- an unreported Run is failed and its Task handed back -----------------------


def test_a_run_without_a_report_is_failed_daemon_stopped_and_its_task_requeued(
    tmp_path: Path, stopped_groups: mock.MagicMock
) -> None:
    ctx, canary, shutdown, seen = _stop(tmp_path, verdict=None)

    assert shutdown["drained"] is True
    run = seen["run"]
    assert run.status.value == "FAILED"
    assert run.failure is not None
    assert run.failure.code == RunEndCause.DAEMON_STOPPED.value
    task = task_now(ctx, canary)
    assert task.status.value == "PLANNED"
    assert task.active_run_ref is None
    assert _requeue_reason(canary) == "daemon-stopped"
    stopped_groups.assert_called_once_with(4242)


# ---- a turn ending within the drain window ends as it always does ---------------


def test_a_turn_ending_within_the_window_is_not_stopped(
    tmp_path: Path, stopped_groups: mock.MagicMock
) -> None:
    ctx, canary, shutdown, seen = _stop(
        tmp_path, verdict="pass", timeout_seconds=30, release_after=0.2
    )

    assert shutdown["drained"] is True
    assert seen["run"].status.value == "COMPLETED"
    assert task_now(ctx, canary).status.value == "READY_TO_INTEGRATE"
    stopped_groups.assert_not_called()


# ---- a stopping daemon takes no new work and is not ready -----------------------


def test_a_stopping_daemon_refuses_dispatch_and_readiness(
    tmp_path: Path, stopped_groups: mock.MagicMock
) -> None:
    _ctx, _canary, _shutdown, seen = _stop(tmp_path, verdict="pass")

    assert DispatchRefusal.DAEMON_STOPPING.value in seen["dispatch_after"]
    assert seen["ping"]["error"]["code"] == DAEMON_SHUTTING_DOWN


def test_a_stop_with_nothing_in_flight_answers_drained(tmp_path: Path) -> None:
    ctx = method_ctx(tmp_path / "runtime")
    ctx.shutdown_event = asyncio.Event()

    answer = asyncio.run(
        methods.dispatch("daemon.shutdown", ctx, {"drain": True, "timeout_seconds": 0})
    )

    assert answer["drained"] is True
    assert ctx.shutdown_event.is_set()


def test_a_stop_without_drain_never_answers_drained(tmp_path: Path) -> None:
    ctx = method_ctx(tmp_path / "runtime")
    ctx.shutdown_event = asyncio.Event()

    answer = asyncio.run(methods.dispatch("daemon.shutdown", ctx, {"drain": False}))

    assert answer["drained"] is False


def test_a_shutdown_window_past_the_bound_is_refused(tmp_path: Path) -> None:
    ctx = method_ctx(tmp_path / "runtime")
    ctx.shutdown_event = asyncio.Event()

    with pytest.raises(ValueError, match="timeout_seconds"):
        asyncio.run(methods.dispatch("daemon.shutdown", ctx, {"timeout_seconds": 601}))
    assert not ctx.shutdown_event.is_set()


@pytest.mark.parametrize(("reported", "cause"), [(True, None), (False, RunEndCause.DAEMON_STOPPED)])
def test_a_stopped_turn_is_judged_by_its_report_alone(
    reported: bool, cause: RunEndCause | None
) -> None:
    turn = TurnOutcome(exit_kind="failed", error_text="the worker was signalled")

    assert end_cause(turn, reported=reported, stopped=True) is cause


# ---- a start issued right after a stop waits for the old daemon -----------------


@pytest.fixture
def unsupervised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run the lifecycle against a private runtime with no OS supervisor."""
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(lifecycle, "STOP_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(lifecycle, "daemon_pid_if_ready", lambda _runtime: None)
    return tmp_path / "rt"


def test_start_after_stop_waits_for_the_predecessor_then_spawns(
    unsupervised: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = iter([True, True, True, False])
    locked: list[bool] = []

    def singleton_locked(_runtime: Path) -> bool:
        locked.append(next(held, False))
        return locked[-1]

    def spawn(_runtime: Path) -> int:
        if locked[-1]:
            raise DaemonSpawnTimeoutError("daemon did not answer daemon.ping within 5.0s")
        return 4343

    monkeypatch.setattr(lifecycle, "daemon_singleton_locked", singleton_locked)
    monkeypatch.setattr(lifecycle, "auto_spawn_daemon", spawn)

    result = start_daemon(runtime_dir=unsupervised)

    assert result.action == "started"
    assert result.pid == 4343


def test_a_predecessor_that_becomes_ready_is_attached_to(
    unsupervised: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([None, 5151])
    monkeypatch.setattr(lifecycle, "daemon_singleton_locked", lambda _runtime: True)
    monkeypatch.setattr(lifecycle, "daemon_pid_if_ready", lambda _runtime: next(answers))

    assert wait_for_predecessor(unsupervised, timeout_seconds=5.0) == 5151


def test_a_free_lock_needs_no_wait(unsupervised: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "daemon_singleton_locked", lambda _runtime: False)

    assert wait_for_predecessor(unsupervised, timeout_seconds=0.001) is None


def test_a_predecessor_that_never_exits_is_reported(
    unsupervised: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "daemon_singleton_locked", lambda _runtime: True)

    with pytest.raises(DaemonLifecycleError, match="still holds the runtime lock"):
        wait_for_predecessor(unsupervised, timeout_seconds=0.01)


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_a_non_positive_predecessor_wait_is_refused(unsupervised: Path, timeout: float) -> None:
    with pytest.raises(DaemonLifecycleError, match="must be positive"):
        wait_for_predecessor(unsupervised, timeout_seconds=timeout)
