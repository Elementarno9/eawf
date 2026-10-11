"""A projection row carries every fact a valid record states, however long it joins.

A Task holds its write claims without limit, and its row joins them into one fact, so a
cap on a fact's length would refuse a whole route over one valid Task.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.projection.compute import ProjectionRow, _status_field
from eawf.kernel.projection.row_facts import Links, row_facts
from eawf.kernel.store.tiers import Epoch2Collection

URN = "eawf://EAWF/task/EAWF-0001"


def _row(facts: dict[str, Any]) -> ProjectionRow:
    return ProjectionRow(
        key="EAWF-0001",
        urn=URN,
        collection=Epoch2Collection.TASK,
        revision=1,
        status=_status_field(status="ACTIVE", urn=URN, revision=1),
        facts=facts,
    )


@pytest.mark.parametrize("length", [1, 1999, 2000, 2001, 20_000])
def test_a_fact_of_any_positive_length_is_carried(length: int) -> None:
    assert len(_row({"write_claims": "a" * length}).facts["write_claims"]) == length


def test_no_facts_is_an_empty_mapping() -> None:
    assert _row({}).facts == {}


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_fact_is_refused(blank: str) -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        _row({"write_claims": blank})


def test_a_fact_that_is_not_text_is_refused() -> None:
    with pytest.raises(ValidationError, match="valid string"):
        _row({"write_claims": 7})


def test_a_task_with_many_write_claims_projects_as_one_row() -> None:
    claims = [f"src/eawf/area_{index:03d}/module_with_a_long_name.py" for index in range(80)]
    stored = {"urn": URN, "revision": 1, "status": "ACTIVE", "write_claims": claims}
    facts = row_facts(Epoch2Collection.TASK, "EAWF-0001", stored, Links({}))
    assert len(facts["write_claims"]) > 2000
    assert _row(facts).facts["write_claims"].split(",") == claims
