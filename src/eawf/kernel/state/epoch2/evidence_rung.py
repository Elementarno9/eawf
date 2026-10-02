"""EvidenceRungRecord: what one rung of a claim's evidence ladder found.

A claim is scored by four ordered rungs -- ``resolve``, ``anchor``,
``screen``, ``entail`` -- and each rung writes its own record. The Evidence
route draws a claim's rung rows from these records and the rung card opens
one of them in full, so a row and its card cannot disagree: both read the
same record, and nothing either shows is stored anywhere else.

No rung's outcome is inferred from another's. A rung that never started is
``not_run`` and names the rung it awaits; a rung that started and has no
result is ``unknown``; only a returned negative is ``failed``. A negative
at rung 3 is advisory and leaves rung 4 runnable, so rung 4 is never
blocked by rung 3. The claim's standing is derived from the four outcomes
at render time and is never stored beside them, which is why this record
has no standing field to fill.

A promotable claim clears rung 1 and rung 4 on the automated path and
keeps the evidence rung 4 entailed it with. Rung 3 alone never promotes,
and an attested rung 4 -- a sign-off with no automated check behind it --
attests without certifying.

What rungs 2 and 4 check is named on the claim itself: rung 2 checks the
spans the claim anchors its references to, and rung 4 reads the gate
receipt the claim names as its deterministic proof. A claim that names
neither leaves those rungs with nothing to run, which is unknown rather
than failed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, ConfigDict, Field, StringConstraints, model_validator

from eawf.kernel.identity import EntityKind, validate_entity_key
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    Sha256DigestStr,
    StrictNonNegativeInt,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.urns import ClaimUrn, EvidenceUrn, RunUrn, render_qualified_urn
from eawf.kernel.state.types import UtcDatetime

#: A bounded single-line label: a count's name, an evaluator's name.
ShortText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=120)
]

RungNumber = Literal[1, 2, 3, 4]
RungName = Literal["resolve", "anchor", "screen", "entail"]

#: Each rung's name, keyed by its number.
RUNG_NAMES: Final[Mapping[int, RungName]] = MappingProxyType(
    {1: "resolve", 2: "anchor", 3: "screen", 4: "entail"}
)

#: Each rung's question, verbatim, as the card's ``CHECK`` row prints it.
RUNG_QUESTIONS: Final[Mapping[int, str]] = MappingProxyType(
    {
        1: "Does every reference resolve to a retrievable artifact at its recorded digest?",
        2: "Does the referenced span exist and match its anchor digest?",
        3: "Does the reference entail the claim under a cheap in-process check?",
        4: "Does the reference entail the claim under the deterministic arm?",
    }
)


def _repo_path(value: str) -> str:
    """Admit a repository-relative POSIX path that cannot leave the repository.

    Raises:
        ValueError: The path is absolute, carries a backslash or drive colon, or steps
            up through ``..``.
    """
    if value.startswith("/") or "\\" in value or ":" in value:
        raise ValueError(f"{value!r} is not a repository-relative POSIX path")
    if ".." in PurePosixPath(value).parts:
        raise ValueError(f"{value!r} steps out of the repository")
    return value


#: A repository-relative POSIX path, so a span can only ever be read from inside the
#: repository.
RepoPath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=400),
    AfterValidator(_repo_path),
]


def _receipt_key(value: str) -> str:
    """Admit only a canonical ``RCP-####`` receipt key."""
    return validate_entity_key(EntityKind.RECEIPT, value)


#: The ``RCP-####`` key of a filed gate receipt.
ReceiptRef = Annotated[str, StringConstraints(strict=True), AfterValidator(_receipt_key)]

#: The literal finding of a rung that started and has not returned.
UNKNOWN_FINDING: Final = "no outcome yet - unknown, not failed"

#: The rungs a promotable claim must clear.
_RESOLVE_RUNG: Final = 1
_ENTAIL_RUNG: Final = 4


class RungOutcome(StrEnum):
    """What one rung returned."""

    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_RUN = "not_run"


class RungBasis(StrEnum):
    """Whether an automated check or a sign-off stands behind a rung."""

    AUTOMATED = "automated"
    ATTESTED = "attested"


