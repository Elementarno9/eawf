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

None of the three writes durable state: the Release ledger and its
per-target attempt rows land with the publication wave, and these
handlers deliberately stay pure so the readiness sweep can be run
against a proposed record before anything is persisted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

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
from eawf.workflow.release.lifecycle import ReleaseTransitionError
from eawf.workflow.release.preflight import approve_release, record_preflight_result
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


__all__ = [
    "ApproveParams",
    "ComputeReadinessParams",
    "ShowParams",
    "approve",
    "compute_readiness_method",
    "show",
]
