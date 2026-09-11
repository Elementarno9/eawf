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
external registry has to remember what it already did.

``release.create`` and ``release.approve`` persist too, into the
separate record collection of :mod:`eawf.workflow.release.records`. They
touch no registry, but they are the two verbs that open and authorise a
checkpoint, and a record only a single RPC reply ever carried could not
be read back by anything -- so neither runs without a state root.

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

Two ladder verbs sit beside them. ``release.create`` opens a checkpoint's
DRAFT record only once every measured contract the checkpoint asserts
over is promoted (:mod:`eawf.workflow.release.admission`), and
``release.advance_train`` walks the ladder forward only from a finished
checkpoint whose gate receipts still bind its exact source
(:mod:`eawf.workflow.release.advance`).
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
from eawf.kernel.spec.publication import PublicationOperation
from eawf.kernel.spec.release import (
    Release,
    ReleaseCheckpoint,
    ReleaseTargetStatus,
    semver_equivalent,
)
from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.release_context import (
    assert_revision,
    require_state_path,
    resolve_config,
    validated_release,
)
from eawf.runtime.daemon.methods.release_disposition import burn_adopted_record
from eawf.surfaces.cli.errors import CliError, UserError
from eawf.workflow.evidence._io import load_state
from eawf.workflow.release.adapters import collect_observation
from eawf.workflow.release.admission import (
    create_checkpoint_release,
    required_contract_ids,
)
from eawf.workflow.release.advance import (
    CheckpointGateReceipt,
    TrainAdvanceError,
    advance_train,
    render_train_ladder,
)
from eawf.workflow.release.ledger import (
    IdempotencyConflictError,
    current_operation,
    record_operation,
    replayed_receipt,
    request_fingerprint,
)
from eawf.workflow.release.lifecycle import ReleaseTransitionError
from eawf.workflow.release.observation import (
    FrozenManifest,
    RecordedResponse,
    assert_manifest_binds,
    observation_request,
)
from eawf.workflow.release.preflight import approve_release, record_preflight_result
from eawf.workflow.release.publication import (
    begin_publication,
    burn_release,
    operation_reference,
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
    record_envelope_id,
    record_release,
)
from eawf.workflow.release.settlement import observe_target
from eawf.workflow.release.target_machine import TargetTransitionError
from eawf.workflow.release.train import V07_TRAIN
from eawf.workflow.verify.release_readiness import (
    DEFAULT_SIGNAL_TTL_SECONDS,
    ReleaseReadiness,
    WaiverAcknowledgement,
    compute_readiness,
)

logger = logging.getLogger(__name__)


class ShowParams(BaseModel):
    """Params for :func:`show`.

    Attributes:
        version: Normalized checkpoint version to describe. ``None``
            describes the rung the train currently has open.
    """

    model_config = ConfigDict(extra="forbid")
    version: str | None = None


class ComputeReadinessParams(BaseModel):
    """Params for :func:`compute_readiness_method`.

    Attributes:
        version: Normalized checkpoint version to preflight.
        observed_revision: Source revision the sweep is computed against.
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

    The ladder is source-resident, but "which rung exists" and "has that
    rung been cut" are different questions, and an operator asking the
    second one should not have to read a store file. So the reply also
    carries the recorded record for the rung, or ``None`` when the
    checkpoint has never been opened.

    Args:
        ctx: Server context; its state root supplies the recorded
            record. A daemon without one answers ``record: None`` rather
            than refusing: describing the ladder is useful even where
            nothing is recorded.
        params: JSON-RPC params per :class:`ShowParams`.

    Returns:
        The train id, target version, the ordered ladder, the requested
        rung, and the record standing at it.

    Raises:
        DaemonValidationError: When the train declares no such rung.
    """
    args = ShowParams.model_validate(params)
    rung = V07_TRAIN.current_checkpoint if args.version is None else _rung_for(args.version)
    record = _recorded_release(ctx, rung.release_key)
    return {
        "train_id": V07_TRAIN.train_id,
        "target_version": V07_TRAIN.target_version,
        "current_checkpoint_index": V07_TRAIN.current_checkpoint_index,
        "checkpoints": [checkpoint.model_dump(mode="json") for checkpoint in V07_TRAIN.checkpoints],
        "checkpoint": rung.model_dump(mode="json"),
        "record": None if record is None else record.model_dump(mode="json"),
    }


def _recorded_release(ctx: MethodContext, release_key: str) -> Release | None:
    """Return the record filed under *release_key*, or ``None``.

    Args:
        ctx: Server context; ``state_path`` may be unset.
        release_key: ``REL-<version>`` key to look up.

    Returns:
        The current record, or ``None`` when the daemon has no state
        root or the collection carries no row for the key.

    Raises:
        DaemonValidationError: When the collection exists but is
            corrupt. A checkpoint reported as never opened because its
            row could not be parsed is the one wrong answer here.
    """
    if ctx.state_path is None:
        return None
    try:
        return read_release_record(Path(ctx.state_path), release_key)
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


