"""A worker's first tool call against a dispatch that has not recorded RUNNING.

Dispatch records ``RUNNING`` only after the launcher returns, and a real
provider is live, and may already have called, before that. The racing
launcher here makes the race certain rather than likely: it sends its tool
call through the registered ``semantic.call`` verb from inside ``launch``
and does not return until the gateway has had the call long enough to
read the Run as ``QUEUED``. The other cases pin what must not change: a
``QUEUED`` Run with no dispatch in flight is refused at once, and one whose
dispatch never reaches ``RUNNING`` is refused once the settle bound runs out.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.semantic import SemanticCall, SemanticToolErrorCode
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods, native_dispatch, semantic_gateway
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.semantic import SEMANTIC_CALL_METHOD
from eawf.runtime.daemon.native_dispatch import DispatchAnswer, DispatchParams, compile_launchers
from eawf.runtime.daemon.run_supervisor import supervisor_of
from eawf.runtime.daemon.semantic_gateway import PreHandlerCheck
from eawf.runtime.runtimes.adapter import NativeLaunchOutcome, NativeLaunchRequest, TurnOutcome
from tests.integration.runtime.daemon.test_native_dispatch import (
    RUN_URN,
    DaemonDiedError,
    LedgerReadingLauncher,
    dispatch,
    dispatch_params,
    make_canary,
    method_ctx,
)

pytestmark = pytest.mark.integration

#: How long the racing launcher holds ``launch`` open after sending its call.
#: On the unrepaired gateway the refusal comes back well inside it, which is
#: what makes the reproduction fail there rather than pass by luck.
LAUNCH_HOLD_SECONDS: Final = 0.5


def budget_call(capsule: AuthorityCapsule, *, ordinal: int = 1) -> SemanticCall:
    """Return a sealed budget read the Run's capsule grants."""
    return SemanticCall.seal(
        {
            "call_id": f"call-{ordinal:016x}",
            "run_ref": RUN_URN,
            "contract_digest": capsule.contract_digest,
            "idempotency_key": f"race-key-{ordinal:02d}",
            "tool_id": "budget_status",
            "tool_schema_version": "1.0.0",
            "payload": {"tool_id": "budget_status", "include_children": False},
            "requested_at": datetime.now(UTC),
        }
    )


async def send_call(
    ctx: MethodContext, canary: CanaryProvision, call: SemanticCall, capsule: AuthorityCapsule
) -> dict[str, Any]:
    """Send one semantic call through the registered verb."""
    return await methods.dispatch(
        SEMANTIC_CALL_METHOD,
        ctx,
        {
            "repo_root": str(canary.root),
            "call": call.model_dump(mode="json"),
            "capsule": capsule.model_dump(mode="json"),
        },
    )


class RacingLauncher(LedgerReadingLauncher):
    """A launcher whose worker calls a tool before ``launch`` returns.

    Attributes:
        call: The worker's tool call, in flight from inside ``launch``.
        done_in_launch: Whether the call was already answered when
            ``launch`` returned, which is only possible if the gateway
            answered while the Run still read ``QUEUED``.
    """

    def __init__(self, canary: CanaryProvision, runtime_root: Path, ctx: MethodContext) -> None:
        """Bind the launcher to the daemon context its worker calls through."""
        super().__init__(canary, runtime_root)
        self._ctx = ctx
        self.call: asyncio.Task[dict[str, Any]] | None = None
        self.done_in_launch = False

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Send the tool call, give the gateway time to read the Run, then answer."""
        self.call = asyncio.create_task(
            send_call(self._ctx, self._canary, budget_call(request.capsule), request.capsule)
        )
        done, _ = await asyncio.wait({self.call}, timeout=LAUNCH_HOLD_SECONDS)
        self.done_in_launch = bool(done)
        outcome = await super().launch(request)
        # A live provider's turn waits on its tool call's answer. A turn that
        # ended at once would let the supervisor end the Run between two
        # settle polls, and the call would then read a finished Run.
        turn = asyncio.get_running_loop().create_task(self._turn())
        return outcome.model_copy(update={"turn": turn})

    async def _turn(self) -> TurnOutcome:
        """End the worker's turn once its tool call has been answered."""
        assert self.call is not None
        await asyncio.wait({self.call})
        return TurnOutcome(exit_kind="completed")


