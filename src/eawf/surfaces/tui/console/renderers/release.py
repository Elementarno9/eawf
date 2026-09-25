"""release: the candidate's membership table over its readiness matrix.

Tab moves the cursor between the two regions, and Enter on a readiness row opens the
matrix overlay.

When the console holds the candidate's read model it draws that instead: the Milestones
the projection carried as the membership, the readiness signals the records in hand
state, and the approval bound to the exact head it was given on. A signal nothing
observes shows the unknown truth token naming why, because a readiness matrix that
renders an unobserved gate as passing is the one thing it must never do.
"""

from __future__ import annotations

from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.frame import (
    TABLES,
    Fixed,
    Table,
    View,
    bar,
    build,
    header,
    route_keys_bar,
    thin,
)
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.renderers.read_model import (
    cell,
    counts,
    crumb,
    native,
    unstated_rows,
)
from eawf.surfaces.tui.console.width import pad
from eawf.workflow.projection.acceptance import ReleaseReadinessView

MEMBERSHIP = "MEMBERSHIP"
READINESS = "READINESS"

#: What the approval row says when no approval is bound to the candidate.
NO_APPROVAL = "∅ no approval is bound to this candidate"

#: What the membership table says when the projection carried no Milestone.
NO_MEMBERS = "   this candidate carries no Milestone the read model renders"
_MEMBERS: tuple[tuple[str, str, str], ...] = pt.RELEASE_MEMBERS
SIGNALS: tuple[tuple[str, str, str], ...] = (
    ("acceptance complete", "no", "1 still in review"),
    ("policy gate", "passed", "receipt EVT-3301"),
    ("artifact build", "passed", "receipt EVT-3302"),
    ("approvals", "1 of 2", "owner sign-off open"),
)


def _approval_text(model: ReleaseReadinessView) -> str:
    """Return the approval row, naming the exact head the approved digest was sealed on."""
    approval = model.approval
    if approval is None:
        return NO_APPROVAL
    return (
        f"head {approval.head_sha} · {approval.milestone_key} revision "
        f"{approval.bundle_revision} · digest {approval.approved_digest}"
    )


def native_frame(view: View, model: ReleaseReadinessView) -> list[str]:
    """Return the Release's frame, drawn from the read model the daemon served.

    Args:
        view: The render being built; its session carries both regions' cursors.
        model: The candidate's read model at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    session, w = view.session, view.w
    members = [row for row in model.rows if row.collection is Epoch2Collection.MILESTONE]
    dv.sel_in(session, len(members))
    on_readiness = (session.rel_reg or MEMBERSHIP) == READINESS
    session.rel_sel = max(0, min(len(model.signals) - 1, session.rel_sel))
    table = TABLES["MB"]
    rows: list[str] = [
        header_row(session, crumb=crumb(view, model), scope=model.scope_id, needs=0, w=w),
        " " + counts(model),
        bar(w),
        table.head([MEMBERSHIP, "KIND", "STATUS"]),
    ]
    if not members:
        rows.append(NO_MEMBERS)
    for index, row in enumerate(members):
        picked = not on_readiness and index == session.sel
        line = table.row([row.key, row.collection.value, cell(row.field("status"))], picked)
        rows.append(line if picked else Fixed(pad(line, w)))
    readiness = Table([34, 11, 0], 2)
    rows.append(thin(w))
    rows.append(readiness.head([READINESS, "STATE", "EVIDENCE"]))
    for index, signal in enumerate(model.signals):
        picked = on_readiness and index == session.rel_sel
        line = readiness.row([signal.name, cell(signal.state), signal.evidence], picked)
        rows.append(line if picked else Fixed(pad(line, w)))
    rows.append(thin(w))
    rows.append(" APPROVAL  " + _approval_text(model))
    rows.append(thin(w))
    rows.extend(unstated_rows(model))
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["release"]))


def render(view: View) -> list[str]:
    """Return the Release frame, native when its read model is held."""
    model = native(view)
    if isinstance(model, ReleaseReadinessView):
        return native_frame(view, model)
    s, fx, w = view.session, view.fixture, view.w
    dv.sel_in(s, len(_MEMBERS))
    on_readiness = (s.rel_reg or MEMBERSHIP) == READINESS
    if on_readiness:
        s.rel_sel = max(0, min(len(SIGNALS) - 1, s.rel_sel))
    members = TABLES["MB"]
    rows = [
        header(view, f" Eä ▸ {fx.scope} ▸ REL-0001"),
        " Release REL-0001 v0.7.0-rc1 · CANDIDATE",
        bar(w),
        members.head(["MEMBERSHIP", "TRACK", "ACCEPTED"]),
    ]
    rows.extend(
        members.row(list(m), not on_readiness and i == s.sel) for i, m in enumerate(_MEMBERS)
    )
    readiness = Table([34, 11, 0], 2)
    rows.append(thin(w))
    rows.append(readiness.head(["READINESS", "STATE", "EVIDENCE"]))
    rows.extend(
        readiness.row(list(x), on_readiness and i == s.rel_sel) for i, x in enumerate(SIGNALS)
    )
    rows.extend(
        [
            thin(w),
            " APPROVAL  Granted at head 9d2b41c · still exact.",
            " PUBLICATION  Not started — nothing has been published.",
        ]
    )
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["release"]))
