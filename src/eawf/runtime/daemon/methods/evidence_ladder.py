"""The claim verbs and ``projection.evidence.ladder``: a claim, its rungs and their checks.

Filing a claim is the scoring transaction. The claim's row and the record of each of its
four rungs are appended to the claim ledger in one session, so no claim is ever held
without the ladder that scored it, and the sequence each rung record states is the one
its own append allocated. Re-running a rung's check later appends a new revision of that
rung and of every rung above it, since each of those waits on it; the prior records stay
addressable. A rung 4 an outside party decides is recorded by attestation, against an
evidence record the attester cites, and attests without certifying.

The read answers one claim with the latest record of each rung. The Evidence route's
rung rows, the evidence viewer and the rung card are all drawn from that one answer,
which is how a row and the card opened on it cannot disagree.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, model_validator

from eawf.kernel.delivery.acceptance import EvidenceRow
from eawf.kernel.delivery.integration import IdempotencyKey
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.evidence_rung import (
    RUNG_NAMES,
    RUNG_QUESTIONS,
    ClaimFiling,
    ClaimLadder,
    ClaimProse,
    EvidenceInput,
    EvidenceRungRecord,
    ReceiptRef,
    RungBasis,
    RungOutcome,
    SpanAnchor,
    latest_rungs,
    promotion_blockers,
    require_cited_anchors,
)
from eawf.kernel.state.epoch2.urns import ClaimUrn, EvidenceUrn
from eawf.kernel.store.ledger import (
    LedgerRecord,
    effective_records,
    line_digest,
    read_ledger_records,
    render_ledger_line,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    TransactionRefusalCode,
    TransactionRefusedError,
    commit_ledger_append,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.delivery import keyed_call
from eawf.runtime.daemon.methods.delivery_acceptance import EVIDENCE_KEY_PREFIX, EVIDENCE_STATUS
from eawf.runtime.daemon.methods.delivery_completion import PROOF_PAYLOAD_KIND, FiledProof
from eawf.runtime.daemon.methods.projection import PROJECTION_UNREADABLE, document_path
from eawf.runtime.daemon.native_guard import native_mutator, native_params, require_native_call
from eawf.workflow.evidence.claim_ladder import (
    EVIDENCE_LADDER_METHOD,
    HeldEvidence,
    HeldReceipt,
    LadderInputs,
    claim_status,
    entailment_summary,
    score_claim,
)

logger = logging.getLogger(__name__)

#: The verb a claim writer files one claim under, scoring its ladder as it lands.
EVIDENCE_CLAIM_FILE_METHOD: Final = "runtime.evidence.claim.file"

#: The verb that re-runs the check behind one rung of a filed claim, and every rung above.
EVIDENCE_CLAIM_CHECK_METHOD: Final = "runtime.evidence.claim.check"

#: The verb that records what an outside party decided about a claim's rung 4.
EVIDENCE_CLAIM_ATTEST_METHOD: Final = "runtime.evidence.claim.attest"

#: The stable code a filing under a key the ledger already holds is refused with.
CLAIM_ALREADY_FILED: Final = "claim_already_filed"

#: The stable code a rung that cannot run yet, or cannot be attested, is refused with.
RUNG_NOT_RUNNABLE: Final = "rung_not_runnable"

#: The stable code an attestation citing an evidence record nobody filed is refused with.
EVIDENCE_UNHELD: Final = "evidence_unheld"

#: The evaluator an attested rung names: the record says who signed, not what checked.
ATTESTER: Final = "operator attestation"

#: The evidence kind an automated rung 4 pass files its entailment under.
_ENTAILMENT_KIND: Final = "store_record"

#: What separates a claim's key from the rung a record in its ledger scores.
_RUNG_KEY_MARK: Final = "#rung-"


def rung_record_key(record: EvidenceRungRecord) -> str:
    """Return the claim-ledger key one rung record is filed under."""
    return f"{record.claim_ref.entity_key}{_RUNG_KEY_MARK}{record.rung}@{record.revision}"


class ClaimFileParams(BaseModel):
    """Params of :data:`EVIDENCE_CLAIM_FILE_METHOD`.

    Attributes:
        urn: The address the claim is filed under.
        actor: Who asked.
        idempotency_key: The client's name for this request.
        title: The claim in one line.
        description: The claim restated plainly.
        implication: What the claim buys if it stands.
        falsifier: What observation would take it away.
        evidence_refs: The evidence records the claim cites.
        anchors: The spans the cited records point into.
        gate_receipt: The receipt of the gate whose result proves the claim.
    """

    model_config = ConfigDict(extra="forbid")

    urn: ClaimUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    title: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=72)]
    description: Annotated[str, StringConstraints(max_length=500)] | None = None
    implication: ClaimProse | None = None
    falsifier: ClaimProse | None = None
    evidence_refs: tuple[EvidenceUrn, ...] = ()
    anchors: tuple[SpanAnchor, ...] = ()
    gate_receipt: ReceiptRef | None = None

    @model_validator(mode="after")
    def _anchors_are_cited(self) -> Self:
        """Refuse an anchor into a record the claim does not cite, before anything is read.

        Raises:
            ValueError: An anchor's evidence record is not cited.
        """
        require_cited_anchors(self.evidence_refs, self.anchors)
        return self


class ClaimFileAnswer(BaseModel):
    """What one claim filing, check or attestation answers with.

    Attributes:
        claim_ref: The claim's address.
        status: The lifecycle it stands at.
        outcomes: Each rung's latest outcome, lowest rung first.
        promotion_blockers: Why the claim cannot promote; empty when it can.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_ref: str
    status: str
    outcomes: tuple[str, ...]
    promotion_blockers: tuple[str, ...]


