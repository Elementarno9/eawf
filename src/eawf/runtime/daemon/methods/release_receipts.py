"""``release.produce_receipts`` and ``release.advance_train``: walking the train.

A checkpoint that reached ``baked`` or ``released`` is finished, but the
train does not move until the checkpoint's gates are proven on its
pinned source. These two verbs take it the rest of the way, and both
read the record from the store rather than from the caller:

* ``release.produce_receipts`` settles every required gate of the
  checkpoint's profile binding table and stores one
  :class:`~eawf.workflow.release.advance.CheckpointGateReceipt` per gate
  that passed (:mod:`eawf.workflow.release.checkpoint_receipts`). The
  signal gates read a fresh sweep of the pinned source. The proof
  commands run in a worktree at that source, which can take an hour, so
  the work runs off the event loop.
* ``release.advance_train`` reads the record and its newest stored
  receipts, judges the advance against the train as the stores place it
  (:func:`~eawf.workflow.release.advance.derive_train`), and records the
  move. It opens no record. The next rung's DRAFT comes from
  ``release.create``, behind measured admission.

Like :mod:`eawf.runtime.daemon.methods.release_disposition`, this module
imports only the shared entry checks from its siblings.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.release.gate_binding import GateBinding, GateBindingError
from eawf.kernel.release.waiver import ReleaseWaiver
from eawf.kernel.spec.release import Release, ReleaseCheckpoint, ReleaseKeyStr
from eawf.kernel.spec.release_config import ReleaseConfig, ReleaseGateName
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.release_context import require_state_path, resolve_config
from eawf.runtime.release.chokepoint import sweep_pinned_source
from eawf.workflow.release.advance import (
    TrainAdvanceDenialCode,
    TrainAdvanceError,
    advance_train,
    derive_train,
    render_train_ladder,
)
from eawf.workflow.release.checkpoint_receipts import (
    DEFAULT_RECEIPT_TTL_SECONDS,
    ReceiptProduction,
    pinned_source,
    pinned_worktree,
    produce_checkpoint_receipts,
)
from eawf.workflow.release.records import read_release_record, read_release_records
from eawf.workflow.release.train import V07_TRAIN, gate_bindings_for
from eawf.workflow.release.train_store import (
    read_checkpoint_receipts,
    read_train_advances,
    record_checkpoint_receipt,
    record_train_advance,
)
from eawf.workflow.verify.release_readiness import WaiverAcknowledgement

logger = logging.getLogger(__name__)

#: The code each train denial reaches the wire under. A gate with no
#: stored receipt and a gate whose stored receipt expired or binds other
#: source have one remedy, rerunning ``eawf release receipts``, so both
#: answer ``prerequisite_receipt_stale``. The library's own code follows
#: it in the message and says which of the two it was.
_WIRE_CODES: Final[Mapping[TrainAdvanceDenialCode, TrainAdvanceDenialCode]] = {
    TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_MISSING: (
        TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE
    ),
}


class ProduceReceiptsParams(BaseModel):
    """Params for :func:`produce_receipts`.

    Attributes:
        version: Normalized checkpoint version whose stored record the
            receipts are produced for.
        ttl_seconds: How long each issued receipt, and each row of the
            sweep behind the signal gates, stays fresh.
        waiver_count: Gate waivers recorded against the checkpoint.
        waivers: The counted waiver rows the waiver-block gate reads.
        acknowledgements: The operator acceptances of those waivers.
    """

    model_config = ConfigDict(extra="forbid")
    version: str
    ttl_seconds: Annotated[int, Field(gt=0)] = DEFAULT_RECEIPT_TTL_SECONDS
    waiver_count: Annotated[int, Field(ge=0)] = 0
    waivers: tuple[ReleaseWaiver, ...] = ()
    acknowledgements: tuple[WaiverAcknowledgement, ...] = ()


class AdvanceTrainParams(BaseModel):
    """Params for :func:`advance`.

    Attributes:
        release_key: The checkpoint the train walks past. Its record and
            receipts are read from the stores, never from the caller.
    """

    model_config = ConfigDict(extra="forbid")
    release_key: ReleaseKeyStr


def _rung(version: str) -> ReleaseCheckpoint:
    """Return the ladder rung tagging *version*, or refuse the verb.

    Args:
        version: Normalized checkpoint version.

    Returns:
        The rung the train declares for it.

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


