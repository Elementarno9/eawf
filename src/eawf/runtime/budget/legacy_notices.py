"""Reinterpreting the legacy over-budget pauses as budget notices.

The detector used to raise a durable pause each time a wave crossed an
advisory band -- ``warn`` at eighty percent of its estimate, ``err`` past
it, ``backstop`` for a wave with no estimate -- and the operator surface
opened each one as a modal. Those rows are not deleted. Each group of them
that describes one claim of one wave collapses into one estimate-basis
notice at the highest band it observed, carrying every pause it came from
as provenance.

Only the bands a legacy row recorded are used, which is why the
approaching band still exists: a ``warn`` row is the one thing that can
reach it, and a later ``err`` row for the same claim escalates that same
notice. A legacy advisory never implies a hard exhaustion, and nothing the
old surface kept only in a session -- a dismissed modal -- becomes a
durable acknowledgement, because no durable record of it exists.

The import reads nothing but its input and its result depends on nothing
else, so running it again over the same rows yields the same notices, and
a notice already on file is never overwritten by a second import.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from eawf.kernel.store.kinds.event import EventPayload
from eawf.runtime.budget.notices import (
    LOCAL_OPERATOR,
    BudgetCrossing,
    BudgetNoticeLedger,
    BudgetThresholdNotice,
    NoticeBand,
    NoticeHistoryEntry,
    RecipientState,
    apply_crossing,
    budget_window_digest,
    rewrite_ledger,
)

logger = logging.getLogger(__name__)

#: ``event_type`` of a legacy pause row in the epoch-1 event store.
PAUSE_EVENT_TYPE: Final = "needs_user_pause"

#: ``event_type`` of a legacy resume row, which answers the pause it names.
RESUME_EVENT_TYPE: Final = "needs_user_resume"

#: The resume choice the wave-close path stamped when it retracted a wave's advisories.
AUTO_RESOLVED_CHOICE: Final = "auto-resolved"

#: The event kind every legacy over-budget pause row carries.
LEGACY_ADVISORY_KIND: Final = "stale_wave_detected"

#: The notice band each legacy advisory band collapses onto. A row from
#: before the bands were recorded was the one flat over-budget alarm.
_LEGACY_BANDS: Final[Mapping[str | None, NoticeBand]] = {
    "warn": "approaching",
    "err": "limit_reached",
    "backstop": "limit_reached",
    None: "limit_reached",
}


def _seconds(minutes: object) -> int | None:
    """Return *minutes* as whole seconds, or ``None`` when no number was recorded."""
    if isinstance(minutes, bool) or not isinstance(minutes, int | float):
        return None
    return round(minutes * 60)


def _crossing(payload: EventPayload) -> BudgetCrossing | None:
    """Return the crossing one legacy pause row records, or ``None`` for another row.

    A row that recorded no elapsed time is left a pause: a notice needs an
    observed value, and inventing one would claim a measurement never made.
    """
    if payload.event_type != PAUSE_EVENT_TYPE or payload.event_kind != LEGACY_ADVISORY_KIND:
        return None
    wave_id, pause_urn = payload.extras.get("wave_id"), payload.extras.get("pause_urn")
    elapsed = _seconds(payload.extras.get("elapsed_minutes"))
    if not isinstance(wave_id, str) or not wave_id or not isinstance(pause_urn, str):
        return None
    if elapsed is None:
        return None
    band = payload.extras.get("advisory_band")
    session = payload.extras.get("session")
    return BudgetCrossing(
        scope_id=wave_id,
        axis="wall_seconds",
        basis="estimate",
        band=_LEGACY_BANDS.get(band if isinstance(band, str) else None, "limit_reached"),
        observed_value=elapsed,
        budget_value=_seconds(payload.extras.get("budget_minutes")),
        observed_at=payload.timestamp,
        contract_digest=budget_window_digest(session if isinstance(session, str) else ""),
        audience=(LOCAL_OPERATOR,),
        provenance=(pause_urn,),
    )


def _disposed(
    notice: BudgetThresholdNotice,
    *,
    resumes: Mapping[str, EventPayload],
    closed_waves: frozenset[str],
) -> BudgetThresholdNotice:
    """Return *notice* with the disposition its legacy rows establish.

    Every pause answered and at least one by an operator is a resolution.
    Every pause retracted by the system, or a subject that closed, is a
    clearing -- not a resolution nobody gave. Anything else stays open.
    The one operator is marked delivered at the imported revision, so the
    import itself delivers nothing.
    """
    answered = [resumes[urn] for urn in notice.provenance if urn in resumes]
    by_operator = [row for row in answered if row.extras.get("choice") != AUTO_RESOLVED_CHOICE]
    every_pause_answered = len(answered) == len(notice.provenance)
    update: dict[str, object] = {
        "recipients": {LOCAL_OPERATOR: RecipientState(delivered_revision=notice.revision)}
    }
    if every_pause_answered and by_operator:
        at = max(row.timestamp for row in by_operator)
        choices = sorted({str(row.extras.get("choice")) for row in by_operator})
        update |= {
            "status": "RESOLVED",
            "resolved_at": max(at, notice.last_observed_at),
            "history": (
                NoticeHistoryEntry(
                    action="resolved",
                    revision=notice.revision,
                    at=max(at, notice.last_observed_at),
                    reason=f"legacy resume: {', '.join(choices)}",
                ),
            ),
        }
    elif every_pause_answered or notice.scope_id in closed_waves:
        at = max((row.timestamp for row in answered), default=notice.last_observed_at)
        at = max(at, notice.last_observed_at)
        update |= {
            "status": "CLEARED",
            "resolved_at": at,
            "history": (
                NoticeHistoryEntry(
                    action="cleared",
                    revision=notice.revision,
                    at=at,
                    reason="legacy subject closed without an operator resume",
                ),
            ),
        }
    return notice.model_copy(update=update)


def import_legacy_advisories(
    payloads: Iterable[EventPayload], *, closed_waves: frozenset[str]
) -> BudgetNoticeLedger:
    """Collapse the legacy over-budget pause rows in *payloads* into notices.

    Rows are grouped by wave and claim session and folded in time order, so
    a group escalates one notice monotonically and a late lower band never
    regresses it.

    Args:
        payloads: Every event payload of the legacy event store.
        closed_waves: The waves that reached a terminal status.

    Returns:
        A ledger holding one notice per group, keyed and ordered by notice
        key; empty when no legacy advisory exists.

    Raises:
        pydantic.ValidationError: A legacy row carries a value a notice
            cannot hold.
    """
    crossings: list[BudgetCrossing] = []
    resumes: dict[str, EventPayload] = {}
    for payload in payloads:
        if payload.event_type == RESUME_EVENT_TYPE:
            urn = payload.extras.get("pause_urn")
            if isinstance(urn, str):
                resumes.setdefault(urn, payload)
            continue
        crossing = _crossing(payload)
        if crossing is not None:
            crossings.append(crossing)
    ledger = BudgetNoticeLedger()
    for crossing in sorted(crossings, key=lambda row: (row.observed_at, row.provenance)):
        ledger, _upsert = apply_crossing(ledger, crossing)
    return BudgetNoticeLedger(
        notices={
            key: _disposed(ledger.notices[key], resumes=resumes, closed_waves=closed_waves)
            for key in sorted(ledger.notices)
        }
    )


def merge_imported(
    ledger: BudgetNoticeLedger, imported: BudgetNoticeLedger
) -> tuple[BudgetNoticeLedger, int]:
    """Add every imported notice *ledger* does not already hold.

    A notice already on file keeps whatever its recipients did to it since
    the first import, so a re-run cannot undo a disposition.

    Args:
        ledger: The ledger on file.
        imported: The notices :func:`import_legacy_advisories` produced.

    Returns:
        The ledger to keep -- *ledger* itself when nothing was new -- and
        how many notices were added.
    """
    added = {key: row for key, row in imported.notices.items() if key not in ledger.notices}
    if not added:
        return ledger, 0
    merged = {**ledger.notices, **added}
    return BudgetNoticeLedger(notices={key: merged[key] for key in sorted(merged)}), len(added)


def import_legacy_notices(
    notices_file: Path, payloads: Iterable[EventPayload], *, closed_waves: frozenset[str]
) -> int:
    """Import the legacy over-budget pauses into the notice ledger at *notices_file*.

    Args:
        notices_file: The notice ledger file.
        payloads: Every event payload of the legacy event store.
        closed_waves: The waves that reached a terminal status.

    Returns:
        How many notices the import added.

    Raises:
        eawf.runtime.lock.portalock.LockTimeout: The ledger lock was not
            acquired in time.
        pydantic.ValidationError: The ledger on disk is corrupt.
    """
    imported = import_legacy_advisories(payloads, closed_waves=closed_waves)
    if not imported.notices:
        return 0
    added = rewrite_ledger(notices_file, lambda ledger: merge_imported(ledger, imported))
    logger.info(f"import_legacy_notices imported={len(imported.notices)} added={added}")
    return added


__all__ = [
    "LEGACY_ADVISORY_KIND",
    "import_legacy_advisories",
    "import_legacy_notices",
    "merge_imported",
]