class ClaimCheckParams(BaseModel):
    """Params of :data:`EVIDENCE_CLAIM_CHECK_METHOD`.

    Attributes:
        urn: The claim, with ``#rung-<n>`` naming the lowest rung to re-run; the whole
            ladder without one.
        expected_revision: The claim revision the caller read.
        actor: Who asked.
        idempotency_key: The client's name for this request.
    """

    model_config = ConfigDict(extra="forbid")

    urn: ClaimUrn
    expected_revision: StrictPositiveInt
    actor: PrincipalKey
    idempotency_key: IdempotencyKey


class ClaimAttestParams(BaseModel):
    """Params of :data:`EVIDENCE_CLAIM_ATTEST_METHOD`.

    Attributes:
        urn: The claim's ``#rung-4`` address; only rung 4 can be attested.
        expected_revision: The claim revision the caller read.
        actor: Who attests.
        idempotency_key: The client's name for this request.
        evidence_ref: The evidence record the outside party's decision is filed as.
        outcome: What the outside party decided.
        finding: What they found, in words.
    """

    model_config = ConfigDict(extra="forbid")

    urn: ClaimUrn
    expected_revision: StrictPositiveInt
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    evidence_ref: EvidenceUrn
    outcome: Literal["passed", "failed"]
    finding: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]


def _held_evidence(path: Path) -> dict[str, HeldEvidence]:
    """Return every evidence record by key, as the scorer reads it."""
    return {
        record.record_key: HeldEvidence(
            digest=line_digest(render_ledger_line(record)),
            summary=EvidenceRow.model_validate(record.payload).summary,
        )
        for record in effective_records(read_ledger_records(path))
        if record.record_key.startswith(EVIDENCE_KEY_PREFIX)
    }


def _held_receipt(path: Path, key: str | None) -> HeldReceipt | None:
    """Return the filed proof receipt under *key*, the newest when it was filed twice."""
    if key is None:
        return None
    proofs = (
        FiledProof.model_validate(item.payload)
        for item in read_ledger_records(path)
        if item.payload.get("payload_kind") == PROOF_PAYLOAD_KIND
    )
    receipt = next(
        (proof.receipt for proof in reversed(tuple(proofs)) if proof.receipt.id == key), None
    )
    if receipt is None:
        return None
    return HeldReceipt(
        key=receipt.id,
        result=receipt.result,
        exit_status=receipt.exit_status,
        gate_id=receipt.gate_id,
        scope_id=receipt.scope_id,
        head_sha=receipt.freshness.revision_binding.head_sha,
    )


