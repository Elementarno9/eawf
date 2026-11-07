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
import json
import uuid
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.projection.row_facts import CRITERION_FACT, Links, row_facts
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import batch_proof, methods
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery_approval import DELIVERY_SEAL_APPROVAL_METHOD
from eawf.runtime.daemon.methods.delivery_waiver import DELIVERY_WAIVE_CRITERION_METHOD
from eawf.runtime.daemon.methods.question_decision import QUESTION_OPEN_DECISION_METHOD
from eawf.surfaces.cli.verb_effects import CLI_VERB_EFFECTS
from eawf.workflow.decision_question import waiver_digest
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    TASK_URN,
    document_path,
    firehose_path,
    method_context,
    rekeyed,
    seed,
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


# ---- an agent waives by a sealed waiver action -----------------------------------

AGENT: Final = "EAWF-PLANNER"
CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
ACTION: Final = f"{CONTAINER}/pending-action/ACT-0001"
RECEIPT: Final = f"{CONTAINER}/evidence/EVD-0001"
OPERATOR: Final[dict[str, str]] = {"principal_kind": "human", "principal_id": "OP-0001"}
CRITERION: Final = "CR-0042-2"


def call(tmp_path: Path, canary: CanaryProvision, method: str, **params: Any) -> dict[str, Any]:
    """Dispatch one verb through the real handler against *canary*."""
    ctx = method_context(tmp_path / "rt")
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def hold_receipt(canary: CanaryProvision) -> None:
    """File the evidence row the operator's answer is recorded under."""
    append_ledger_record(
        ledger_path(document_path(canary), Epoch2Collection.EVIDENCE),
        LedgerRecord(
            collection=Epoch2Collection.EVIDENCE,
            record_key="EVD-0001",
            status="recorded",
            recorded_at=AT,
            payload={
                "id": "EVD-0001",
                "kind": "decision",
                "summary": "the operator agreed to waive the check",
                "recorded_at": AT.isoformat(),
            },
        ),
    )


def waiver_options() -> list[dict[str, Any]]:
    """Return the two presentable answers a waiver decision offers."""
    return [
        {
            "option_id": "waive",
            "label": "Waive the check",
            "effect": "approve",
            "consequence": "The task closes without the deleted check.",
            "cost": "Nothing proves the removed behaviour again.",
            "preview": "check -> waived",
        },
        {
            "option_id": "keep",
            "label": "Keep the check",
            "effect": "decline",
            "consequence": "The task stays open until the check passes.",
            "cost": "The batch waits on a check that cannot run.",
            "preview": "check -> open",
        },
    ]


def open_waiver_decision(
    tmp_path: Path, canary: CanaryProvision, **overrides: Any
) -> dict[str, Any]:
    """File the waiver decision an agent raises through ``runtime.question.open_decision``."""
    params: dict[str, Any] = {
        "urn": TASK_URN,
        "idempotency_key": "req-waive-1",
        "actor": AGENT,
        "requested_by": {
            "principal_kind": "agent",
            "principal_id": AGENT,
            "run_ref": f"{CONTAINER}/run/RUN-00000010",
        },
        "question": "Should the deleted check stop holding this task open?",
        "options": waiver_options(),
        "recommended_option_id": "waive",
        "recommendation_rationale": "The check was removed with the code it tested.",
        "waiver": {"criterion_id": CRITERION, "reason": REASON},
        **overrides,
    }
    return call(tmp_path, canary, QUESTION_OPEN_DECISION_METHOD, **params)


def seal(tmp_path: Path, canary: CanaryProvision, option_id: str) -> dict[str, Any]:
    """Seal ACT-0001 with *option_id*, answered by the operator OP-0001."""
    return call(
        tmp_path,
        canary,
        DELIVERY_SEAL_APPROVAL_METHOD,
        urn=ACTION,
        expected_revision=1,
        idempotency_key=f"seal-{option_id}",
        actor="OP-0001",
        resolver=dict(OPERATOR),
        option_id=option_id,
        receipt_ref=RECEIPT,
    )


def waived_events(canary: CanaryProvision) -> list[dict[str, Any]]:
    """Return the payload of every criterion-waived event on the firehose, which a tree
    nothing was written to does not hold yet."""
    path = firehose_path(canary)
    lines = path.read_text().splitlines() if path.exists() else []
    rows = [json.loads(line) for line in lines]
    return [
        row["payload"]
        for row in rows
        if row["payload"].get("criterion_id") is not None and "reason" in row["payload"]
    ]


def sealed_waiver_row(**overrides: Any) -> dict[str, Any]:
    """Return a waiver decision about EAWF-0042 that OP-0001 sealed approving."""
    row: dict[str, Any] = {
        "id": "ACT-0001",
        "urn": ACTION,
        "kind": "operator_decision",
        "subject_ref": TASK_URN,
        "question": "Should the deleted check stop holding this task open?",
        "bundle_digest": waiver_digest(parse_qualified_urn(TASK_URN), CRITERION, REASON),
        "options": waiver_options(),
        "idempotency_key": "req-waive-1",
        "status": "SEALED",
        "revision": 2,
        "requested_by": {"principal_kind": "human", "principal_id": "OP-0001"},
        "resolution_actor": dict(OPERATOR),
        "selected_option_id": "waive",
        "receipt_ref": RECEIPT,
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
    }
    row.update(overrides)
    return row


