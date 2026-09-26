"""``release.*`` JSON-RPC methods: checkpoint readiness and approval.

Domain logic stays in the library; these handlers parse params, resolve
the authored checkpoint configuration against the train, and hand back
JSON. Three methods land here:

* ``release.show`` -- the train ladder plus the rung a version occupies;
* ``release.compute_readiness`` -- the twelve-signal preflight sweep,
  optionally applied to a supplied candidate record so the caller learns
  which status the result puts it into;
* ``release.approve`` -- the approval guard, which denies
  ``release_not_ready`` naming the first red signal.

``release.compute_readiness`` and ``release.publish`` both run the
shared sweep of :mod:`eawf.runtime.release.chokepoint` over the checkout
that holds the daemon's state root, at the source commit the record
pins. The daemon's own working directory and HEAD never enter it.

A counted waiver is only ever explained by the rows the caller supplies,
so ``release.compute_readiness`` and ``release.publish`` both carry
``waivers`` and ``acknowledgements`` beside the bare ``waiver_count``.
Without the rows every counted waiver classifies ``unexplained``, which
is red and not acknowledgeable, and the acknowledgement tier is
unreachable from the RPC surface. ``release.approve`` takes no separate
acknowledgement list: the block it honours is the one inside the
``readiness`` it binds, so an acknowledgement enters the receipt in
exactly one place and cannot drift between the sweep and the approval.

Four more verbs touch an external registry and are keyed differently:
``release.publish``, ``release.retry_target``, ``release.reconcile`` and
``release.observe_target`` each require an ``expected_revision``
(compare-and-swap against the record the caller holds) and an
``idempotency_key`` (replay identity in the durable ledger).
``release.show`` and ``release.compute_readiness`` stay pure -- a
readiness sweep can be run against a proposed record before anything is
persisted; the four registry verbs append to
``<state_dir>/store/release.jsonl`` through
:mod:`eawf.workflow.release.ledger`, because a verb that touches an
external registry has to remember what it already did. Their shared
replay and persistence plumbing is
:mod:`eawf.runtime.daemon.methods.release_keyed`.

Every verb that moves a record also persists it, into the separate
record collection of :mod:`eawf.workflow.release.records`, one row per
revision it produced: ``release.create`` and ``release.approve`` open
and authorise a checkpoint, and the four registry verbs carry it on to
BAKED. A record only a single RPC reply ever carried could not be read
back by anything -- ``release.show`` would go on reporting ``approved``
after a publish -- so none of them runs without a state root.

A leg settling does not move the release by itself, so
``release.reconcile`` and ``release.observe_target`` both follow up with
:func:`~eawf.workflow.release.settlement.follow_guarded_edges`: the
reconcile that completes the required reports opens verification, and
the observation that completes the required read-backs bakes. A
reconcile that carries the publish job's receipt also moves a queued
leg into flight first, because the receipt's run id proves the dispatch
nothing else recorded.

``release.reconcile`` and ``release.observe_target`` are deliberately
separate verbs rather than one with a flag. Reconciliation records what
the adapter finally said about its own call; observation records what an
independent read-back found. Only the second may write an ``observed_*``
status, and only from an observation receipt, so no adapter is ever the
judge of its own publication.

``release.burn`` is the terminal move of that same recovery path. It
makes no registry call -- there is nothing left to call -- but it is
keyed like the four that do, because it abandons the open operation and
writes a status no later transition can revise. It is the one verb that
requires an operator ``reason``, and it records that reason on both rows
it appends, so a burned version's record says why it was abandoned
instead of leaving a reader to infer an outcome from a terminal status.

``release.adopt`` and ``release.cancel`` are the two verbs for a version
whose publication this machinery did not run, and they live beside this
module in :mod:`eawf.runtime.daemon.methods.release_disposition`. The
burn reaches into that module for the one case it shares: an adopted
record has no publication episode to abandon, so its terminal move
writes a record row and no ledger row at all. The entry checks both
modules run first are in
:mod:`eawf.runtime.daemon.methods.release_context`.

``release.create`` is the one way into a checkpoint record. It opens a
DRAFT only once every measured contract the checkpoint asserts over is
promoted (:mod:`eawf.workflow.release.admission`), and only once the rung
below is finished with: a predecessor that shipped also needs its
recorded train advance. The two verbs that walk the train past a
finished checkpoint, ``release.produce_receipts`` and
``release.advance_train``, live in
:mod:`eawf.runtime.daemon.methods.release_receipts`. ``release.show``
reports the open rung the way those verbs judge it, derived from the
stored records and advances.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from eawf.kernel.release.waiver import ReleaseWaiver
from eawf.kernel.spec.release import (
    Release,
    ReleaseCheckpoint,
    ReleaseTargetStatus,
    ReleaseTrain,
    release_key,
    semver_equivalent,
)
from eawf.kernel.spec.release_config import ReleaseConfig
from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.release_context import (
    assert_revision,
    require_state_path,
    resolve_config,
    validated_release,
)
from eawf.runtime.daemon.methods.release_disposition import burn_adopted_record
from eawf.runtime.daemon.methods.release_keyed import (
    keyed_reply,
    open_operation,
    persist_walk,
    replay_record,
    replayed_operation,
    request_identity,
)
from eawf.runtime.release.chokepoint import sweep_pinned_source
from eawf.surfaces.cli.errors import CliError, UserError
from eawf.workflow.evidence._io import load_state
from eawf.workflow.evidence.provider_certification import CanaryEvidence, load_canary_evidence
from eawf.workflow.release.adapters import collect_observation
from eawf.workflow.release.admission import (
    create_checkpoint_release,
    required_contract_ids,
)
from eawf.workflow.release.advance import TrainAdvanceError, derive_train
from eawf.workflow.release.ledger import record_operation
from eawf.workflow.release.lifecycle import ReleaseTransitionError
from eawf.workflow.release.observation import (
    FrozenManifest,
    RecordedResponse,
    assert_manifest_binds,
    observation_request,
)
from eawf.workflow.release.preflight import (
    approve_release,
    assert_approval_receipts,
    record_preflight_result,
)
from eawf.workflow.release.publication import (
    begin_publication,
    burn_release,
    reconcile_target,
    retry_publication,
)
from eawf.workflow.release.publication_receipt import (
    PublicationReceipt,
    load_receipt,
    reported_status,
)
from eawf.workflow.release.records import (
    read_release_record,
    read_release_records,
    record_envelope_id,
    record_release,
)
from eawf.workflow.release.settlement import follow_guarded_edges, observe_target
from eawf.workflow.release.target_machine import TargetTransitionError
from eawf.workflow.release.train import V07_TRAIN
from eawf.workflow.release.train_store import read_checkpoint_receipts, read_train_advances
from eawf.workflow.verify.checkpoint_succession import (
    CheckpointSuccessionError,
    assert_predecessor_terminal,
    predecessor_rung,
)
from eawf.workflow.verify.release_readiness import (
    DEFAULT_SIGNAL_TTL_SECONDS,
    ReleaseReadiness,
    WaiverAcknowledgement,
)

logger = logging.getLogger(__name__)


class ShowParams(BaseModel):
    """Params for :func:`show`.

    Attributes:
        version: Normalized checkpoint version to describe. ``None``
            describes the rung the stores place the train on.
    """

    model_config = ConfigDict(extra="forbid")
    version: str | None = None


class ComputeReadinessParams(BaseModel):
    """Params for :func:`compute_readiness_method`.

    Attributes:
        version: Normalized checkpoint version to preflight.
        observed_revision: Source commit the sweep is computed against.
            A supplied :attr:`release` pins it already, so here it may
            only repeat that pin. With neither, the sweep reads HEAD.
        ttl_seconds: Freshness window stamped on each row.
        waiver_count: Gate waivers recorded against the checkpoint.
            Left at zero it is derived from :attr:`waivers`, so a caller
            supplying the rows never has to count them too.
        waivers: The counted waiver rows, each naming its scope, reason
            and protected principal. Without them a counted waiver has
            no explanation attached and classifies ``unexplained``,
            which no acknowledgement can clear.
        acknowledgements: Operator acceptances of the counted waivers.
            An explained waiver holds the sweep at
            ``awaiting_acknowledgement`` until one names it.
        release: Optional serialized candidate record. When supplied,
            the handler also reports the status the sweep result moves
            it into.
    """

    model_config = ConfigDict(extra="forbid")
    version: str
    observed_revision: str | None = None
    ttl_seconds: int = DEFAULT_SIGNAL_TTL_SECONDS
    waiver_count: int = 0
    waivers: tuple[ReleaseWaiver, ...] = ()
    acknowledgements: tuple[WaiverAcknowledgement, ...] = ()
    release: dict[str, Any] | None = None


class ApproveParams(BaseModel):
    """Params for :func:`approve`.

    Attributes:
        release: Serialized candidate record being approved.
        readiness: Serialized sweep the approval binds.
        approval_ref: Reference to the approval receipt.
    """

    model_config = ConfigDict(extra="forbid")
    release: dict[str, Any]
    readiness: dict[str, Any]
    approval_ref: str


@register("release.show")
async def show(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Describe the train ladder, one checkpoint rung, and its record.

    The ladder is source-resident, but "which rung exists", "which rung
    is open" and "has that rung been cut" are different questions, and an
    operator asking the last two should not have to read a store file.
    So the open index is derived from the stored records and advances,
    and the reply also carries the recorded record for the rung, or
    ``None`` when the checkpoint has never been opened.

    Args:
        ctx: Server context; its state root supplies the stored records
            and advances. A daemon without one answers from the
            source-declared ladder with ``record: None`` rather than
            refusing: describing the ladder is useful even where nothing
            is recorded.
        params: JSON-RPC params per :class:`ShowParams`.

    Returns:
        The train id, target version, the derived open index, the
        ordered ladder, the requested rung (the open one when no version
        is named), and the record standing at it.

    Raises:
        DaemonValidationError: When the train declares no such rung, or
            a store is corrupt.
    """
    args = ShowParams.model_validate(params)
    records, train = _recorded_train(ctx)
    rung = train.current_checkpoint if args.version is None else _rung_for(args.version)
    record = records.get(rung.release_key)
    return {
        "train_id": train.train_id,
        "target_version": train.target_version,
        "current_checkpoint_index": train.current_checkpoint_index,
        "checkpoints": [checkpoint.model_dump(mode="json") for checkpoint in train.checkpoints],
        "checkpoint": rung.model_dump(mode="json"),
        "record": None if record is None else record.model_dump(mode="json"),
    }


