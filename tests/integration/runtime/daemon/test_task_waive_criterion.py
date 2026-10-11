"""A claimed Task's criterion can be waived, with a reason, and is then never proved.

Criteria are set when a Task is promoted and a claimed Task cannot go back to DRAFT, so a
criterion whose verify command names a test file that was since deleted would hold its
Batch open forever. ``runtime.delivery.waive_criterion`` sets that one criterion's
``waiver_reason``; the prover then counts it as satisfied without running it, and the
console says it is waived. Each test drives the verb through the daemon's dispatcher
against a canary over a real git repository.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.row_facts import CRITERION_FACT, Links, row_facts
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import batch_proof, methods
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery_waiver import DELIVERY_WAIVE_CRITERION_METHOD
from eawf.surfaces.cli.verb_effects import CLI_VERB_EFFECTS
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    TASK_URN,
    method_context,
    rekeyed,
    seed_row,
)
from tests.integration.runtime.daemon.test_batch_close import ACTOR, batch, close, live
from tests.integration.runtime.daemon.test_batch_close_proof import (  # noqa: F401  (fixture)
    PASSING,
    NeverRun,
    make_tree,
    proved_task,
    pytest_of,
    receipts,
    uv_shim,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("uv_shim")]

REASON: Final = "the drift check this test covered was removed with its test file"


def waive(
    tmp_path: Path, canary: CanaryProvision, criterion_id: str, **overrides: Any
) -> dict[str, Any]:
    """Send ``runtime.delivery.waive_criterion`` for EAWF-0042 and return its answer."""
    revision = live(canary, Epoch2Collection.TASK)["EAWF-0042"]["revision"]
    call = {
        "repo_root": str(canary.root),
        "urn": TASK_URN,
        "criterion_id": criterion_id,
        "reason": REASON,
        "expected_revision": revision,
        "actor": ACTOR,
        "idempotency_key": f"waive-{uuid.uuid4().hex}",
        **overrides,
    }
    return asyncio.run(
        methods.dispatch(DELIVERY_WAIVE_CRITERION_METHOD, method_context(tmp_path / "rt"), call)
    )


def tree_with(tmp_path: Path, row: dict[str, Any] | None = None) -> tuple[CanaryProvision, Path]:
    """Provision a Batch holding EAWF-0042, by default ready with one passing criterion and
    one naming a test file the head does not hold."""
    task_row = row or proved_task("EAWF-0042", PASSING, pytest_of("deleted"))
    return make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": task_row},
    )


def criteria_of(canary: CanaryProvision) -> dict[str, dict[str, Any]]:
    """Return EAWF-0042's stored criteria by id."""
    stored = live(canary, Epoch2Collection.TASK)["EAWF-0042"]["criteria"]
    return {row["id"]: row for row in stored}


# ---- the verb -------------------------------------------------------------------


def test_waiving_sets_only_that_criterions_reason_and_moves_the_revision(
    tmp_path: Path,
) -> None:
    canary, _repo = tree_with(tmp_path)
    before = live(canary, Epoch2Collection.TASK)["EAWF-0042"]

    answer = waive(tmp_path, canary, "CR-0042-2")

    after = live(canary, Epoch2Collection.TASK)["EAWF-0042"]
    assert answer["waived"] is True
    assert answer["revision"] == before["revision"] + 1
    assert after["revision"] == before["revision"] + 1
    assert after["status"] == before["status"]
    assert criteria_of(canary)["CR-0042-2"]["waiver_reason"] == REASON
    untouched = criteria_of(canary)["CR-0042-1"]
    assert untouched["waiver_reason"] is None
    assert untouched["measurable_signal"] == before["criteria"][0]["measurable_signal"]


def test_waiving_again_with_the_same_reason_changes_nothing(tmp_path: Path) -> None:
    canary, _repo = tree_with(tmp_path)
    first = waive(tmp_path, canary, "CR-0042-2")

    again = waive(tmp_path, canary, "CR-0042-2")

    assert again["waived"] is False
    assert again["revision"] == first["revision"]


def test_a_retry_under_the_same_key_replays_its_answer(tmp_path: Path) -> None:
    canary, _repo = tree_with(tmp_path)
    first = waive(tmp_path, canary, "CR-0042-2", idempotency_key="waive-1")

    retried = waive(tmp_path, canary, "CR-0042-2", idempotency_key="waive-1", expected_revision=1)

    assert retried == first
    assert live(canary, Epoch2Collection.TASK)["EAWF-0042"]["revision"] == first["revision"]


