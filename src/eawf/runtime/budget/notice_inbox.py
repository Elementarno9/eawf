"""Delivering budget notices to their recipients and recording what each did.

A notice's status and a recipient's delivery state are different things.
The status belongs to the notice and changes for everyone at once, which
only a resolution does. Delivery, opening, acknowledgement and snooze each
belong to one recipient and are recorded against the revision that
recipient was shown, so another recipient's inbox never moves because of
them, and a restart reads the recorded delivery rather than delivering the
same revision again.

Every write names the revision it acts on. A recipient acting on a
revision the notice has since escalated past is refused with the current
revision, so the client reloads rather than acknowledging something it
never saw.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from eawf.runtime.budget.notices import (
    BudgetNoticeLedger,
    BudgetThresholdNotice,
    NoticeAction,
    NoticeHistoryEntry,
    RecipientState,
    rewrite_ledger,
)

logger = logging.getLogger(__name__)

#: What a recipient may do to a notice.
NoticeDisposition = Literal["open", "acknowledge", "snooze", "resolve"]

_HISTORY_ACTION: dict[NoticeDisposition, NoticeAction] = {
    "open": "seen",
    "acknowledge": "acknowledged",
    "snooze": "snoozed",
    "resolve": "resolved",
}


class NoticeDispositionError(ValueError):
    """A disposition was refused; the notice on file is unchanged."""


class StaleNoticeRevisionError(NoticeDispositionError):
    """A disposition named a revision the notice is no longer at.

    Attributes:
        current_revision: The revision the notice is at now.
    """

    def __init__(self, *, notice_key: str, expected: int, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__(
            f"notice {notice_key} is at revision {current_revision}, not {expected}; "
            "reload it before acting"
        )


@dataclass(frozen=True, slots=True)
class NoticeInbox:
    """One recipient's view of the notices addressed to them.

    Attributes:
        active: Open notices this recipient has not acknowledged at their
            current revision and has not snoozed.
        acknowledged: Open notices this recipient acknowledged at their
            current revision.
        history: Notices that reached a terminal status.
    """

    active: tuple[BudgetThresholdNotice, ...]
    acknowledged: tuple[BudgetThresholdNotice, ...]
    history: tuple[BudgetThresholdNotice, ...]


def _state(notice: BudgetThresholdNotice, principal: str) -> RecipientState:
    """Return *principal*'s state on *notice*, empty when nothing is recorded."""
    return notice.recipients.get(principal, RecipientState())


def _snoozed(state: RecipientState, now: datetime) -> bool:
    """Return whether a snooze is still in force at *now*."""
    return state.snoozed_until is not None and state.snoozed_until > now


def inbox_for(ledger: BudgetNoticeLedger, *, principal: str, now: datetime) -> NoticeInbox:
    """Return *principal*'s inbox over *ledger* at *now*.

    Args:
        ledger: The notice ledger.
        principal: The recipient.
        now: The reference time a snooze is judged against.

    Returns:
        The recipient's active, acknowledged and history notices, each in
        ledger-key order. A notice addressed to others appears nowhere.
    """
    active: list[BudgetThresholdNotice] = []
    acknowledged: list[BudgetThresholdNotice] = []
    history: list[BudgetThresholdNotice] = []
    for key in sorted(ledger.notices):
        notice = ledger.notices[key]
        if principal not in notice.audience:
            continue
        if notice.status != "OPEN":
            history.append(notice)
            continue
        state = _state(notice, principal)
        if _snoozed(state, now):
            continue
        if state.acknowledged_revision == notice.revision:
            acknowledged.append(notice)
        else:
            active.append(notice)
    return NoticeInbox(
        active=tuple(active), acknowledged=tuple(acknowledged), history=tuple(history)
    )


def _deliver(
    ledger: BudgetNoticeLedger, *, principal: str, now: datetime
) -> tuple[BudgetNoticeLedger, tuple[BudgetThresholdNotice, ...]]:
    """Mark every undelivered revision addressed to *principal* as delivered."""
    delivered: list[BudgetThresholdNotice] = []
    notices = dict(ledger.notices)
    for key in sorted(notices):
        notice = notices[key]
        state = _state(notice, principal)
        if (
            notice.status != "OPEN"
            or principal not in notice.audience
            or _snoozed(state, now)
            or state.delivered_revision == notice.revision
        ):
            continue
        marked = state.model_copy(update={"delivered_revision": notice.revision})
        notices[key] = notice.model_copy(
            update={"recipients": {**notice.recipients, principal: marked}}
        )
        delivered.append(notices[key])
    if not delivered:
        return ledger, ()
    return BudgetNoticeLedger(notices=notices), tuple(delivered)


