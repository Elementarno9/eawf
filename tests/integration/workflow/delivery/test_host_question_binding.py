"""The acceptance question reaches the host bound to the durable pending action.

Driven through the real ``runtime.delivery.open_acceptance_approval`` and
``runtime.delivery.seal_acceptance_approval`` handlers against a provisioned
canary. Row ids name the packet requirement each test proves: SURF-030 and
SURF-073 (bound one-to-one, free text is not consent), SURF-031 (exactly the
filed options), SURF-033 (durable before displayed, and recovered after a
lost surface), SURF-091 (the skill that asks renders the bound question) and
SURF-111 (the produced question passes every presentation rule).
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusedError
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.workflow.skills import verify as verify_skill
from eawf.workflow.skills.bodies.user_question import UserQuestion
from eawf.workflow.skills.bodies.verify import VerifyBody
from eawf.workflow.skills.engine import SkillContext
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    MILESTONE_URN,
    document_path,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

OPEN_METHOD: Final = "runtime.delivery.open_acceptance_approval"
SEAL_METHOD: Final = "runtime.delivery.seal_acceptance_approval"
OPERATOR: Final[dict[str, Any]] = {"principal_kind": "human", "principal_id": "OP-0001"}
RECEIPT_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"
INSTALL_EVIDENCE: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0002"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
ACCEPTED_BINDING: Final[dict[str, Any]] = seed_row("milestone", "COMPLETED")["accepted_binding"]
#: How the seal verb refuses: a malformed id at the params boundary, an
#: unoffered one at the transaction.
REFUSALS: Final = (DaemonValidationError, TransactionRefusedError)
STEP: Final[dict[str, Any]] = {
    "step_id": "AS-01",
    "passed": True,
    "observation": "the install completed and reported the version",
    "evidence_kinds": ["artifact"],
    "evidence_refs": [INSTALL_EVIDENCE],
}


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding MLS-0030 in review, one verified Batch and its evidence."""
    provisioned = provision(tmp_path / "ask", code="ASK")
    seed(
        provisioned,
        {
            "milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")},
            "batch": {"BAT-0007": seed_row("batch", "READY_TO_MERGE")},
        },
    )
    context = root_context(provisioned, tmp_path / "runtime")
    with context.session([MILESTONE_URN]) as session:
        for key, kind in (("EVD-0001", "decision"), ("EVD-0002", "artifact")):
            append_ledger_record(
                session.ledger_path(Epoch2Collection.EVIDENCE),
                LedgerRecord(
                    collection=Epoch2Collection.EVIDENCE,
                    record_key=key,
                    status="recorded",
                    recorded_at=AT,
                    payload={
                        "id": key,
                        "kind": kind,
                        "summary": f"{kind} observation",
                        "recorded_at": AT.isoformat(),
                    },
                ),
            )
    return provisioned


def call(canary: CanaryProvision, tmp_path: Path, method: str, **params: Any) -> dict[str, Any]:
    """Dispatch one verb through the real handler against *canary*."""
    ctx = method_context(tmp_path / "runtime")
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def open_approval(canary: CanaryProvision, tmp_path: Path) -> dict[str, Any]:
    """Open the acceptance question the way verify does."""
    return call(
        canary,
        tmp_path,
        OPEN_METHOD,
        urn=MILESTONE_URN,
        actor="SKILL-VERIFY",
        requested_by=dict(OPERATOR),
        steps=[STEP],
        accepted_binding=ACCEPTED_BINDING,
    )


def filed_action(canary: CanaryProvision) -> dict[str, Any]:
    """Return the pending-action row the tree holds."""
    rows = read_document(document_path(canary))[Epoch2Collection.PENDING_ACTION.value]
    assert sorted(rows) == ["ACT-0001"]
    return dict(rows["ACT-0001"])


