"""``runtime.evidence.claim.file`` and ``projection.evidence.ladder``: a claim and its rungs.

Filing a claim is the scoring transaction. The claim's row and the record of each of its
four rungs are appended to the claim ledger in one session, so no claim is ever held
without the ladder that scored it, and the sequence each rung record states is the one
its own append allocated.

The read answers one claim with the latest record of each rung. The Evidence route's
rung rows, the evidence viewer and the rung card are all drawn from that one answer,
which is how a row and the card opened on it cannot disagree.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from eawf.kernel.delivery.integration import IdempotencyKey
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.evidence_rung import (
    ClaimFiling,
    ClaimLadder,
    ClaimProse,
    EvidenceRungRecord,
    latest_rungs,
    promotion_blockers,
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
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY, commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.delivery import keyed_call
from eawf.runtime.daemon.methods.projection import PROJECTION_UNREADABLE, document_path
from eawf.runtime.daemon.native_guard import native_mutator, native_params, require_native_call
from eawf.workflow.evidence.claim_ladder import EVIDENCE_LADDER_METHOD, claim_status, score_claim

logger = logging.getLogger(__name__)

#: The verb a claim writer files one claim under, scoring its ladder as it lands.
EVIDENCE_CLAIM_FILE_METHOD: Final = "runtime.evidence.claim.file"

#: The stable code a filing under a key the ledger already holds is refused with.
CLAIM_ALREADY_FILED: Final = "claim_already_filed"

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


class ClaimFileAnswer(BaseModel):
    """What one claim filing answers with.

    Attributes:
        claim_ref: The filed claim's address.
        status: The lifecycle it was filed at.
        outcomes: Each rung's outcome, lowest rung first.
        promotion_blockers: Why the claim cannot promote; empty when it can.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_ref: str
    status: str
    outcomes: tuple[str, ...]
    promotion_blockers: tuple[str, ...]


def _evidence_digests(path: Path) -> dict[str, str]:
    """Return the digest of every evidence record's ledger line, by its key."""
    return {
        record.record_key: line_digest(render_ledger_line(record))
        for record in effective_records(read_ledger_records(path))
    }


def file_claim(
    context: Epoch2RootContext, args: ClaimFileParams, *, now: datetime
) -> ClaimFileAnswer:
    """File one claim and the four rung records scoring it, in one session.

    Args:
        context: The native context of the tree the claim belongs to.
        args: The validated request.
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
        filing = {
            "key": key,
            "urn": args.urn,
            "title": args.title,
            "description": args.description,
            "implication": args.implication,
            "falsifier": args.falsifier,
            "evidence_refs": args.evidence_refs,
            "recorded_at": now,
        }
        # the claim's own line takes the next sequence and each rung the one after it
        rungs = score_claim(
            ClaimFiling.model_validate({**filing, "status": "OPEN"}),
            _evidence_digests(session.ledger_path(Epoch2Collection.EVIDENCE)),
            now=now,
            first_sequence=cursor + 2,
        )
        claim = ClaimFiling.model_validate({**filing, "status": claim_status(rungs)})
        commit_ledger_append(
            session,
            LedgerRecord(
                collection=Epoch2Collection.CLAIM,
                record_key=key,
                status=claim.status,
                recorded_at=now,
                payload=claim.model_dump(mode="json"),
            ),
        )
        for record in rungs:
            commit_ledger_append(
                session,
                LedgerRecord(
                    collection=Epoch2Collection.CLAIM,
                    record_key=rung_record_key(record),
                    status=record.outcome.value,
                    recorded_at=now,
                    payload=record.model_dump(mode="json"),
                ),
            )
    logger.info(f"file_claim key={key} status={claim.status} refs={len(claim.evidence_refs)}")
    return ClaimFileAnswer(
        claim_ref=str(args.urn),
        status=claim.status,
        outcomes=tuple(record.outcome.value for record in rungs),
        promotion_blockers=promotion_blockers(rungs),
    )


@native_mutator(EVIDENCE_CLAIM_FILE_METHOD)
async def _file_claim(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """File one claim and score its ladder."""
    args = native_params(ClaimFileParams, params)
    context = ctx.native_root_context(authority.root)
    now = datetime.now(UTC)
    return await asyncio.to_thread(
        keyed_call,
        context,
        method=EVIDENCE_CLAIM_FILE_METHOD,
        key=args.idempotency_key,
        params=args.model_dump(mode="json"),
        call=partial(file_claim, context, args, now=now),
        at=now,
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