class EvidenceInput(Epoch2Model):
    """One input a rung ran over, with the full digest it was read at.

    Attributes:
        ref: The evidence record or Run the input is.
        digest: The input's digest; ``None`` when it could not be fetched.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: EvidenceUrn | RunUrn
    digest: Sha256DigestStr | None


class EvidenceRungRecord(Epoch2Model):
    """One rung of one claim's ladder, as the evidence scorer wrote it.

    Attributes:
        claim_ref: The claim the rung scores.
        rung: The rung's number.
        name: The rung's name; must agree with ``rung``.
        question: The rung's question, verbatim.
        outcome: What the rung returned.
        basis: Whether an automated check or a sign-off stands behind it.
        awaits_rung: The rung a not-run rung waits on.
        input_refs: Every input the rung ran over.
        finding: The finding in words.
        counts: The sampled counts the finding cites, by name.
        evaluated_at: When the rung returned; ``None`` until it has.
        evaluator: The evidence-scoring producer that wrote the record.
        evidence_ref: The evidence record the rung wrote, once it has one.
        kept_with: The claim the record is kept with; equals ``claim_ref``.
        written_at_sequence: The workspace canonical sequence it was written at.
        revision: The record's revision; a re-evaluation is a new record.

    Raises:
        pydantic.ValidationError: A name, question or keeper that disagrees
            with the rung, an attestation below rung 4, a not-run rung
            without the rung it awaits, a returned outcome with no return
            time, or a rung 1 that passed over an input it could not fetch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_ref: ClaimUrn
    rung: RungNumber
    name: RungName
    question: str
    outcome: RungOutcome
    basis: RungBasis = RungBasis.AUTOMATED
    awaits_rung: Literal[1, 2, 3] | None = None
    input_refs: tuple[EvidenceInput, ...] = ()
    finding: Annotated[str, Field(max_length=500)]
    counts: dict[ShortText, StrictNonNegativeInt] = Field(default_factory=dict)
    evaluated_at: UtcDatetime | None = None
    evaluator: ShortText
    evidence_ref: EvidenceUrn | None = None
    kept_with: ClaimUrn
    written_at_sequence: StrictNonNegativeInt
    revision: StrictNonNegativeInt

    @model_validator(mode="after")
    def _record_agrees_with_its_rung(self) -> Self:
        """Refuse a record whose name, question, keeper or basis disagree with its rung.

        Raises:
            ValueError: One of the combinations the class docstring names.
        """
        if self.claim_ref.rung is not None:
            raise ValueError("claim_ref addresses the claim, not one of its rungs")
        if self.kept_with != self.claim_ref:
            raise ValueError("a rung record is kept with the claim it scores")
        if self.name != RUNG_NAMES[self.rung]:
            raise ValueError(f"rung {self.rung} is {RUNG_NAMES[self.rung]!r}, not {self.name!r}")
        if self.question != RUNG_QUESTIONS[self.rung]:
            raise ValueError(f"rung {self.rung} asks its own question verbatim")
        if self.basis is RungBasis.ATTESTED and self.rung != _ENTAIL_RUNG:
            raise ValueError(f"rung {self.rung} cannot be attested; only rung 4 is")
        return self

    @model_validator(mode="after")
    def _record_agrees_with_its_outcome(self) -> Self:
        """Refuse a record whose fields disagree with what the rung returned.

        Raises:
            ValueError: One of the combinations the class docstring names.
        """
        not_run = self.outcome is RungOutcome.NOT_RUN
        if not_run != (self.awaits_rung is not None):
            raise ValueError("awaits_rung is set exactly when the rung has not run")
        if self.awaits_rung is not None and self.awaits_rung >= self.rung:
            raise ValueError(f"rung {self.rung} cannot await rung {self.awaits_rung}")
        returned = self.outcome in (RungOutcome.PASSED, RungOutcome.FAILED)
        if returned != (self.evaluated_at is not None):
            raise ValueError("evaluated_at is set exactly when the rung has returned")
        if self.outcome is RungOutcome.UNKNOWN and self.finding != UNKNOWN_FINDING:
            raise ValueError(f"an unknown rung's finding reads {UNKNOWN_FINDING!r}")
        unfetched = any(i.digest is None for i in self.input_refs)
        if self.rung == _RESOLVE_RUNG and unfetched and self.outcome is RungOutcome.PASSED:
            raise ValueError("rung 1 cannot pass over an input whose digest it could not fetch")
        return self

    @property
    def means(self) -> str:
        """Return what this rung's outcome means for the claim, as the ``MEANS`` row reads."""
        if self.outcome is RungOutcome.PASSED:
            if self.rung != _ENTAIL_RUNG:
                return "holds - it does not certify the claim on its own"
            if self.basis is RungBasis.ATTESTED:
                return "attests - no automated check stands behind it"
            return "certifies"
        if self.outcome is RungOutcome.UNKNOWN:
            return "is unknown, not failed"
        if self.outcome is RungOutcome.NOT_RUN:
            return f"not run - awaiting rung {self.awaits_rung}"
        if self.rung == _ENTAIL_RUNG - 1:
            return "an advisory negative - it routes the claim to rung 4"
        return "refutes"