def test_surf_033_the_question_is_shown_only_from_a_row_the_tree_already_holds(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The host question names a row that is filed, at the revision it is filed at."""
    opened = open_approval(canary, tmp_path)

    question = UserQuestion.model_validate(opened["host_question"])
    row = filed_action(canary)
    assert opened["created"] is True
    assert question.action_ref == opened["action_ref"]
    assert question.action_revision == row["revision"] == opened["revision"]
    assert row["status"] == "WAITING"


def test_surf_033_a_lost_surface_is_recovered_from_the_standing_row(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Asking again after the surface was lost writes nothing and shows the same question."""
    first = open_approval(canary, tmp_path)
    before = document_path(canary).read_bytes()

    again = open_approval(canary, tmp_path)

    assert again["created"] is False
    assert again["host_question"] == first["host_question"]
    assert document_path(canary).read_bytes() == before


def test_surf_031_the_host_options_are_exactly_the_filed_options(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Ids, labels, order and renderings on the surface are the persisted ones."""
    question = UserQuestion.model_validate(open_approval(canary, tmp_path)["host_question"])

    filed = filed_action(canary)["options"]
    assert [(o.option_id, o.label, o.preview) for o in question.options] == [
        (o["option_id"], o["label"], o["preview"]) for o in filed
    ]


@pytest.mark.parametrize("reply", ["Accept the Milestone", "yes please", "yes"])
def test_surf_073_a_free_text_reply_is_never_consent(
    canary: CanaryProvision, tmp_path: Path, reply: str
) -> None:
    """A label, a paraphrase or a bare yes has no option id to seal, so nothing is sealed."""
    opened = open_approval(canary, tmp_path)
    before = document_path(canary).read_bytes()

    with pytest.raises(REFUSALS, match=r"schema_validation_failed|option_id"):
        call(
            canary,
            tmp_path,
            SEAL_METHOD,
            urn=opened["action_ref"],
            expected_revision=opened["revision"],
            idempotency_key="req-seal-0001",
            actor="OP-0001",
            resolver=dict(OPERATOR),
            option_id=reply,
            receipt_ref=RECEIPT_URN,
        )
    assert document_path(canary).read_bytes() == before


def test_surf_030_the_presented_option_id_is_what_seals_the_action(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The id the surface bound to the chosen label seals the action, and the seal hides it."""
    opened = open_approval(canary, tmp_path)
    question = UserQuestion.model_validate(opened["host_question"])
    chosen = next(o for o in question.options if o.label == "Do not accept it")

    sealed = call(
        canary,
        tmp_path,
        SEAL_METHOD,
        urn=question.action_ref,
        expected_revision=question.action_revision,
        idempotency_key="req-seal-0002",
        actor="OP-0001",
        resolver=dict(OPERATOR),
        option_id=chosen.option_id,
        receipt_ref=RECEIPT_URN,
    )

    assert sealed["status"] == "SEALED"
    assert filed_action(canary)["selected_option_id"] == "decline"
    assert sealed["host_question"] is None
    assert open_approval(canary, tmp_path)["host_question"] is None


def test_surf_111_the_produced_acceptance_question_passes_every_presentation_rule(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Renderings differ, the Milestone key is expanded, and accepting is the one recommendation."""
    question = UserQuestion.model_validate(open_approval(canary, tmp_path)["host_question"])

    assert "MLS-0030 is the Milestone" in question.question
    assert len({o.preview for o in question.options}) == len(question.options)
    recommended = [
        o.option_id for o in question.options if (o.description or "").startswith("Recommended.")
    ]
    assert recommended == ["approve"]


def test_surf_091_verify_renders_the_bound_question_it_asked(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The /verify skill carries the daemon's bound question as the report's user_question."""

    def caller(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == OPEN_METHOD:
            return call(canary, tmp_path, method, **params)
        if method == verify_skill.DELIVERY_VERIFY_BATCH_METHOD:
            return {"head_generation": 2, "stage": "ready_to_merge", "merge_ready": True}
        return {"rows": {}}

    args = {
        "subject_ref": "batch-1",
        "mode": "all",
        "milestone": MILESTONE_URN,
        "journey": [STEP],
        "accepted_binding": ACCEPTED_BINDING,
        "requested_by": dict(OPERATOR),
    }
    result = verify_skill.VerifySkill(caller=caller).action(
        SkillContext(scope="scope", session="session", args=args)
    )

    body = VerifyBody.model_validate(result.body)
    assert body.user_question is not None
    assert body.user_question.action_ref == body.approval_ref
    assert [o.option_id for o in body.user_question.options] == ["approve", "decline", "repair"]