def drive_racing_dispatch(
    ctx: MethodContext, canary: CanaryProvision, launcher: RacingLauncher
) -> tuple[DispatchAnswer, dict[str, Any]]:
    """Dispatch with *launcher*, then return the answer and the worker's receipt."""
    args = DispatchParams.model_validate(
        {key: value for key, value in dispatch_params(canary).items() if key != "repo_root"}
    )
    context = ctx.native_root_context(canary.root / ".ea")

    async def drive() -> tuple[DispatchAnswer, dict[str, Any]]:
        answer = await native_dispatch.dispatch_run(
            context,
            args,
            now=datetime.now(UTC),
            launchers=dict(compile_launchers((launcher,))),
        )
        assert launcher.call is not None
        receipt = await launcher.call
        supervised = supervisor_of(context).task_of(str(args.urn))
        if supervised is not None:
            await supervised
        return answer, receipt

    return asyncio.run(drive())


def assert_run_not_active(receipt: dict[str, Any]) -> None:
    """Assert *receipt* is the run-state refusal."""
    assert receipt["result"]["status"] == "denied"
    assert receipt["refused_check"] == PreHandlerCheck.RUN_STATE.value
    assert receipt["result"]["error"]["code"] == SemanticToolErrorCode.RUN_NOT_ACTIVE.value


def test_a_call_sent_before_running_is_recorded_is_served_once_it_is(tmp_path: Path) -> None:
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    ctx = method_ctx(runtime)
    launcher = RacingLauncher(canary, runtime, ctx)

    answer, receipt = drive_racing_dispatch(ctx, canary, launcher)

    assert launcher.done_in_launch is False
    assert answer.refusal_code is None
    assert receipt["result"]["status"] == "succeeded", receipt["result"]["error"]
    assert receipt["refused_check"] is None


def test_a_queued_run_with_no_dispatch_in_flight_is_refused_at_once(tmp_path: Path) -> None:
    canary = make_canary(tmp_path / "repo")
    ctx = method_ctx(tmp_path / "runtime")
    probe = LedgerReadingLauncher(canary, tmp_path / "probe", die=True)
    # A dispatch on a throwaway canary yields a sealed capsule for this Run
    # without leaving any attempt on the canary under test.
    other = make_canary(tmp_path / "other")
    with pytest.raises(DaemonDiedError):
        dispatch(method_ctx(tmp_path / "probe"), other, probe, now=datetime.now(UTC))
    sealed: AuthorityCapsule = probe.calls[0]["capsule"]

    started = time.monotonic()
    receipt = asyncio.run(send_call(ctx, canary, budget_call(sealed), sealed))

    assert time.monotonic() - started < semantic_gateway.DISPATCH_SETTLE_SECONDS
    assert_run_not_active(receipt)


def test_a_dispatch_whose_lease_has_lapsed_is_not_waited_for(tmp_path: Path) -> None:
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    lost = LedgerReadingLauncher(canary, runtime, die=True)
    # Stamped long ago, so the lease the lost attempt holds has expired.
    with pytest.raises(DaemonDiedError):
        dispatch(method_ctx(runtime), canary, lost)
    sealed: AuthorityCapsule = lost.calls[0]["capsule"]

    started = time.monotonic()
    receipt = asyncio.run(send_call(method_ctx(runtime), canary, budget_call(sealed), sealed))

    assert time.monotonic() - started < semantic_gateway.DISPATCH_SETTLE_SECONDS
    assert_run_not_active(receipt)


def test_a_dispatch_that_never_reaches_running_is_refused_after_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(semantic_gateway, "DISPATCH_SETTLE_SECONDS", 0.3)
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    lost = LedgerReadingLauncher(canary, runtime, die=True)
    with pytest.raises(DaemonDiedError):
        dispatch(method_ctx(runtime), canary, lost, now=datetime.now(UTC))
    sealed: AuthorityCapsule = lost.calls[0]["capsule"]

    started = time.monotonic()
    receipt = asyncio.run(send_call(method_ctx(runtime), canary, budget_call(sealed), sealed))

    assert time.monotonic() - started >= 0.3
    assert_run_not_active(receipt)
