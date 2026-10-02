"""The evidence scorer: one record per rung of a claim's ladder, written when it is filed.

A claim cites evidence records. The scorer runs the ladder over them in rung order and
returns one :class:`~eawf.kernel.state.epoch2.evidence_rung.EvidenceRungRecord` per rung,
which the daemon appends beside the claim in one session. The Evidence route, the
evidence viewer and the rung card all draw these same records.

Each rung runs a check that exists in this tree, over inputs the daemon read for it:

- rung 1 ``resolve``: each cited evidence record must be held in the evidence ledger,
  and the digest of its ledger line is the digest the rung ran over;
- rung 2 ``anchor``: each span the claim anchors a reference to must still exist in the
  repository and hash to the digest the claim's writer read it at;
- rung 3 ``screen``: the claim's words are scored against the cited records' summaries
  by the in-process entailment scorer, and anything short of entailment is the advisory
  negative that routes the claim on to rung 4;
- rung 4 ``entail``: the deterministic arm reads the gate receipt the claim names, and a
  gate that passed with exit status zero entails the claim.

A rung whose check has nothing to run over -- a claim that anchors no span, or names no
receipt -- started and cannot answer, so it is unknown and never failed. A rung above an
unpassed rung 1 or 2 is not run. No outcome is inferred: ``failed`` comes only from a
returned negative, and a rung 3 negative never blocks rung 4.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.state.epoch2.evidence_rung import (
    RUNG_NAMES,
    RUNG_QUESTIONS,
    UNKNOWN_FINDING,
    ClaimFiling,
    ClaimStatus,
    EvidenceInput,
    EvidenceRungRecord,
    RungOutcome,
    SpanAnchor,
    promotion_blockers,
    span_digest,
)
from eawf.kernel.state.epoch2.urns import EvidenceUrn
from eawf.workflow.evidence import rung2

#: The producer every rung record names as its evaluator; never the conformance runner.
SCORER: Final = "evidence scorer"

#: The read verb a surface asks for one claim's ladder by.
EVIDENCE_LADDER_METHOD: Final = "projection.evidence.ladder"

#: The rungs whose pass every rung above them waits on.
_GATING_RUNGS: Final = (1, 2)

#: The last rung of the ladder.
_ENTAIL_RUNG: Final = 4


@dataclass(frozen=True, slots=True, kw_only=True)
class HeldEvidence:
    """One evidence record as the evidence ledger holds it.

    Attributes:
        digest: The digest of the record's ledger line.
        summary: What the record says it shows.
    """

    digest: str
    summary: str


@dataclass(frozen=True, slots=True, kw_only=True)
class HeldReceipt:
    """The gate receipt a claim names, as the receipt ledger holds it.

    Attributes:
        key: The receipt's ``RCP-####`` key.
        result: What the gate returned.
        exit_status: The gate command's exit status, when it ran one.
        gate_id: The gate that ran.
        scope_id: The scope the gate ran for.
        head_sha: The commit the gate ran at.
    """

    key: str
    result: GateReceiptResult
    exit_status: int | None
    gate_id: str
    scope_id: str
    head_sha: str


@dataclass(frozen=True, slots=True, kw_only=True)
class LadderInputs:
    """Everything the daemon read for one scoring, so the scorer itself reads nothing.

    Attributes:
        evidence: Every held evidence record, by its ``EVD-####`` key.
        spans: The text of each anchored span, ``None`` where the file or the lines are
            gone.
        receipt: The receipt the claim names, ``None`` when it names none or it is not
            held.
        entail_ref: The evidence record an automated rung 4 pass is filed under;
            ``None`` for a claim citing nothing, which never reaches rung 4.
    """

    evidence: Mapping[str, HeldEvidence]
    spans: Mapping[SpanAnchor, str | None]
    receipt: HeldReceipt | None
    entail_ref: EvidenceUrn | None


def _record(
    claim: ClaimFiling,
    rung: int,
    *,
    outcome: RungOutcome,
    finding: str,
    sequence: int,
    revision: int,
    now: datetime | None = None,
    awaits: int | None = None,
    inputs: tuple[EvidenceInput, ...] = (),
    counts: Mapping[str, int] | None = None,
    evidence_ref: EvidenceUrn | None = None,
) -> EvidenceRungRecord:
    """Return one rung record; a returned outcome is stamped with *now*."""
    returned = outcome in (RungOutcome.PASSED, RungOutcome.FAILED)
    return EvidenceRungRecord.model_validate(
        {
            "claim_ref": claim.urn,
            "rung": rung,
            "name": RUNG_NAMES[rung],
            "question": RUNG_QUESTIONS[rung],
            "outcome": outcome,
            "awaits_rung": awaits,
            "input_refs": inputs,
            "finding": finding,
            "counts": dict(counts or {}),
            "evaluated_at": now if returned else None,
            "evaluator": SCORER,
            "evidence_ref": evidence_ref,
            "kept_with": claim.urn,
            "written_at_sequence": sequence,
            "revision": revision,
        }
    )


def _cited(claim: ClaimFiling, inputs: LadderInputs) -> tuple[EvidenceInput, ...]:
    """Return each cited evidence record with the digest of its ledger line, if held."""
    return tuple(
        EvidenceInput(
            ref=ref,
            digest=held.digest if (held := inputs.evidence.get(ref.entity_key)) else None,
        )
        for ref in claim.evidence_refs
    )


def _resolve(
    claim: ClaimFiling, inputs: LadderInputs, *, now: datetime, sequence: int, revision: int
) -> EvidenceRungRecord:
    """Return rung 1: whether every cited evidence record is held, with its digest."""
    cited = _cited(claim, inputs)
    resolved = sum(1 for item in cited if item.digest is not None)
    passed = bool(cited) and resolved == len(cited)
    if not cited:
        finding = "the claim cites no evidence record, so no reference resolves"
    elif passed:
        finding = "every cited evidence record is held at the digest of its ledger line"
    else:
        finding = "a cited evidence record is not held, so its digest cannot be fetched"
    return _record(
        claim,
        1,
        outcome=RungOutcome.PASSED if passed else RungOutcome.FAILED,
        finding=finding,
        sequence=sequence,
        revision=revision,
        now=now,
        inputs=cited,
        counts={"references cited": len(cited), "references resolved": resolved},
    )


def _anchor(
    claim: ClaimFiling, inputs: LadderInputs, *, now: datetime, sequence: int, revision: int
) -> EvidenceRungRecord:
    """Return rung 2: whether every anchored span still exists and matches its digest."""
    if not claim.anchors:
        return _record(
            claim,
            2,
            outcome=RungOutcome.UNKNOWN,
            finding=UNKNOWN_FINDING,
            sequence=sequence,
            revision=revision,
        )
    read = tuple((anchor, inputs.spans.get(anchor)) for anchor in claim.anchors)
    found = tuple(
        EvidenceInput(ref=anchor.evidence_ref, digest=None if text is None else span_digest(text))
        for anchor, text in read
    )
    missing = sum(1 for item in found if item.digest is None)
    matched = sum(
        1
        for anchor, item in zip(claim.anchors, found, strict=True)
        if item.digest == anchor.anchor_digest
    )
    changed = len(found) - missing - matched
    if matched == len(found):
        finding = "every anchored span exists and matches its anchor digest"
    elif missing:
        finding = f"{missing} anchored span(s) no longer exist in the repository"
    else:
        finding = f"{changed} anchored span(s) changed since the claim was written"
    return _record(
        claim,
        2,
        outcome=RungOutcome.PASSED if matched == len(found) else RungOutcome.FAILED,
        finding=finding,
        sequence=sequence,
        revision=revision,
        now=now,
        inputs=found,
        counts={
            "spans anchored": len(found),
            "spans matched": matched,
            "spans changed": changed,
            "spans missing": missing,
        },
    )


def _screen(
    claim: ClaimFiling, inputs: LadderInputs, *, now: datetime, sequence: int, revision: int
) -> EvidenceRungRecord:
    """Return rung 3: the in-process entailment score of the claim over its cited records."""
    hypothesis = " ".join(text for text in (claim.title, claim.description) if text)
    premise = " ".join(
        held.summary
        for ref in claim.evidence_refs
        if (held := inputs.evidence.get(ref.entity_key)) is not None
    )
    result = rung2.score_claim(hypothesis, premise, scorer=rung2.load_default_scorer())
    entailed = result.verdict is rung2.Rung2Verdict.ENTAILED
    return _record(
        claim,
        3,
        outcome=RungOutcome.PASSED if entailed else RungOutcome.FAILED,
        finding=f"{result.verdict.value}: {result.reason}",
        sequence=sequence,
        revision=revision,
        now=now,
        inputs=_cited(claim, inputs),
        counts={"entailment per mille": round(result.probability * 1000)},
    )


def _entail(
    claim: ClaimFiling, inputs: LadderInputs, *, now: datetime, sequence: int, revision: int
) -> EvidenceRungRecord:
    """Return rung 4: whether the gate receipt the claim names passed."""
    receipt = inputs.receipt
    unknown = claim.gate_receipt is None or (
        receipt is not None
        and receipt.result not in (GateReceiptResult.PASS, GateReceiptResult.FAIL)
    )
    if unknown:
        # no deterministic gate measures the claim, or the gate never reached a verdict;
        # the text-claim jury arm is not wired, so nothing answers
        return _record(
            claim,
            4,
            outcome=RungOutcome.UNKNOWN,
            finding=UNKNOWN_FINDING,
            sequence=sequence,
            revision=revision,
        )
    if receipt is None:
        return _record(
            claim,
            4,
            outcome=RungOutcome.FAILED,
            finding=f"the receipt {claim.gate_receipt} the claim names is not held, "
            "so no gate result proves it",
            sequence=sequence,
            revision=revision,
            now=now,
            inputs=_cited(claim, inputs),
        )
    passed = receipt.result is GateReceiptResult.PASS and receipt.exit_status == 0
    verb = "passed" if passed else "failed"
    status = "no" if receipt.exit_status is None else str(receipt.exit_status)
    return _record(
        claim,
        4,
        outcome=RungOutcome.PASSED if passed else RungOutcome.FAILED,
        finding=f"gate {receipt.gate_id} of {receipt.scope_id} {verb} with exit status "
        f"{status} at {receipt.head_sha[:12]}, receipt {receipt.key}",
        sequence=sequence,
        revision=revision,
        now=now,
        inputs=_cited(claim, inputs),
        # a signalled command exits negative, which a count cannot state; the finding does
        counts={"exit status": receipt.exit_status}
        if receipt.exit_status is not None and receipt.exit_status >= 0
        else {},
        evidence_ref=inputs.entail_ref if passed else None,
    )


_CHECKS: Final = {1: _resolve, 2: _anchor, 3: _screen, 4: _entail}


def score_claim(
    claim: ClaimFiling,
    inputs: LadderInputs,
    *,
    now: datetime,
    first_sequence: int,
    from_rung: int = 1,
    held: Sequence[EvidenceRungRecord] = (),
    revision: int = 0,
) -> tuple[EvidenceRungRecord, ...]:
    """Return the records scoring rungs *from_rung* to 4 of *claim*, lowest rung first.

    Args:
        claim: The claim being scored.
        inputs: What the daemon read for the checks.
        now: When the scorer ran.
        first_sequence: The canonical sequence the first record is written at; each
            record above it is written at the next one.
        from_rung: The lowest rung to run; the rungs below it stand as *held* says.
        held: The latest record of each rung below *from_rung*.
        revision: The revision every returned record is written at.

    Returns:
        One record per rung from *from_rung* to 4.
    """
    outcomes: dict[int, RungOutcome] = {
        record.rung: record.outcome for record in held if record.rung < from_rung
    }
    records: list[EvidenceRungRecord] = []
    for rung in range(from_rung, _ENTAIL_RUNG + 1):
        sequence = first_sequence + len(records)
        unpassed = next(
            (r for r in _GATING_RUNGS if r < rung and outcomes.get(r) is not RungOutcome.PASSED),
            None,
        )
        if unpassed is not None:
            record = _record(
                claim,
                rung,
                outcome=RungOutcome.NOT_RUN,
                finding=f"not run - awaiting rung {unpassed}",
                sequence=sequence,
                revision=revision,
                awaits=unpassed,
            )
        else:
            record = _CHECKS[rung](claim, inputs, now=now, sequence=sequence, revision=revision)
        outcomes[rung] = record.outcome
        records.append(record)
    return tuple(records)


def entailment_summary(claim: ClaimFiling, receipt: HeldReceipt) -> str:
    """Return what the evidence record an automated rung 4 pass files says it shows."""
    return (
        f"Receipt {receipt.key}: gate {receipt.gate_id} of {receipt.scope_id} passed with "
        f"exit status 0 at {receipt.head_sha}; rung 4 of {claim.key} entailed the claim with it"
    )


def claim_status(records: Sequence[EvidenceRungRecord]) -> ClaimStatus:
    """Return the lifecycle a scored claim is filed at: supported only when it can promote."""
    return "SUPPORTED" if not promotion_blockers(records) else "OPEN"


__all__ = [
    "EVIDENCE_LADDER_METHOD",
    "SCORER",
    "HeldEvidence",
    "HeldReceipt",
    "LadderInputs",
    "claim_status",
    "entailment_summary",
    "score_claim",
]
