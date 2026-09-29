"""PLAN-044: the evidence viewer draws a claim's prose rows only when the prose exists.

``IN WORDS``, ``IT PROVES`` and ``BREAKS IF`` render the claim's description,
implication and falsifier; an absent field renders no row, never a ``∅``
placeholder, because prose is not a measurement.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.console.decisions import DecisionRecords

from . import decision_support as ds

CLAIMS = ds.records("overlays/evidence.json")
PROSE_LABELS = ("IN WORDS", "IT PROVES", "BREAKS IF")


def _rows(records: DecisionRecords) -> list[str]:
    return ds.frame(ds.opened("attention", "evidence", "CLM-0001"), decisions=records)


def _without(*fields: str) -> DecisionRecords:
    first, *rest = CLAIMS.claims
    bare = first.model_copy(update=dict.fromkeys(fields))
    return CLAIMS.model_copy(update={"claims": (bare, *rest)})


def _prose_rows(rows: list[str]) -> list[str]:
    return [r for r in rows if r.strip().startswith(PROSE_LABELS)]


def test_plan_044_present_prose_draws_all_three_rows() -> None:
    first = CLAIMS.claims[0]
    assert first.in_words and first.proves and first.breaks_if
    labels = [r.strip()[:9] for r in _prose_rows(_rows(CLAIMS))]
    assert labels == ["IN WORDS ", "IT PROVES", "BREAKS IF"]


@pytest.mark.parametrize(("absent", "label"), [("breaks_if", "BREAKS IF"), ("proves", "IT PROVES")])
def test_plan_044_an_absent_prose_field_draws_no_row(absent: str, label: str) -> None:
    rows = _rows(_without(absent))
    assert not any(label in r for r in rows)


def test_plan_044_absent_prose_never_draws_the_unavailable_glyph() -> None:
    rows = _rows(_without("in_words", "proves", "breaks_if"))
    assert _prose_rows(rows) == []
    assert not any(r.strip().startswith(("IN WORDS", "IT PROVES", "BREAKS IF", "∅")) for r in rows)
