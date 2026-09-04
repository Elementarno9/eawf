"""REL-011: the guard that opens the next rung of a release train.

A train walks one target version up a ladder of checkpoints. Moving
:attr:`~eawf.kernel.spec.release.ReleaseTrain.current_checkpoint_index`
is the moment the project stops working on one checkpoint and starts
working on the next, so it is the moment worth guarding. Two rules do
all the work here:

* **Only a finished checkpoint closes.** The index moves only when the
  :class:`~eawf.kernel.spec.release.Release` at the current index stands
  at :attr:`~eawf.kernel.spec.release.ReleaseStatus.BAKED` or
  :attr:`~eawf.kernel.spec.release.ReleaseStatus.RELEASED` -- the two
  terminal states that mean *the artifacts are out there and were
  independently observed*. Every other status denies
  ``checkpoint_not_terminal`` and leaves the index where it was.
  :data:`ADVANCING_STATUSES` is deliberately narrower than
  :data:`~eawf.workflow.release.lifecycle.TERMINAL_RELEASE_STATUSES`:
  ``cancelled`` and ``partially_released`` are terminal in the status
  machine but they are *abandonment*, and a train that walked forward on
  an abandoned rung would claim a checkpoint it never shipped.

* **The prior checkpoint's evidence has to still be true.** Opening rung
  ``n+1`` re-validates every gate receipt of rung ``n``
  (:func:`assert_prerequisite_receipts`): each required gate carries a
  receipt, each receipt binds that Release's exact ``source_sha`` and
  ``manifest_digest``, and none is past its ``expires_at``. A receipt
  that binds different source proves a gate passed on a *different
  build*, which is the failure this check exists to catch.

The advance never mutates the closing record. :class:`Release` is frozen
and :func:`advance_train` returns the prior record untouched alongside a
brand-new DRAFT record for the next rung, so a checkpoint's history can
never be rewritten by the checkpoint that followed it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from pydantic import ConfigDict, model_validator

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import (
    ReferenceStr,
    Release,
    ReleaseCheckpoint,
    ReleaseKeyStr,
    ReleaseStatus,
    ReleaseTrain,
    Sha256DigestStr,
    validate_release_against_train,
)
from eawf.kernel.spec.release_config import ReleaseConfig, ReleaseGateName
from eawf.kernel.state.models import ShaStr
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)

#: The two statuses that let a train walk past a checkpoint. Narrower
#: than the status machine's terminal set on purpose -- see the module
#: docstring.
ADVANCING_STATUSES: Final[frozenset[ReleaseStatus]] = frozenset(
    {ReleaseStatus.BAKED, ReleaseStatus.RELEASED}
)


class TrainAdvanceDenialCode(StrEnum):
    """Named error a refused train advance raises.

    Values:
        CHECKPOINT_NOT_TERMINAL: The Release at the current index has not
            reached ``baked`` or ``released``.
        CHECKPOINT_KEY_MISMATCH: The offered record is not the one the
            train currently has open.
        LADDER_EXHAUSTED: The train already stands on its last rung.
        PREREQUISITE_RECEIPT_MISSING: A required gate of the closing
            checkpoint carries no receipt at all.
        PREREQUISITE_RECEIPT_STALE: A receipt binds different source or
            has passed its expiry.
    """

    CHECKPOINT_NOT_TERMINAL = "checkpoint_not_terminal"
    CHECKPOINT_KEY_MISMATCH = "checkpoint_key_mismatch"
    LADDER_EXHAUSTED = "ladder_exhausted"
    PREREQUISITE_RECEIPT_MISSING = "prerequisite_receipt_missing"
    PREREQUISITE_RECEIPT_STALE = "prerequisite_receipt_stale"


class CheckpointLadderStatus(StrEnum):
    """Where one rung stands relative to the open checkpoint.

    Values:
        PASSED: The train has already walked past this rung.
        OPEN: The rung the train currently has open.
        PENDING: A rung the train has not reached.
    """

    PASSED = "passed"
    OPEN = "open"
    PENDING = "pending"


class TrainAdvanceError(Exception):
    """A train advance was refused.

    Attributes:
        code: The named denial.
        train_id: Train the advance was attempted on.
        release_key: Checkpoint the train had open.
        gate: The gate whose receipt was missing or stale; ``None`` for
            the denials that are not about a receipt.
    """

    def __init__(
        self,
        code: TrainAdvanceDenialCode,
        message: str,
        *,
        train_id: str,
        release_key: str,
        gate: ReleaseGateName | None = None,
    ) -> None:
        """Store the typed denial alongside the scope it was raised in."""
        super().__init__(message)
        self.code = code
        self.train_id = train_id
        self.release_key = release_key
        self.gate = gate


class CheckpointGateReceipt(_StrictModel):
    """Proof that one gate of one checkpoint passed on exact source.

    The record is a *binding*, not a verdict: its whole job is to name
    which build the gate was run against, so a later reader can tell a
    receipt that still applies from one that was earned on source the
    checkpoint has since moved off.

    Attributes:
        gate: Which gate of the checkpoint's profile this settles.
        release_key: ``REL-<version>`` the receipt was earned for.
        source_sha: Commit the gate ran at.
        manifest_digest: Digest of the manifest the gate ran against.
        issued_at: When the gate ran (timezone-aware UTC).
        expires_at: When the receipt goes stale; strictly after
            :attr:`issued_at`.
        receipt_ref: Pointer to the stored evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: ReleaseGateName
    release_key: ReleaseKeyStr
    source_sha: ShaStr
    manifest_digest: Sha256DigestStr
    issued_at: UtcDatetime
    expires_at: UtcDatetime
    receipt_ref: ReferenceStr

    @model_validator(mode="after")
    def _window_moves_forward(self) -> CheckpointGateReceipt:
        """Reject a freshness window that does not move forward.

        Raises:
            ValueError: When :attr:`expires_at` is not strictly after
                :attr:`issued_at` -- a receipt that expires before it was
                issued was never fresh.
        """
        if self.expires_at <= self.issued_at:
            raise ValueError(f"gate {self.gate.value!r} expires_at must be after issued_at")
        return self