def span_digest(text: str) -> str:
    """Return the ``sha256:`` digest of a span's text, the form an anchor digest takes."""
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


class SpanAnchor(Epoch2Model):
    """One span of a repository file a cited reference points into, and its digest.

    Attributes:
        evidence_ref: The cited evidence record the span belongs to.
        path: The file, relative to the repository root.
        start_line: The span's first line, counted from one.
        end_line: The span's last line, inclusive.
        anchor_digest: :func:`span_digest` of the span's lines joined by newlines, as
            the claim's writer read them.

    Raises:
        pydantic.ValidationError: The span ends before it starts, or the path leaves
            the repository.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_ref: EvidenceUrn
    path: RepoPath
    start_line: StrictPositiveInt
    end_line: StrictPositiveInt
    anchor_digest: Sha256DigestStr

    @model_validator(mode="after")
    def _span_runs_forward(self) -> Self:
        """Refuse a span that ends before it starts.

        Raises:
            ValueError: ``end_line`` precedes ``start_line``.
        """
        if self.end_line < self.start_line:
            raise ValueError(f"the span ends at line {self.end_line}, before {self.start_line}")
        return self


def require_cited_anchors(
    evidence_refs: Iterable[EvidenceUrn], anchors: Iterable[SpanAnchor]
) -> None:
    """Refuse an anchor into a record the claim does not cite.

    Raises:
        ValueError: An anchor's evidence record is not among *evidence_refs*.
    """
    cited = {str(ref) for ref in evidence_refs}
    for anchor in anchors:
        if str(anchor.evidence_ref) not in cited:
            raise ValueError(f"an anchor belongs to {anchor.evidence_ref}, which is not cited")


#: The lifecycle a filed claim stands at: open until a promotion clears it.
ClaimStatus = Literal["OPEN", "SUPPORTED"]

#: A claim's prose field, bounded so the Evidence route can render it on a line or two.
ClaimProse = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=300)
]


class ClaimFiling(Epoch2Model):
    """One claim as its writer filed it into the claim ledger.

    The row carries its own key, address, revision and status, so a route that lists
    claims renders it like any other record, and the rung records scoring it are kept
    beside it in the same ledger.

    Attributes:
        key: The claim's ``CLM-####`` key; the entity key of ``urn``.
        urn: The claim's canonical address.
        revision: The filing's revision; a claim is filed once.
        status: The lifecycle it was filed at.
        title: The claim in one line.
        description: The claim restated plainly, which the frames print as ``IN WORDS``.
        implication: What the claim buys if it stands, printed as ``IT PROVES``.
        falsifier: What observation would take it away, printed as ``BREAKS IF``.
        evidence_refs: The evidence records the claim cites.
        anchors: The spans the cited records point into, which rung 2 checks.
        gate_receipt: The receipt of the gate whose result proves the claim, which
            rung 4 reads; ``None`` for a claim no deterministic gate measures.
        recorded_at: When it was filed.

    Raises:
        pydantic.ValidationError: The key is not the address's entity key, the address
            names a rung, a prose field is over its bound, or an anchor belongs to a
            record the claim does not cite.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Annotated[str, StringConstraints(pattern=r"^CLM-\d{4,}$")]
    urn: ClaimUrn
    revision: StrictPositiveInt = 1
    status: ClaimStatus
    title: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=72)]
    description: Annotated[str, StringConstraints(max_length=500)] | None = None
    implication: ClaimProse | None = None
    falsifier: ClaimProse | None = None
    evidence_refs: tuple[EvidenceUrn, ...] = ()
    anchors: tuple[SpanAnchor, ...] = ()
    gate_receipt: ReceiptRef | None = None
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def _key_is_the_address(self) -> Self:
        """Refuse a filing whose key and address disagree, or that anchors an uncited record.

        Raises:
            ValueError: The address names a rung, keys another claim, or an anchor
                belongs to a record the claim does not cite.
        """
        if self.urn.rung is not None:
            raise ValueError("a claim is filed under its own address, not one of its rungs")
        if self.urn.entity_key != self.key:
            raise ValueError(f"{self.key} is filed under {self.urn.entity_key}'s address")
        require_cited_anchors(self.evidence_refs, self.anchors)
        return self