def test_a_probe_measured_criterion_is_regraded_assumed_when_waived(tmp_path: Path) -> None:
    row = proved_task("EAWF-0042", PASSING, pytest_of("deleted"))
    row["criteria"][1]["grounding"] = "measured"
    canary, _repo = tree_with(tmp_path, row)

    waive(tmp_path, canary, "CR-0042-2")

    waived = criteria_of(canary)["CR-0042-2"]
    assert (waived["waiver_reason"], waived["grounding"]) == (REASON, "assumed")


@pytest.mark.parametrize("status", ["PLANNED", "CLAIMED", "RUNNING", "READY_TO_INTEGRATE"])
def test_a_criterion_is_waivable_from_planned_through_ready(tmp_path: Path, status: str) -> None:
    row = rekeyed(seed_row("task", status), key="EAWF-0042")
    canary, _repo = tree_with(tmp_path, row)

    answer = waive(tmp_path, canary, "CR-01")

    assert answer["waived"] is True
    assert criteria_of(canary)["CR-01"]["waiver_reason"] == REASON


@pytest.mark.parametrize("status", ["DRAFT", "COMPLETED", "CANCELLED", "FAILED"])
def test_a_criterion_is_not_waivable_outside_planned_through_ready(
    tmp_path: Path, status: str
) -> None:
    row = rekeyed(seed_row("task", status), key="EAWF-0042")
    canary, _repo = tree_with(tmp_path, row)

    with pytest.raises(DaemonValidationError, match="illegal_transition"):
        waive(tmp_path, canary, "CR-01")


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"criterion_id": "CR-0042-9"}, "criterion_unknown"),
        ({"reason": ""}, "schema_validation_failed"),
        ({"reason": "   "}, "transition_reason_missing"),
        ({"expected_revision": 99}, "revision_conflict"),
        ({"unexpected": True}, "schema_validation_failed"),
    ],
)
def test_a_malformed_or_stale_waiver_is_refused(
    tmp_path: Path, overrides: dict[str, Any], code: str
) -> None:
    canary, _repo = tree_with(tmp_path)
    before = live(canary, Epoch2Collection.TASK)["EAWF-0042"]

    criterion_id = overrides.pop("criterion_id", "CR-0042-2")

    with pytest.raises(DaemonValidationError, match=code):
        waive(tmp_path, canary, criterion_id, **overrides)

    assert live(canary, Epoch2Collection.TASK)["EAWF-0042"] == before


def test_a_criterion_carrying_an_accepted_risk_decision_is_refused(tmp_path: Path) -> None:
    row = proved_task("EAWF-0042", PASSING, None)
    row["criteria"][1].update(grounding="accepted-risk", accepted_risk_decision_ref="D-0081")
    canary, _repo = tree_with(tmp_path, row)

    with pytest.raises(DaemonValidationError, match="criterion_accepted_risk"):
        waive(tmp_path, canary, "CR-0042-2")


# ---- the prover and the console -------------------------------------------------


def test_a_waived_criterion_is_never_proved_and_lets_its_batch_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = NeverRun()
    monkeypatch.setattr(batch_proof, "run_leg", runner)
    row = proved_task("EAWF-0042", pytest_of("deleted"))
    canary, _repo = tree_with(tmp_path, row)
    assert close(canary, tmp_path).proof == "unmet"

    waive(tmp_path, canary, "CR-0042-1")
    closure = close(canary, tmp_path)

    assert closure.closed, closure.reason
    assert runner.calls == 0
    assert receipts(canary) == []


def test_the_console_says_a_waived_criterion_is_waived() -> None:
    row = rekeyed(seed_row("task", "PLANNED"), key="EAWF-0042")
    row["criteria"][0]["waiver_reason"] = REASON

    facts = row_facts(Epoch2Collection.TASK, "EAWF-0042", row, Links({}))

    assert facts[f"{CRITERION_FACT}1"].split(" · ")[2] == "waived"


def test_the_cli_verb_is_registered_as_a_mutation() -> None:
    effect = CLI_VERB_EFFECTS["task waive-criterion"]

    assert (effect.effect_class, effect.routes) == ("mutate", (DELIVERY_WAIVE_CRITERION_METHOD,))