@dataclass(frozen=True, slots=True)
class TrainAdvance:
    """The result of walking a train onto its next rung.

    Attributes:
        train: The train at the new index, one revision on.
        closed: The record that was standing at the old index, returned
            unchanged so a caller can assert it was never rewritten.
        opened: The brand-new DRAFT record of the next rung.
        receipt_refs: The prior checkpoint's validated receipt
            references, in the profile's gate order.
    """

    train: ReleaseTrain
    closed: Release
    opened: Release
    receipt_refs: tuple[str, ...]


def draft_release_for(
    rung: ReleaseCheckpoint,
    *,
    uid: UUID,
    membership_refs: Sequence[str] = (),
) -> Release:
    """Return a fresh DRAFT :class:`Release` for *rung*.

    Every field is derived from the rung declaration, so a checkpoint
    record cannot be opened at an epoch or a channel its train never
    declared. The record starts at revision ``0``: it is a new object
    with its own identity, never a copy of the checkpoint before it.

    Args:
        rung: The ladder rung to open.
        uid: Stable identity for the new record.
        membership_refs: Milestone acceptance bundles, for the rungs
            that require them. Empty for the epoch-1 rungs, which forbid
            them outright.

    Returns:
        The DRAFT record.

    Raises:
        ValidationError: When the rung's version forbids the supplied
            membership bundles.
    """
    return Release(
        uid=uid,
        key=rung.release_key,
        version=rung.version,
        channel=rung.channel,
        authority_epoch=rung.authority_epoch,
        membership_refs=tuple(membership_refs),
        status=ReleaseStatus.DRAFT,
        revision=0,
    )


def _index_receipts(
    receipts: Iterable[CheckpointGateReceipt],
) -> Mapping[ReleaseGateName, CheckpointGateReceipt]:
    """Return *receipts* keyed by gate, refusing a duplicated gate.

    Args:
        receipts: The receipts offered for one checkpoint.

    Returns:
        One receipt per gate.

    Raises:
        ValueError: When two receipts claim the same gate. Picking one
            silently would let a stale receipt hide behind a fresh one.
    """
    indexed: dict[ReleaseGateName, CheckpointGateReceipt] = {}
    for receipt in receipts:
        if receipt.gate in indexed:
            raise ValueError(f"two receipts offered for gate {receipt.gate.value!r}")
        indexed[receipt.gate] = receipt
    return indexed