def _read_span(repo_root: Path, anchor: SpanAnchor) -> str | None:
    """Return the anchored lines joined by newlines, ``None`` when the file or lines are gone.

    A path that resolves outside the repository -- through a symlink -- reads as gone.
    """
    root = repo_root.resolve()
    path = (root / anchor.path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if anchor.end_line > len(lines):
        return None
    return "\n".join(lines[anchor.start_line - 1 : anchor.end_line])


def _inputs(session: RootSession, claim: ClaimFiling, repo_root: Path) -> LadderInputs:
    """Return what the checks of *claim* run over, read under *session*'s locks."""
    held = _held_evidence(session.ledger_path(Epoch2Collection.EVIDENCE))
    ordinal = 1 + max((int(key.removeprefix(EVIDENCE_KEY_PREFIX)) for key in held), default=0)
    return LadderInputs(
        evidence=held,
        spans={anchor: _read_span(repo_root, anchor) for anchor in claim.anchors},
        receipt=_held_receipt(session.ledger_path(Epoch2Collection.RECEIPT), claim.gate_receipt),
        # an automated rung 4 pass files its entailment in the repository its cited records
        # are filed in, under the next key; a claim citing nothing never reaches rung 4
        entail_ref=replace(claim.evidence_refs[0], entity_key=f"{EVIDENCE_KEY_PREFIX}{ordinal:04d}")
        if claim.evidence_refs
        else None,
    )


def _append(session: RootSession, collection: Epoch2Collection, **line: Any) -> None:
    """Commit one ledger line under *session*."""
    commit_ledger_append(session, LedgerRecord(collection=collection, **line))


def _append_rungs(
    session: RootSession,
    claim: ClaimFiling,
    records: Sequence[EvidenceRungRecord],
    receipt: HeldReceipt | None,
    *,
    now: datetime,
) -> None:
    """Append each rung record, then the evidence an automated rung 4 pass entailed with."""
    for record in records:
        _append(
            session,
            Epoch2Collection.CLAIM,
            record_key=rung_record_key(record),
            status=record.outcome.value,
            recorded_at=now,
            payload=record.model_dump(mode="json"),
        )
    entail = next((r for r in records if r.rung == len(RUNG_NAMES)), None)
    if entail is None or entail.evidence_ref is None or entail.basis is RungBasis.ATTESTED:
        return
    assert receipt is not None, "an automated rung 4 pass reads a held receipt"
    row = EvidenceRow(
        id=entail.evidence_ref.entity_key,
        kind=_ENTAILMENT_KIND,
        summary=entailment_summary(claim, receipt),
        recorded_at=now,
    )
    _append(
        session,
        Epoch2Collection.EVIDENCE,
        record_key=row.id,
        status=EVIDENCE_STATUS,
        recorded_at=now,
        payload=row.model_dump(mode="json"),
    )


def _answer(claim: ClaimFiling, ladder: Sequence[EvidenceRungRecord]) -> ClaimFileAnswer:
    """Return what a claim verb answers with, over the claim's latest rung records."""
    return ClaimFileAnswer(
        claim_ref=str(claim.urn),
        status=claim.status,
        outcomes=tuple(record.outcome.value for record in ladder),
        promotion_blockers=promotion_blockers(ladder),
    )


def file_claim(
    context: Epoch2RootContext, args: ClaimFileParams, *, repo_root: Path, now: datetime
) -> ClaimFileAnswer:
    """File one claim and the four rung records scoring it, in one session.

    Args:
        context: The native context of the tree the claim belongs to.
        args: The validated request.
        repo_root: The repository whose files rung 2 reads the anchored spans from.
        now: When it was filed.

    Returns:
        The claim's address, the status it was filed at, each rung's outcome and why it
        cannot promote.

    Raises:
        DaemonValidationError: The claim ledger already holds this key.
    """
    key = args.urn.entity_key
    with context.session([args.urn]) as session:
        claims = session.ledger_path(Epoch2Collection.CLAIM)
        if any(record.record_key == key for record in read_ledger_records(claims)):
            raise DaemonValidationError(
                f"validation_failed: {CLAIM_ALREADY_FILED}: {key} is already filed"
            )
        cursor = session.read_document().get(CANONICAL_SEQUENCE_KEY, 0)
        filing = args.model_dump(exclude={"actor", "idempotency_key"}) | {
            "key": key,
            "urn": args.urn,
            "recorded_at": now,
        }
        opened = ClaimFiling.model_validate({**filing, "status": "OPEN"})
        inputs = _inputs(session, opened, repo_root)
        # the claim's own line takes the next sequence and each rung the one after it
        rungs = score_claim(opened, inputs, now=now, first_sequence=cursor + 2)
        claim = opened.model_copy(update={"status": claim_status(rungs)})
        _append(
            session,
            Epoch2Collection.CLAIM,
            record_key=key,
            status=claim.status,
            recorded_at=now,
            payload=claim.model_dump(mode="json"),
        )
        _append_rungs(session, claim, rungs, inputs.receipt, now=now)
    logger.info(f"file_claim key={key} status={claim.status} refs={len(claim.evidence_refs)}")
    return _answer(claim, rungs)


def _filed(
    session: RootSession, urn: ClaimUrn, expected_revision: int
) -> tuple[LedgerRecord, ClaimFiling, tuple[EvidenceRungRecord, ...]]:
    """Return claim *urn*'s standing line, its filing and its rung records, all revisions.

    Raises:
        DaemonValidationError: The ledger holds no such claim.
        TransactionRefusedError: The claim stands at another revision than expected.
    """
    key = urn.entity_key
    line: LedgerRecord | None = None
    rungs: list[EvidenceRungRecord] = []
    lines = read_ledger_records(session.ledger_path(Epoch2Collection.CLAIM))
    for record in effective_records(lines):
        if record.record_key == key:
            line = record
        elif record.record_key.startswith(f"{key}{_RUNG_KEY_MARK}"):
            rungs.append(EvidenceRungRecord.model_validate(record.payload))
    if line is None:
        raise DaemonValidationError(
            f"validation_failed: {PROJECTION_UNREADABLE}: the claim ledger holds no {key}"
        )
    claim = ClaimFiling.model_validate(line.payload)
    if claim.revision != expected_revision:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.REVISION_CONFLICT,
            detail=f"{key} is at revision {claim.revision}, not {expected_revision}",
            entity_ref=str(claim.urn),
            remediation="Re-read the claim and decide again.",
            revision=claim.revision,
        )
    return line, claim, tuple(rungs)


