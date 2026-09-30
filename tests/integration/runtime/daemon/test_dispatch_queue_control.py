"""The dispatch queue is a daemon read, and pause, drain and resume are daemon verbs.

UI-023 / UI-067: the unattended route observes a queue the daemon states -- its Runs with
the budget their capsules sealed, the concurrency plan derived from the governor and the
dependency graph, and the control with its last request -- and every control on it is a
request the daemon records. A pause holds the admission of a new Run and stops nothing
already claimed; a resume admits again; a drain asked while a release publishes is refused
with its reason.

Each case drives the real verbs and the real ``dispatch_run`` on a provisioned canary.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.dispatch_queue import (
    DispatchOutcome,
    DispatchQueueView,
    DispatchVerb,
)
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.dispatch_queue import DRAIN_WHILE_PUBLISHING
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.dispatch_queue import (
    DISPATCH_CONTROL_REQUEST_METHOD,
    DISPATCH_QUEUE_READ_METHOD,
)
from eawf.runtime.daemon.native_dispatch import DispatchRefusal
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed, seed_row
from tests.integration.runtime.daemon.test_governor_admission import (
    declare,
    second_params,
    two_runs,
)
from tests.integration.runtime.daemon.test_native_dispatch import (
    ACTOR,
    RUN_KEY,
    SUCCESSOR_KEY,
    LedgerReadingLauncher,
    attempts_of,
    dispatch,
    method_ctx,
    run_urn,
)

pytestmark = pytest.mark.integration

#: A governor with room for two live Runs.
TWO_SLOTS: Final = {
    "max_concurrent_runs": 2,
    "max_in_flight_tokens": 1_000_000,
    "admission": "queue",
}


def tree(root: Path) -> CanaryProvision:
    """Provision the two queued Runs in a tree that names its one project, as init leaves it."""
    canary = two_runs(root)
    seed(canary, {"project": {"DSP": {"key": "DSP"}}})
    return canary


def call(method: str, ctx: MethodContext, canary: CanaryProvision, **params: Any) -> Any:
    """Dispatch one daemon verb the way the server does."""
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def queue(ctx: MethodContext, canary: CanaryProvision) -> DispatchQueueView:
    """Read the dispatch queue through the daemon verb."""
    return DispatchQueueView.model_validate(call(DISPATCH_QUEUE_READ_METHOD, ctx, canary))


def ask(ctx: MethodContext, canary: CanaryProvision, verb: str, ref: str) -> dict[str, Any]:
    """Send one dispatch control request through the daemon verb."""
    return dict(
        call(DISPATCH_CONTROL_REQUEST_METHOD, ctx, canary, verb=verb, actor=ACTOR, request_ref=ref)
    )


def test_ui_023_pause_holds_admission_and_stops_no_claimed_run(tmp_path: Path) -> None:
    canary = tree(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    declare(canary, governor=TWO_SLOTS)
    launcher = LedgerReadingLauncher(canary, runtime)
    dispatch(method_ctx(runtime), canary, launcher)

    answer = ask(method_ctx(runtime), canary, "pause", "DSP-0001")

    assert answer["disposition"] == "confirmed"
    with pytest.raises(DaemonValidationError) as caught:
        dispatch(method_ctx(runtime), canary, launcher, params=second_params(canary))
    assert DispatchRefusal.DISPATCH_HELD.value in str(caught.value)
    assert "held by a pause request" in str(caught.value)
    # the Run claimed before the pause keeps its attempt; the held one never got one
    claimed = [row.run_ref for row in attempts_of(canary, runtime)]
    assert run_urn(RUN_KEY) in claimed and run_urn(SUCCESSOR_KEY) not in claimed
    stated = queue(method_ctx(runtime), canary)
    assert stated.control.dispatch_paused is True
    assert stated.control.last_request is not None
    assert stated.control.last_request.verb is DispatchVerb.PAUSE
    assert stated.control.last_request.outcome is DispatchOutcome.CONFIRMED


def test_ui_023_resume_admits_again_and_a_resent_request_records_once(tmp_path: Path) -> None:
    canary = tree(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    declare(canary, governor=TWO_SLOTS)
    launcher = LedgerReadingLauncher(canary, runtime)
    ask(method_ctx(runtime), canary, "pause", "DSP-0001")
    ask(method_ctx(runtime), canary, "resume", "DSP-0002")
    again = ask(method_ctx(runtime), canary, "resume", "DSP-0002")

    dispatch(method_ctx(runtime), canary, launcher, params=second_params(canary))

    assert again["fact"]["request_ref"] == "DSP-0002"
    assert run_urn(SUCCESSOR_KEY) in [row.run_ref for row in attempts_of(canary, runtime)]
    stated = queue(method_ctx(runtime), canary)
    assert stated.control.holding is None


def test_ui_067_drain_is_refused_while_a_release_is_publishing(tmp_path: Path) -> None:
    canary = tree(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    seed(canary, {"release": {"REL-0001": {"key": "REL-0001", "status": "publishing"}}})

    answer = ask(method_ctx(runtime), canary, "drain", "DSP-0003")

    assert answer["disposition"] == "rejected"
    assert answer["reason"] == DRAIN_WHILE_PUBLISHING
    stated = queue(method_ctx(runtime), canary)
    assert stated.control.drain_requested is False
    assert stated.control.last_request is not None
    assert stated.control.last_request.outcome is DispatchOutcome.REJECTED


def test_ui_067_the_queue_states_the_plan_and_each_run_its_sealed_budget(
    tmp_path: Path,
) -> None:
    canary = tree(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    declare(canary, governor=TWO_SLOTS)
    waiting = seed_row("task", "PLANNED")
    blocker = seed_row("task", "PLANNED")
    blocker["key"] = "EAWF-0041"
    blocker["urn"] = str(waiting["urn"]).replace("EAWF-0042", "EAWF-0041")
    waiting["depends_on"] = [blocker["urn"]]
    seed(canary, {"task": {"EAWF-0042": waiting, "EAWF-0041": blocker}})
    dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))

    stated = queue(method_ctx(runtime), canary)

    assert stated.plan.slots == 2
    assert stated.plan.in_use == 1
    held = stated.run(RUN_KEY)
    assert held is not None and held.admitted_at is not None
    assert held.budget_seconds is not None and held.budget_seconds >= 1
    successor = stated.run(SUCCESSOR_KEY)
    assert successor is not None and successor.state == "QUEUED"
    assert [(edge.waits, edge.on) for edge in stated.plan.edges] == [("EAWF-0042", "EAWF-0041")]
    assert stated.forced_sequential() == 1
