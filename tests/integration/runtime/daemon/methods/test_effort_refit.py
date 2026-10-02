"""The effort-mapping re-fit is a daemon verb that stores a revision or asks the operator.

Driven through the real ``runtime.estimation.refit`` handler against a provisioned canary
whose actual ledger holds the recorded actuals. Row ids name the packet requirement each
test proves: MEAS-020 (a due re-fit is stored as the next revision with its fit), MEAS-022
and the operator ruling on re-fits (a move of at most a quarter applies with a notice; a
larger one files an operator decision and leaves the mapping in force unchanged until the
operator adopts it), and MEAS-015 (the stored revision is the one new estimates read).
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.effort_mapping import EFFORT_REFIT_METHOD
from eawf.runtime.daemon.methods.question_decision import QUESTION_ANSWER_NUMBERED_METHOD
from eawf.workflow.estimation import mapping_revisions
from eawf.workflow.estimation.mapping import CURRENT_EFFORT_MAPPING, MappingStatus
from eawf.workflow.estimation.mapping_revisions import (
    applied_mappings,
    mapping_in_force,
    proposed_mappings,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    method_context,
    provision,
    root_context,
    seed,
)

pytestmark = pytest.mark.integration

REPOSITORY: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
OPERATOR: Final[dict[str, Any]] = {"principal_kind": "human", "principal_id": "OP-0001"}

#: The shipped mapping, dated far enough back that its re-fit cadence has elapsed.
SHIPPED: Final = CURRENT_EFFORT_MAPPING.model_copy(update={"effective_on": date(2026, 1, 1)})


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep canary runtimes under tmp, the operator's configuration out, and the re-fit due."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("EAWF_PREFERENCES__AUTO_CHOOSE", raising=False)
    monkeypatch.setattr(mapping_revisions, "CURRENT_EFFORT_MAPPING", SHIPPED)


def canary(tmp_path: Path, *, elapsed_eu: float, count: int | None = None) -> CanaryProvision:
    """A canary whose actual ledger holds *count* measured waves of *elapsed_eu* each."""
    provisioned = provision(tmp_path / "fit", code="FIT")
    seed(
        provisioned,
        {
            "project": {"FIT": {"key": "FIT"}},
            "repository": {"REP-EAWF": {"key": "REP-EAWF", "urn": REPOSITORY}},
        },
    )
    context = root_context(provisioned, tmp_path / "runtime")
    total = CURRENT_EFFORT_MAPPING.refit_minimum_sample if count is None else count
    with context.session([MILESTONE_URN]) as session:
        path = session.ledger_path(Epoch2Collection.ACTUAL)
        for index in range(total):
            append_ledger_record(path, _actual(f"FIT-{index:04d}", elapsed_eu))
        append_ledger_record(path, _actual("FIT-X", 9.0, excluded=True))
        append_ledger_record(
            session.ledger_path(Epoch2Collection.EVIDENCE),
            LedgerRecord(
                collection=Epoch2Collection.EVIDENCE,
                record_key="EVD-0001",
                status="recorded",
                recorded_at=AT,
                payload={
                    "id": "EVD-0001",
                    "kind": "decision",
                    "summary": "the operator chose",
                    "recorded_at": AT.isoformat(),
                },
            ),
        )
    return provisioned


def _actual(subject: str, elapsed_eu: float, *, excluded: bool = False) -> LedgerRecord:
    """Return one imported actual row as the cutover writes it."""
    return LedgerRecord(
        collection=Epoch2Collection.ACTUAL,
        record_key=subject,
        status="history",
        recorded_at=AT,
        payload={
            "kind": "actual",
            "map_key": subject,
            "payload": {
                "id": f"ACT-{subject}",
                "scope_id": subject,
                "status": "done",
                "elapsed_eu": elapsed_eu,
                "calibration_excluded": excluded,
                "current_store_record_id": f"REC-{subject}",
                "updated_at": AT.isoformat(),
            },
        },
    )


def call(provisioned: CanaryProvision, tmp_path: Path, method: str, **params: Any) -> Any:
    """Dispatch one verb through the real handler against *provisioned*."""
    ctx = method_context(tmp_path / "runtime")
    return asyncio.run(
        methods.dispatch(method, ctx, {"repo_root": str(provisioned.root), **params})
    )


def refit(provisioned: CanaryProvision, tmp_path: Path, key: str) -> dict[str, Any]:
    return dict(
        call(provisioned, tmp_path, EFFORT_REFIT_METHOD, actor="OP-0001", idempotency_key=key)
    )


def estimates(provisioned: CanaryProvision) -> tuple[LedgerRecord, ...]:
    return read_ledger_records(ledger_path(document_path(provisioned), Epoch2Collection.ESTIMATE))


def pending(provisioned: CanaryProvision) -> dict[str, Any]:
    return dict(read_document(document_path(provisioned)).get("pending_action", {}))


def test_meas_020_022_a_small_move_is_stored_as_the_next_revision_with_a_notice(
    tmp_path: Path,
) -> None:
    provisioned = canary(tmp_path, elapsed_eu=0.9)

    answer = refit(provisioned, tmp_path, "refit-1")

    assert answer["result"] == "applied"
    assert answer["revision"] == CURRENT_EFFORT_MAPPING.revision + 1
    assert "revision 3 applied" in answer["notice"]
    assert answer["outcome"]["excluded"] == [{"reason": "flagged_excluded", "count": 1}]
    stored = mapping_in_force(estimates(provisioned))
    assert stored.revision == 3
    assert stored.status is MappingStatus.FITTED
    assert stored.effort_eu == pytest.approx(0.9)
    assert stored.fit is not None and len(stored.fit.input_sample) == 100
    assert answer["digest"] == stored.digest
    assert pending(provisioned) == {}


def test_meas_020_the_same_request_sent_again_stores_nothing_twice(tmp_path: Path) -> None:
    provisioned = canary(tmp_path, elapsed_eu=0.9)

    refit(provisioned, tmp_path, "refit-1")
    again = refit(provisioned, tmp_path, "refit-1")

    assert again["result"] == "applied"
    assert len(applied_mappings(estimates(provisioned))) == 1


def test_meas_020_a_thin_sample_is_not_due_and_stores_nothing(tmp_path: Path) -> None:
    provisioned = canary(tmp_path, elapsed_eu=0.9, count=99)

    answer = refit(provisioned, tmp_path, "refit-1")

    assert answer["result"] == "not_due"
    assert answer["outcome"]["not_due"] == ["sample_below_minimum"]
    assert answer["revision"] == CURRENT_EFFORT_MAPPING.revision
    assert estimates(provisioned) == ()


def test_meas_022_a_large_move_opens_a_decision_and_leaves_the_mapping_unchanged(
    tmp_path: Path,
) -> None:
    provisioned = canary(tmp_path, elapsed_eu=1.2)

    answer = refit(provisioned, tmp_path, "refit-1")

    assert answer["result"] == "decision_opened"
    assert answer["revision"] == CURRENT_EFFORT_MAPPING.revision
    assert answer["decision"]["host_question"] is not None
    rows = pending(provisioned)
    assert len(rows) == 1
    (action,) = rows.values()
    assert action["kind"] == "operator_decision"
    assert action["status"] == "WAITING"
    assert [option["effect"] for option in action["options"]] == ["approve", "decline"]
    assert mapping_in_force(estimates(provisioned)) == SHIPPED
    assert len(proposed_mappings(estimates(provisioned))) == 1

    waiting = refit(provisioned, tmp_path, "refit-2")

    assert waiting["result"] == "awaiting_decision"
    assert waiting["action_ref"] == answer["action_ref"]
    assert len(pending(provisioned)) == 1


def _answer(provisioned: CanaryProvision, tmp_path: Path, action_ref: str, reply: str) -> None:
    call(
        provisioned,
        tmp_path,
        QUESTION_ANSWER_NUMBERED_METHOD,
        urn=action_ref,
        expected_revision=1,
        idempotency_key="answer-1",
        actor="OP-0001",
        resolver=dict(OPERATOR),
        reply=reply,
        receipt_ref=action_ref.rsplit("/", 2)[0] + "/evidence/EVD-0001",
    )


def test_meas_022_an_adopted_proposal_becomes_the_revision_in_force(tmp_path: Path) -> None:
    provisioned = canary(tmp_path, elapsed_eu=1.2)
    opened = refit(provisioned, tmp_path, "refit-1")
    _answer(provisioned, tmp_path, opened["action_ref"], "1")

    adopted = refit(provisioned, tmp_path, "refit-2")

    assert adopted["result"] == "applied"
    assert adopted["action_ref"] == opened["action_ref"]
    stored = applied_mappings(estimates(provisioned))
    assert [row.decision_ref for row in stored] == [opened["action_ref"]]
    assert mapping_in_force(estimates(provisioned)).effort_eu == pytest.approx(1.2)


def test_meas_022_a_refused_proposal_is_not_asked_again(tmp_path: Path) -> None:
    provisioned = canary(tmp_path, elapsed_eu=1.2)
    opened = refit(provisioned, tmp_path, "refit-1")
    _answer(provisioned, tmp_path, opened["action_ref"], "2")

    again = refit(provisioned, tmp_path, "refit-2")

    assert again["result"] == "declined"
    assert again["revision"] == CURRENT_EFFORT_MAPPING.revision
    assert applied_mappings(estimates(provisioned)) == ()
    assert len(pending(provisioned)) == 1


def test_the_verb_refuses_an_unknown_parameter(tmp_path: Path) -> None:
    provisioned = canary(tmp_path, elapsed_eu=0.9, count=0)
    with pytest.raises(DaemonValidationError):
        call(
            provisioned,
            tmp_path,
            EFFORT_REFIT_METHOD,
            actor="OP-0001",
            idempotency_key="refit-1",
            ladder=True,
        )