def _stored_record(state_path: Path, release_key: str) -> Release:
    """Return the record the collection holds for *release_key*, or refuse.

    Args:
        state_path: Path to ``state.json``.
        release_key: ``REL-<version>`` key to look up.

    Returns:
        The highest-revision stored record.

    Raises:
        DaemonValidationError: When nothing is stored for the key, or the
            collection is corrupt.
    """
    try:
        record = read_release_record(state_path, release_key)
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    if record is None:
        raise DaemonValidationError(
            f"validation_failed: no release record is stored for {release_key!r}; open it "
            f"with `eawf release create` first"
        )
    return record


def _bindings(rung: ReleaseCheckpoint) -> Mapping[ReleaseGateName, GateBinding]:
    """Return the binding table of *rung*'s profile, or refuse the verb.

    Args:
        rung: The rung whose gate profile is looked up.

    Returns:
        The profile's validated bindings, keyed by gate.

    Raises:
        DaemonValidationError: When no table is authored for the profile,
            or the authored one does not validate.
    """
    try:
        return gate_bindings_for(rung.gate_profile)
    except (KeyError, GateBindingError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc


def _produce(
    state_path: Path,
    release: Release,
    config: ReleaseConfig,
    bindings: Mapping[ReleaseGateName, GateBinding],
    args: ProduceReceiptsParams,
) -> ReceiptProduction:
    """Sweep the pinned source, run its proofs in a worktree and settle every gate.

    Blocking: the proof commands run to completion here, so the handler
    calls this off the event loop.

    Args:
        state_path: Path to ``state.json``; its checkout is swept and
            holds the commit the worktree checks out.
        release: The stored checkpoint record.
        config: Its loaded configuration.
        bindings: Its profile's binding table.
        args: The verb's params.

    Returns:
        The issued receipts and the refused gates.

    Raises:
        ReceiptProductionError: When the record is unpinned or its source
            cannot be checked out.
        ValueError: When the sweep rejects its inputs.
    """
    source_sha, _manifest_digest = pinned_source(release)
    readiness = sweep_pinned_source(
        config,
        version=release.version,
        state_path=state_path,
        pinned=source_sha,
        observed_revision=None,
        waiver_count=args.waiver_count,
        computed_at=datetime.now(UTC),
        ttl_seconds=args.ttl_seconds,
        waivers=args.waivers,
        acknowledgements=args.acknowledgements,
    )
    with pinned_worktree(state_path.parent.parent, source_sha) as run_proof:
        return produce_checkpoint_receipts(
            release,
            required=config.gates.required,
            bindings=bindings,
            readiness=readiness,
            run_proof=run_proof,
            ttl_seconds=args.ttl_seconds,
        )


@register("release.produce_receipts")
async def produce_receipts(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Prove every required gate of one checkpoint and store the receipts.

    Each gate that passes gets one stored receipt bound to the record's
    ``source_sha`` and ``manifest_digest``. A gate that does not pass,
    including a proof command that exits non-zero, gets none, and the
    reply names why. A rerun stores fresh receipts beside the old ones.
    The advance reads the newest of each.

    Args:
        ctx: Server context; supplies the state root the record is read
            from and the receipts are stored under.
        params: JSON-RPC params per :class:`ProduceReceiptsParams`.

    Returns:
        The checkpoint key, its pinned source and manifest digest, the
        stored receipts and the refused gates.

    Raises:
        DaemonValidationError: When the daemon has no state root, the
            train declares no such rung, nothing is stored for it, the
            record is unpinned (``release_not_pinned``), its profile or
            configuration is not authored, or the sweep or the worktree
            fails.
    """
    args = ProduceReceiptsParams.model_validate(params)
    state_path = require_state_path(ctx)
    rung = _rung(args.version)
    config = resolve_config(args.version)
    release = _stored_record(state_path, rung.release_key)
    bindings = _bindings(rung)
    try:
        production = await asyncio.to_thread(_produce, state_path, release, config, bindings, args)
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    recorded_at = datetime.now(UTC)
    for issued in production.receipts:
        record_checkpoint_receipt(
            state_path,
            issued.receipt,
            recorded_at=recorded_at,
            summary=f"receipt {release.key} {issued.receipt.gate.value}: {issued.evidence}",
        )
    logger.info(
        f"produce_receipts key={release.key!r} stored={len(production.receipts)} "
        f"refused={len(production.refusals)}"
    )
    return {
        "release_key": release.key,
        "source_sha": release.source_sha,
        "manifest_digest": release.manifest_digest,
        "receipts": [issued.receipt.model_dump(mode="json") for issued in production.receipts],
        "refused": [
            {
                "gate": refusal.gate.value,
                "evidence_ref": refusal.evidence_ref,
                "detail": refusal.detail,
            }
            for refusal in production.refusals
        ],
    }


@register("release.advance_train")
async def advance(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Walk the train past one checkpoint, or refuse and change nothing.

    The checkpoint must be the rung the stores place the train on, it
    must stand at ``baked`` or ``released``, and every required gate must
    have a fresh stored receipt that binds its exact source and manifest.
    Only then is the advance recorded. A refusal writes nothing.

    Args:
        ctx: Server context; supplies the state root the record, the
            receipts and the advances live under.
        params: JSON-RPC params per :class:`AdvanceTrainParams`.

    Returns:
        The ladder after the advance, the closed record exactly as it is
        stored, the recorded advance and its row id, and the validated
        receipt refs.

    Raises:
        DaemonValidationError: With the named denial when the advance is
            refused: ``checkpoint_key_mismatch``,
            ``checkpoint_not_terminal``, ``ladder_exhausted``, or
            ``prerequisite_receipt_stale`` for a gate whose stored receipt
            is missing, expired or bound to other source. Also when the
            daemon has no state root, nothing is stored for the key, or a
            store is corrupt.
    """
    args = AdvanceTrainParams.model_validate(params)
    state_path = require_state_path(ctx)
    try:
        records = read_release_records(state_path)
        train = derive_train(
            V07_TRAIN, recorded_keys=records, advances=read_train_advances(state_path)
        )
        receipts = read_checkpoint_receipts(state_path, args.release_key)
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    current = records.get(args.release_key)
    if current is None:
        raise DaemonValidationError(
            f"validation_failed: no release record is stored for {args.release_key!r}"
        )
    config = resolve_config(current.version, membership_refs=current.membership_refs)
    now = datetime.now(UTC)
    try:
        result = advance_train(train, current=current, config=config, receipts=receipts, now=now)
    except TrainAdvanceError as exc:
        code = _WIRE_CODES.get(exc.code, exc.code)
        remedy = (
            f"; produce fresh receipts with `eawf release receipts {current.version}`"
            if code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE
            else ""
        )
        raise DaemonValidationError(f"validation_failed: {code.value}: {exc}{remedy}") from exc
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    advance_id = record_train_advance(
        state_path,
        result.record,
        recorded_at=now,
        summary=f"advance {train.train_id} past {current.key} to {result.record.opened_key}",
    )
    logger.info(
        f"advance closed={current.key!r} opened={result.record.opened_key!r} "
        f"index={result.train.current_checkpoint_index}"
    )
    return {
        "train": render_train_ladder(result.train),
        "closed": current.model_dump(mode="json"),
        "advance": result.record.model_dump(mode="json"),
        "advance_record_id": advance_id,
        "receipt_refs": list(result.receipt_refs),
    }


__all__ = [
    "AdvanceTrainParams",
    "ProduceReceiptsParams",
    "advance",
    "produce_receipts",
]