def _restatus(
    session: RootSession,
    line: LedgerRecord,
    claim: ClaimFiling,
    ladder: Sequence[EvidenceRungRecord],
    *,
    now: datetime,
) -> ClaimFiling:
    """Re-file *claim* at its next revision when its ladder moved its lifecycle."""
    status = claim_status(ladder)
    if status == claim.status:
        return claim
    moved = claim.model_copy(update={"status": status, "revision": claim.revision + 1})
    _append(
        session,
        Epoch2Collection.CLAIM,
        record_key=claim.key,
        status=status,
        recorded_at=now,
        supersedes=line_digest(render_ledger_line(line)),
        payload=moved.model_dump(mode="json"),
    )
    return moved


def recheck_claim(
    context: Epoch2RootContext, args: ClaimCheckParams, *, repo_root: Path, now: datetime
) -> ClaimFileAnswer:
    """Re-run the check of the addressed rung and of every rung above it.

    Args:
        context: The native context of the tree the claim belongs to.
        args: The validated request; its URN's rung fragment names the lowest rung.
        repo_root: The repository whose files rung 2 reads the anchored spans from.
        now: When the checks ran.

    Returns:
        The claim's address, the status it now stands at, each rung's latest outcome and
        why it cannot promote.

    Raises:
        DaemonValidationError: The ledger holds no such claim.
        TransactionRefusedError: The claim moved since the caller read it.
    """
    base = replace(args.urn, rung=None)
    from_rung = args.urn.rung or 1
    with context.session([base]) as session:
        line, claim, rungs = _filed(session, base, args.expected_revision)
        cursor = session.read_document().get(CANONICAL_SEQUENCE_KEY, 0)
        held = latest_rungs(rungs)
        inputs = _inputs(session, claim, repo_root)
        fresh = score_claim(
            claim,
            inputs,
            now=now,
            first_sequence=cursor + 1,
            from_rung=from_rung,
            held=held,
            revision=1 + max((r.revision for r in rungs), default=-1),
        )
        _append_rungs(session, claim, fresh, inputs.receipt, now=now)
        ladder = latest_rungs((*held, *fresh))
        claim = _restatus(session, line, claim, ladder, now=now)
    logger.info(f"recheck_claim key={claim.key} from_rung={from_rung} status={claim.status}")
    return _answer(claim, ladder)


