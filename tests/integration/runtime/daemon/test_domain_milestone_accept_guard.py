"""Accepting a Milestone needs a sealed approval, and refuses without one.

Acceptance is the moment the work is declared done, so it is the one
lifecycle verb the daemon will not take on the caller's word. The request
must carry the reference of a sealed PendingAction receipt; a request that
carries none, or that names something other than a PendingAction, is
answered ``protected_approval_required`` and the tree is byte-for-byte
what it was -- no WAL intent, no document rewrite, no firehose row.

A supplied reference is not merely a ticket to pass the gate. It travels
into the committed event's binding refs, so the approval an acceptance was
taken against is readable from the event alone, and it joins the digest a
retry is matched against, so the same key cannot be re-presented with a
different approval.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.domain import DOMAIN_LIFECYCLE_METHODS
from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode
from eawf.runtime.daemon.wal import list_records
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    APPROVAL_URN,
    MILESTONE_URN,
    acceptance_bundle_row,
    approval_rows,
    document_path,
    firehose_path,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)

ACCEPT_METHOD = "domain.milestone.accept"
ACTOR = "OP-0001"
KEY = "req-accept-0001"
EVIDENCE_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"

ACCEPTED_BINDING: dict[str, Any] = seed_row("milestone", "COMPLETED")["accepted_binding"]


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one Milestone in review and the approval that seals it."""
    provisioned = provision(tmp_path / "accept", code="ACCEPT")
    seed(
        provisioned,
        {
            "milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")},
            **approval_rows(),
        },
    )
    return provisioned


def _accept(canary: CanaryProvision, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """Dispatch the acceptance verb and return the machine envelope."""
    ctx = method_context(tmp_path / "runtime")
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": MILESTONE_URN,
        "expected_revision": 1,
        "idempotency_key": KEY,
        "actor": ACTOR,
        "updates": {"accepted_binding": ACCEPTED_BINDING},
        "acceptance_bundle": acceptance_bundle_row(),
    }
    params.update(overrides)
    return asyncio.run(methods.dispatch(ACCEPT_METHOD, ctx, params))


def _firehose_rows(canary: CanaryProvision) -> list[dict[str, Any]]:
    """Return every row the canary's firehose holds."""
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _wal_records(canary: CanaryProvision, tmp_path: Path) -> list[Path]:
    """Return every WAL record filed under the canary's native namespace."""
    context = method_context(tmp_path / "runtime").native_root_context(tree_root(canary))
    return list(list_records(context.wal_dir))


def test_the_acceptance_verb_is_one_of_the_registered_lifecycle_verbs() -> None:
    assert ACCEPT_METHOD in DOMAIN_LIFECYCLE_METHODS
    assert ACCEPT_METHOD in methods.registered_methods()


def test_accept_without_an_approval_is_denied_and_writes_nothing(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    before = document_path(canary).read_bytes()

    answer = _accept(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["result"] is None
    assert answer["operation"] == ACCEPT_METHOD
    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.PROTECTED_APPROVAL_REQUIRED.value
    assert row["entity_ref"] == MILESTONE_URN
    assert row["guard"] == "acceptance_journey_passed"
    assert "PendingAction" in row["remediation"]
    assert answer["revision_before"] == answer["revision_after"] == 1
    assert document_path(canary).read_bytes() == before
    assert _firehose_rows(canary) == []
    assert _wal_records(canary, tmp_path) == []


def test_accept_with_an_explicit_null_approval_is_denied(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Spelling the field as null is the same answer as leaving it out."""
    before = document_path(canary).read_bytes()

    answer = _accept(canary, tmp_path, approval_receipt_ref=None)

    assert answer["errors"][0]["code"] == DomainErrorCode.PROTECTED_APPROVAL_REQUIRED.value
    assert document_path(canary).read_bytes() == before


def test_accept_with_a_reference_to_another_kind_is_denied(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """An evidence URN is a reference, and it is not a sealed approval."""
    before = document_path(canary).read_bytes()

    answer = _accept(canary, tmp_path, approval_receipt_ref=EVIDENCE_URN)

    assert answer["errors"][0]["code"] == DomainErrorCode.PROTECTED_APPROVAL_REQUIRED.value
    assert document_path(canary).read_bytes() == before
    assert _firehose_rows(canary) == []
    assert _wal_records(canary, tmp_path) == []


def test_accept_with_an_unparsable_reference_is_a_schema_refusal(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """A malformed reference never reaches the guard; the loader refuses it."""
    answer = _accept(canary, tmp_path, approval_receipt_ref="not-a-urn")

    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert "approval_receipt_ref" in row["message"]


def test_accept_with_a_sealed_approval_commits(canary: CanaryProvision, tmp_path: Path) -> None:
    answer = _accept(canary, tmp_path, approval_receipt_ref=APPROVAL_URN)

    assert answer["errors"] == []
    assert answer["status"] == "ok"
    assert answer["result"]["event_name"] == "domain.milestone.completed"
    assert (answer["revision_before"], answer["revision_after"]) == (1, 2)
    assert len(_wal_records(canary, tmp_path)) == 1


def test_a_committed_acceptance_records_the_approval_it_was_taken_against(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    _accept(canary, tmp_path, approval_receipt_ref=APPROVAL_URN)

    rows = _firehose_rows(canary)
    assert len(rows) == 1
    assert rows[0]["payload"]["binding_refs"] == [APPROVAL_URN]


def test_the_approval_is_part_of_the_digest_a_retry_is_matched_against(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Re-presenting one key with another approval is a reused key, not a retry."""
    _accept(canary, tmp_path, approval_receipt_ref=APPROVAL_URN)

    answer = _accept(
        canary,
        tmp_path,
        approval_receipt_ref="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0002",
    )

    assert answer["errors"][0]["code"] == DomainErrorCode.IDEMPOTENCY_CONFLICT.value
    assert len(_firehose_rows(canary)) == 1


def test_a_denied_acceptance_leaves_the_key_free_to_be_retried(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Nothing was written, so the same key may carry the approved request."""
    denied = _accept(canary, tmp_path)
    accepted = _accept(canary, tmp_path, approval_receipt_ref=APPROVAL_URN)

    assert denied["status"] == "error"
    assert accepted["status"] == "ok"
    assert len(_firehose_rows(canary)) == 1


def test_accept_of_a_milestone_outside_review_is_denied_before_the_approval(tmp_path: Path) -> None:
    """A Milestone that never opened review is not acceptable with any approval."""
    canary = provision(tmp_path / "planned", code="PLANNED")
    seed(
        canary,
        {"milestone": {"MLS-0030": seed_row("milestone", "PLANNED")}, **approval_rows()},
    )
    before = document_path(canary).read_bytes()

    answer = _accept(canary, tmp_path, approval_receipt_ref=APPROVAL_URN)

    assert answer["errors"][0]["code"] == DomainErrorCode.ILLEGAL_TRANSITION.value
    assert document_path(canary).read_bytes() == before