def test_an_agent_waives_with_a_sealed_waiver_action(tmp_path: Path) -> None:
    canary, _repo = tree_with(tmp_path)
    hold_receipt(canary)
    opened = open_waiver_decision(tmp_path, canary)
    assert opened["action_ref"] == ACTION
    filed = live(canary, Epoch2Collection.PENDING_ACTION)["ACT-0001"]
    assert filed["bundle_digest"] == waiver_digest(parse_qualified_urn(TASK_URN), CRITERION, REASON)
    assert seal(tmp_path, canary, "waive")["outcome"] == "sealed"
    before = live(canary, Epoch2Collection.TASK)["EAWF-0042"]

    answer = waive(tmp_path, canary, CRITERION, actor=AGENT, action_ref=ACTION)

    assert answer["waived"] is True
    assert answer["revision"] == before["revision"] + 1
    assert criteria_of(canary)[CRITERION]["waiver_reason"] == REASON
    [event] = waived_events(canary)
    assert (event["actor"], event["action_ref"]) == (AGENT, ACTION)
    again = waive(tmp_path, canary, CRITERION, actor=AGENT, action_ref=ACTION)
    assert again["waived"] is False
    assert again["revision"] == answer["revision"]
    assert len(waived_events(canary)) == 1


def test_an_operator_waiver_without_an_action_records_none(tmp_path: Path) -> None:
    canary, _repo = tree_with(tmp_path)

    waive(tmp_path, canary, CRITERION)

    [event] = waived_events(canary)
    assert (event["actor"], event["action_ref"]) == (ACTOR, None)


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (sealed_waiver_row(), None),
        (None, "waiver_action_held"),
        (
            sealed_waiver_row(
                status="WAITING",
                revision=1,
                resolution_actor=None,
                selected_option_id=None,
                receipt_ref=None,
            ),
            "waiver_action_unsealed",
        ),
        (sealed_waiver_row(selected_option_id="keep"), "waiver_action_approves"),
        (
            sealed_waiver_row(subject_ref=f"{CONTAINER}/task/EAWF-0043"),
            "waiver_action_subject",
        ),
        (
            sealed_waiver_row(
                bundle_digest=waiver_digest(parse_qualified_urn(TASK_URN), CRITERION, "other")
            ),
            "waiver_action_digest",
        ),
        (sealed_waiver_row(bundle_digest=None), "waiver_action_digest"),
        (
            sealed_waiver_row(
                bundle_digest=waiver_digest(parse_qualified_urn(TASK_URN), "CR-0042-1", REASON)
            ),
            "waiver_action_digest",
        ),
        (
            sealed_waiver_row(resolution_actor={"principal_kind": "human", "principal_id": AGENT}),
            "waiver_action_operator",
        ),
        (
            sealed_waiver_row(
                resolution_actor={"principal_kind": "human", "principal_id": "OPERATOR"}
            ),
            "waiver_action_operator",
        ),
    ],
    ids=[
        "matching",
        "unheld",
        "waiting",
        "declined",
        "other-task",
        "other-reason",
        "no-digest",
        "other-criterion",
        "agent-resolver",
        "operator-word-resolver",
    ],
)
def test_a_waiver_action_that_does_not_match_is_refused(
    tmp_path: Path, row: dict[str, Any] | None, code: str | None
) -> None:
    canary, _repo = tree_with(tmp_path)
    if row is not None:
        seed(canary, {Epoch2Collection.PENDING_ACTION.value: {"ACT-0001": row}})
    before = live(canary, Epoch2Collection.TASK)["EAWF-0042"]

    if code is None:
        assert waive(tmp_path, canary, CRITERION, actor=AGENT, action_ref=ACTION)["waived"]
        return
    with pytest.raises(DaemonValidationError, match=code):
        waive(tmp_path, canary, CRITERION, actor=AGENT, action_ref=ACTION)

    assert live(canary, Epoch2Collection.TASK)["EAWF-0042"] == before
    assert waived_events(canary) == []
    with pytest.raises(DaemonValidationError, match="waiver_operator_required"):
        waive(tmp_path, canary, CRITERION, actor=AGENT)


def test_an_operator_citing_a_mismatched_action_is_refused(tmp_path: Path) -> None:
    canary, _repo = tree_with(tmp_path)
    declined = sealed_waiver_row(selected_option_id="keep")
    seed(canary, {Epoch2Collection.PENDING_ACTION.value: {"ACT-0001": declined}})

    with pytest.raises(DaemonValidationError, match="waiver_action_approves"):
        waive(tmp_path, canary, CRITERION, action_ref=ACTION)


@pytest.mark.parametrize(
    "action_ref",
    [f"{CONTAINER}/task/EAWF-0042", "ACT-0001", "", 7],
)
def test_an_action_ref_that_is_not_a_pending_action_urn_is_refused(
    tmp_path: Path, action_ref: object
) -> None:
    canary, _repo = tree_with(tmp_path)

    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        waive(tmp_path, canary, CRITERION, actor=AGENT, action_ref=action_ref)


def test_a_waiver_decision_on_a_milestone_is_refused_with_nothing_filed(tmp_path: Path) -> None:
    canary, _repo = tree_with(tmp_path)

    with pytest.raises(DaemonValidationError, match="waiver_subject_not_task"):
        open_waiver_decision(tmp_path, canary, urn=f"{CONTAINER}/milestone/MLS-0030")

    assert live(canary, Epoch2Collection.PENDING_ACTION) == {}


def test_the_same_key_naming_another_waiver_is_refused(tmp_path: Path) -> None:
    canary, _repo = tree_with(tmp_path)
    open_waiver_decision(tmp_path, canary)

    with pytest.raises(DaemonValidationError, match="idempotency_conflict"):
        open_waiver_decision(tmp_path, canary, waiver={"criterion_id": CRITERION, "reason": "x"})


def test_the_cli_verb_is_registered_as_a_mutation() -> None:
    effect = CLI_VERB_EFFECTS["task waive-criterion"]

    assert (effect.effect_class, effect.routes) == ("mutate", (DELIVERY_WAIVE_CRITERION_METHOD,))