def _binding_mismatch(release: Release, receipt: CheckpointGateReceipt) -> str | None:
    """Return the first field on which *receipt* fails to bind *release*.

    Args:
        release: The checkpoint record the receipt claims to prove.
        receipt: The offered receipt.

    Returns:
        A rendered ``field=offered, not expected`` phrase, or ``None``
        when the receipt binds the record exactly.
    """
    for name, expected, offered in (
        ("release_key", release.key, receipt.release_key),
        ("source_sha", release.source_sha, receipt.source_sha),
        ("manifest_digest", release.manifest_digest, receipt.manifest_digest),
    ):
        if expected != offered:
            return f"{name}={offered!r}, not the checkpoint's {expected!r}"
    return None


def _assert_receipt_binds(
    release: Release,
    receipt: CheckpointGateReceipt,
    *,
    train_id: str,
    now: datetime,
) -> None:
    """Raise unless *receipt* binds *release* exactly and is still fresh.

    Args:
        release: The checkpoint record being closed.
        receipt: The offered receipt.
        train_id: Train the advance was attempted on, for the error.
        now: Timezone-aware UTC instant freshness is judged at.

    Raises:
        TrainAdvanceError: ``prerequisite_receipt_stale``, naming the
            gate, when the receipt binds different source or has expired.
    """
    mismatch = _binding_mismatch(release, receipt)
    if mismatch is not None:
        raise TrainAdvanceError(
            TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE,
            f"prerequisite_receipt_stale: gate {receipt.gate.value!r} receipt binds {mismatch}",
            train_id=train_id,
            release_key=release.key,
            gate=receipt.gate,
        )
    if receipt.expires_at <= now:
        raise TrainAdvanceError(
            TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE,
            f"prerequisite_receipt_stale: gate {receipt.gate.value!r} receipt expired at "
            f"{receipt.expires_at.isoformat()}, before {now.isoformat()}",
            train_id=train_id,
            release_key=release.key,
            gate=receipt.gate,
        )


