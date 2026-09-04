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

Three more verbs carry external effect and are keyed differently:
``release.publish``, ``release.retry_target`` and ``release.reconcile``
each require an ``expected_revision`` (compare-and-swap against the
record the caller holds) and an ``idempotency_key`` (replay identity in
the durable ledger). The first three verbs stay pure -- a readiness
sweep can be run against a proposed record before anything is
persisted; the last three append to
``<state_dir>/store/release.jsonl`` through
:mod:`eawf.workflow.release.ledger`, because a verb that touches an
external registry has to remember what it already did.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.spec.publication import PublicationOperation
from eawf.kernel.spec.release import (
    Release,
    ReleaseCheckpoint,
    validate_release_against_train,
)
from eawf.kernel.spec.release_config import (
    ReleaseConfig,
    ReleaseConfigError,
    load_release_config,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.workflow.release.ledger import (
    IdempotencyConflictError,
    StaleReleaseRevisionError,
    assert_fresh_revision,
    current_operation,
    record_operation,
    replayed_receipt,
    request_fingerprint,
)
from eawf.workflow.release.lifecycle import ReleaseTransitionError
from eawf.workflow.release.preflight import approve_release, record_preflight_result
from eawf.workflow.release.publication import (
    begin_publication,
    operation_reference,
    reconcile_target,
    retry_publication,
)
from eawf.workflow.release.target_machine import TargetTransitionError
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.verify.release_readiness import (
    DEFAULT_SIGNAL_TTL_SECONDS,
    ReleaseReadiness,
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
        release: Optional serialized candidate record. When supplied,
            the handler also reports the status the sweep result moves
            it into.
    """

    model_config = ConfigDict(extra="forbid")
    version: str
    observed_revision: str | None = None
    ttl_seconds: int = DEFAULT_SIGNAL_TTL_SECONDS
    waiver_count: int = 0
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


def _resolve_config(version: str) -> ReleaseConfig:
    """Return the loaded configuration for checkpoint *version*.

    Args:
        version: Normalized checkpoint version.

    Returns:
        The validated :class:`~eawf.kernel.spec.release_config.ReleaseConfig`.

    Raises:
        DaemonValidationError: When no configuration is authored for the
            version, or the authored one is rejected by the loader.
    """
    try:
        source = checkpoint_config_yaml(version)
    except KeyError as exc:
        raise DaemonValidationError(
            f"validation_failed: no release configuration for {version!r}"
        ) from exc
    try:
        return load_release_config(source, train=V07_TRAIN)
    except ReleaseConfigError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc


@register("release.show")
async def show(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Describe the train ladder and one checkpoint rung.

    Args:
        ctx: Server context; unused, the ladder is source-resident data.
        params: JSON-RPC params per :class:`ShowParams`.

    Returns:
        The train id, target version, the ordered ladder, and the
        requested rung.

    Raises:
        DaemonValidationError: When the train declares no such rung.
    """
    args = ShowParams.model_validate(params)
    rung = V07_TRAIN.current_checkpoint if args.version is None else _rung_for(args.version)
    return {
        "train_id": V07_TRAIN.train_id,
        "target_version": V07_TRAIN.target_version,
        "current_checkpoint_index": V07_TRAIN.current_checkpoint_index,
        "checkpoints": [checkpoint.model_dump(mode="json") for checkpoint in V07_TRAIN.checkpoints],
        "checkpoint": rung.model_dump(mode="json"),
    }


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
    config = _resolve_config(args.version)
    try:
        readiness = compute_readiness(
            config,
            observed_revision=args.observed_revision,
            computed_at=datetime.now(UTC),
            ttl_seconds=args.ttl_seconds,
            waiver_count=args.waiver_count,
        )
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    result: dict[str, Any] = {
        "readiness": readiness.model_dump(mode="json"),
        "first_red": None if readiness.first_red is None else readiness.first_red.value,
    }
    if args.release is not None:
        candidate = _validated_release(args.release)
        try:
            result["next_status"] = record_preflight_result(candidate, readiness).status.value
        except (ReleaseTransitionError, ValueError) as exc:
            raise DaemonValidationError(f"validation_failed: {exc}") from exc
    return result


@register("release.approve")
async def approve(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Approve a candidate against a readiness sweep.

    Args:
        ctx: Server context; unused, approval is a pure transition.
        params: JSON-RPC params per :class:`ApproveParams`.

    Returns:
        The serialized approved record.

    Raises:
        DaemonValidationError: When the record or sweep is invalid, or
            the transition is denied -- the message leads with the named
            denial code (``release_not_ready`` when a required signal is
            not passing).
    """
    args = ApproveParams.model_validate(params)
    candidate = _validated_release(args.release)
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
    logger.info(f"approve key={approved.key!r} revision={approved.revision}")
    return {"release": approved.model_dump(mode="json")}


def _validated_release(payload: dict[str, Any]) -> Release:
    """Return the :class:`Release` in *payload*, placed on the train.

    Args:
        payload: Serialized release record.

    Returns:
        The validated record, whose epoch and membership agree with the
        rung the train declares for it.

    Raises:
        DaemonValidationError: When the payload fails the record schema,
            names a checkpoint the train does not declare, or
            contradicts the rung's declaration.
    """
    try:
        release = Release.model_validate(payload)
    except ValidationError as exc:
        raise DaemonValidationError(
            f"validation_failed: release payload invalid: {exc.error_count()} error(s)"
        ) from exc
    try:
        validate_release_against_train(release, V07_TRAIN)
    except (KeyError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    return release


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
    """

    approved_manifest_digest: str
    proof_digest: str
    observed_revision: str | None = None
    ttl_seconds: int = DEFAULT_SIGNAL_TTL_SECONDS
    waiver_count: int = 0


class RetryTargetParams(_PublicationParams):
    """Params for :func:`retry_target`.

    Attributes:
        target_id: The single leg to re-queue.
        proof_digest: Artifact-set digest; must equal the operation's,
            or the retry is a different publication.
    """

    target_id: str
    proof_digest: str


class ReconcileParams(_PublicationParams):
    """Params for :func:`reconcile`.

    Attributes:
        target_id: The leg that was independently read back.
        observation_receipt_ref: The read-back receipt.
        observation_matched: Whether the read-back matched the frozen
            digests.
        effect_receipt_ref: Effect receipt the read-back recovered for a
            leg that timed out without one.
    """

    target_id: str
    observation_receipt_ref: str
    observation_matched: bool = True
    effect_receipt_ref: str | None = None


def _require_state_path(ctx: MethodContext) -> Path:
    """Return the daemon's state path, or refuse the verb.

    Args:
        ctx: Server context.

    Returns:
        The bound ``state.json`` path.

    Raises:
        DaemonValidationError: When the daemon runs without on-disk
            state. An external-effect verb with nowhere to record what
            it did is worse than one that refuses.
    """
    if ctx.state_path is None:
        raise DaemonValidationError(
            "validation_failed: publication verbs require an on-disk state root"
        )
    return Path(ctx.state_path)


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


def _assert_revision(release: Release, expected_revision: int) -> None:
    """Refuse the verb when the caller holds a stale record.

    Args:
        release: The record being mutated.
        expected_revision: The revision the caller believes it holds.

    Raises:
        DaemonValidationError: With a ``stale_release_revision`` message.
    """
    try:
        assert_fresh_revision(release, expected_revision)
    except StaleReleaseRevisionError as exc:
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
    state_path = _require_state_path(ctx)
    release = _validated_release(args.release)
    fingerprint = _fingerprint("release.publish", params)
    replayed = _replay(state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint)
    if replayed is not None:
        return _receipt(replayed, release, replayed=True)
    _assert_revision(release, args.expected_revision)
    config = _resolve_config(release.version)
    now = datetime.now(UTC)
    try:
        readiness = compute_readiness(
            config,
            observed_revision=args.observed_revision,
            computed_at=now,
            ttl_seconds=args.ttl_seconds,
            waiver_count=args.waiver_count,
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
    state_path = _require_state_path(ctx)
    release = _validated_release(args.release)
    fingerprint = _fingerprint("release.retry_target", params)
    replayed = _replay(state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint)
    if replayed is not None:
        return _receipt(replayed, release, replayed=True)
    _assert_revision(release, args.expected_revision)
    config = _resolve_config(release.version)
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


@register("release.reconcile")
async def reconcile(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Settle one leg against an independent read-back.

    Args:
        ctx: Server context; supplies the state root the ledger lives in.
        params: JSON-RPC params per :class:`ReconcileParams`.

    Returns:
        The operation reference, the operation with that leg observed,
        the re-projected record and whether this was a replay.

    Raises:
        DaemonValidationError: On a stale revision, an idempotency
            conflict, an unconfigured or unattempted target, or a leg
            that cannot be observed from where it stands.
    """
    args = ReconcileParams.model_validate(params)
    state_path = _require_state_path(ctx)
    release = _validated_release(args.release)
    fingerprint = _fingerprint("release.reconcile", params)
    replayed = _replay(state_path, idempotency_key=args.idempotency_key, fingerprint=fingerprint)
    if replayed is not None:
        return _receipt(replayed, release, replayed=True)
    _assert_revision(release, args.expected_revision)
    config = _resolve_config(release.version)
    operation = _open_operation(state_path, release)
    now = datetime.now(UTC)
    try:
        reconciled, settled = reconcile_target(
            release,
            config,
            operation,
            target_id=args.target_id,
            observation_receipt_ref=args.observation_receipt_ref,
            observation_matched=args.observation_matched,
            effect_receipt_ref=args.effect_receipt_ref,
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


__all__ = [
    "ApproveParams",
    "ComputeReadinessParams",
    "PublishParams",
    "ReconcileParams",
    "RetryTargetParams",
    "ShowParams",
    "approve",
    "compute_readiness_method",
    "publish",
    "reconcile",
    "retry_target",
    "show",
]