@register("release.compute_readiness")
async def compute_readiness_method(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Compute the twelve-signal preflight sweep for one checkpoint.

    Args:
        ctx: Server context; unused, the sweep is a pure projection.
        params: JSON-RPC params per :class:`ComputeReadinessParams`.

    Returns:
        The serialized :class:`ReleaseReadiness`, plus ``next_status``
        when a candidate record was supplied.

    Raises:
        DaemonValidationError: When the checkpoint has no configuration,
            the supplied record is invalid or disagrees with the train,
            or the record is not in a state preflight applies to.
    """
    args = ComputeReadinessParams.model_validate(params)
    config = resolve_config(args.version)
    try:
        readiness = compute_readiness(
            config,
            observed_revision=args.observed_revision,
            computed_at=datetime.now(UTC),
            ttl_seconds=args.ttl_seconds,
            waiver_count=args.waiver_count,
            waivers=args.waivers,
            acknowledgements=args.acknowledgements,
        )
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    result: dict[str, Any] = {
        "readiness": readiness.model_dump(mode="json"),
        "first_red": None if readiness.first_red is None else readiness.first_red.value,
    }
    if args.release is not None:
        candidate = validated_release(args.release)
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
            passing).
    """
    args = ApproveParams.model_validate(params)
    state_path = require_state_path(ctx)
    candidate = validated_release(args.release)
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
        observed_revision: Source revision the chokepoint sweep runs at.
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
        response: A recorded registry answer already in hand. ``None``
            asks the leg's reader, which reports ``registry_unreachable``
            until a live client ships.
        effect_receipt_ref: The adapter's receipt, for observing a leg
            that timed out without one.
    """

    target_id: str
    manifest: dict[str, Any]
    response: dict[str, Any] | None = None
    effect_receipt_ref: str | None = None


def _receipt(
    operation: PublicationOperation,
    release: Release,
    *,
    replayed: bool,
) -> dict[str, Any]:
    """Return the wire shape every publication verb answers with.

    Args:
        operation: The operation snapshot the call produced.
        release: The record the call produced.
        replayed: Whether this is the original receipt of an earlier
            identical call rather than fresh work.

    Returns:
        The operation reference, both records and the replay flag.
    """
    return {
        "operation_ref": operation_reference(operation),
        "operation": operation.model_dump(mode="json"),
        "release": release.model_dump(mode="json"),
        "replayed": replayed,
    }


def _fingerprint(method: str, params: dict[str, Any]) -> str:
    """Return the request digest of *params*, minus the replay key.

    Args:
        method: JSON-RPC method name.
        params: Raw request params.

    Returns:
        The digest the idempotency index compares against.
    """
    return request_fingerprint(
        method, {key: value for key, value in params.items() if key != "idempotency_key"}
    )


def _replay(
    state_path: Path,
    *,
    idempotency_key: str,
    fingerprint: str,
) -> PublicationOperation | None:
    """Return the receipt an earlier identical call produced, if any.

    Args:
        state_path: Path to ``state.json``.
        idempotency_key: The key the caller presented.
        fingerprint: Digest of the request the caller presented.

    Returns:
        The recorded snapshot, or ``None`` when the key is new.

    Raises:
        DaemonValidationError: When the key was reused with a different
            payload (``idempotency_conflict``), or the ledger is corrupt.
    """
    try:
        return replayed_receipt(
            state_path, idempotency_key=idempotency_key, fingerprint=fingerprint
        )
    except IdempotencyConflictError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc


def _open_operation(state_path: Path, release: Release) -> PublicationOperation:
    """Return the operation already open for *release*, or refuse.

    Args:
        state_path: Path to ``state.json``.
        release: The record whose episode is looked up.

    Returns:
        The highest-revision snapshot recorded for that release.

    Raises:
        DaemonValidationError: When no episode has been opened, or the
            ledger is corrupt.
    """
    try:
        operation = current_operation(state_path, release.key)
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    if operation is None:
        raise DaemonValidationError(
            f"validation_failed: no publication operation is open for {release.key!r}"
        )
    return operation


@register("release.publish")
async def publish(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Open the publication episode and return its reference at once.

    The chokepoint sweep is recomputed here rather than trusted from the
    approval: the whole point of the tag chokepoint is that the last
    thing to run before external effect is a fresh preflight. Only once
    it recomputes green does the operation open, and the handler returns
    the operation reference immediately -- the legs are queued, not
    awaited, so a slow registry cannot hold the RPC open.

    Args:
        ctx: Server context; supplies the state root the ledger lives in.
        params: JSON-RPC params per :class:`PublishParams`.

    Returns:
        The operation reference, the operation, the record at PUBLISHING
        and whether this was a replay.

    Raises:
        DaemonValidationError: On a stale revision, an idempotency
            conflict, an invalid record, or a denied transition (the
            message leads with ``approval_stale`` when the approval no
            longer binds).
    """
    args = PublishParams.model_validate(params)
    state_path = require_state_path(ctx)
    release = validated_release(args.release)
    fingerprint = _fingerprint("release.publish", params)
    replayed = _replay(state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint)
    if replayed is not None:
        return _receipt(replayed, release, replayed=True)
    assert_revision(release, args.expected_revision)
    config = resolve_config(release.version)
    now = datetime.now(UTC)
    try:
        readiness = compute_readiness(
            config,
            observed_revision=args.observed_revision,
            computed_at=now,
            ttl_seconds=args.ttl_seconds,
            waiver_count=args.waiver_count,
            waivers=args.waivers,
            acknowledgements=args.acknowledgements,
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
    logger.info(f"publish key={release.key!r} operation_id={recorded.operation_id}")
    return _receipt(recorded, published, replayed=False)


@register("release.retry_target")
async def retry_target(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Re-queue one leg of the open episode under the idempotency proof.

    Args:
        ctx: Server context; supplies the state root the ledger lives in.
        params: JSON-RPC params per :class:`RetryTargetParams`.

    Returns:
        The operation reference, the operation, the record back at
        PUBLISHING and whether this was a replay.

    Raises:
        DaemonValidationError: On a stale revision, an idempotency
            conflict, a leg with no budget left, or a proof digest that
            does not match the episode (``unsafe_release_retry``).
    """
    args = RetryTargetParams.model_validate(params)
    state_path = require_state_path(ctx)
    release = validated_release(args.release)
    fingerprint = _fingerprint("release.retry_target", params)
    replayed = _replay(state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint)
    if replayed is not None:
        return _receipt(replayed, release, replayed=True)
    assert_revision(release, args.expected_revision)
    config = resolve_config(release.version)
    operation = _open_operation(state_path, release)
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
    return _receipt(recorded, republished, replayed=False)


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

    The burned record is persisted here, unlike at the other publication
    verbs: the burn is where the checkpoint stops moving, so a reader
    asking ``eawf release show`` after it must be answered
    ``partially_released`` and not the status the record left behind.

    What the verb deliberately does NOT write is any claim about the
    publication's outcome. :func:`~eawf.workflow.release.publication.burn_release`
    accepts no field updates, so the pinned source, tree, manifest and
    digest are frozen exactly as recovery found them, and the reason is
    an annotation beside them rather than a revision of them.

    A replay answers with the *recorded* record rather than the payload
    the caller presented, because after a burn the recorded one is the
    burned one and echoing the pre-burn status back would report the
    checkpoint as still moving. It falls back to the presented payload
    only where the collection holds nothing for the key, which is the
    torn-write case of a ledger row landing without its record row.

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
    fingerprint = _fingerprint("release.burn", params)
    replayed = _replay(state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint)
    if replayed is not None:
        settled = read_release_record(state_path, release.key) or release
        return {
            **_receipt(replayed, settled, replayed=True),
            "reason": args.reason,
            "release_record_id": record_envelope_id(settled),
        }
    assert_revision(release, args.expected_revision)
    config = resolve_config(release.version)
    operation = _open_operation(state_path, release)
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
        **_receipt(recorded, burned, replayed=False),
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

    Args:
        ctx: Server context; supplies the state root the ledger lives in.
        params: JSON-RPC params per :class:`ReconcileParams`.

    Returns:
        The operation reference, the operation with that leg settled,
        the re-projected record and whether this was a replay.

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
    fingerprint = _fingerprint("release.reconcile", params)
    replayed = _replay(state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint)
    if replayed is not None:
        return _receipt(replayed, release, replayed=True)
    assert_revision(release, args.expected_revision)
    config = resolve_config(release.version)
    operation = _open_operation(state_path, release)
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
        )
    except TargetTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except (KeyError, ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    recorded = record_operation(
        state_path,
        settled,
        idempotency_key=args.idempotency_key,
        fingerprint=fingerprint,
        recorded_at=now,
        summary=f"reconcile {release.key} target {args.target_id}",
    )
    return _receipt(recorded, reconciled, replayed=False)


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
    bakes it. An inconclusive read-back writes nothing and answers
    ``observation_inconclusive``.

    Args:
        ctx: Server context; supplies the state root the ledger lives in.
        params: JSON-RPC params per :class:`ObserveTargetParams`.

    Returns:
        The operation reference, the operation with that leg observed,
        the routed record, the replay flag, and the observation itself
        (``None`` on a replay, which returns the original receipt rather
        than re-judging the registry).

    Raises:
        DaemonValidationError: On a stale revision, an idempotency
            conflict, a manifest that does not bind, an unconfigured or
            unattempted target, an inconclusive read-back, or a denied
            release or target transition.
    """
    args = ObserveTargetParams.model_validate(params)
    state_path = require_state_path(ctx)
    release = validated_release(args.release)
    fingerprint = _fingerprint("release.observe_target", params)
    replayed = _replay(state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint)
    if replayed is not None:
        return {**_receipt(replayed, release, replayed=True), "observation": None}
    assert_revision(release, args.expected_revision)
    config = resolve_config(release.version)
    operation = _open_operation(state_path, release)
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
    except ReleaseTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except TargetTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except (KeyError, ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    recorded = record_operation(
        state_path,
        settled,
        idempotency_key=args.idempotency_key,
        fingerprint=fingerprint,
        recorded_at=now,
        summary=f"observe {release.key} target {args.target_id}: {observation.code.value}",
    )
    logger.info(
        f"observe key={release.key!r} target={args.target_id!r} "
        f"code={observation.code.value!r} status={observed.status.value!r}"
    )
    return {
        **_receipt(recorded, observed, replayed=False),
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


class AdvanceTrainParams(BaseModel):
    """Params for :func:`advance`.

    Attributes:
        release: Serialized record standing at the open checkpoint.
        receipts: The open checkpoint's gate receipts, each binding its
            source and manifest.
        membership_refs: Milestone acceptance bundles for the rung being
            opened.
    """

    model_config = ConfigDict(extra="forbid")
    release: dict[str, Any]
    receipts: list[dict[str, Any]] = Field(default_factory=list)
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


@register("release.create")
async def create(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Open the DRAFT record of one checkpoint, after measured admission.

    A checkpoint the admission table covers may not be created until
    every measured contract backing it is promoted and resolvable. The
    refusal names the single contract that is missing plus the command
    that promotes it, so the operator's next action is in the error.

    The admitted record is persisted before the reply is built. A record
    that existed only in one RPC response could not be found again, so
    every later verb would have to be handed the record it is acting on
    and no reader could tell an opened checkpoint from an imagined one.

    Args:
        ctx: Server context; supplies the state the citations resolve
            against and the root the record is recorded under.
        params: JSON-RPC params per :class:`CreateParams`.

    Returns:
        The serialized DRAFT record, the id of the collection row
        carrying it, plus the contract ids that admitted it.

    Raises:
        DaemonValidationError: With ``measured_contract_missing`` when a
            required contract is not promoted, or when the train
            declares no such rung.
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
        )
    except UserError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.kind}: {exc}") from exc
    except (KeyError, ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
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
    }


@register("release.advance_train")
async def advance(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Walk the train onto its next rung, or refuse and change nothing.

    The index moves only from a ``baked`` or ``released`` checkpoint
    whose every required gate receipt still binds its exact source and
    manifest. The closing record is returned unchanged beside the new
    DRAFT record, so the caller can assert the prior rung was not
    rewritten.

    Args:
        ctx: Server context; unused, the advance is a pure projection
            over the records the caller supplies.
        params: JSON-RPC params per :class:`AdvanceTrainParams`.

    Returns:
        The rendered ladder at the new index, the opened DRAFT record,
        the unchanged closing record and the validated receipt refs.

    Raises:
        DaemonValidationError: With the named denial
            (``checkpoint_not_terminal``, ``prerequisite_receipt_stale``,
            ...) when the advance is refused.
    """
    args = AdvanceTrainParams.model_validate(params)
    current = validated_release(args.release)
    config = resolve_config(current.version)
    try:
        receipts = [CheckpointGateReceipt.model_validate(row) for row in args.receipts]
        result = advance_train(
            V07_TRAIN,
            current=current,
            config=config,
            receipts=receipts,
            now=datetime.now(UTC),
            next_uid=uuid4(),
            membership_refs=tuple(args.membership_refs),
        )
    except TrainAdvanceError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except (ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    logger.info(
        f"advance closed={result.closed.key!r} opened={result.opened.key!r} "
        f"index={result.train.current_checkpoint_index}"
    )
    return {
        "train": render_train_ladder(result.train),
        "closed": result.closed.model_dump(mode="json"),
        "opened": result.opened.model_dump(mode="json"),
        "receipt_refs": list(result.receipt_refs),
    }


__all__ = [
    "AdvanceTrainParams",
    "ApproveParams",
    "BurnParams",
    "ComputeReadinessParams",
    "CreateParams",
    "ObserveTargetParams",
    "PublishParams",
    "ReconcileParams",
    "RetryTargetParams",
    "ShowParams",
    "advance",
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
