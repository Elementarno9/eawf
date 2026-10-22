"""Which stored Milestone a plan that is not a re-plan may fill in place.

A Milestone made by ``eawf milestone create`` holds no plan and no work,
so the first plan naming it claims the row rather than being refused for
reusing its key. Every other stored Milestone -- one a plan placed, one
past PLANNED, one with Batches, one imported from the previous epoch --
stays a claimed key.
"""

from __future__ import annotations

from typing import Any

import pytest

from eawf.workflow.planning.materialise import (
    _claimable,
    _claimed_keys,
    _milestone_claim_refusal,
)
from tests.unit.workflow.planning.lenses.test_ownership_lens import make_body

BODY = make_body()
MILESTONE_KEY = BODY.milestone_urn.entity_key
MILESTONE_LOCATOR = f"milestone/{MILESTONE_KEY}"


def _document(**milestone: Any) -> dict[str, Any]:
    """Return a document holding one bare PLANNED Milestone, edited by *milestone*."""
    row = {"status": "PLANNED", "origin": {"kind": "native"}, **milestone}
    return {"milestone": {MILESTONE_KEY: row}}


def test_a_bare_planned_milestone_is_claimable() -> None:
    """No plan, no Batch and no required Batch leaves nothing to protect."""
    document = _document()

    assert _milestone_claim_refusal(document, body=BODY) is None
    assert _claimed_keys(document, body=BODY, parent=None) == ()


def test_an_empty_required_batch_list_is_still_bare() -> None:
    """An explicit empty list is the create verb's own default, not a placement."""
    assert _milestone_claim_refusal(_document(required_batch_refs=[]), body=BODY) is None


@pytest.mark.parametrize(
    ("milestone", "reason"),
    [
        pytest.param({"status": "ACTIVE"}, "it is ACTIVE, not PLANNED", id="active"),
        pytest.param({"status": "CANCELLED"}, "it is CANCELLED, not PLANNED", id="cancelled"),
        pytest.param({"status": None}, "it is unreadable, not PLANNED", id="no-status"),
        pytest.param(
            {"origin": {"kind": "legacy"}},
            "it was imported from the previous epoch",
            id="legacy",
        ),
        pytest.param(
            {"required_batch_refs": ["eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0009"]},
            "it already requires Batches",
            id="required-batch",
        ),
    ],
)
def test_a_milestone_row_that_is_not_bare_stays_claimed(
    milestone: dict[str, Any], reason: str
) -> None:
    """The row itself shows it was placed, started or imported."""
    document = _document(**milestone)

    assert _milestone_claim_refusal(document, body=BODY) == reason
    assert _claimed_keys(document, body=BODY, parent=None) == (MILESTONE_LOCATOR,)


def test_a_milestone_a_batch_belongs_to_stays_claimed() -> None:
    """A Batch created outside a plan already gives the Milestone work."""
    document = {
        **_document(),
        "batch": {
            "BAT-0009": {"milestone_ref": str(BODY.milestone_urn)},
            "BAT-0008": {"milestone_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-9999"},
        },
    }

    assert _milestone_claim_refusal(document, body=BODY) == "Batch BAT-0009 already belongs to it"
    assert _claimed_keys(document, body=BODY, parent=None) == (MILESTONE_LOCATOR,)


def test_a_milestone_an_applied_plan_placed_stays_claimed() -> None:
    """Only a re-plan under that plan may rewrite what it placed."""
    urn = str(BODY.milestone_urn)
    document = {
        **_document(),
        "plan_revision": {
            "PRV-0001": {"status": "APPROVED", "body": {"milestone_urn": urn}},
            "PRV-0002": {"status": "APPLIED", "body": {"milestone_urn": urn}},
        },
    }

    assert (
        _milestone_claim_refusal(document, body=BODY) == "applied plan revision PRV-0002 placed it"
    )
    assert _claimed_keys(document, body=BODY, parent=None) == (MILESTONE_LOCATOR,)


def test_an_unapplied_or_foreign_plan_does_not_place_the_milestone() -> None:
    """A plan that never applied, or applied elsewhere, placed nothing here."""
    document = {
        **_document(),
        "plan_revision": {
            "PRV-0001": {"status": "APPROVED", "body": {"milestone_urn": str(BODY.milestone_urn)}},
            "PRV-0002": {
                "status": "APPLIED",
                "body": {"milestone_urn": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-9999"},
            },
        },
    }

    assert _milestone_claim_refusal(document, body=BODY) is None


def test_only_a_stored_milestone_of_a_plan_that_is_not_a_re_plan_is_claimable() -> None:
    """A re-plan owns its Milestone through its parent; an absent row is simply created."""
    assert _claimable(_document(), body=BODY, parent=None) == frozenset({MILESTONE_LOCATOR})
    assert _claimable(_document(), body=BODY, parent=BODY) == frozenset()
    assert _claimable({}, body=BODY, parent=None) == frozenset()


def test_a_bare_milestone_does_not_exempt_a_colliding_task_or_batch() -> None:
    """Claiming the Milestone leaves every Batch and Task key collision refused."""
    batch_key = BODY.batches[0].urn.entity_key
    task_key = BODY.tasks[0].urn.entity_key
    document = {
        **_document(),
        "batch": {batch_key: {"milestone_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/X"}},
        "task": {task_key: {"status": "PLANNED"}},
    }

    assert _claimed_keys(document, body=BODY, parent=None) == (
        f"batch/{batch_key}",
        f"task/{task_key}",
    )
