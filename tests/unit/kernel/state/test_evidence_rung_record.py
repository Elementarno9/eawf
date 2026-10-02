"""PLAN-048: each rung of a claim writes one EvidenceRungRecord the card reads whole.

A full record, an unknown record, a not-run record and the attested case, plus
the validation failures: a name that disagrees with its rung, an attestation
below rung 4, and a card field with no record source.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.evidence_rung import (
    RUNG_NAMES,
    RUNG_QUESTIONS,
    UNKNOWN_FINDING,
    ClaimFiling,
    EvidenceRungRecord,
    RungBasis,
    RungOutcome,
    latest_rungs,
)

pytestmark = pytest.mark.unit

SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
PROJECT = "eawf://WSP-MAIN/PRJ-EAWF/_"
CLAIM = f"{PROJECT}/claim/CLM-0004"
OTHER_CLAIM = f"{PROJECT}/claim/CLM-0005"
EVIDENCE = f"{SLOT}/evidence/EVD-0007"
RUN = f"{SLOT}/run/RUN-00000001"
DIGEST = "sha256:" + "a" * 64
AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def record(rung: int = 1, outcome: str = "passed", **overrides: Any) -> EvidenceRungRecord:
    """Return a valid rung record, overridden field by field."""
    returned = outcome in ("passed", "failed")
    fields: dict[str, Any] = {
        "claim_ref": CLAIM,
        "rung": rung,
        "name": RUNG_NAMES[rung],
        "question": RUNG_QUESTIONS[rung],
        "outcome": outcome,
        "input_refs": [{"ref": EVIDENCE, "digest": DIGEST}, {"ref": RUN, "digest": DIGEST}],
        "finding": UNKNOWN_FINDING if outcome == "unknown" else "40 events compared, 0 inversions",
        "counts": {"events compared": 40, "inversions found": 0},
        "evaluated_at": AT if returned else None,
        "evaluator": "evidence scorer",
        "evidence_ref": EVIDENCE if returned else None,
        "kept_with": CLAIM,
        "written_at_sequence": 812,
        "revision": 0,
    }
    if outcome == "not_run":
        fields["awaits_rung"] = rung - 1
    fields.update(overrides)
    return EvidenceRungRecord.model_validate(fields)


# ---- the four record shapes ----------------------------------------------------


def test_plan_048_full_record_carries_every_card_field() -> None:
    r = record(4)
    assert r.question == RUNG_QUESTIONS[4]
    assert [i.digest for i in r.input_refs] == [DIGEST, DIGEST]
    assert r.counts == {"events compared": 40, "inversions found": 0}
    assert (r.evaluated_at, r.evaluator) == (AT, "evidence scorer")
    assert (r.kept_with, r.written_at_sequence) == (r.claim_ref, 812)
    assert r.means == "certifies"


@pytest.mark.parametrize("rung", [1, 2, 3])
def test_plan_048_a_passed_rung_below_four_holds_without_certifying(rung: int) -> None:
    assert record(rung).means == "holds - it does not certify the claim on its own"


def test_plan_048_unknown_record_is_never_an_empty_card() -> None:
    r = record(2, "unknown")
    assert r.finding == UNKNOWN_FINDING
    assert r.evaluated_at is None
    assert r.means == "is unknown, not failed"
    assert r.question == RUNG_QUESTIONS[2]


def test_plan_048_not_run_record_names_the_rung_it_awaits() -> None:
    r = record(3, "not_run", finding="not run", input_refs=[])
    assert r.awaits_rung == 2
    assert r.means == "not run - awaiting rung 2"


def test_plan_048_attested_rung_four_attests_and_never_certifies() -> None:
    r = record(4, basis="attested")
    assert r.basis is RungBasis.ATTESTED
    assert r.means == "attests - no automated check stands behind it"


def test_plan_048_rung_three_negative_is_advisory() -> None:
    assert record(3, "failed").means == "an advisory negative - it routes the claim to rung 4"
    assert record(4, "failed").means == "refutes"


def test_plan_048_the_four_outcomes_are_closed() -> None:
    assert {o.value for o in RungOutcome} == {"passed", "failed", "unknown", "not_run"}


# ---- validation failures -------------------------------------------------------


@pytest.mark.parametrize(
    ("rung", "overrides"),
    [
        (1, {"name": "anchor"}),
        (2, {"question": RUNG_QUESTIONS[1]}),
        (3, {"basis": "attested"}),
        (1, {"basis": "attested"}),
        (1, {"kept_with": OTHER_CLAIM}),
        (1, {"claim_ref": f"{CLAIM}#rung-1", "kept_with": f"{CLAIM}#rung-1"}),
        (1, {"awaits_rung": 1}),
        (1, {"evaluated_at": None}),
        (1, {"input_refs": [{"ref": EVIDENCE, "digest": None}]}),
        (1, {"standing": "certified"}),
        (1, {"finding": "x" * 501}),
        (1, {"counts": {"inversions": -1}}),
        (1, {"written_at_sequence": -1}),
    ],
)
def test_plan_048_a_disagreeing_record_fails_validation(
    rung: int, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        record(rung, **overrides)


def test_plan_048_a_rung_outside_the_ladder_fails() -> None:
    fields = record(4).model_dump()
    with pytest.raises(ValidationError):
        EvidenceRungRecord.model_validate({**fields, "rung": 5})


def test_plan_048_unknown_with_another_finding_fails() -> None:
    with pytest.raises(ValidationError, match="unknown, not failed"):
        record(2, "unknown", finding="nothing yet")


def test_plan_048_not_run_without_awaits_rung_fails() -> None:
    with pytest.raises(ValidationError, match="awaits_rung"):
        record(3, "not_run", awaits_rung=None, finding="not run")


def test_plan_048_not_run_cannot_await_itself_or_a_later_rung() -> None:
    with pytest.raises(ValidationError, match="cannot await"):
        record(2, "not_run", awaits_rung=3, finding="not run")


def test_plan_048_rung_one_fails_over_an_unfetchable_digest() -> None:
    r = record(1, "failed", input_refs=[{"ref": EVIDENCE, "digest": None}])
    assert r.outcome is RungOutcome.FAILED


def test_plan_048_finding_at_its_bound_validates() -> None:
    assert len(record(1, finding="x" * 500).finding) == 500


def test_plan_048_a_record_is_immutable() -> None:
    with pytest.raises(ValidationError):
        record(1).outcome = RungOutcome.FAILED


# ---- the claim a ladder scores --------------------------------------------------


def filing(**overrides: Any) -> ClaimFiling:
    """Return a valid claim filing, overridden field by field."""
    fields: dict[str, Any] = {
        "key": "CLM-0004",
        "urn": CLAIM,
        "status": "OPEN",
        "title": "Replay keeps order",
        "evidence_refs": [EVIDENCE],
        "recorded_at": AT,
    }
    fields.update(overrides)
    return ClaimFiling.model_validate(fields)


def test_plan_044_absent_prose_is_absent_and_300_characters_fit() -> None:
    assert filing().implication is None
    assert filing(falsifier="x" * 300).falsifier == "x" * 300


def test_plan_044_prose_over_its_bound_fails() -> None:
    with pytest.raises(ValidationError):
        filing(implication="x" * 301)


@pytest.mark.parametrize("urn", [OTHER_CLAIM, f"{CLAIM}#rung-2"])
def test_plan_048_a_claim_filed_under_another_address_fails(urn: str) -> None:
    with pytest.raises(ValidationError):
        filing(urn=urn)


def test_plan_048_the_latest_revision_of_each_rung_is_the_one_drawn() -> None:
    old = record(2, "unknown", revision=0)
    new = record(2, "passed", revision=1)
    assert latest_rungs([new, record(1), old]) == (record(1), new)


def test_plan_048_no_records_is_no_ladder() -> None:
    """Boundary: a claim nothing has scored has no rungs, never four invented ones."""
    assert latest_rungs([]) == ()
