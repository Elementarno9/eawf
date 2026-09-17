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

The advance never mutates the closing record, and it opens no record
either. :class:`Release` is frozen, and :func:`advance_train` returns the
prior record untouched beside a :class:`TrainAdvanceRecord` naming the
rung now open. The DRAFT of that rung is opened by ``release create``
behind measured admission, so there is one way into a checkpoint record
rather than a second one that skips the admission check.

Where the train stands is derived rather than stored. The ladder is
source data, so its index cannot be a field anyone writes;
:func:`open_rung_index` reads it off the release records and the
recorded advances instead.

This module stays free of store I/O: the store-kind registry imports its
two payload models, and a store import here would close that loop.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import (
    ReferenceStr,
    Release,
    ReleaseCheckpoint,
    ReleaseKeyStr,
    ReleaseStatus,
    ReleaseTrain,
    ReleaseTrainIdStr,
    Sha256DigestStr,
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


class TrainAdvanceRecord(_StrictModel):
    """The durable row one train advance leaves behind.

    Attributes:
        train_id: Train that walked.
        closed_key: Checkpoint the train walked past.
        closed_revision: Revision of that checkpoint's record when it
            closed, which is the revision its receipts were judged
            against.
        opened_key: The rung the train has open after the advance. It
            names a rung, not a record: nothing is opened until
            ``release create`` admits it.
        receipt_refs: The closed checkpoint's validated receipt
            references, in its configuration's gate order.
        advanced_at: When the advance was judged (timezone-aware UTC).
        train_revision: The train's revision after the advance.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    train_id: ReleaseTrainIdStr
    closed_key: ReleaseKeyStr
    closed_revision: Annotated[int, Field(ge=0)]
    opened_key: ReleaseKeyStr
    receipt_refs: tuple[ReferenceStr, ...]
    advanced_at: UtcDatetime
    train_revision: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _walks_forward(self) -> TrainAdvanceRecord:
        """Reject an advance that closes and opens the same rung.

        Raises:
            ValueError: When :attr:`opened_key` equals :attr:`closed_key`.
        """
        if self.opened_key == self.closed_key:
            raise ValueError(f"train advance closes and opens the same rung {self.closed_key!r}")
        return self


@dataclass(frozen=True, slots=True)
class TrainAdvance:
    """The result of walking a train onto its next rung.

    Attributes:
        train: The train at the new index, one revision on.
        closed: The record that was standing at the old index, returned
            unchanged so a caller can assert it was never rewritten.
        record: The row a caller persists so the move outlives the call.
    """

    train: ReleaseTrain
    closed: Release
    record: TrainAdvanceRecord

    @property
    def receipt_refs(self) -> tuple[str, ...]:
        """Return the closed checkpoint's validated receipt references."""
        return self.record.receipt_refs


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
    Measured admission is the caller's job, which is why the train
    advance does not call this.

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
) -> TrainAdvance:
    """Walk *train* onto its next rung, or refuse and leave it untouched.

    The checks run in the order a reader would ask them: is this the
    record the train has open, has it finished, is there anywhere to go,
    and does its evidence still bind. Nothing is built until all four
    hold, so a refused advance leaves both the train and the closing
    record exactly as they were. No record is built for the next rung
    either way; ``release create`` opens it once admission holds.

    Args:
        train: The train to advance, standing at its derived open index.
        current: The record standing at the open index.
        config: The open checkpoint's loaded configuration.
        receipts: Gate receipts earned by the open checkpoint, at most
            one per gate.
        now: Timezone-aware UTC instant freshness is judged at.

    Returns:
        A :class:`TrainAdvance` carrying the advanced train, the
        unchanged closing record and the row that records the move.

    Raises:
        TrainAdvanceError: On any of the five named denials.
        ValueError: When *now* is naive, when *config* describes a
            different checkpoint, or when a gate is offered twice.
    """
    _assert_open_checkpoint(train, current)
    assert_checkpoint_terminal(train, current)
    rung = _next_rung(train, current)
    refs = assert_prerequisite_receipts(current, config, receipts, now=now, train_id=train.train_id)
    advanced = ReleaseTrain.model_validate(
        train.model_copy(
            update={
                "current_checkpoint_index": train.current_checkpoint_index + 1,
                "gate_receipt_refs": {**train.gate_receipt_refs, current.key: refs},
                "revision": train.revision + 1,
            }
        ).model_dump(mode="json")
    )
    record = TrainAdvanceRecord(
        train_id=train.train_id,
        closed_key=current.key,
        closed_revision=current.revision,
        opened_key=rung.release_key,
        receipt_refs=refs,
        advanced_at=now,
        train_revision=advanced.revision,
    )
    logger.info(
        f"advance_train train_id={train.train_id!r} closed={current.key!r} "
        f"opened={rung.release_key!r} index={advanced.current_checkpoint_index} "
        f"revision={advanced.revision}"
    )
    return TrainAdvance(train=advanced, closed=current, record=record)