def _recorded_train(ctx: MethodContext) -> tuple[dict[str, Release], ReleaseTrain]:
    """Return the stored records and the train standing where they put it.

    Args:
        ctx: Server context; ``state_path`` may be unset.

    Returns:
        The current record of every stored release, and the train at
        its derived open index. Without a state root, no records and the
        source-declared train.

    Raises:
        DaemonValidationError: When a store exists but is corrupt. A
            checkpoint reported as never opened because its row could not
            be parsed is the one wrong answer here.
    """
    if ctx.state_path is None:
        return {}, V07_TRAIN
    state_path = Path(ctx.state_path)
    try:
        records = read_release_records(state_path)
        advances = read_train_advances(state_path)
        return records, derive_train(V07_TRAIN, recorded_keys=records, advances=advances)
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc


def _rung_for(version: str) -> ReleaseCheckpoint:
    """Return the ladder rung tagging *version*.

    Args:
        version: Normalized checkpoint version.

    Returns:
        The matching
        :class:`~eawf.kernel.spec.release.ReleaseCheckpoint`.

    Raises:
        DaemonValidationError: When the version is malformed or the
            train declares no such rung.
    """
    try:
        return V07_TRAIN.checkpoint_for_version(version)
    except (KeyError, ValueError) as exc:
        raise DaemonValidationError(
            f"validation_failed: train {V07_TRAIN.train_id} declares no checkpoint for {version!r}"
        ) from exc