def attest_claim(
    context: Epoch2RootContext, args: ClaimAttestParams, *, now: datetime
) -> ClaimFileAnswer:
    """Record what an outside party decided about a claim's rung 4, against cited evidence.

    The record's basis is ``attested``: it says who signed and on what record, and it
    never certifies, so a claim whose rung 4 is attested stays open.

    Args:
        context: The native context of the tree the claim belongs to.
        args: The validated request.
        now: When the attestation was recorded.

    Returns:
        The claim's address, the status it now stands at, each rung's latest outcome and
        why it cannot promote.

    Raises:
        DaemonValidationError: The URN names a rung other than 4, rung 1 or 2 has not
            passed, the cited evidence record is not held, or the ledger holds no such
            claim.
        TransactionRefusedError: The claim moved since the caller read it.
    """
    if args.urn.rung != len(RUNG_NAMES):
        raise DaemonValidationError(
            f"validation_failed: {RUNG_NOT_RUNNABLE}: only rung 4 is attested; "
            f"address the claim as {replace(args.urn, rung=4)}"
        )
    base = replace(args.urn, rung=None)
    with context.session([base]) as session:
        line, claim, rungs = _filed(session, base, args.expected_revision)
        held = latest_rungs(rungs)
        unpassed = [r.rung for r in held if r.rung < 3 and r.outcome is not RungOutcome.PASSED]
        if unpassed:
            raise DaemonValidationError(
                f"validation_failed: {RUNG_NOT_RUNNABLE}: rung {unpassed[0]} of {claim.key} "
                "has not passed, so rung 4 awaits it"
            )
        evidence = _held_evidence(session.ledger_path(Epoch2Collection.EVIDENCE))
        cited = evidence.get(args.evidence_ref.entity_key)
        if cited is None:
            raise DaemonValidationError(
                f"validation_failed: {EVIDENCE_UNHELD}: {args.evidence_ref.entity_key} is "
                "not held; record the outside party's decision as evidence first"
            )
        cursor = session.read_document().get(CANONICAL_SEQUENCE_KEY, 0)
        record = EvidenceRungRecord(
            claim_ref=claim.urn,
            rung=4,
            name=RUNG_NAMES[4],
            question=RUNG_QUESTIONS[4],
            outcome=RungOutcome(args.outcome),
            basis=RungBasis.ATTESTED,
            input_refs=(EvidenceInput(ref=args.evidence_ref, digest=cited.digest),),
            finding=f"{args.actor} attests: {args.finding}",
            evaluated_at=now,
            evaluator=ATTESTER,
            evidence_ref=args.evidence_ref,
            kept_with=claim.urn,
            written_at_sequence=cursor + 1,
            revision=1 + max((r.revision for r in rungs), default=-1),
        )
        _append_rungs(session, claim, (record,), None, now=now)
        ladder = latest_rungs((*held, record))
        claim = _restatus(session, line, claim, ladder, now=now)
    logger.info(f"attest_claim key={claim.key} outcome={args.outcome} status={claim.status}")
    return _answer(claim, ladder)


async def _keyed(
    ctx: MethodContext,
    authority: RootAuthority,
    *,
    method: str,
    args: BaseModel,
    key: str,
    call: Callable[..., BaseModel],
) -> dict[str, Any]:
    """Run one claim verb once per idempotency key, off the event loop."""
    context = ctx.native_root_context(authority.root)
    now = datetime.now(UTC)
    return await asyncio.to_thread(
        keyed_call,
        context,
        method=method,
        key=key,
        params=args.model_dump(mode="json"),
        call=partial(call, context, args, now=now),
        at=now,
    )