def deliver_pending(
    path: Path, *, principal: str, now: datetime
) -> tuple[BudgetThresholdNotice, ...]:
    """Deliver to *principal* every open revision not yet delivered to them.

    The delivery is recorded before it is returned, so a client that
    reconnects, or a daemon that restarts, is handed nothing it was already
    handed: each revision reaches each recipient at most once.

    Args:
        path: The notice ledger file.
        principal: The recipient.
        now: The reference time a snooze is judged against.

    Returns:
        The notices delivered now, in ledger-key order.

    Raises:
        eawf.runtime.lock.portalock.LockTimeout: The ledger lock was not
            acquired in time.
        pydantic.ValidationError: The ledger on disk is corrupt.
    """
    delivered = rewrite_ledger(path, lambda ledger: _deliver(ledger, principal=principal, now=now))
    logger.info(f"deliver_pending principal={principal} delivered={len(delivered)}")
    return delivered


def _disposed(
    notice: BudgetThresholdNotice,
    *,
    principal: str,
    disposition: NoticeDisposition,
    at: datetime,
    snooze_until: datetime | None,
) -> BudgetThresholdNotice:
    """Return *notice* after *principal*'s *disposition* at its current revision."""
    entry = NoticeHistoryEntry(
        action=_HISTORY_ACTION[disposition], revision=notice.revision, at=at, principal=principal
    )
    history = (*notice.history, entry)
    if disposition == "resolve":
        return notice.model_copy(
            update={
                "status": "RESOLVED",
                "resolved_at": at,
                "resolved_by": principal,
                "history": history,
            }
        )
    state = _state(notice, principal)
    if disposition == "open":
        state = state.model_copy(update={"seen_revision": notice.revision})
    elif disposition == "acknowledge":
        state = state.model_copy(update={"acknowledged_revision": notice.revision})
    else:
        state = state.model_copy(update={"snoozed_until": snooze_until})
    return notice.model_copy(
        update={"recipients": {**notice.recipients, principal: state}, "history": history}
    )


def dispose_notice(
    path: Path,
    *,
    notice_key: str,
    principal: str,
    disposition: NoticeDisposition,
    expected_revision: int,
    at: datetime,
    snooze_until: datetime | None = None,
) -> BudgetThresholdNotice:
    """Record *principal*'s *disposition* of one notice.

    Opening marks the revision seen, acknowledging and snoozing affect this
    recipient alone, and resolving closes the notice for its whole audience
    and records who did it. None of them stops, extends or restarts the
    work the notice describes.

    Args:
        path: The notice ledger file.
        notice_key: The notice acted on.
        principal: The recipient acting.
        disposition: What they do.
        expected_revision: The revision they were shown.
        at: When they acted.
        snooze_until: When a snooze lapses; required for ``snooze`` and
            refused otherwise.

    Returns:
        The notice as recorded after the disposition.

    Raises:
        NoticeDispositionError: The notice does not exist, is addressed to
            others, is already terminal, or the snooze deadline is missing,
            misplaced or not in the future.
        StaleNoticeRevisionError: The notice is no longer at
            *expected_revision*.
        eawf.runtime.lock.portalock.LockTimeout: The ledger lock was not
            acquired in time.
    """
    if (disposition == "snooze") != (snooze_until is not None):
        raise NoticeDispositionError("snooze_until is given exactly when snoozing")
    if snooze_until is not None and snooze_until <= at:
        raise NoticeDispositionError("snooze_until must be after the disposition")

    def change(ledger: BudgetNoticeLedger) -> tuple[BudgetNoticeLedger, BudgetThresholdNotice]:
        notice = ledger.notices.get(notice_key)
        if notice is None:
            raise NoticeDispositionError(f"no notice {notice_key}")
        if principal not in notice.audience:
            raise NoticeDispositionError(f"notice {notice_key} is not addressed to {principal}")
        if notice.status != "OPEN":
            raise NoticeDispositionError(f"notice {notice_key} is already {notice.status}")
        if notice.revision != expected_revision:
            raise StaleNoticeRevisionError(
                notice_key=notice_key, expected=expected_revision, current_revision=notice.revision
            )
        updated = _disposed(
            notice, principal=principal, disposition=disposition, at=at, snooze_until=snooze_until
        )
        return BudgetNoticeLedger(notices={**ledger.notices, notice_key: updated}), updated

    notice = rewrite_ledger(path, change)
    logger.info(
        f"dispose_notice key={notice_key} principal={principal} disposition={disposition} "
        f"revision={notice.revision}"
    )
    return notice


__all__ = [
    "NoticeDisposition",
    "NoticeDispositionError",
    "NoticeInbox",
    "StaleNoticeRevisionError",
    "deliver_pending",
    "dispose_notice",
    "inbox_for",
]