def _sweep(
    state_path: Path,
    config: ReleaseConfig,
    *,
    version: str,
    pinned: str | None,
    args: ComputeReadinessParams | PublishParams,
    computed_at: datetime,
) -> ReleaseReadiness:
    """Run the shared chokepoint sweep for one daemon verb.

    Args:
        state_path: Path to ``state.json``; names the checkout swept.
        config: The checkpoint configuration the sweep is judged against.
        version: Checkpoint version the verb names.
        pinned: The ``source_sha`` the verb's record carries, if any.
        args: The verb's params: observed revision, ttl and waiver block.
        computed_at: Timezone-aware UTC instant the sweep runs at.

    Returns:
        The total readiness sweep.

    Raises:
        ValueError: When the observed revision contradicts *pinned*, or
            the sweep rejects its inputs.
    """
    return sweep_pinned_source(
        config,
        version=version,
        state_path=state_path,
        pinned=pinned,
        observed_revision=args.observed_revision,
        waiver_count=args.waiver_count,
        computed_at=computed_at,
        ttl_seconds=args.ttl_seconds,
        waivers=args.waivers,
        acknowledgements=args.acknowledgements,
    )


@register("release.compute_readiness")
async def compute_readiness_method(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Compute the twelve-signal preflight sweep for one checkpoint.

    Args:
        ctx: Server context; its state root names the checkout swept.
        params: JSON-RPC params per :class:`ComputeReadinessParams`.

    Returns:
        The serialized :class:`ReleaseReadiness`, plus ``next_status``
        when a candidate record was supplied.

    Raises:
        DaemonValidationError: When the daemon has no state root, the
            checkpoint has no configuration, the supplied record is
            invalid or disagrees with the train, the observed revision
            contradicts the record's pin, or the record is not in a
            state preflight applies to.
    """
    args = ComputeReadinessParams.model_validate(params)
    state_path = require_state_path(ctx)
    candidate = None if args.release is None else validated_release(args.release)
    pinned = None if candidate is None else candidate.source_sha
    config = resolve_config(
        args.version,
        membership_refs=() if candidate is None else candidate.membership_refs,
    )
    try:
        readiness = _sweep(
            state_path,
            config,
            version=args.version,
            pinned=pinned,
            args=args,
            computed_at=datetime.now(UTC),
        )
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    result: dict[str, Any] = {
        "readiness": readiness.model_dump(mode="json"),
        "first_red": None if readiness.first_red is None else readiness.first_red.value,
    }
    if candidate is not None:
        try:
            result["next_status"] = record_preflight_result(candidate, readiness).status.value
        except (ReleaseTransitionError, ValueError) as exc:
            raise DaemonValidationError(f"validation_failed: {exc}") from exc
    return result


@register("release.approve")
async def approve(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Approve a candidate against a readiness sweep, and record it.

    The transition itself is pure, but the approval is the decision the
    whole publication path is authorised by, so it is written to the
    release-record collection before the reply is built. An approval no
    reader can find again is indistinguishable from one that never
    happened, which is why the verb refuses rather than approving into
    the void when there is nowhere to record it.

    Args:
        ctx: Server context; supplies the root the approved record is
            recorded under.
        params: JSON-RPC params per :class:`ApproveParams`.

    Returns:
        The serialized approved record and the id of the collection row
        carrying it.

    Raises:
        DaemonValidationError: When the daemon has no on-disk state
            root, the record or sweep is invalid, or the transition is
            denied -- the message leads with the named denial code
            (``release_not_ready`` when a required signal is not
            passing, ``prerequisite_receipt_missing`` or
            ``prerequisite_receipt_stale`` when an epoch-2 rung lacks a
            fresh stored gate receipt bound to the candidate).
    """
    args = ApproveParams.model_validate(params)
    state_path = require_state_path(ctx)
    candidate = validated_release(args.release)
    config = resolve_config(candidate.version, membership_refs=candidate.membership_refs)
    try:
        assert_approval_receipts(
            candidate,
            config,
            read_checkpoint_receipts(state_path, candidate.key),
            rung=V07_TRAIN.checkpoint_for_version(candidate.version),
            now=datetime.now(UTC),
            train_id=V07_TRAIN.train_id,
        )
    except TrainAdvanceError as exc:
        raise DaemonValidationError(
            f"validation_failed: {exc.code.value}: {exc}; produce fresh receipts with "
            f"`eawf release receipts {candidate.version}`"
        ) from exc
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    try:
        readiness = ReleaseReadiness.model_validate(args.readiness)
    except ValidationError as exc:
        raise DaemonValidationError(
            f"validation_failed: readiness payload invalid: {exc.error_count()} error(s)"
        ) from exc
    try:
        approved = approve_release(
            candidate,
            readiness,
            approval_ref=args.approval_ref,
            approved_at=datetime.now(UTC),
        )
    except ReleaseTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    record_release(
        state_path,
        approved,
        recorded_at=datetime.now(UTC),
        summary=f"approve {approved.key}: {approved.status.value}",
    )
    logger.info(f"approve key={approved.key!r} revision={approved.revision}")
    return {
        "release": approved.model_dump(mode="json"),
        "release_record_id": record_envelope_id(approved),
    }


class _PublicationParams(BaseModel):
    """Shared params of every external-effect release verb.

    Both keying fields are required rather than defaulted, because a
    default would silently turn a compare-and-swap into a
    last-writer-wins overwrite and an idempotent verb into a repeatable
    one.

    Attributes:
        release: Serialized record the verb mutates.
        expected_revision: The record revision the caller holds.
        idempotency_key: Replay identity of this call.
    """

    model_config = ConfigDict(extra="forbid")
    release: dict[str, Any]
    expected_revision: Annotated[int, Field(ge=0)]
    idempotency_key: str


class PublishParams(_PublicationParams):
    """Params for :func:`publish`.

    Attributes:
        approved_manifest_digest: The manifest digest the approval bound.
        proof_digest: Digest binding the exact artifact set.
        observed_revision: Source commit the chokepoint sweep runs at.
            The record's ``source_sha`` already pins it, so this may only
            repeat that pin.
        ttl_seconds: Freshness window stamped on each readiness row.
        waiver_count: Gate waivers recorded against the checkpoint.
        waivers: The counted waiver rows. Carried here as well as at
            approval because the chokepoint recomputes the sweep from
            scratch: a waiver the approval acknowledged but the
            chokepoint never sees would red the publication it cleared.
        acknowledgements: The operator acceptances the approval bound,
            replayed into the recomputed sweep for the same reason.
    """

    approved_manifest_digest: str
    proof_digest: str
    observed_revision: str | None = None
    ttl_seconds: int = DEFAULT_SIGNAL_TTL_SECONDS
    waiver_count: int = 0
    waivers: tuple[ReleaseWaiver, ...] = ()
    acknowledgements: tuple[WaiverAcknowledgement, ...] = ()


class RetryTargetParams(_PublicationParams):
    """Params for :func:`retry_target`.

    Attributes:
        target_id: The single leg to re-queue.
        proof_digest: Artifact-set digest; must equal the operation's,
            or the retry is a different publication.
    """

    target_id: str
    proof_digest: str


class BurnParams(_PublicationParams):
    """Params for :func:`burn`.

    Attributes:
        reason: Why the version is spent, in the operator's own words.
            Required and non-blank: the burn writes a terminal status
            that no later transition can explain, so the explanation has
            to arrive with the call. Whitespace is stripped before the
            length check, which is what makes a reason of spaces a
            refusal rather than an empty annotation.
    """

    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ReconcileParams(_PublicationParams):
    """Params for :func:`reconcile`.

    The reported result arrives one of two ways and never both. A
    ``status`` is the operator asserting what the leg did; a ``receipt``
    is the publish job's own downloaded
    :class:`~eawf.workflow.release.publication_receipt.PublicationReceipt`,
    which the verb maps onto a status itself. Accepting both would let a
    caller attach a green receipt to a failure claim, and the ledger
    would keep the claim.

    Attributes:
        target_id: The leg whose adapter reported late.
        status: The asserted result. Only ``reported_success``,
            ``reported_failure`` and ``unknown`` are accepted; the two
            ``observed_*`` statuses belong to :func:`observe`.
        receipt: The downloaded publication receipt, whose
            ``job_conclusion`` decides the status instead.
        effect_receipt_ref: The adapter's receipt, required by the two
            reported statuses. Derived from *receipt* when omitted.
    """

    target_id: str
    status: ReleaseTargetStatus | None = None
    receipt: dict[str, Any] | None = None
    effect_receipt_ref: str | None = None

    @model_validator(mode="after")
    def _exactly_one_result_source(self) -> ReconcileParams:
        """Refuse a call carrying neither result source, or both.

        Returns:
            The validated params.

        Raises:
            ValueError: When ``status`` and ``receipt`` are both set or
                both absent.
        """
        if (self.status is None) == (self.receipt is None):
            raise ValueError(
                "reconcile takes exactly one of 'status' (the asserted result) or "
                "'receipt' (the publish job's own, which decides the status)"
            )
        return self


class ObserveTargetParams(_PublicationParams):
    """Params for :func:`observe`.

    Attributes:
        target_id: The leg to read back.
        manifest: Serialized frozen manifest. Its recomputed digest must
            be the one the release approved, so an observation cannot be
            collected against a manifest nobody signed off.
        response: A reader's recorded answer already in hand. ``None``
            asks the leg's live reader to query its registry.
        effect_receipt_ref: The adapter's receipt, for observing a leg
            that timed out without one.
    """

    target_id: str
    manifest: dict[str, Any]
    response: dict[str, Any] | None = None
    effect_receipt_ref: str | None = None


@register("release.publish")
async def publish(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Open the publication episode and return its reference at once.

    The chokepoint sweep is recomputed here rather than trusted from the
    approval: the whole point of the tag chokepoint is that the last
    thing to run before external effect is a fresh preflight. It sweeps
    the record's pinned ``source_sha`` in the checkout holding the state
    root, so a version bump committed on HEAD since the approval does
    not stale it. Only once it recomputes green does the operation open,
    and the handler returns the operation reference immediately -- the
    legs are queued, not awaited, so a slow registry cannot hold the RPC
    open. The record at PUBLISHING is persisted after the ledger row, so
    ``release.show`` stops reporting ``approved``.

    Args:
        ctx: Server context; supplies the state root both stores live in.
        params: JSON-RPC params per :class:`PublishParams`.

    Returns:
        The operation reference, the operation, the record at PUBLISHING
        (the recorded record on a replay) and whether this was a replay.

    Raises:
        DaemonValidationError: On a stale revision, an idempotency
            conflict, an invalid record, an observed revision other than
            the pinned source, or a denied transition (the message leads
            with ``approval_stale`` when the approval no longer binds).
    """
    args = PublishParams.model_validate(params)
    state_path = require_state_path(ctx)
    release = validated_release(args.release)
    fingerprint = request_identity("release.publish", params, release_key=release.key)
    replayed = replayed_operation(
        state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint
    )
    if replayed is not None:
        return keyed_reply(replayed, replay_record(state_path, release), replayed=True)
    assert_revision(release, args.expected_revision)
    config = resolve_config(release.version, membership_refs=release.membership_refs)
    now = datetime.now(UTC)
    try:
        readiness = _sweep(
            state_path,
            config,
            version=release.version,
            pinned=release.source_sha,
            args=args,
            computed_at=now,
        )
        published, operation = begin_publication(
            release,
            config,
            readiness,
            operation_id=uuid4(),
            approved_manifest_digest=args.approved_manifest_digest,
            idempotency_key=args.idempotency_key,
            proof_digest=args.proof_digest,
            opened_at=now,
        )
    except ReleaseTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except (ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    recorded = record_operation(
        state_path,
        operation,
        idempotency_key=args.idempotency_key,
        fingerprint=fingerprint,
        recorded_at=now,
        summary=f"publish {release.key} to {len(config.targets)} target(s)",
    )
    persist_walk(state_path, (published,), recorded_at=now, summary=f"publish {release.key}")
    logger.info(f"publish key={release.key!r} operation_id={recorded.operation_id}")
    return keyed_reply(recorded, published, replayed=False)


@register("release.retry_target")
async def retry_target(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Re-queue one leg of the open episode under the idempotency proof.

    The record back at PUBLISHING is persisted after the ledger row.

    Args:
        ctx: Server context; supplies the state root both stores live in.
        params: JSON-RPC params per :class:`RetryTargetParams`.

    Returns:
        The operation reference, the operation, the record back at
        PUBLISHING (the recorded record on a replay) and whether this was
        a replay.

    Raises:
        DaemonValidationError: On a stale revision, an idempotency
            conflict, a leg with no budget left, or a proof digest that
            does not match the episode (``unsafe_release_retry``).
    """
    args = RetryTargetParams.model_validate(params)
    state_path = require_state_path(ctx)
    release = validated_release(args.release)
    fingerprint = request_identity("release.retry_target", params, release_key=release.key)
    replayed = replayed_operation(
        state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint
    )
    if replayed is not None:
        return keyed_reply(replayed, replay_record(state_path, release), replayed=True)
    assert_revision(release, args.expected_revision)
    config = resolve_config(release.version, membership_refs=release.membership_refs)
    operation = open_operation(state_path, release)
    now = datetime.now(UTC)
    try:
        republished, retried = retry_publication(
            release,
            config,
            operation,
            idempotency_key=operation.idempotency_key,
            proof_digest=args.proof_digest,
            now=now,
            targets=(args.target_id,),
        )
    except ReleaseTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except TargetTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except (KeyError, ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    recorded = record_operation(
        state_path,
        retried,
        idempotency_key=args.idempotency_key,
        fingerprint=fingerprint,
        recorded_at=now,
        summary=f"retry {release.key} target {args.target_id}",
    )
    persist_walk(
        state_path,
        (republished,),
        recorded_at=now,
        summary=f"retry {release.key} target {args.target_id}",
    )
    return keyed_reply(recorded, republished, replayed=False)


@register("release.burn")
async def burn(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Burn the version: record the spent checkpoint at PARTIALLY_RELEASED.

    This is the terminal move of the recovery path and the only one that
    admits a version is spent. It takes an operator *reason* and writes
    it to both durable rows the call appends -- the abandoned operation
    in the publication ledger and the burned record in the record
    collection -- because a terminal status carries no later transition
    that could explain it, and a burned version whose record does not
    say why reads as an outcome rather than as an abandonment.

    The burned record is persisted here like every other record move,
    and it matters most here: the burn is where the checkpoint stops
    moving, so a reader asking ``eawf release show`` after it must be
    answered ``partially_released`` and not the status the record left
    behind.

    What the verb deliberately does NOT write is any claim about the
    publication's outcome. :func:`~eawf.workflow.release.publication.burn_release`
    accepts no field updates, so the pinned source, tree, manifest and
    digest are frozen exactly as recovery found them, and the reason is
    an annotation beside them rather than a revision of them.

    A replay answers with the *recorded* record rather than the payload
    the caller presented, as every keyed verb's replay does, because
    after a burn the recorded one is the burned one and echoing the
    pre-burn status back would report the checkpoint as still moving.

    Args:
        ctx: Server context; supplies the state root both stores live in.
        params: JSON-RPC params per :class:`BurnParams`.

    Returns:
        The operation reference, the abandoned operation, the burned
        record, the replay flag, the reason as recorded and the id of the
        record row carrying the burn.

    An adopted record takes a shorter route through
    :func:`~eawf.runtime.daemon.methods.release_disposition.burn_adopted_record`:
    there is no publication episode to abandon, so there is no ledger row
    and no idempotency index to key it in.

    Raises:
        DaemonValidationError: On a blank or absent reason, a stale
            revision, an idempotency conflict, no open operation, or a
            leg that still has a retry left
            (``recovery_budget_available``) -- a burn declared while
            recovery could still succeed is a burn that was not
            exhausted.
    """
    args = BurnParams.model_validate(params)
    state_path = require_state_path(ctx)
    release = validated_release(args.release)
    if release.adoption is not None:
        return burn_adopted_record(
            state_path,
            release,
            reason=args.reason,
            expected_revision=args.expected_revision,
        )
    fingerprint = request_identity("release.burn", params, release_key=release.key)
    replayed = replayed_operation(
        state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint
    )
    if replayed is not None:
        settled = replay_record(state_path, release)
        return {
            **keyed_reply(replayed, settled, replayed=True),
            "reason": args.reason,
            "release_record_id": record_envelope_id(settled),
        }
    assert_revision(release, args.expected_revision)
    config = resolve_config(release.version, membership_refs=release.membership_refs)
    operation = open_operation(state_path, release)
    now = datetime.now(UTC)
    try:
        burned, abandoned = burn_release(release, config, operation)
    except ReleaseTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except (ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    summary = f"burn {burned.key}: {args.reason}"
    settled_operation = operation if abandoned is None else abandoned
    recorded = record_operation(
        state_path,
        settled_operation,
        idempotency_key=args.idempotency_key,
        fingerprint=fingerprint,
        recorded_at=now,
        summary=summary,
    )
    record_release(state_path, burned, recorded_at=now, summary=summary)
    logger.info(
        f"burn key={burned.key!r} operation_id={recorded.operation_id} "
        f"status={burned.status.value!r} reason={args.reason!r}"
    )
    return {
        **keyed_reply(recorded, burned, replayed=False),
        "reason": args.reason,
        "release_record_id": record_envelope_id(burned),
    }


def _receipt_result(
    args: ReconcileParams,
    release: Release,
) -> tuple[ReleaseTargetStatus, str | None]:
    """Return the status and effect receipt *args* settles the leg with.

    A supplied ``status`` passes through unchanged. A supplied
    ``receipt`` is validated, bound to the leg and the version it claims
    to be about, and then mapped -- so the only three statuses a receipt
    can produce are the reported ones, and neither ``observed_*`` status
    is reachable through this door at all.

    Args:
        args: The validated reconcile params.
        release: The record the leg belongs to.

    Returns:
        The status to write, and the effect-receipt locator. A receipt
        with no explicit locator supplies its own, pointing at the run
        that produced it.

    Raises:
        ValueError: When the receipt does not validate, names another
            leg, or reports a version this checkpoint does not publish
            under either of its two spellings.
    """
    if args.status is not None:
        return args.status, args.effect_receipt_ref
    receipt: PublicationReceipt = load_receipt(args.receipt)
    if receipt.target_id != args.target_id:
        raise ValueError(
            f"publication receipt reports target {receipt.target_id!r}, not {args.target_id!r}"
        )
    published_as = {release.version, semver_equivalent(release.version)}
    if receipt.version not in published_as:
        raise ValueError(
            f"publication receipt reports version {receipt.version!r}; "
            f"{release.key} publishes {sorted(published_as)}"
        )
    locator = args.effect_receipt_ref or f"receipt://{receipt.target_id}/run/{receipt.run_id}"
    return reported_status(receipt), locator


@register("release.reconcile")
async def reconcile(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Settle one leg against what its publish job finally reported.

    Reconciliation records the publisher's own late word and nothing
    more. It cannot write either ``observed_*`` status -- that takes an
    independent read-back, which is :func:`observe`.

    The word arrives either as an operator-asserted ``status`` or as the
    publish job's downloaded ``receipt``. The receipt path is the one
    the pipeline uses: each publish job uploads
    ``publication-receipt-<target>.json``, the caller downloads the
    tag's run artifacts, and the ``job_conclusion`` inside decides
    between ``reported_success``, ``reported_failure`` and ``unknown``.
    The receipt's run id also proves the leg was dispatched, so a leg
    still queued is moved into flight before the report settles it.

    Once the settled leg completes the required reports, the record moves
    on to VERIFYING in the same call. Every revision the call produced is
    persisted after the ledger row.

    Args:
        ctx: Server context; supplies the state root both stores live in.
        params: JSON-RPC params per :class:`ReconcileParams`.

    Returns:
        The operation reference, the operation with that leg settled,
        the last record the call produced (the recorded record on a
        replay) and whether this was a replay.

    Raises:
        DaemonValidationError: On a stale revision, an idempotency
            conflict, an unconfigured or unattempted target, a receipt
            that does not validate or belongs to another leg or
            version, a leg that cannot reach the reported status from
            where it stands, or an observer-only status
            (``observer_only_status``).
    """
    args = ReconcileParams.model_validate(params)
    state_path = require_state_path(ctx)
    release = validated_release(args.release)
    fingerprint = request_identity("release.reconcile", params, release_key=release.key)
    replayed = replayed_operation(
        state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint
    )
    if replayed is not None:
        return keyed_reply(replayed, replay_record(state_path, release), replayed=True)
    assert_revision(release, args.expected_revision)
    config = resolve_config(release.version, membership_refs=release.membership_refs)
    operation = open_operation(state_path, release)
    now = datetime.now(UTC)
    try:
        status, effect_receipt_ref = _receipt_result(args, release)
        reconciled, settled = reconcile_target(
            release,
            config,
            operation,
            target_id=args.target_id,
            status=status,
            effect_receipt_ref=effect_receipt_ref,
            now=now,
            dispatch_proven=args.receipt is not None,
        )
        walked = (reconciled, *follow_guarded_edges(reconciled, config, settled))
    except ReleaseTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except TargetTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except (KeyError, ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    summary = f"reconcile {release.key} target {args.target_id}"
    recorded = record_operation(
        state_path,
        settled,
        idempotency_key=args.idempotency_key,
        fingerprint=fingerprint,
        recorded_at=now,
        summary=summary,
    )
    current = persist_walk(state_path, walked, recorded_at=now, summary=summary)
    logger.info(
        f"reconcile key={release.key!r} target={args.target_id!r} "
        f"leg={status.value!r} status={current.status.value!r}"
    )
    return keyed_reply(recorded, current, replayed=False)


@register("release.observe_target")
async def observe(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Read one leg back and settle it against the frozen manifest.

    This is the only verb that writes ``observed_success`` or
    ``observed_mismatch``. The adapter is resolved from the target's
    declared ``observe_adapter`` -- there is no fallback -- and the
    manifest offered as the comparison basis must recompute to the
    digest the release approved.

    A contradicted or missing read-back routes the record to
    ``RECOVERING``; the matched read-back that completes the required set
    bakes it. A leg read back while the record is still PUBLISHING (a
    timed-out leg resolved by observation) can complete the required
    reports too, so the record then opens verification and, when every
    required leg is observed, bakes in the same call. An inconclusive
    read-back writes nothing and answers ``observation_inconclusive``.
    Every revision the call produced is persisted after the ledger row.

    Args:
        ctx: Server context; supplies the state root both stores live in.
        params: JSON-RPC params per :class:`ObserveTargetParams`.

    Returns:
        The operation reference, the operation with that leg observed,
        the last record the call produced, the replay flag, and the
        observation itself. A replay answers with the recorded record
        and ``observation: None``, returning the original receipt rather
        than re-judging the registry.

    Raises:
        DaemonValidationError: On a stale revision, an idempotency
            conflict, a manifest that does not bind, an unconfigured or
            unattempted target, an inconclusive read-back, or a denied
            release or target transition.
    """
    args = ObserveTargetParams.model_validate(params)
    state_path = require_state_path(ctx)
    release = validated_release(args.release)
    fingerprint = request_identity("release.observe_target", params, release_key=release.key)
    replayed = replayed_operation(
        state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint
    )
    if replayed is not None:
        settled_record = replay_record(state_path, release)
        return {**keyed_reply(replayed, settled_record, replayed=True), "observation": None}
    assert_revision(release, args.expected_revision)
    config = resolve_config(release.version, membership_refs=release.membership_refs)
    operation = open_operation(state_path, release)
    now = datetime.now(UTC)
    try:
        manifest = FrozenManifest.model_validate(args.manifest)
        assert_manifest_binds(release, manifest)
        observation = collect_observation(
            observation_request(config, manifest, target_id=args.target_id),
            response=None
            if args.response is None
            else RecordedResponse.model_validate(args.response),
            observed_at=now,
        )
        observed, settled = observe_target(
            release,
            config,
            operation,
            observation=observation,
            now=now,
            effect_receipt_ref=args.effect_receipt_ref,
        )
        walked = (observed, *follow_guarded_edges(observed, config, settled))
    except ReleaseTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except TargetTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except (KeyError, ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    summary = f"observe {release.key} target {args.target_id}: {observation.code.value}"
    recorded = record_operation(
        state_path,
        settled,
        idempotency_key=args.idempotency_key,
        fingerprint=fingerprint,
        recorded_at=now,
        summary=summary,
    )
    current = persist_walk(state_path, walked, recorded_at=now, summary=summary)
    logger.info(
        f"observe key={release.key!r} target={args.target_id!r} "
        f"code={observation.code.value!r} status={current.status.value!r}"
    )
    return {
        **keyed_reply(recorded, current, replayed=False),
        "observation": observation.model_dump(mode="json"),
    }


class CreateParams(BaseModel):
    """Params for :func:`create`.

    Attributes:
        version: Normalized checkpoint version to open, e.g.
            ``0.7.0.dev2``.
        membership_refs: Milestone acceptance bundles, for the rungs that
            require them.
    """

    model_config = ConfigDict(extra="forbid")
    version: str
    membership_refs: list[str] = Field(default_factory=list)


def _require_state(ctx: MethodContext) -> State:
    """Return the daemon's typed state, or refuse the verb.

    Args:
        ctx: Server context.

    Returns:
        The loaded :class:`~eawf.kernel.state.models.State`.

    Raises:
        DaemonValidationError: When the daemon runs without on-disk
            state, or the state on disk does not validate. A checkpoint
            admitted against state nobody could read would be admitted
            against nothing.
    """
    if ctx.state_path is None:
        raise DaemonValidationError(
            "validation_failed: release.create requires an on-disk state root"
        )
    try:
        return load_state(Path(ctx.state_path))
    except CliError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc


def _membership_evidence(
    state_path: Path, version: str, membership_refs: list[str]
) -> CanaryEvidence | None:
    """Return the canary export *membership_refs* resolve against.

    Read only when references are declared, so an unreadable export
    cannot block a rung that names none.

    Args:
        state_path: Bound ``state.json``; the export sits in its checkout.
        version: The checkpoint being opened; its release key picks the
            export.
        membership_refs: The references the create declares.

    Returns:
        The committed export, or ``None`` when no reference is declared
        or no export is committed.

    Raises:
        DaemonValidationError: When an export is committed but does not
            load, since references resolved against it would be resolved
            against nothing.
    """
    if not membership_refs:
        return None
    try:
        return load_canary_evidence(state_path.parent.parent, release_key(version))
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: membership_unresolved: {exc}") from exc


def _terminal_predecessor(state_path: Path, version: str) -> Release | None:
    """Return the finished record *version* succeeds, or refuse the open.

    Args:
        state_path: State root the record collection is read from.
        version: Normalized checkpoint version being opened.

    Returns:
        The predecessor record, or ``None`` at the head of the ladder
        where a checkpoint succeeds nothing.

    Raises:
        DaemonValidationError: With ``predecessor_unrecorded`` when the
            rung below has no record, ``predecessor_live`` when it has
            one that can still move, ``predecessor_not_advanced`` when it
            shipped and no train advance past it is recorded, naming the
            train when no rung is declared for *version*, or when a store
            is corrupt.
    """
    try:
        rung = predecessor_rung(V07_TRAIN, version)
        recorded = None if rung is None else read_release_record(state_path, rung.release_key)
        advanced = {
            row.closed_key
            for row in read_train_advances(state_path)
            if row.train_id == V07_TRAIN.train_id
        }
    except (KeyError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    try:
        assert_predecessor_terminal(
            V07_TRAIN, version=version, predecessor=recorded, advanced_keys=advanced
        )
    except CheckpointSuccessionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    return recorded


def _superseding(record: Release, predecessor_key: str) -> Release:
    """Return *record* carrying the lineage back to *predecessor_key*.

    The reply named the predecessor from the first, but the stored row
    did not, so the correction lineage lived in one RPC response and
    nowhere else: every later reader -- the candidate pin, the burn, the
    operator asking what a version replaced -- saw a record that
    superseded nothing. Re-validating rather than copying is what keeps
    the self-supersede invariant enforced on the way in.

    Args:
        record: The freshly opened DRAFT.
        predecessor_key: Key of the terminal rung it succeeds.

    Returns:
        The same record with
        :attr:`~eawf.kernel.spec.release.Release.supersedes_release_ref`
        set.

    Raises:
        ValidationError: When the lineage contradicts a record
            invariant, e.g. a record superseding itself.
    """
    return Release.model_validate(
        {**record.model_dump(mode="json"), "supersedes_release_ref": predecessor_key}
    )


@register("release.create")
async def create(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Open the DRAFT record of one checkpoint, after measured admission.

    Three things gate the open. Every measured contract the checkpoint
    asserts over must be promoted and resolvable, every membership
    reference must name a COMPLETED Milestone bundle in the committed
    canary export, and the rung below must
    be recorded and finished with, so two records never claim one line at
    once and the reply can name the predecessor the new record
    supersedes. A predecessor that shipped counts as finished with only
    once the train advance past it is recorded. Each refusal names the
    single thing that is missing plus the command that repairs it, so the
    operator's next action is in the error. Admission is asked first
    because it is a question about this checkpoint's own evidence, which
    the operator is here to supply; the succession answer sends them
    somewhere else entirely.

    The admitted record is persisted before the reply is built, carrying
    the predecessor it supersedes. A record that existed only in one RPC
    response could not be found again, so every later verb would have to
    be handed the record it is acting on and no reader could tell an
    opened checkpoint from an imagined one -- and a lineage that lived
    only in the reply was lost the same way.

    Args:
        ctx: Server context; supplies the state the citations resolve
            against and the root the record is recorded under.
        params: JSON-RPC params per :class:`CreateParams`.

    Returns:
        The serialized DRAFT record, the id of the collection row
        carrying it, the contract ids that admitted it, plus the key of
        the terminal predecessor it succeeds (``None`` at the head of
        the ladder).

    Raises:
        DaemonValidationError: With ``measured_contract_missing`` when a
            required contract is not promoted, with
            ``membership_unresolved`` when a membership reference names
            no COMPLETED Milestone bundle in the committed canary export,
            with
            ``predecessor_unrecorded`` / ``predecessor_live`` /
            ``predecessor_not_advanced`` when the rung below has not
            finished, or when the train declares no such rung.
    """
    args = CreateParams.model_validate(params)
    state = _require_state(ctx)
    state_path = require_state_path(ctx)
    try:
        record = create_checkpoint_release(
            state,
            train=V07_TRAIN,
            version=args.version,
            uid=uuid4(),
            membership_refs=tuple(args.membership_refs),
            canary_evidence=_membership_evidence(state_path, args.version, args.membership_refs),
        )
    except UserError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.kind}: {exc}") from exc
    except (KeyError, ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    predecessor = _terminal_predecessor(state_path, args.version)
    if predecessor is not None:
        record = _superseding(record, predecessor.key)
    record_release(
        state_path,
        record,
        recorded_at=datetime.now(UTC),
        summary=f"create {record.key}: {record.status.value}",
    )
    logger.info(f"create key={record.key!r} version={args.version!r}")
    return {
        "release": record.model_dump(mode="json"),
        "release_record_id": record_envelope_id(record),
        "measured_contracts": list(required_contract_ids(args.version)),
        "supersedes_release_ref": None if predecessor is None else predecessor.key,
    }


__all__ = [
    "ApproveParams",
    "BurnParams",
    "ComputeReadinessParams",
    "CreateParams",
    "ObserveTargetParams",
    "PublishParams",
    "ReconcileParams",
    "RetryTargetParams",
    "ShowParams",
    "approve",
    "burn",
    "compute_readiness_method",
    "create",
    "observe",
    "publish",
    "reconcile",
    "retry_target",
    "show",
]
