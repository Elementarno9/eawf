"""The two collections a release train walks on: gate receipts and advances.

A checkpoint closes on evidence, and the train moves when it closes. Both
facts have to outlive the call that produced them. Otherwise
``release.advance_train`` would have to be handed its receipts by whoever
called it, and ``release.show`` would go on reporting a train that never
moved. This module stores both:

* checkpoint gate receipts under
  :attr:`~eawf.kernel.state.enums.StoreKind.RELEASE_CHECKPOINT_RECEIPT`,
  one row per receipt ever issued, read back as the newest receipt of
  each gate;
* train advances under
  :attr:`~eawf.kernel.state.enums.StoreKind.RELEASE_TRAIN_ADVANCE`, one
  row per rung the train walked past.

Both kinds are named ``release_*`` because the tree-cleanliness probe
ignores ``.ea/store/release*.jsonl``. A store that fell outside that
glob would red the next sweep of the checkpoint whose verbs wrote it.

A corrupt row is refused, never skipped, as in the record collection. A
skipped receipt would read as a gate nobody proved, and a skipped
advance as a train that never moved.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.workflow.release.advance import CheckpointGateReceipt, TrainAdvanceRecord
from eawf.workflow.release.boundaries import PublicationBoundary, durable_boundary

logger = logging.getLogger(__name__)


def checkpoint_receipts_path(state_path: Path) -> Path:
    """Return the JSONL path of the checkpoint gate receipt collection.

    Args:
        state_path: Path to ``state.json``.

    Returns:
        ``<state_dir>/store/release_checkpoint_receipt.jsonl``.
    """
    return store_path(state_path, StoreKind.RELEASE_CHECKPOINT_RECEIPT)


def train_advances_path(state_path: Path) -> Path:
    """Return the JSONL path of the train advance collection.

    Args:
        state_path: Path to ``state.json``.

    Returns:
        ``<state_dir>/store/release_train_advance.jsonl``.
    """
    return store_path(state_path, StoreKind.RELEASE_TRAIN_ADVANCE)


def train_advance_envelope_id(advance: TrainAdvanceRecord) -> str:
    """Return the envelope id of *advance*.

    Args:
        advance: The advance being written.

    Returns:
        ``<train id>:<closed key>``. A rung is walked past at most once,
        so the closed key is enough to identify the move.
    """
    return f"{advance.train_id}:{advance.closed_key}"


def _require_aware(recorded_at: datetime) -> None:
    """Refuse a naive append instant.

    Args:
        recorded_at: The instant a row is appended at.

    Raises:
        ValueError: When *recorded_at* carries no zone, since such a row
            cannot be ordered against the others.
    """
    if recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be timezone-aware")


@durable_boundary(PublicationBoundary.OBSERVATION_RECEIPT_WRITE)
def record_checkpoint_receipt(
    state_path: Path,
    receipt: CheckpointGateReceipt,
    *,
    recorded_at: datetime,
    summary: str,
) -> CheckpointGateReceipt:
    """Append *receipt* to the collection and return it unchanged.

    The envelope id is the receipt's own ``receipt_ref``, which is unique
    per issue. A rerun therefore adds rows beside the earlier ones and
    never overwrites them.

    Args:
        state_path: Path to ``state.json``.
        receipt: The receipt to persist.
        recorded_at: Timezone-aware UTC instant of the append.
        summary: One-line operator-facing description of what settled
            the gate.

    Returns:
        *receipt*.

    Raises:
        ValueError: When *recorded_at* is naive.
        StateConflict: When the append lock cannot be acquired.
    """
    _require_aware(recorded_at)
    append_envelope(
        checkpoint_receipts_path(state_path),
        Envelope(
            id=receipt.receipt_ref,
            kind=StoreKind.RELEASE_CHECKPOINT_RECEIPT,
            scope_id=receipt.release_key,
            created_at=recorded_at,
            summary=summary[:500],
            payload=receipt.model_dump(mode="json"),
        ),
    )
    logger.info(
        f"record_checkpoint_receipt key={receipt.release_key!r} gate={receipt.gate.value!r} "
        f"ref={receipt.receipt_ref!r}"
    )
    return receipt


@durable_boundary(PublicationBoundary.TRANSITION_APPLY)
def record_train_advance(
    state_path: Path,
    advance: TrainAdvanceRecord,
    *,
    recorded_at: datetime,
    summary: str,
) -> str:
    """Append *advance* to the collection and return its envelope id.

    This row is the train's transition. Before it lands, the train
    stands where it stood. After it lands, the move stands, and a repeat
    is refused because the closed rung is no longer the open one.

    Args:
        state_path: Path to ``state.json``.
        advance: The advance to persist.
        recorded_at: Timezone-aware UTC instant of the append.
        summary: One-line operator-facing description.

    Returns:
        The envelope id the row was written under.

    Raises:
        ValueError: When *recorded_at* is naive.
        StateConflict: When the append lock cannot be acquired.
    """
    _require_aware(recorded_at)
    envelope_id = train_advance_envelope_id(advance)
    append_envelope(
        train_advances_path(state_path),
        Envelope(
            id=envelope_id,
            kind=StoreKind.RELEASE_TRAIN_ADVANCE,
            scope_id=advance.train_id,
            created_at=recorded_at,
            summary=summary[:500],
            payload=advance.model_dump(mode="json"),
        ),
    )
    logger.info(
        f"record_train_advance train_id={advance.train_id!r} closed={advance.closed_key!r} "
        f"opened={advance.opened_key!r} revision={advance.train_revision}"
    )
    return envelope_id


def _read_rows[Row: BaseModel](path: Path, kind: StoreKind, model: type[Row]) -> list[Row]:
    """Return every row of the collection at *path*, in file order.

    Args:
        path: The collection file.
        kind: The store kind every envelope in it must carry.
        model: The payload model every envelope must validate against.

    Returns:
        The validated payloads; empty when the file does not exist yet.

    Raises:
        ValueError: When a line is not an envelope, is filed under
            another kind, or carries a payload *model* rejects.
    """
    if not path.exists():
        return []
    rows: list[Row] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            envelope = Envelope.model_validate_json(line)
        except ValidationError as exc:
            raise ValueError(
                f"{kind.value} collection {path} line {number} is not an envelope: {exc}"
            ) from exc
        if envelope.kind is not kind:
            raise ValueError(
                f"{kind.value} collection {path} line {number} is filed under "
                f"{envelope.kind.value!r}, not {kind.value!r}"
            )
        try:
            rows.append(model.model_validate(envelope.payload))
        except ValidationError as exc:
            raise ValueError(
                f"{kind.value} collection {path} line {number} payload is not a "
                f"{model.__name__}: {exc}"
            ) from exc
    return rows


def read_checkpoint_receipts(
    state_path: Path,
    release_key: str,
) -> tuple[CheckpointGateReceipt, ...]:
    """Return the newest stored receipt of each gate of *release_key*.

    A rerun of the producer issues fresh receipts beside the old ones,
    so the newest issue of a gate is the one that speaks for it. An
    older receipt that still happens to be fresh is not a second voice.
    It is history.

    Args:
        state_path: Path to ``state.json``.
        release_key: ``REL-<version>`` key whose receipts are read.

    Returns:
        At most one receipt per gate, in the order each gate was first
        stored. Empty when nothing is stored for the key.

    Raises:
        ValueError: When the collection is corrupt.
    """
    newest: dict[str, CheckpointGateReceipt] = {}
    for receipt in _read_rows(
        checkpoint_receipts_path(state_path),
        StoreKind.RELEASE_CHECKPOINT_RECEIPT,
        CheckpointGateReceipt,
    ):
        if receipt.release_key != release_key:
            continue
        held = newest.get(receipt.gate.value)
        if held is None or receipt.issued_at >= held.issued_at:
            newest[receipt.gate.value] = receipt
    return tuple(newest.values())


def read_train_advances(state_path: Path) -> tuple[TrainAdvanceRecord, ...]:
    """Return every recorded train advance, in the order they were written.

    Args:
        state_path: Path to ``state.json``.

    Returns:
        The advances; empty when the train never moved.

    Raises:
        ValueError: When the collection is corrupt.
    """
    return tuple(
        _read_rows(
            train_advances_path(state_path),
            StoreKind.RELEASE_TRAIN_ADVANCE,
            TrainAdvanceRecord,
        )
    )


__all__ = [
    "checkpoint_receipts_path",
    "read_checkpoint_receipts",
    "read_train_advances",
    "record_checkpoint_receipt",
    "record_train_advance",
    "train_advance_envelope_id",
    "train_advances_path",
]