def assert_prerequisite_receipts(
    release: Release,
    config: ReleaseConfig,
    receipts: Sequence[CheckpointGateReceipt],
    *,
    now: datetime,
    train_id: str,
) -> tuple[str, ...]:
    """Return the receipt refs of *release*, asserting each still holds.

    Args:
        release: The checkpoint being closed.
        config: That checkpoint's loaded configuration, whose
            ``gates.required`` list names every gate that must carry a
            receipt.
        receipts: The offered receipts.
        now: Timezone-aware UTC instant freshness is judged at.
        train_id: Train the advance was attempted on, for the error.

    Returns:
        The validated receipt references, in the configuration's gate
        order.

    Raises:
        TrainAdvanceError: ``prerequisite_receipt_missing`` when a
            required gate carries no receipt, or
            ``prerequisite_receipt_stale`` when one binds different
            source or has expired. Both name the gate.
        ValueError: When *now* is naive, when *config* describes a
            different checkpoint, or when a gate is offered twice.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if config.version != release.version:
        raise ValueError(
            f"configuration describes {config.version!r}, not checkpoint {release.version!r}"
        )
    indexed = _index_receipts(receipts)
    refs: list[str] = []
    for gate in config.gates.required:
        receipt = indexed.get(gate)
        if receipt is None:
            raise TrainAdvanceError(
                TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_MISSING,
                f"prerequisite_receipt_missing: checkpoint {release.key} carries no gate "
                f"receipt for {gate.value!r}",
                train_id=train_id,
                release_key=release.key,
                gate=gate,
            )
        _assert_receipt_binds(release, receipt, train_id=train_id, now=now)
        refs.append(receipt.receipt_ref)
    logger.debug(
        f"assert_prerequisite_receipts key={release.key!r} gates={len(refs)} train_id={train_id!r}"
    )
    return tuple(refs)


def _assert_open_checkpoint(train: ReleaseTrain, release: Release) -> None:
    """Raise unless *release* is the record the train currently has open.

    Args:
        train: The train being advanced.
        release: The record offered as the closing checkpoint.

    Raises:
        TrainAdvanceError: ``checkpoint_key_mismatch`` when the record is
            not the open rung.
    """
    open_key = train.current_checkpoint.release_key
    if release.key != open_key:
        raise TrainAdvanceError(
            TrainAdvanceDenialCode.CHECKPOINT_KEY_MISMATCH,
            f"checkpoint_key_mismatch: train {train.train_id} has {open_key!r} open, "
            f"not {release.key!r}",
            train_id=train.train_id,
            release_key=release.key,
        )


def assert_checkpoint_terminal(train: ReleaseTrain, release: Release) -> None:
    """Raise unless *release* has finished, so the train may walk past it.

    Args:
        train: The train being advanced.
        release: The record standing at the open index.

    Raises:
        TrainAdvanceError: ``checkpoint_not_terminal`` when the record is
            not ``baked`` or ``released``.
    """
    if release.status in ADVANCING_STATUSES:
        return
    raise TrainAdvanceError(
        TrainAdvanceDenialCode.CHECKPOINT_NOT_TERMINAL,
        f"checkpoint_not_terminal: {release.key} stands at {release.status.value!r}; the "
        f"train advances only from "
        f"{sorted(status.value for status in ADVANCING_STATUSES)}",
        train_id=train.train_id,
        release_key=release.key,
    )


def _next_rung(train: ReleaseTrain, release: Release) -> ReleaseCheckpoint:
    """Return the rung after the open one.

    Args:
        train: The train being advanced.
        release: The closing checkpoint, for the error message.

    Returns:
        The next ladder rung.

    Raises:
        TrainAdvanceError: ``ladder_exhausted`` when the train already
            stands on its last rung.
    """
    following = train.current_checkpoint_index + 1
    if following >= len(train.checkpoints):
        raise TrainAdvanceError(
            TrainAdvanceDenialCode.LADDER_EXHAUSTED,
            f"ladder_exhausted: train {train.train_id} stands on its last rung "
            f"{release.key!r}; there is no checkpoint to open",
            train_id=train.train_id,
            release_key=release.key,
        )
    return train.checkpoints[following]


def advance_train(
    train: ReleaseTrain,
    *,
    current: Release,
    config: ReleaseConfig,
    receipts: Sequence[CheckpointGateReceipt],
    now: datetime,
    next_uid: UUID,
    membership_refs: Sequence[str] = (),
) -> TrainAdvance:
    """Walk *train* onto its next rung, or refuse and leave it untouched.

    The checks run in the order a reader would ask them: is this the
    record the train has open, has it finished, is there anywhere to go,
    and does its evidence still bind. Nothing is built until all four
    hold, so a refused advance leaves both the train and the closing
    record exactly as they were.

    Args:
        train: The train to advance.
        current: The record standing at the open index.
        config: The open checkpoint's loaded configuration.
        receipts: Gate receipts earned by the open checkpoint.
        now: Timezone-aware UTC instant freshness is judged at.
        next_uid: Identity for the DRAFT record of the next rung.
        membership_refs: Milestone acceptance bundles for the next rung,
            for the rungs that require them.

    Returns:
        A :class:`TrainAdvance` carrying the advanced train, the
        unchanged closing record and the newly opened DRAFT record.

    Raises:
        TrainAdvanceError: On any of the five named denials.
        ValueError: When *now* is naive, when *config* describes a
            different checkpoint, when a gate is offered twice, or when
            the opened record disagrees with the rung the train declares.
    """
    _assert_open_checkpoint(train, current)
    assert_checkpoint_terminal(train, current)
    rung = _next_rung(train, current)
    refs = assert_prerequisite_receipts(current, config, receipts, now=now, train_id=train.train_id)
    opened = draft_release_for(rung, uid=next_uid, membership_refs=membership_refs)
    validate_release_against_train(opened, train)
    advanced = ReleaseTrain.model_validate(
        train.model_copy(
            update={
                "current_checkpoint_index": train.current_checkpoint_index + 1,
                "gate_receipt_refs": {**train.gate_receipt_refs, current.key: refs},
                "revision": train.revision + 1,
            }
        ).model_dump(mode="json")
    )
    logger.info(
        f"advance_train train_id={train.train_id!r} closed={current.key!r} "
        f"opened={opened.key!r} index={advanced.current_checkpoint_index} "
        f"revision={advanced.revision}"
    )
    return TrainAdvance(train=advanced, closed=current, opened=opened, receipt_refs=refs)


def ladder_status(train: ReleaseTrain, index: int) -> CheckpointLadderStatus:
    """Return where the rung at *index* stands relative to the open one.

    Args:
        train: The train being described.
        index: Zero-based ladder position.

    Returns:
        ``passed`` below the open index, ``open`` at it, ``pending``
        above it.

    Raises:
        IndexError: When *index* is off the ladder.
    """
    if not 0 <= index < len(train.checkpoints):
        raise IndexError(f"index {index} is off a ladder of {len(train.checkpoints)} checkpoints")
    if index < train.current_checkpoint_index:
        return CheckpointLadderStatus.PASSED
    if index == train.current_checkpoint_index:
        return CheckpointLadderStatus.OPEN
    return CheckpointLadderStatus.PENDING


def _rung_row(train: ReleaseTrain, index: int, rung: ReleaseCheckpoint) -> dict[str, Any]:
    """Return the JSON row describing one rung.

    Args:
        train: The train being described.
        index: Zero-based ladder position of *rung*.
        rung: The rung declaration.

    Returns:
        A JSON-safe mapping of the rung's declaration plus its ladder
        status and the receipts recorded against it.
    """
    return {
        "index": index,
        "release_key": rung.release_key,
        "version": rung.version,
        "channel": rung.channel.value,
        "authority_epoch": rung.authority_epoch,
        "gate_profile": rung.gate_profile.value,
        "requires_membership": rung.requires_membership,
        "status": ladder_status(train, index).value,
        "gate_receipt_refs": list(train.gate_receipt_refs.get(rung.release_key, ())),
    }


def render_train_ladder(train: ReleaseTrain) -> dict[str, Any]:
    """Return the JSON body ``eawf release train show`` emits.

    Args:
        train: The train to describe.

    Returns:
        The train identity, the open index, and one row per rung
        carrying its declaration and its ladder status.
    """
    return {
        "train_id": train.train_id,
        "target_version": train.target_version,
        "current_checkpoint_index": train.current_checkpoint_index,
        "current_checkpoint": train.current_checkpoint.release_key,
        "revision": train.revision,
        "checkpoints": [
            _rung_row(train, index, rung) for index, rung in enumerate(train.checkpoints)
        ],
    }


def render_train_ladder_text(train: ReleaseTrain) -> str:
    """Return the human-readable rendering of *train*.

    Args:
        train: The train to describe.

    Returns:
        One header line plus one line per rung, the open rung marked.
    """
    lines = [f"{train.train_id} -> {train.target_version}"]
    for index, rung in enumerate(train.checkpoints):
        status = ladder_status(train, index)
        marker = "*" if status is CheckpointLadderStatus.OPEN else " "
        lines.append(
            f" {marker} {rung.release_key}  status={status.value} "
            f"epoch={rung.authority_epoch} profile={rung.gate_profile.value} "
            f"membership={'required' if rung.requires_membership else 'forbidden'}"
        )
    return "\n".join(lines)


__all__ = [
    "ADVANCING_STATUSES",
    "CheckpointGateReceipt",
    "CheckpointLadderStatus",
    "TrainAdvance",
    "TrainAdvanceDenialCode",
    "TrainAdvanceError",
    "advance_train",
    "assert_checkpoint_terminal",
    "assert_prerequisite_receipts",
    "draft_release_for",
    "ladder_status",
    "render_train_ladder",
    "render_train_ladder_text",
]
