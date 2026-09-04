"""Gate waivers counted against a checkpoint, and what makes one explained.

A waiver is the record that a gate was cleared without its evidence. The
count alone is not a fact an operator can act on -- "one gate was
waived" says nothing about which protection was suspended or who
suspended it -- so a counted waiver carries three fields and is
*explained* only when all three are present: the ``scope`` the waiver
was granted on, the ``reason`` it was granted for, and the
``protected_principal`` it suspends.

The distinction drives two different outcomes. An unexplained waiver is
red: no acknowledgement can clear it, because there is nothing to
acknowledge. A fully explained set is not red -- it is *reported for
acknowledgement*, so the operator approving the checkpoint sees exactly
which protections they are accepting the loss of.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import ConfigDict

from eawf.kernel.spec.common import _StrictModel


class WaiverDisposition(StrEnum):
    """How a checkpoint's counted waivers bear on its readiness.

    Values:
        NONE: No waiver is counted against the checkpoint.
        AWAITING_ACKNOWLEDGEMENT: Every counted waiver names its scope,
            reason and protected principal. Nothing is unexplained; the
            operator must still acknowledge the set before approval.
        UNEXPLAINED: At least one counted waiver is missing one of the
            three fields, or is counted with no row at all. Red, and not
            acknowledgeable -- the waiver is documented or dropped.
    """

    NONE = "none"
    AWAITING_ACKNOWLEDGEMENT = "awaiting_acknowledgement"
    UNEXPLAINED = "unexplained"


class ReleaseWaiver(_StrictModel):
    """One gate waiver counted against a checkpoint.

    The three fields are deliberately plain strings rather than
    ``min_length=1`` constraints: an unexplained waiver has to be
    *representable* so the readiness sweep can report it as red. Refusing
    to construct it would only move the blindness one layer out, where a
    caller would drop the row and report a bare count instead.

    Attributes:
        scope: What the waiver was granted on, e.g. a wave id.
        reason: Why the evidence could not be produced.
        protected_principal: The protection the waiver suspends, stated
            so the acknowledging operator knows what they are giving up.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str = ""
    reason: str = ""
    protected_principal: str = ""

    @property
    def explained(self) -> bool:
        """Return whether all three fields carry non-blank text."""
        return bool(self.scope.strip() and self.reason.strip() and self.protected_principal.strip())

    @property
    def missing_fields(self) -> tuple[str, ...]:
        """Return the names of the blank fields, in declaration order."""
        present = {
            "scope": self.scope,
            "reason": self.reason,
            "protected_principal": self.protected_principal,
        }
        return tuple(name for name, value in present.items() if not value.strip())


def classify_waivers(waivers: Sequence[ReleaseWaiver], *, waiver_count: int) -> WaiverDisposition:
    """Return the disposition *waiver_count* waivers described by *waivers* imply.

    A count larger than the number of rows means some waiver was counted
    with no explanation attached at all, which is the same failure as a
    row with blank fields and is classified the same way.

    Args:
        waivers: The explained-or-not waiver rows.
        waiver_count: How many waivers are counted against the
            checkpoint.

    Returns:
        :attr:`WaiverDisposition.NONE` when nothing is counted,
        :attr:`WaiverDisposition.UNEXPLAINED` when any counted waiver
        lacks one of the three fields or has no row, and
        :attr:`WaiverDisposition.AWAITING_ACKNOWLEDGEMENT` otherwise.

    Raises:
        ValueError: When *waiver_count* is negative, or more rows are
            supplied than are counted.
    """
    if waiver_count < 0:
        raise ValueError(f"waiver_count must not be negative, got {waiver_count}")
    if len(waivers) > waiver_count:
        raise ValueError(
            f"{len(waivers)} waiver row(s) supplied for a count of {waiver_count}; "
            f"every row is a counted waiver"
        )
    if waiver_count == 0:
        return WaiverDisposition.NONE
    if len(waivers) < waiver_count or any(not waiver.explained for waiver in waivers):
        return WaiverDisposition.UNEXPLAINED
    return WaiverDisposition.AWAITING_ACKNOWLEDGEMENT


__all__ = [
    "ReleaseWaiver",
    "WaiverDisposition",
    "classify_waivers",
]