@native_mutator(EVIDENCE_CLAIM_FILE_METHOD)
async def _file_claim(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """File one claim and score its ladder."""
    args = native_params(ClaimFileParams, params)
    return await _keyed(
        ctx,
        authority,
        method=EVIDENCE_CLAIM_FILE_METHOD,
        args=args,
        key=args.idempotency_key,
        call=partial(file_claim, repo_root=authority.root.parent),
    )


@native_mutator(EVIDENCE_CLAIM_CHECK_METHOD)
async def _check_claim(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Re-run the checks of one rung of a filed claim and the rungs above it."""
    args = native_params(ClaimCheckParams, params)
    return await _keyed(
        ctx,
        authority,
        method=EVIDENCE_CLAIM_CHECK_METHOD,
        args=args,
        key=args.idempotency_key,
        call=partial(recheck_claim, repo_root=authority.root.parent),
    )


@native_mutator(EVIDENCE_CLAIM_ATTEST_METHOD)
async def _attest_claim(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record an outside party's decision about a claim's rung 4."""
    args = native_params(ClaimAttestParams, params)
    return await _keyed(
        ctx,
        authority,
        method=EVIDENCE_CLAIM_ATTEST_METHOD,
        args=args,
        key=args.idempotency_key,
        call=attest_claim,
    )


class LadderParams(BaseModel):
    """The parameters one ladder read carries.

    Attributes:
        repo_root: The repository whose tree to answer for; the daemon's bound tree
            when absent.
        claim_key: The claim whose ladder to read.
    """

    model_config = ConfigDict(extra="forbid")

    repo_root: str | None = None
    claim_key: Annotated[str, StringConstraints(pattern=r"^CLM-\d{4,}$")]


def read_ladder(path: Path, key: str, *, now: datetime) -> ClaimLadder:
    """Return claim *key* with the latest record of each rung, read from its ledger.

    Args:
        path: The claim ledger.
        key: The claim's key.
        now: When the ladder is read.

    Returns:
        The claim and its ladder.

    Raises:
        DaemonValidationError: The ledger holds no claim under *key*.
    """
    claim: ClaimFiling | None = None
    rungs: list[EvidenceRungRecord] = []
    for record in effective_records(read_ledger_records(path)):
        if record.record_key == key:
            claim = ClaimFiling.model_validate(record.payload)
        elif record.record_key.startswith(f"{key}{_RUNG_KEY_MARK}"):
            rungs.append(EvidenceRungRecord.model_validate(record.payload))
    if claim is None:
        raise DaemonValidationError(
            f"validation_failed: {PROJECTION_UNREADABLE}: the claim ledger holds no {key}"
        )
    return ClaimLadder(claim=claim, rungs=latest_rungs(rungs), read_at=now)


@register(EVIDENCE_LADDER_METHOD)
async def read_evidence_ladder(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return one claim with the latest record of each rung that scored it. Nothing is written.

    Args:
        ctx: Server context, whose bound state path is the tree fallback.
        params: The request parameters, validated as :class:`LadderParams`.

    Returns:
        The :class:`~eawf.kernel.state.epoch2.evidence_rung.ClaimLadder` as a JSON-mode
        mapping.

    Raises:
        NativeAuthorityRefusedError: The request addresses no epoch-2 tree.
        DaemonValidationError: The parameters name no claim, or the ledger holds none.
    """
    try:
        args = LadderParams.model_validate(params)
    except ValidationError as error:
        raise DaemonValidationError(
            f"validation_failed: {PROJECTION_UNREADABLE}: {error.error_count()} bad "
            f"parameter(s) for {EVIDENCE_LADDER_METHOD}"
        ) from error
    authority = require_native_call(ctx, params)
    path = ledger_path(document_path(authority), Epoch2Collection.CLAIM)
    ladder = await asyncio.to_thread(read_ladder, path, args.claim_key, now=datetime.now(UTC))
    logger.debug(f"read_evidence_ladder claim={args.claim_key} rungs={len(ladder.rungs)}")
    return ladder.model_dump(mode="json")


__all__ = [
    "CLAIM_ALREADY_FILED",
    "EVIDENCE_CLAIM_FILE_METHOD",
    "ClaimFileAnswer",
    "ClaimFileParams",
    "LadderParams",
    "file_claim",
    "read_evidence_ladder",
    "read_ladder",
    "rung_record_key",
]
