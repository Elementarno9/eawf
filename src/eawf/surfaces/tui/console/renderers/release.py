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

from eawf.kernel.projection.route_view import RouteRecord
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.spec.release import ReleaseStatus
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.frame import (
    TABLES,
    Fixed,
    Table,
    View,
    bar,
    build,
    header,
    needs_count,
    route_keys_bar,
    scope_label,
    thin,
)
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    counts,
    label,
    native,
    route_crumb,
    unstated_rows,
)
from eawf.surfaces.tui.console.width import pad
from eawf.workflow.projection.acceptance import ReleaseReadinessView

#: The one release the prototype registers record.
OWN = "REL-0001"
MEMBERSHIP = "MEMBERSHIP"
READINESS = "READINESS"

#: What the approval row says when no approval is bound to the candidate.
NO_APPROVAL = "∅ no approval is bound to this candidate"

#: What the frame says when the read model holds no Release row.
NO_RELEASE = "no release is cut"

#: What the publication row says when no release is cut.
NOTHING_TO_PUBLISH = f"∅ nothing to publish · {NO_RELEASE}"

#: What the publication row says of a release that has not reached publishing.
NOT_PUBLISHED = "Not started — nothing has been published."

# The states a release holds before any external effect can have started.
_BEFORE_PUBLICATION = frozenset(
    {
        ReleaseStatus.DRAFT,
        ReleaseStatus.CANDIDATE,
        ReleaseStatus.PREFLIGHT_FAILED,
        ReleaseStatus.APPROVED,
    }
)

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


def release_row(model: ReleaseReadinessView) -> RouteRecord | None:
    """Return the Release row the read model holds, or ``None`` when no release is cut."""
    return next((row for row in model.rows if row.collection is Epoch2Collection.RELEASE), None)


def _status(row: RouteRecord) -> ReleaseStatus | None:
    """Return the lifecycle state the Release row states, or ``None`` when it states none."""
    field = row.field("status")
    value = (field.value or "").lower() if field.state is TruthState.KNOWN else ""
    return ReleaseStatus(value) if value in ReleaseStatus._value2member_map_ else None


def publication_text(status: ReleaseStatus | None) -> str:
    """Return the publication row: what the Release's own state says about publishing.

    A release that has not reached publishing has published nothing, which its state
    alone proves; a state past that is named, and a release stating no state says so.
    """
    if status is None:
        return f"{UNKNOWN_WORD} · the release states no status"
    if status in _BEFORE_PUBLICATION:
        return NOT_PUBLISHED
    if status is ReleaseStatus.CANCELLED:
        return "Never — the release was cancelled before any external effect."
    return status.value.replace("_", " ")


def native_frame(view: View, model: ReleaseReadinessView) -> list[str]:
    """Return the Release's frame, drawn from the read model the daemon served.

    With no Release row held nothing is a member of anything, so the frame says no release
    is cut rather than listing the tree's Milestones as its membership.

    Args:
        view: The render being built; its session carries both regions' cursors.
        model: The candidate's read model at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    session, w = view.session, view.w
    release = release_row(model)
    status = _status(release) if release is not None else None
    members = (
        [row for row in model.rows if row.collection is Epoch2Collection.MILESTONE]
        if release is not None
        else []
    )
    dv.sel_in(session, len(members))
    # the membership row under the caret is what Enter drills, by its own id
    session.sel_id = members[session.sel].key if members else None
    on_readiness = (session.rel_reg or MEMBERSHIP) == READINESS
    session.rel_sel = max(0, min(len(model.signals) - 1, session.rel_sel))
    if on_readiness:
        # the arrows walk the region in focus, so the bar offers them for its signals
        session.nav_rows = len(model.signals)
    leaf = session.subj_id or (release.key if release is not None else "Release")
    if release is not None:
        state = status.value.upper() if status is not None else UNKNOWN_WORD
        title = f" {release.title}" if release.title else ""
        subject = f"Release {release.key}{title} · {state}"
    else:
        subject = f"{NO_RELEASE} · {counts(model)}"
    table = TABLES["MB"]
    rows: list[str] = [
        header_row(
            session,
            crumb=route_crumb(view, model, leaf),
            scope=scope_label(view, model.scope_id),
            needs=needs_count(view),
            w=w,
        ),
        " " + subject,
        bar(w),
        table.head([MEMBERSHIP, "TRACK", "ACCEPTED"]),
    ]
    if release is None:
        rows.append(f"   {NO_RELEASE} · a release lists the accepted Milestones it ships")
    elif not members:
        rows.append(NO_MEMBERS)
    for index, row in enumerate(members):
        picked = not on_readiness and index == session.sel
        name = f"{row.key} {row.title}" if row.title else row.key
        track = row.parent_key or value_cell(row.field("track")).slot
        line = table.row([name, track, value_cell(row.field("accepted")).slot], picked)
        rows.append(line if picked else Fixed(pad(line, w)))
    readiness = Table([34, 11, 0], 2)
    rows.append(thin(w))
    rows.append(readiness.head([READINESS, "STATE", "EVIDENCE"]))
    for index, signal in enumerate(model.signals):
        picked = on_readiness and index == session.rel_sel
        # the reason an unknown signal has no state is its evidence, so it is never cut
        evidence = signal.evidence or signal.state.missing_reason or ""
        known = signal.state.state is TruthState.KNOWN
        state = value_cell(signal.state).slot if known else UNKNOWN_WORD
        line = readiness.row([signal.name, state, evidence], picked)
        rows.append(line if picked else Fixed(pad(line, w)))
    rows.append(thin(w))
    rows.append(label("APPROVAL", _approval_text(model)))
    rows.append(
        label(
            "PUBLICATION",
            publication_text(status)
            if release is not None
            else f"∅ nothing to publish · {NO_RELEASE}",
        )
    )
    if members:
        rows.append(thin(w))
        rows.extend(unstated_rows(model))
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["release"]))


def render(view: View) -> list[str]:
    """Return the Release frame, native when its read model is held."""
    model = native(view)
    if isinstance(model, ReleaseReadinessView):
        return native_frame(view, model)
    s, fx, w = view.session, view.fixture, view.w
    if s.subj_id and s.subj_id != OWN:
        # a release the prototype does not record is said to be absent, never swapped
        rows = [header(view, f" Eä ▸ {fx.scope} ▸ {s.subj_id}"), f" Release {s.subj_id}", bar(w)]
        rows = dv.absent(s, fx, rows, entity_id=s.subj_id, what="membership or readiness", w=w)
        return build(view, rows, route_keys_bar(view, ROUTE_KEYS["release"]))
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
