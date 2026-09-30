"""The evidence scorer: one record per rung of a claim's ladder, written when it is filed.

A claim cites evidence records. The scorer runs the ladder over them in rung order and
returns four :class:`~eawf.kernel.state.epoch2.evidence_rung.EvidenceRungRecord` rows,
one per rung, which the daemon appends beside the claim in one session. The Evidence
route, the evidence viewer and the rung card all draw these same records.

Only rung 1 has a check in this tree: each cited evidence record must be held in the
evidence ledger, and the digest of its ledger line is the digest the rung ran over. No
anchor, screen or entailment check exists yet, so a ladder whose rung 1 passed records
rung 2 as started with no outcome -- unknown, never failed -- and the rungs above it as
not run. A ladder whose rung 1 failed runs nothing above it. No outcome is inferred:
``failed`` comes only from a returned negative, and a claim is never certified here.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Final

from eawf.kernel.state.epoch2.evidence_rung import (
    RUNG_NAMES,
    RUNG_QUESTIONS,
    UNKNOWN_FINDING,
    ClaimFiling,
    ClaimStatus,
    EvidenceInput,
    EvidenceRungRecord,
    RungOutcome,
    promotion_blockers,
)

#: The producer every rung record names as its evaluator; never the conformance runner.
SCORER: Final = "evidence scorer"

#: The read verb a surface asks for one claim's ladder by.
EVIDENCE_LADDER_METHOD: Final = "projection.evidence.ladder"

#: The rung a ladder whose rung 1 passed has started and cannot yet answer.
_ANCHOR_RUNG: Final = 2


def _resolve_rung(
    claim: ClaimFiling, digests: Mapping[str, str], *, now: datetime, sequence: int
) -> EvidenceRungRecord:
    """Return rung 1: whether every cited evidence record is held, with its digest."""
    inputs = tuple(
        EvidenceInput(ref=ref, digest=digests.get(ref.entity_key)) for ref in claim.evidence_refs
    )
    resolved = sum(1 for item in inputs if item.digest is not None)
    passed = bool(inputs) and resolved == len(inputs)
    if not inputs:
        finding = "the claim cites no evidence record, so no reference resolves"
    elif passed:
        finding = "every cited evidence record is held at the digest of its ledger line"
    else:
        finding = "a cited evidence record is not held, so its digest cannot be fetched"
    return EvidenceRungRecord(
        claim_ref=claim.urn,
        rung=1,
        name=RUNG_NAMES[1],
        question=RUNG_QUESTIONS[1],
        outcome=RungOutcome.PASSED if passed else RungOutcome.FAILED,
        input_refs=inputs,
        finding=finding,
        counts={"references cited": len(inputs), "references resolved": resolved},
        evaluated_at=now,
        evaluator=SCORER,
        kept_with=claim.urn,
        written_at_sequence=sequence,
        revision=0,
    )


def _open_rung(
    claim: ClaimFiling, rung: int, *, awaits: int | None, sequence: int
) -> EvidenceRungRecord:
    """Return a rung with no outcome: started and unanswered, or never started."""
    outcome = RungOutcome.UNKNOWN if awaits is None else RungOutcome.NOT_RUN
    finding = UNKNOWN_FINDING if awaits is None else f"not run - awaiting rung {awaits}"
    return EvidenceRungRecord.model_validate(
        {
            "claim_ref": claim.urn,
            "rung": rung,
            "name": RUNG_NAMES[rung],
            "question": RUNG_QUESTIONS[rung],
            "outcome": outcome,
            "awaits_rung": awaits,
            "finding": finding,
            "evaluator": SCORER,
            "kept_with": claim.urn,
            "written_at_sequence": sequence,
            "revision": 0,
        }
    )


def score_claim(
    claim: ClaimFiling, digests: Mapping[str, str], *, now: datetime, first_sequence: int
) -> tuple[EvidenceRungRecord, ...]:
    """Return the four rung records scoring *claim*, lowest rung first.

    Args:
        claim: The claim being filed.
        digests: The digest of every evidence record held, by its ``EVD-####`` key.
        now: When the scorer ran.
        first_sequence: The canonical sequence rung 1's record is written at; each
            record above it is written at the next one.

    Returns:
        Exactly four records, one per rung.
    """
    resolve = _resolve_rung(claim, digests, now=now, sequence=first_sequence)
    unpassed = 1 if resolve.outcome is not RungOutcome.PASSED else _ANCHOR_RUNG
    above = tuple(
        _open_rung(
            claim,
            rung,
            awaits=None if rung == _ANCHOR_RUNG and unpassed == _ANCHOR_RUNG else unpassed,
            sequence=first_sequence + rung - 1,
        )
        for rung in range(2, len(RUNG_NAMES) + 1)
    )
    return (resolve, *above)


def claim_status(records: tuple[EvidenceRungRecord, ...]) -> ClaimStatus:
    """Return the lifecycle a scored claim is filed at: supported only when it can promote."""
    return "SUPPORTED" if not promotion_blockers(records) else "OPEN"


__all__ = ["EVIDENCE_LADDER_METHOD", "SCORER", "claim_status", "score_claim"]
