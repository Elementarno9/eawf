"""Whether the rung before this one is finished with.

A release train walks one ladder of checkpoints, and each rung is the
successor of exactly one other. Opening a rung while its predecessor is
still live means two records claim the same line at once: the earlier one
can still move -- be approved, publish, recover -- while the later one is
already being pinned and swept, and whichever finishes last silently
decides what the version meant.

The train's own index guard (:func:`eawf.workflow.release.advance.advance_train`)
answers a narrower question. It moves the index only from ``baked`` or
``released``, because a train that walked forward on an abandoned rung
would claim a checkpoint it never shipped. That is the right rule for
*walking the index* and the wrong rule for *opening a successor*: an
abandoned rung is still finished with, and its successor is precisely the
correction it calls for. So succession reads the status machine's own
terminal set, which includes ``cancelled`` and ``partially_released``,
rather than the narrower advancing set.

The predecessor must also *exist*. A rung opened over a predecessor
nobody recorded is a checkpoint whose lineage cannot be read back at all,
which is the shape the ``0.7.0.dev1`` incident took: a version published
to four registries while the machinery held no record of it. An absent
predecessor is therefore a refusal naming the record to open, not a
silently permitted open.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Final

from eawf.kernel.spec.release import Release, ReleaseCheckpoint, ReleaseTrain
from eawf.workflow.release.lifecycle import TERMINAL_RELEASE_STATUSES

logger = logging.getLogger(__name__)

#: The statuses a predecessor may stand at for its successor to open.
#: Read off the status machine rather than authored, so a new terminal
#: status is admitted here the moment its out-edges are removed and a
#: second list cannot drift from the first.
SUCCEEDABLE_STATUSES: Final[frozenset[str]] = frozenset(
    status.value for status in TERMINAL_RELEASE_STATUSES
)


class SuccessionDenialCode(StrEnum):
    """Named reason a checkpoint may not open over its predecessor.

    Values:
        PREDECESSOR_UNRECORDED: The rung before this one has no release
            record, so its lineage cannot be read back.
        PREDECESSOR_LIVE: The rung before this one has not reached a
            terminal status and can still move.
    """

    PREDECESSOR_UNRECORDED = "predecessor_unrecorded"
    PREDECESSOR_LIVE = "predecessor_live"


class CheckpointSuccessionError(Exception):
    """A checkpoint was refused because its predecessor is not finished.

    Attributes:
        code: The named denial.
        release_key: Key of the checkpoint that was refused.
        predecessor_key: Key of the rung before it.
    """

    def __init__(
        self,
        code: SuccessionDenialCode,
        message: str,
        *,
        release_key: str,
        predecessor_key: str,
    ) -> None:
        """Build the refusal.

        Args:
            code: The named denial.
            message: Operator-facing detail, opening with *code*.
            release_key: Key of the refused checkpoint.
            predecessor_key: Key of the rung before it.
        """
        super().__init__(message)
        self.code = code
        self.release_key = release_key
        self.predecessor_key = predecessor_key


def predecessor_rung(train: ReleaseTrain, version: str) -> ReleaseCheckpoint | None:
    """Return the rung *version* succeeds on *train*, or ``None`` at the head.

    Args:
        train: Train that declares the ladder.
        version: Normalized version of the rung being opened.

    Returns:
        The rung immediately below, or ``None`` when *version* is the
        first rung and succeeds nothing.

    Raises:
        KeyError: When *train* declares no rung for *version*.
        ValueError: When *version* is not a normalized train version.
    """
    key = train.checkpoint_for_version(version).release_key
    index = next(
        position for position, rung in enumerate(train.checkpoints) if rung.release_key == key
    )
    return None if index == 0 else train.checkpoints[index - 1]


def assert_predecessor_terminal(
    train: ReleaseTrain,
    *,
    version: str,
    predecessor: Release | None,
) -> ReleaseCheckpoint | None:
    """Raise unless the rung below *version* is recorded and finished with.

    The first rung of a ladder succeeds nothing, so it opens against a
    ``None`` predecessor without a record being required. Every later
    rung needs one, and it needs to have stopped moving.

    Args:
        train: Train that declares the ladder.
        version: Normalized version of the rung being opened.
        predecessor: The recorded release of the rung below, or ``None``
            when no record was found for it.

    Returns:
        The predecessor rung declaration, or ``None`` at the head of the
        ladder.

    Raises:
        CheckpointSuccessionError: ``predecessor_unrecorded`` when the
            rung below has no record, ``predecessor_live`` when it has
            one that is not terminal.
        KeyError: When *train* declares no rung for *version*.
        ValueError: When *version* is not a normalized train version, or
            *predecessor* is a record of some other rung.
    """
    rung = predecessor_rung(train, version)
    if rung is None:
        logger.info(f"assert_predecessor_terminal version={version!r} predecessor=none")
        return None
    key = train.checkpoint_for_version(version).release_key
    if predecessor is None:
        raise CheckpointSuccessionError(
            SuccessionDenialCode.PREDECESSOR_UNRECORDED,
            f"{SuccessionDenialCode.PREDECESSOR_UNRECORDED.value}: {key} succeeds "
            f"{rung.release_key}, which has no release record; open and finish "
            f"{rung.release_key} before opening its successor",
            release_key=key,
            predecessor_key=rung.release_key,
        )
    if predecessor.key != rung.release_key:
        raise ValueError(
            f"predecessor record {predecessor.key!r} is not the rung below {key!r}, "
            f"which is {rung.release_key!r}"
        )
    if predecessor.status.value not in SUCCEEDABLE_STATUSES:
        raise CheckpointSuccessionError(
            SuccessionDenialCode.PREDECESSOR_LIVE,
            f"{SuccessionDenialCode.PREDECESSOR_LIVE.value}: {key} succeeds "
            f"{rung.release_key}, which stands at {predecessor.status.value!r} and can "
            f"still move; a successor opens only over "
            f"{sorted(SUCCEEDABLE_STATUSES)}",
            release_key=key,
            predecessor_key=rung.release_key,
        )
    logger.info(
        f"assert_predecessor_terminal version={version!r} "
        f"predecessor={predecessor.key!r} status={predecessor.status.value!r}"
    )
    return rung


__all__ = [
    "SUCCEEDABLE_STATUSES",
    "CheckpointSuccessionError",
    "SuccessionDenialCode",
    "assert_predecessor_terminal",
    "predecessor_rung",
]
