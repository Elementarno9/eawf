"""The view a plan is judged by, and the Tasks an append finds already placed in it."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from eawf.kernel.store.tiers import Epoch2Collection
from eawf.workflow.planning.materialise import appended_placements, with_compacted_rows
from tests.unit.workflow.planning.lenses.test_ownership_lens import make_body

DOCUMENT: dict[str, Any] = {
    "milestone": {"MLS-0001": {"status": "ACTIVE"}},
    "task": {"EAWF-0002": {"key": "EAWF-0002", "status": "PLANNED"}},
}


def test_no_compacted_rows_reads_as_the_document() -> None:
    """An empty ledger map leaves every collection as the document holds it."""
    assert with_compacted_rows(DOCUMENT, compacted={}) == DOCUMENT


def test_a_compacted_row_joins_its_collection_and_the_document_is_untouched() -> None:
    """A ledger-only row appears in the view, and the input document gains nothing."""
    before = copy.deepcopy(DOCUMENT)
    ledger = {"EAWF-0001": {"key": "EAWF-0001", "status": "COMPLETED"}}

    placed = with_compacted_rows(DOCUMENT, compacted={Epoch2Collection.TASK: ledger})

    assert placed["task"] == {**ledger, **DOCUMENT["task"]}
    assert placed["milestone"] is DOCUMENT["milestone"]
    assert before == DOCUMENT


def test_a_collection_the_document_lacks_reads_its_ledger_rows() -> None:
    """A collection held only in the ledger still reads its rows."""
    ledger = {"BAT-0001": {"key": "BAT-0001", "status": "COMPLETED"}}

    placed = with_compacted_rows(DOCUMENT, compacted={Epoch2Collection.BATCH: ledger})

    assert placed["batch"] == ledger


def test_a_document_row_wins_over_its_ledger_twin() -> None:
    """The document is the current tier, so its row shadows a ledger row under one key."""
    ledger = {"EAWF-0002": {"key": "EAWF-0002", "status": "COMPLETED"}}

    placed = with_compacted_rows(DOCUMENT, compacted={Epoch2Collection.TASK: ledger})

    assert placed["task"]["EAWF-0002"]["status"] == "PLANNED"


def test_a_collection_holding_a_non_object_is_refused() -> None:
    """A document whose collection is not an object fails the same way a direct read does."""
    with pytest.raises(ValueError, match="holds a list"):
        with_compacted_rows({"task": []}, compacted={Epoch2Collection.TASK: {}})


def _placements(milestone_status: str, tasks: dict[str, Any]) -> dict[str, str]:
    """Return what ``appended_placements`` answers over a two-Task body, by Task key."""
    body = make_body()
    document = {
        "milestone": {body.milestone_urn.entity_key: {"status": milestone_status}},
        "task": tasks,
    }
    placed = appended_placements(document, body=body)
    return {urn.entity_key: status.value for urn, status in placed.items()}


def test_a_milestone_not_active_places_nothing() -> None:
    """A PLANNED Milestone is re-planned, not appended to, so no Task counts as placed."""
    assert _placements("PLANNED", {"EAWF-0001": {"status": "CLAIMED"}}) == {}


def test_an_active_milestone_places_each_stored_task_but_no_backlog_draft() -> None:
    """A stored in-flight or finished Task is placed; a draft the plan promotes is new."""
    tasks = {"EAWF-0001": {"status": "COMPLETED"}, "EAWF-0002": {"status": "DRAFT"}}

    assert _placements("ACTIVE", tasks) == {"EAWF-0001": "COMPLETED"}


def test_an_active_milestone_with_no_stored_task_places_nothing() -> None:
    """Every Task of a plan that names only new Tasks is new."""
    assert _placements("ACTIVE", {}) == {}


def test_a_stored_task_with_an_unknown_status_is_refused() -> None:
    """A status no Task has is a corrupt store, not a new Task."""
    with pytest.raises(ValueError, match="BOGUS"):
        _placements("ACTIVE", {"EAWF-0001": {"status": "BOGUS"}})
