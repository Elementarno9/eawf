"""The scorer's rung 2 to 4 checks, the anchor model and the ladder facts a route states."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.projection.compute import FACTS_FIELD
from eawf.kernel.projection.verification import claim_ladder_facts, ladder_route_rows
from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.state.epoch2.evidence_rung import (
    ClaimFiling,
    RungOutcome,
    SpanAnchor,
    span_digest,
)
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.workflow.evidence.claim_ladder import (
    HeldEvidence,
    HeldReceipt,
    LadderInputs,
    score_claim,
)

pytestmark = pytest.mark.unit

CLAIM: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/claim/CLM-0004"
EVIDENCE: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"
ENTAIL: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0002"
SUMMARY: Final = "replaying run 12 kept every event in order"
AT: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _anchor(**overrides: Any) -> SpanAnchor:
    fields = {
        "evidence_ref": EVIDENCE,
        "path": "docs/notes.md",
        "start_line": 3,
        "end_line": 3,
        "anchor_digest": span_digest(SUMMARY),
    }
    return SpanAnchor.model_validate(fields | overrides)


def _claim(**overrides: Any) -> ClaimFiling:
    fields = {
        "key": "CLM-0004",
        "urn": CLAIM,
        "subject_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042",
        "status": "OPEN",
        "title": "Replay keeps order",
        "description": "Replaying run 12 kept every event in order.",
        "evidence_refs": [EVIDENCE],
        "anchors": [_anchor()],
        "recorded_at": AT,
    }
    return ClaimFiling.model_validate(fields | overrides)


def _receipt(result: GateReceiptResult, exit_status: int | None) -> HeldReceipt:
    return HeldReceipt(
        key="RCP-0001",
        result=result,
        exit_status=exit_status,
        gate_id="G-01",
        scope_id="EAWF-0042",
        head_sha="a" * 40,
    )


def _inputs(*, span: str | None = SUMMARY, receipt: HeldReceipt | None = None) -> LadderInputs:
    return LadderInputs(
        evidence={"EVD-0001": HeldEvidence(digest="sha256:" + "b" * 64, summary=SUMMARY)},
        spans={_anchor(): span},
        receipt=receipt,
        entail_ref=_claim(evidence_refs=[ENTAIL], anchors=[]).evidence_refs[0],
    )


def _score(claim: ClaimFiling, inputs: LadderInputs, **kwargs: Any) -> list[RungOutcome]:
    return [r.outcome for r in score_claim(claim, inputs, now=AT, first_sequence=1, **kwargs)]


def test_a_single_line_span_is_admitted() -> None:
    """Boundary: a span may start and end on one line."""
    assert _anchor().end_line == _anchor().start_line


def test_a_span_ending_before_it_starts_is_refused() -> None:
    with pytest.raises(ValidationError, match="before"):
        _anchor(start_line=4, end_line=3)


@pytest.mark.parametrize("path", ["/abs/notes.md", "../notes.md", "docs/../../x", "C:x", "a\\b"])
def test_a_path_leaving_the_repository_is_refused(path: str) -> None:
    with pytest.raises(ValidationError):
        _anchor(path=path)


def test_a_gate_receipt_key_must_be_a_receipt_key() -> None:
    with pytest.raises(ValidationError):
        _claim(gate_receipt="PRF-0001")


def test_span_digest_is_the_sha256_of_the_text() -> None:
    assert span_digest("") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_a_whole_passing_ladder_entails_with_the_evidence_it_files() -> None:
    receipt = _receipt(GateReceiptResult.PASS, 0)
    records = score_claim(
        _claim(gate_receipt="RCP-0001"), _inputs(receipt=receipt), now=AT, first_sequence=1
    )
    assert [r.outcome for r in records] == [RungOutcome.PASSED] * 4
    assert str(records[3].evidence_ref) == ENTAIL
    assert [r.written_at_sequence for r in records] == [1, 2, 3, 4]


def test_a_gate_that_never_reached_a_verdict_leaves_rung_4_unknown() -> None:
    outcomes = _score(
        _claim(gate_receipt="RCP-0001"),
        _inputs(receipt=_receipt(GateReceiptResult.BLOCKED, None)),
    )
    assert outcomes[3] is RungOutcome.UNKNOWN


def test_a_signalled_gate_fails_rung_4_with_no_exit_count() -> None:
    [*_, entail] = score_claim(
        _claim(gate_receipt="RCP-0001"),
        _inputs(receipt=_receipt(GateReceiptResult.FAIL, -9)),
        now=AT,
        first_sequence=1,
    )
    assert (entail.outcome, entail.counts) == (RungOutcome.FAILED, {})
    assert "exit status -9" in entail.finding


def test_a_missing_span_fails_rung_2() -> None:
    assert _score(_claim(), _inputs(span=None))[1:] == [
        RungOutcome.FAILED,
        RungOutcome.NOT_RUN,
        RungOutcome.NOT_RUN,
    ]


def test_a_rerun_from_rung_3_waits_on_a_held_failed_rung_2() -> None:
    held = score_claim(_claim(), _inputs(span=None), now=AT, first_sequence=1)
    records = score_claim(
        _claim(), _inputs(), now=AT, first_sequence=9, from_rung=3, held=held, revision=1
    )
    assert [(r.rung, r.outcome, r.awaits_rung) for r in records] == [
        (3, RungOutcome.NOT_RUN, 2),
        (4, RungOutcome.NOT_RUN, 2),
    ]
    assert {r.revision for r in records} == {1}


def test_an_open_ladder_is_not_checked_and_states_its_latest_return() -> None:
    """A ladder with an unknown rung 2 is open; rung 1 still returned, so it has an instant."""
    ladder = score_claim(_claim(anchors=[]), _inputs(), now=AT, first_sequence=1)
    assert claim_ladder_facts(ladder) == {
        "outcome": "1 passed · 2 unknown · 3 not_run · 4 not_run",
        "checked": "no",
        "as_of": AT.isoformat(),
    }


def test_a_returned_ladder_states_when_its_latest_rung_returned() -> None:
    receipt = _receipt(GateReceiptResult.PASS, 0)
    ladder = score_claim(
        _claim(gate_receipt="RCP-0001"), _inputs(receipt=receipt), now=AT, first_sequence=1
    )
    facts = claim_ladder_facts(ladder)
    assert facts == {
        "outcome": "1 passed · 2 passed · 3 passed · 4 passed",
        "checked": "yes",
        "as_of": AT.isoformat(),
    }


def _line(key: str, payload: dict[str, Any], collection: Epoch2Collection) -> LedgerRecord:
    return LedgerRecord(
        collection=collection, record_key=key, status="recorded", recorded_at=AT, payload=payload
    )


def test_route_rows_carry_ladder_facts_on_claims_and_skip_unscored_ones() -> None:
    ladder = score_claim(_claim(), _inputs(), now=AT, first_sequence=1)
    lines = [
        _line(f"CLM-0004#rung-{r.rung}@0", r.model_dump(mode="json"), Epoch2Collection.CLAIM)
        for r in ladder
    ]
    rows = {Epoch2Collection.CLAIM: ({"key": "CLM-0004"}, {"key": "CLM-0005"})}
    merged = ladder_route_rows(
        route="evidence", rows=rows, claim_lines=lines, evidence_lines=(), subject=None
    )
    scored, unscored = merged[Epoch2Collection.CLAIM]
    assert scored[FACTS_FIELD]["checked"] == "no"
    assert FACTS_FIELD not in unscored
    assert Epoch2Collection.EVIDENCE not in merged
