"""A Run the stall sweep finds quiet is held under a provider pause citing its stall.

CON-106 says a lost Run raises a pause, and PLAN-047 says a provider pause over a lost Run
projects as waiting on a check whose control outcome is unknown, offering reconcile and let
go and never retry. The sweep that raises the stall fact opens that pause, one per quiet
episode, citing the fact; the next sweep ends it once the Run answers or stops running. A
disposable epoch-2 canary holds one running Run, driven through the daemon's own verbs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from eawf.kernel.state.epoch2.pause import OpenPause, PauseReason, PauseStatus
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.pause import PAUSE_READ_METHOD
from eawf.runtime.daemon.stall_sweep import sweep_once
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed, seed_row
from tests.integration.runtime.daemon.test_run_stall_interval import RUN_KEY, RUN_URN
from tests.integration.runtime.daemon.test_run_stall_signal import act, call, canary, ctx, quiet

__all__ = ["canary", "ctx"]

pytestmark = pytest.mark.integration


def _pauses(ctx: MethodContext, canary: CanaryProvision) -> dict[str, Any]:
    return call(PAUSE_READ_METHOD, ctx, repo_root=str(canary.root))


def test_con_106_plan_047_a_stall_opens_one_provider_pause_over_its_lost_run(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    quiet(ctx, canary)
    now = datetime.now(UTC)
    sweep_once(ctx, now=now)
    sweep_once(ctx, now=now + timedelta(seconds=30))

    answer = _pauses(ctx, canary)

    (item,) = answer["pauses"]
    pause = OpenPause.model_validate(item["pause"])
    assert pause.reason is PauseReason.PROVIDER and pause.status is PauseStatus.OPEN
    assert pause.scope_ref.entity_key == RUN_KEY
    assert pause.health_evidence_refs == (f"STL-{RUN_KEY}-1",)
    assert item["situation"] == "control_outcome_unknown"
    assert answer["run_states"] == {RUN_KEY: "LOST"}


def test_con_106_the_pause_resolves_when_the_run_answers_again(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    quiet(ctx, canary)
    sweep_once(ctx, now=datetime.now(UTC))
    act(ctx, canary, 2)

    sweep_once(ctx, now=datetime.now(UTC))

    pauses = {item["pause"]["key"]: item for item in _pauses(ctx, canary)["pauses"]}
    assert pauses["PAU-0001"]["situation"] == "resolved"
    # the Run went quiet again at once, so its second episode holds it under a new pause
    assert pauses["PAU-0002"]["pause"]["health_evidence_refs"] == [f"STL-{RUN_KEY}-2"]


def test_con_106_the_pause_is_cancelled_with_its_run(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    quiet(ctx, canary)
    sweep_once(ctx, now=datetime.now(UTC))
    cancelled = seed_row("run", "CANCELLED")
    cancelled["urn"], cancelled["key"] = RUN_URN, RUN_KEY
    seed(canary, {"run": {RUN_KEY: cancelled}})

    sweep_once(ctx, now=datetime.now(UTC))

    (item,) = _pauses(ctx, canary)["pauses"]
    assert item["situation"] == "cancelled"