class ClaimLadder(Epoch2Model):
    """One claim with the latest record of each rung that scored it, as a surface reads it.

    Attributes:
        claim: The claim as filed.
        rungs: The latest revision of each rung's record, lowest rung first; empty
            while nothing has scored the claim.
        read_at: When the ladder was read.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim: ClaimFiling
    rungs: tuple[EvidenceRungRecord, ...] = ()
    read_at: UtcDatetime


def latest_rungs(records: Iterable[EvidenceRungRecord]) -> tuple[EvidenceRungRecord, ...]:
    """Return each rung's latest revision among *records*, lowest rung first.

    A re-evaluation is a new record and the prior stays addressable, so the record a
    surface draws for a rung is the highest revision written for it.
    """
    latest: dict[int, EvidenceRungRecord] = {}
    for record in records:
        held = latest.get(record.rung)
        if held is None or record.revision > held.revision:
            latest[record.rung] = record
    return tuple(latest[rung] for rung in sorted(latest))


def promotion_blockers(records: Iterable[EvidenceRungRecord]) -> tuple[str, ...]:
    """Return why the claim the *records* score cannot promote; empty when it can.

    Only each rung's latest revision counts, since a re-evaluation is a new
    record and the prior stays addressable. A claim promotes when rung 1 and
    rung 4 both passed on the automated path and rung 4 kept the evidence it
    entailed the claim with.

    Args:
        records: Every rung record written for one claim.

    Returns:
        One reason per unmet requirement, in rung order.

    Raises:
        ValueError: The records score more than one claim.
    """
    held = tuple(records)
    claims = {render_qualified_urn(record.claim_ref) for record in held}
    latest = {record.rung: record for record in latest_rungs(held)}
    if len(claims) > 1:
        raise ValueError(f"promotion is judged per claim, got {len(claims)} claims")
    reasons: list[str] = []
    for rung in (_RESOLVE_RUNG, _ENTAIL_RUNG):
        decisive = latest.get(rung)
        if decisive is None or decisive.outcome is not RungOutcome.PASSED:
            state = "no record" if decisive is None else decisive.outcome.value
            reasons.append(f"rung {rung} ({RUNG_NAMES[rung]}) has not passed: {state}")
        elif decisive.basis is RungBasis.ATTESTED:
            reasons.append(f"rung {rung} is attested, and an attestation does not certify")
    entail = latest.get(_ENTAIL_RUNG)
    if (
        entail is not None
        and entail.outcome is RungOutcome.PASSED
        and (entail.evidence_ref is None or not entail.input_refs)
    ):
        reasons.append("rung 4 passed without keeping the evidence it entailed the claim with")
    return tuple(reasons)


__all__ = [
    "RUNG_NAMES",
    "RUNG_QUESTIONS",
    "UNKNOWN_FINDING",
    "ClaimFiling",
    "ClaimLadder",
    "ClaimProse",
    "ClaimStatus",
    "EvidenceInput",
    "EvidenceRungRecord",
    "ReceiptRef",
    "RepoPath",
    "RungBasis",
    "RungOutcome",
    "SpanAnchor",
    "latest_rungs",
    "promotion_blockers",
    "require_cited_anchors",
    "span_digest",
]
