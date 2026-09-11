"""``release.adopt`` and ``release.cancel``: disposing of a publication
this machinery did not run.

Every verb in :mod:`eawf.runtime.daemon.methods.release` assumes the
machinery was there -- a manifest was pinned, a sweep ran, an operator
approved, an operation dispatched. A version published without a record
has none of those, and the two verbs here are what let its record say so
and then stop.

``release.adopt`` writes the observed per-target facts onto the draft
record and nothing else. ``release.cancel`` is the refusal those facts
then earn: a cancellation asserts nothing was published, so the guard
behind it is computed from the record by
:func:`~eawf.workflow.release.lifecycle.external_effect_started` rather
than taken from the caller, and a record that can see its own
publication is denied ``release_effect_already_started``. Before this
verb existed, ``cancelled`` had no operator surface at all.

Neither takes an ``idempotency_key``: they touch no registry, so there
is no ledger to replay against and a repeat is refused by the
compare-and-swap on ``expected_revision`` or by the status machine.

:func:`burn_adopted_record` is the third piece and belongs here for the
same reason: it is the burn of a record with no publication episode, so
it writes no ledger row. ``release.burn`` calls it for an adopted
record and keeps the recovery path for everything else -- one terminal
burn, reached two ways.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from eawf.kernel.spec.release import Release, ReleaseAdoption, ReleaseStatus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.release_context import (
    assert_revision,
    require_state_path,
    resolve_config,
    validated_release,
)
from eawf.workflow.release.adoption import adopt_publication, unconfigured_targets
from eawf.workflow.release.lifecycle import ReleaseTransitionError, cancel_release
from eawf.workflow.release.publication import burn_release
from eawf.workflow.release.records import (
    read_release_record,
    record_envelope_id,
    record_release,
)

logger = logging.getLogger(__name__)


class _RecordParams(BaseModel):
    """Shared params of the two record verbs that touch no registry.

    They carry no ``idempotency_key`` because they have no ledger to
    replay against: a repeat is refused by the compare-and-swap on
    ``expected_revision``, or by the status machine once the record has
    already moved.

    Attributes:
        release: Serialized record the verb mutates.
        expected_revision: The record revision the caller holds.
    """

    model_config = ConfigDict(extra="forbid")
    release: dict[str, Any]
    expected_revision: Annotated[int, Field(ge=0)]


class AdoptParams(_RecordParams):
    """Params for :func:`adopt`.

    Attributes:
        adoption: Serialized
            :class:`~eawf.kernel.spec.release.ReleaseAdoption`: the
            per-target read-backs, the incident they belong to and the
            reason the version is being written up this way.
    """

    adoption: dict[str, Any]


class CancelParams(_RecordParams):
    """Params for :func:`cancel`.

    Attributes:
        reason: Why the checkpoint is being abandoned. Required and
            non-blank for the same reason the burn's is: ``cancelled``
            is terminal, so no later transition can explain it.
    """

    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


def burn_adopted_record(
    state_path: Path,
    release: Release,
    *,
    reason: str,
    expected_revision: int,
) -> dict[str, Any]:
    """Burn an adopted record, which has no episode to abandon.

    The reply carries ``operation_ref: None`` rather than inventing a
    locator, because an adopted publication ran outside this machinery
    and no operation was ever opened for it. That absence is the point:
    a reader of the burned record can tell it was adopted from the
    missing operation and the present adoption alone.

    A repeat is answered from the record collection rather than from an
    idempotency index, for the same reason -- nothing was written to the
    ledger to key a replay against.

    Args:
        state_path: Path to ``state.json``.
        release: The adopted record being burned.
        reason: Why the version is spent, in the operator's own words.
        expected_revision: The revision the caller believes it holds.

    Returns:
        The burned record, the replay flag, the reason as recorded and
        the id of the record row carrying the burn.

    Raises:
        DaemonValidationError: On a stale revision, or when the record's
            status has no burn edge.
    """
    settled = read_release_record(state_path, release.key)
    if settled is not None and settled.status is ReleaseStatus.PARTIALLY_RELEASED:
        return {
            "operation_ref": None,
            "operation": None,
            "release": settled.model_dump(mode="json"),
            "replayed": True,
            "reason": reason,
            "release_record_id": record_envelope_id(settled),
        }
    assert_revision(release, expected_revision)
    config = resolve_config(release.version)
    try:
        burned, _abandoned = burn_release(release, config, None)
    except ReleaseTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except (ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    record_release(
        state_path,
        burned,
        recorded_at=datetime.now(UTC),
        summary=f"burn {burned.key} (adopted): {reason}",
    )
    logger.info(
        f"burn key={burned.key!r} operation_id=none status={burned.status.value!r} "
        f"reason={reason!r}"
    )
    return {
        "operation_ref": None,
        "operation": None,
        "release": burned.model_dump(mode="json"),
        "replayed": False,
        "reason": reason,
        "release_record_id": record_envelope_id(burned),
    }


@register("release.adopt")
async def adopt(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Adopt a publication that ran without a release record.

    The verb writes observed facts and nothing else. It asserts no
    approval, runs no readiness sweep and pins no manifest, because none
    of the three happened -- and the record model refuses an adoption
    beside an ``approval_ref``, so the adopted checkpoint cannot later be
    dressed up as one that earned its approval.

    Every configured target must carry a read-back or the call is
    refused: a record that adopts half a publication is still blind to
    the other half, which is the condition adoption exists to end. A
    target the checkpoint configuration never declared is recorded
    rather than refused, and named in the reply, because an uncontrolled
    publication can reach somewhere the configuration does not know
    about.

    The adopted record stays at DRAFT. Disposing of it is a separate
    decision taken by a separate verb: ``release.burn`` ends it,
    ``release.cancel`` is refused on it.

    Args:
        ctx: Server context; supplies the state root the record is
            recorded under.
        params: JSON-RPC params per :class:`AdoptParams`.

    Returns:
        The adopted record, the id of the collection row carrying it,
        the observed per-target states and any adopted target the
        configuration does not declare.

    Raises:
        DaemonValidationError: On a stale revision, a record that is not
            a draft or already carries an adoption, an observation that
            is not an independent read-back, or a configured target with
            no observation at all.
    """
    args = AdoptParams.model_validate(params)
    state_path = require_state_path(ctx)
    release = validated_release(args.release)
    assert_revision(release, args.expected_revision)
    config = resolve_config(release.version)
    try:
        adoption = ReleaseAdoption.model_validate(args.adoption)
        adopted = adopt_publication(release, config, adoption=adoption)
    except (ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    record_release(
        state_path,
        adopted,
        recorded_at=datetime.now(UTC),
        summary=f"adopt {adopted.key}: {adoption.reason}",
    )
    logger.info(
        f"adopt key={adopted.key!r} incident={adoption.incident_ref!r} "
        f"targets={sorted(adoption.observed_target_statuses)}"
    )
    return {
        "release": adopted.model_dump(mode="json"),
        "release_record_id": record_envelope_id(adopted),
        "observed_targets": {
            target: status.value
            for target, status in sorted(adoption.observed_target_statuses.items())
        },
        "unconfigured_targets": list(unconfigured_targets(config, adoption)),
    }


@register("release.cancel")
async def cancel(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Abandon a checkpoint that never touched a registry, or refuse.

    A cancellation asserts that nothing was published under this
    version, so the assertion is checked against the record rather than
    trusted from the caller:
    :func:`~eawf.workflow.release.lifecycle.cancel_release` derives the
    ``no_external_effect`` guard from the record's own adoption,
    publication-operation reference and per-target states. That is the
    whole difference between this verb and the gap it closes -- the
    guard used to be answered by a caller-supplied default that no
    caller ever computed, so a version published to four registries
    would have satisfied it.

    Args:
        ctx: Server context; supplies the state root the record is
            recorded under.
        params: JSON-RPC params per :class:`CancelParams`.

    Returns:
        The cancelled record, the id of the collection row carrying it
        and the reason as recorded.

    Raises:
        DaemonValidationError: With ``release_effect_already_started``
            when the record can see external effect, with
            ``illegal_release_transition`` when its status has no cancel
            edge, or on a stale revision.
    """
    args = CancelParams.model_validate(params)
    state_path = require_state_path(ctx)
    release = validated_release(args.release)
    assert_revision(release, args.expected_revision)
    try:
        cancelled = cancel_release(release)
    except ReleaseTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except (ValidationError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    record_release(
        state_path,
        cancelled,
        recorded_at=datetime.now(UTC),
        summary=f"cancel {cancelled.key}: {args.reason}",
    )
    logger.info(f"cancel key={cancelled.key!r} reason={args.reason!r}")
    return {
        "release": cancelled.model_dump(mode="json"),
        "release_record_id": record_envelope_id(cancelled),
        "reason": args.reason,
    }


__all__ = [
    "AdoptParams",
    "CancelParams",
    "adopt",
    "burn_adopted_record",
    "cancel",
]
