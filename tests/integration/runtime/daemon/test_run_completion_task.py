"""A dispatched Run that completes on a delivering report moves its Task to READY_TO_INTEGRATE.

The suite drives ``runtime.task.dispatch`` against a canary with a real git
repository, with a fake worker that files its terminal report through the
semantic gateway and then ends its turn. The Task's status, revision and the
committed change line are read back from the tree, so "the Task advanced" is a
fact about the stored record rather than about the dispatch answer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

import eawf.runtime.daemon.methods.semantic  # noqa: F401  (registers the gateway verb)
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.semantic import SemanticCall
from eawf.kernel.store.changes import read_change_page
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.semantic import SEMANTIC_CALL_METHOD
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    TurnExitKind,
    TurnOutcome,
)
from eawf.workflow.evidence.machine_certification import append_machine_certification
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import firehose_path, seed
from tests.integration.runtime.daemon.test_native_dispatch import make_canary, method_ctx
from tests.integration.runtime.daemon.test_task_dispatch import (  # noqa: F401  (autouse homes)
    CLAUDE_VERSION,
    TASK_KEY,
    TASK_URN,
    ParkedLauncher,
    _private_homes,
    dispatch,
    run_now,
    task_now,
    task_row,
    walked_certification,
)

pytestmark = pytest.mark.integration

#: The event a Task's RUNNING -> READY_TO_INTEGRATE edge is recorded under.
READY_EVENT: Final = "domain.task.ready"


def make_tree(tmp_path: Path) -> CanaryProvision:
    """Provision a canary holding one PLANNED Task, certified to carry semantic tools.

    The semantic tool capability is what puts ``submit_report`` in the Run's
    capsule, so without it the worker could not bind the report at all.
    """
    canary = make_canary(tmp_path / "repo", rows={})
    seed(canary, {"task": {TASK_KEY: task_row()}})
    append_machine_certification(
        canary.root / ".ea" / "state.json", walked_certification("claude-code", CLAUDE_VERSION)
    )
    return canary


class ReportingLauncher(ParkedLauncher):
    """A worker that files its report with *verdict*, then ends its turn as told."""

    def __init__(
        self,
        ctx: MethodContext,
        canary: CanaryProvision,
        *,
        verdict: str | None,
        exit_kind: TurnExitKind = "completed",
    ) -> None:
        """Bind the fake to the daemon its worker reaches and the ending it plays."""
        super().__init__("claude")
        self._ctx = ctx
        self._canary = canary
        self._verdict = verdict
        self._exit_kind: TurnExitKind = exit_kind
        self._request: NativeLaunchRequest | None = None

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Keep the request, so the turn can report under its capsule."""
        self._request = request
        return await super().launch(request)

    async def _turn(self) -> TurnOutcome:
        await self.release.wait()
        assert self._request is not None
        if self._verdict is not None:
            answer = await self._submit(self._request.capsule)
            assert answer["result"]["bounded_output"]["accepted"] is True, answer
        return TurnOutcome(exit_kind=self._exit_kind)

    async def _submit(self, capsule: AuthorityCapsule) -> dict[str, Any]:
        call = SemanticCall.seal(
            {
                "call_id": f"call-{1:016x}",
                "run_ref": str(capsule.run_ref),
                "contract_digest": capsule.contract_digest,
                "idempotency_key": "report-01",
                "tool_id": "submit_report",
                "tool_schema_version": "1.0.0",
                "payload": {
                    "tool_id": "submit_report",
                    "verdict": self._verdict,
                    "summary": "the worker finished its turn",
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


def _end_run(
    tmp_path: Path, *, verdict: str | None, exit_kind: TurnExitKind = "completed"
) -> tuple[MethodContext, CanaryProvision, int]:
    """Dispatch the Task, let the worker end its turn, and return the Task's start revision."""
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    launcher = ReportingLauncher(ctx, canary, verdict=verdict, exit_kind=exit_kind)

    started: dict[str, int] = {}
    original = launcher.release.set

    def release() -> None:
        started["revision"] = task_now(ctx, canary).revision
        original()

    launcher.release.set = release  # type: ignore[method-assign]
    (answer,) = dispatch(ctx, canary, launcher, {"task": TASK_KEY})
    assert isinstance(answer, dict), answer
    return ctx, canary, started["revision"]


def _requeue_reason(canary: CanaryProvision) -> str | None:
    """Return the reason the Task's move back to PLANNED was recorded under."""
    for line in firehose_path(canary).read_text(encoding="utf-8").splitlines():
        payload = json.loads(line).get("payload", {})
        if payload.get("entity_ref") == str(TASK_URN) and payload.get("to_status") == "PLANNED":
            return payload.get("reason_code")
    raise AssertionError("the Task was never handed back to PLANNED")


def _task_events(ctx: MethodContext, canary: CanaryProvision) -> list[str]:
    context = ctx.native_root_context(canary.root / ".ea")
    with context.session([TASK_URN]) as session:
        state_path = session.document_path
    page = read_change_page(state_path, record_key=TASK_KEY, cursor=None, limit=100)
    return [change.event_name for change in page.changes]


@pytest.mark.parametrize("verdict", ["pass", "pass-with-followups"])
def test_a_delivering_report_moves_the_task_to_ready(tmp_path: Path, verdict: str) -> None:
    ctx, canary, revision = _end_run(tmp_path, verdict=verdict)

    run = run_now(ctx, canary)
    assert run.status.value == "COMPLETED"
    task = task_now(ctx, canary)
    assert task.status.value == "READY_TO_INTEGRATE"
    assert task.revision == revision + 1
    assert _task_events(ctx, canary).count(READY_EVENT) == 1


@pytest.mark.parametrize("verdict", ["blocked", "fail"])
def test_a_report_that_does_not_deliver_hands_the_task_back_naming_its_verdict(
    tmp_path: Path, verdict: str
) -> None:
    ctx, canary, revision = _end_run(tmp_path, verdict=verdict)

    assert run_now(ctx, canary).status.value == "COMPLETED"
    task = task_now(ctx, canary)
    assert task.status.value == "PLANNED"
    assert task.active_run_ref is None
    assert task.revision == revision + 1
    assert READY_EVENT not in _task_events(ctx, canary)
    assert _requeue_reason(canary) == f"report-{verdict}"


def test_a_failed_run_hands_the_task_back(tmp_path: Path) -> None:
    ctx, canary, revision = _end_run(tmp_path, verdict="pass", exit_kind="failed")

    assert run_now(ctx, canary).status.value == "FAILED"
    task = task_now(ctx, canary)
    assert task.status.value == "PLANNED"
    assert task.active_run_ref is None
    assert task.revision == revision + 1
    assert READY_EVENT not in _task_events(ctx, canary)


def test_a_run_ending_without_a_report_hands_the_task_back(tmp_path: Path) -> None:
    ctx, canary, revision = _end_run(tmp_path, verdict=None)

    assert run_now(ctx, canary).status.value == "FAILED"
    task = task_now(ctx, canary)
    assert task.status.value == "PLANNED"
    assert task.active_run_ref is None
    assert task.revision == revision + 1