def open_rung_index(
    train: ReleaseTrain,
    *,
    recorded_keys: Iterable[str],
    advances: Iterable[TrainAdvanceRecord],
) -> int:
    """Return the index of the rung *train* has open, read off the stores.

    The open rung is the later of two facts. A rung that carries a
    record has been opened, so the train stands at least that far up.
    A rung the train recorded an advance past is closed, so the train
    stands at least one rung above it. Neither fact alone is enough: a
    checkpoint burned to ``partially_released`` never advances, yet its
    successor's record moves the train past it, and a baked checkpoint
    that advanced has no successor record until ``release create``
    opens one.

    Args:
        train: The train whose ladder is read.
        recorded_keys: The key of every record in the release-record
            collection. A key the ladder does not declare belongs to
            another train and is skipped.
        advances: Every recorded train advance. Rows of another train
            are skipped.

    Returns:
        The zero-based open index; ``0`` when nothing is recorded.

    Raises:
        ValueError: When an advance of this train opened a rung its
            ladder does not declare.
    """
    positions = {rung.release_key: index for index, rung in enumerate(train.checkpoints)}
    highest_recorded = max((positions[key] for key in recorded_keys if key in positions), default=0)
    after_advance = 0
    for row in advances:
        if row.train_id != train.train_id:
            continue
        if row.opened_key not in positions:
            raise ValueError(
                f"train {train.train_id} advance opened {row.opened_key!r}, which its "
                f"ladder does not declare"
            )
        after_advance = max(after_advance, positions[row.opened_key])
    return max(highest_recorded, after_advance)


def derive_train(
    train: ReleaseTrain,
    *,
    recorded_keys: Iterable[str],
    advances: Sequence[TrainAdvanceRecord],
) -> ReleaseTrain:
    """Return *train* standing where its records and advances put it.

    Args:
        train: The source-declared train, standing at its first rung.
        recorded_keys: The key of every record in the release-record
            collection.
        advances: Every recorded train advance.

    Returns:
        The train at :func:`open_rung_index`, one revision on per
        advance of its own, carrying each advance's receipt references
        against the rung it closed.

    Raises:
        ValueError: When an advance of this train names a rung its
            ladder does not declare.
    """
    own = [row for row in advances if row.train_id == train.train_id]
    index = open_rung_index(train, recorded_keys=recorded_keys, advances=own)
    refs = {**train.gate_receipt_refs, **{row.closed_key: row.receipt_refs for row in own}}
    return ReleaseTrain.model_validate(
        train.model_copy(
            update={
                "current_checkpoint_index": index,
                "gate_receipt_refs": refs,
                "revision": train.revision + len(own),
            }
        ).model_dump(mode="json")
    )


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
    "TrainAdvanceRecord",
    "advance_train",
    "assert_checkpoint_terminal",
    "assert_prerequisite_receipts",
    "derive_train",
    "draft_release_for",
    "ladder_status",
    "open_rung_index",
    "render_train_ladder",
    "render_train_ladder_text",
]
