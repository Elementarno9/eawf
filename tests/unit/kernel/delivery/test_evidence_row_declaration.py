"""An evidence row reads back when its superseding line declares unreachable commits."""

from __future__ import annotations

from typing import Any

import pydantic
import pytest

from eawf.kernel.delivery.acceptance import EvidenceRow
from eawf.platform.lint.ledger_history import UNREACHABLE_KEY
from eawf.workflow.delivery.acceptance import evidence_view

pytestmark = pytest.mark.unit

_DECLARATION = [
    {"sha": "a" * 40, "archive_ref": f"refs/eawf/archive/{'a' * 40}", "reason": "squashed"}
]


def _row(**extra: Any) -> dict[str, Any]:
    return {
        "id": "EVD-0001",
        "kind": "decision",
        "summary": "Operator chose option 1.",
        "recorded_at": "2026-10-31T00:00:00Z",
        **extra,
    }


def test_a_row_without_a_declaration_reads_as_before() -> None:
    assert EvidenceRow.model_validate(_row()).id == "EVD-0001"


def test_a_row_declaring_unreachable_commits_reads_back() -> None:
    row = EvidenceRow.model_validate(_row(**{UNREACHABLE_KEY: _DECLARATION}))
    assert row.summary == "Operator chose option 1."


def test_an_empty_declaration_reads_back() -> None:
    assert EvidenceRow.model_validate(_row(**{UNREACHABLE_KEY: []})).id == "EVD-0001"


def test_the_view_holds_a_declaring_row_beside_a_plain_one() -> None:
    plain = {**_row(), "id": "EVD-0002"}
    view = evidence_view([_row(**{UNREACHABLE_KEY: _DECLARATION}), plain])
    assert view.keys == ("EVD-0001", "EVD-0002")


def test_any_other_extra_key_is_still_refused() -> None:
    with pytest.raises(pydantic.ValidationError, match="Extra inputs are not permitted"):
        EvidenceRow.model_validate(_row(note="not an evidence field"))


def test_a_missing_field_is_still_refused() -> None:
    row = _row(**{UNREACHABLE_KEY: _DECLARATION})
    del row["summary"]
    with pytest.raises(pydantic.ValidationError, match="summary"):
        EvidenceRow.model_validate(row)


def test_a_payload_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(pydantic.ValidationError):
        EvidenceRow.model_validate(["EVD-0001"])
