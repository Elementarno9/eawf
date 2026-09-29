"""SURF-081: a delivery verb refuses a stale compare-and-swap anchor.

The candidate, integration, proof, approval and evidence verbs file facts
beside a lifecycle record, so the anchor they are sent under is checked by
:func:`~eawf.runtime.daemon.methods.delivery_anchor.require_anchor` rather
than by the transition transaction. These tests pin its answers on a real
canary tree -- a current anchor, a stale one, no anchor, a subject with no
revision, a subject nobody holds, and a subject compacted into its ledger
-- and drive one verb end to end to show a stale anchor writes nothing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusalCode, TransactionRefusedError
from eawf.runtime.daemon.methods.candidate import CandidateReportParams, CandidateSubmitParams
from eawf.runtime.daemon.methods.delivery import DeliveryIntegrateParams
from eawf.runtime.daemon.methods.delivery_acceptance import (
    MergeReconcileParams,
    RecordEvidenceParams,
)
from eawf.runtime.daemon.methods.delivery_anchor import require_anchor
from eawf.runtime.daemon.methods.delivery_approval import ApprovalOpenParams
from eawf.runtime.daemon.methods.delivery_landed import AdoptLandedParams, ReadBackParams
from eawf.runtime.daemon.methods.delivery_proof import TaskProveParams
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    document_path,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
EVIDENCE_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"

#: Every delivery params model a mutating CLI verb sends, each of which
#: must carry the anchor the CLI requires.
ANCHORED_PARAMS: tuple[type[BaseModel], ...] = (
    CandidateSubmitParams,
    CandidateReportParams,
    TaskProveParams,
    DeliveryIntegrateParams,
    AdoptLandedParams,
    ReadBackParams,
    MergeReconcileParams,
    RecordEvidenceParams,
    ApprovalOpenParams,
)


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one ACTIVE Batch and one ACTIVE Milestone, both at revision one."""
    root = tmp_path / "repo"
    root.mkdir()
    provisioned = provision(root, code="ANC")
    seed(
        provisioned,
        {
            "batch": {"BAT-0007": seed_row("batch", "ACTIVE")},
            "milestone": {"MLS-0030": seed_row("milestone", "ACTIVE")},
        },
    )
    return provisioned


@pytest.fixture
def context(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    """The native context of the canary."""
    return root_context(canary, tmp_path / "runtime")


def _refusal(context: Epoch2RootContext, urn: str, expected: int) -> TransactionRefusedError:
    with pytest.raises(TransactionRefusedError) as caught:
        require_anchor(context, parse_qualified_urn(urn), expected)
    return caught.value


@pytest.mark.parametrize("model", ANCHORED_PARAMS, ids=lambda model: model.__name__)
def test_surf_081_every_delivery_params_model_declares_the_anchor(model: type[BaseModel]) -> None:
    """The RPC contract names the anchor exactly as the CLI flag does."""
    assert "expected_revision" in model.model_fields


def test_surf_081_a_current_anchor_lets_the_request_through(context: Epoch2RootContext) -> None:
    require_anchor(context, parse_qualified_urn(BATCH_URN), 1)


def test_surf_081_a_stale_anchor_is_a_revision_conflict(context: Epoch2RootContext) -> None:
    """Off-by-one: the record moved on by one, and the refusal names where it stands."""
    refusal = _refusal(context, BATCH_URN, 2)
    assert refusal.code is TransactionRefusalCode.REVISION_CONFLICT
    assert refusal.revision == 1
    assert str(refusal).startswith("validation_failed: revision_conflict: ")


def test_surf_081_no_anchor_is_not_checked(context: Epoch2RootContext) -> None:
    """Boundary: a caller that sends no anchor is not refused, even for a subject nobody holds."""
    require_anchor(context, parse_qualified_urn(TASK_URN), None)


def test_surf_081_a_subject_with_no_revision_is_a_kind_mismatch(
    context: Epoch2RootContext,
) -> None:
    refusal = _refusal(context, EVIDENCE_URN, 1)
    assert refusal.code is TransactionRefusalCode.IDENTITY_KIND_MISMATCH


def test_surf_081_a_subject_nobody_holds_is_not_found(context: Epoch2RootContext) -> None:
    refusal = _refusal(context, TASK_URN, 1)
    assert refusal.code is TransactionRefusalCode.IDENTITY_NOT_FOUND


def test_surf_081_a_compacted_subject_is_read_from_its_ledger(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    """A finished Run left the document; its report is still bound against its revision."""
    finished = seed_row("run", "COMPLETED")
    append_ledger_record(
        ledger_path(document_path(canary), Epoch2Collection.RUN),
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key="RUN-00000010",
            status="COMPLETED",
            recorded_at=AT,
            payload=finished,
        ),
    )
    require_anchor(context, parse_qualified_urn(RUN_URN), finished["revision"])
    refusal = _refusal(context, RUN_URN, finished["revision"] + 1)
    assert refusal.code is TransactionRefusalCode.REVISION_CONFLICT


def _record_evidence(canary: CanaryProvision, tmp_path: Path, expected: int) -> dict[str, Any]:
    params = {
        "repo_root": str(canary.root),
        "urn": MILESTONE_URN,
        "expected_revision": expected,
        "actor": "OP-0001",
        "idempotency_key": f"evd-{expected}",
        "kind": "audit",
        "summary": "audit A-ANCHOR-01 passed",
    }
    methods.ensure_all_methods_registered()
    return asyncio.run(
        methods.dispatch(
            "runtime.delivery.record_evidence", method_context(tmp_path / "rt"), params
        )
    )


def test_surf_081_a_stale_anchor_writes_nothing(canary: CanaryProvision, tmp_path: Path) -> None:
    """End to end: the verb refuses before its effect, then files under a current anchor."""
    evidence = ledger_path(document_path(canary), Epoch2Collection.EVIDENCE)

    with pytest.raises(methods.DaemonValidationError, match="revision_conflict"):
        _record_evidence(canary, tmp_path, 3)
    assert not evidence.exists() or read_ledger_records(evidence) == ()

    answer = _record_evidence(canary, tmp_path, 1)
    assert answer["created"] is True
    assert len(read_ledger_records(evidence)) == 1
