"""Budget notices on the Attention route: listed beside the actions, snoozed or resolved.

A budget notice is not a pending action. It blocks nothing and needs no answer, so it is
listed under its own bucket after the actions, and the only verbs it takes are a snooze,
which keeps it out of this principal's inbox for a while, and a resolve, which closes it
for its whole audience. Either is previewed on the consequence card first and sent to the
notice ledger's disposition verb at the revision the operator was shown; neither stops,
extends or restarts the work the notice describes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from eawf.kernel.projection.attention import AttentionBucket
from eawf.kernel.projection.compute import ProjectionRow
from eawf.kernel.projection.read_models import NoticeDetailView
from eawf.kernel.projection.truth import TruthState
from eawf.runtime.budget.notices import BudgetThresholdNotice
from eawf.surfaces.tui.console.navigation import open_overlay

if TYPE_CHECKING:
    from eawf.surfaces.tui.console.navigation import Ctx

#: The group heading the notices are listed under: the bucket a budget notice lands in.
NOTICE_BUCKET: Final = AttentionBucket.OVER_BUDGET.value.upper()

#: The verbs a notice takes, by key, and the disposition each records.
NOTICE_VERBS: Final = {"z": "snooze", "v": "resolve"}

#: Why a notice refuses an answer or a denial: it asks nothing.
NOTHING_TO_ANSWER: Final = "a notice has nothing to {verb} — snooze or resolve it"


def notice_of(
    notices: Sequence[BudgetThresholdNotice], key: str | None
) -> BudgetThresholdNotice | None:
    """Return the held notice ``key`` names, or ``None`` when it names none."""
    return next((notice for notice in notices if notice.notice_key == key), None)


def short_key(notice: BudgetThresholdNotice) -> str:
    """Return the notice's key as a row shows it: the first eight digits of its digest.

    A notice is keyed by a digest of the condition it names, too long for a key column;
    eight digits tell the notices of one inbox apart.
    """
    return notice.notice_key.removeprefix("sha256:")[:8]


def notice_cells(notice: BudgetThresholdNotice) -> list[str]:
    """Return the notice's cells in the Attention table: key, what crossed, status, due."""
    crossed = f"{notice.scope_id} {notice.axis} {notice.highest_band.replace('_', ' ')}"
    return [short_key(notice), crossed, notice.status, "due –"]  # noqa: RUF001


def notice_detail(notice: BudgetThresholdNotice) -> str:
    """Return the selected notice's second line: what it measured, and that it blocks nothing."""
    budget = notice.budget_value if notice.budget_value is not None else "?"
    return (
        f"budget notice · {notice.observed_value} of {budget} {notice.axis} · "
        f"revision {notice.revision} · blocks nothing"
    )


def _disposition(notice: BudgetThresholdNotice, principal: str | None) -> str | None:
    """Return where ``principal`` stands with ``notice``, newest disposition first."""
    state = notice.recipients.get(principal) if principal is not None else None
    if state is None:
        return None
    if state.acknowledged_revision is not None:
        return f"acknowledged revision {state.acknowledged_revision}"
    if state.snoozed_until is not None:
        return f"snoozed until {state.snoozed_until.isoformat()}"
    if state.seen_revision is not None:
        return f"seen revision {state.seen_revision}"
    if state.delivered_revision is not None:
        return f"delivered revision {state.delivered_revision}"
    return None


def detail_view(
    notice: BudgetThresholdNotice, *, principal: str | None, rows: Sequence[ProjectionRow]
) -> NoticeDetailView:
    """Return the notice detail of ``notice``, for the principal the console acts as.

    The Run a notice's scope names is read from the rows the link holds; a Run fact the
    held rows do not state stays unknown.

    Args:
        notice: The held notice.
        principal: Who the console acts as; ``None`` when nobody.
        rows: Every row the link's held projections carry.
    """
    run_key = notice.scope_id.rsplit("/", 1)[-1]
    run = next((row for row in rows if row.key == run_key), None) if "RUN-" in run_key else None
    status = run.status if run is not None else None
    return NoticeDetailView(
        notice_ref=notice.notice_key,
        basis=notice.basis,
        axis=notice.axis,
        threshold=notice.highest_band,
        observed_value=notice.observed_value,
        budget_value=notice.budget_value,
        measurement_quality=None,
        provenance_event_refs=tuple(notice.provenance),
        run_ref=run.urn if run is not None else (run_key if "RUN-" in run_key else None),
        run_owner=None,
        run_state=(
            str(status.value) if status is not None and status.state is TruthState.KNOWN else None
        ),
        last_progress_at=None,
        last_heartbeat_at=None,
        history=tuple(
            f"{entry.revision} · {entry.action} · {entry.principal or 'imported'}"
            for entry in notice.history
        ),
        my_disposition=_disposition(notice, principal),
    )


def notice_verb(ctx: Ctx, key: str) -> bool:
    """Preview a snooze or resolve of the notice the cursor is on; refuse an answer to it.

    Args:
        ctx: The keystroke's context, carrying the notices the link holds.
        key: The attention verb letter pressed.

    Returns:
        Whether the cursor is on a held notice, so the key was claimed here.
    """
    s = ctx.s
    notice = notice_of(ctx.notices, s.sel_id)
    if notice is None:
        return False
    if key not in NOTICE_VERBS:
        verb = "answer" if key == "a" else "deny"
        ctx.log(key, NOTHING_TO_ANSWER.format(verb=verb))
        return True
    s.verb = key
    s.c_target = None
    open_overlay(s, "consequence", subject=notice.notice_key)
    ctx.log(key, f"{NOTICE_VERBS[key]} → consequence preview first")
    return True


__all__ = [
    "NOTHING_TO_ANSWER",
    "NOTICE_BUCKET",
    "NOTICE_VERBS",
    "detail_view",
    "notice_cells",
    "notice_detail",
    "notice_of",
    "notice_verb",
    "short_key",
]
