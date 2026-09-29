"""milestone: the record-path row geometry, with six sections Tab cycles.

A subject other than the fixture's own milestone renders its stored record or states the
absence.

When the console holds the Milestone's read model it draws that instead: the rows the
daemon projected at one committed cursor, the sealed bundle those rows were accepted at,
the approval bound to the exact head that bundle names, and the journey's criteria. A
bundle nobody sealed and an approval nobody gave are said to be absent rather than
drawn from the prototype registers, which would be the frame borrowing a fixture's
acceptance for a real Milestone. Opened on one Milestone, the frame is about that record:
its Track, the Batches cut under it by their parent key with the caret on them, its
bundle and approval, and the section Tab has focused.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from eawf.kernel.projection.route_view import RouteRecord
from eawf.kernel.state.epoch2.batch import BatchStatus
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.fixture import Milestone, Track
from eawf.surfaces.tui.console.frame import (
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
from eawf.surfaces.tui.console.keybar import KEY, ROUTE_KEYS
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.registry import SECTIONS
from eawf.surfaces.tui.console.renderers.children import (
    child_cursor,
    child_rows,
    children,
    lrow,
    named,
    reference,
    status,
)
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    counts,
    crumb,
    native,
    native_head,
    record_rows,
    restore,
    route_crumb,
    unstated_rows,
)
from eawf.surfaces.tui.console.renderers.spine import finished_rows, finished_subject
from eawf.surfaces.tui.console.width import cell_len, pad
from eawf.workflow.projection.acceptance import AcceptanceBundleView

OWN = pt.OWN_MILESTONE
_UNAVAILABLE = "∅ unavailable"

#: What the bundle row says when no acceptance bundle is held for the Milestone.
NO_BUNDLE = "∅ no acceptance bundle is sealed for this Milestone"

#: What the approval row says when no approval binds the held bundle. An approval given
#: to other bytes is not this bundle's, so it reads here as no approval at all.
NO_APPROVAL = "∅ no approval is bound to this bundle"

#: What the criteria read when no bundle is held: there are no steps to list, and saying
#: the held bundle states none would claim a bundle that is not there.
NO_BUNDLE_CRITERIA = "∅ no bundle is held, so no step is listed"

#: What the criteria read when a held bundle lists no step.
NO_STEP = "∅ the held bundle states no step"


def _no_criteria(model: AcceptanceBundleView) -> str:
    """Return why no criterion is listed: no bundle is held, or the held one lists none."""
    return NO_BUNDLE_CRITERIA if model.bundle_revision is None else NO_STEP


#: The criteria table of the native frame: the step, its verdict, then what it showed.
_CRITERIA_TABLE = Table([16, 8, 0], 2)
_CRITERIA: tuple[tuple[str, str], ...] = (
    ("Every escape has an authority reference.", "Receipt EVT-2201"),
    ("No escape bypasses the ledger.", "Receipt EVT-2204"),
    ("Replay of the ledger is exact.", "Receipt EVT-2209"),
    ("The audit sign-off is recorded.", "≠ Denied to your class"),
)
# The two rows each section other than the glance and the try section shows.
_SECTION_ROWS: Mapping[str, tuple[str, str, str]] = MappingProxyType(
    {
        "changes": (
            "CHANGES",
            "2 reverted · both re-landed · 1 repair requested",
            "No change is hidden; a revert keeps its own receipt.",
        ),
        "evidence": (
            "EVIDENCE",
            "EVT-2201 · EVT-2204 · EVT-2209",
            "Each receipt is quotable at this exact digest.",
        ),
        "risks": (
            "RISKS",
            "1 open · repair requested once, resolved",
            "Resolved history is kept, not hidden.",
        ),
        "raw": (
            "RAW",
            "The sealed bundle as stored, bounded and scrubbed.",
            "Raw is for quoting a runner, never for learning state.",
        ),
    }
)


def _lrow(label: str, value: str, cur: bool = False) -> str:
    return "  " + pad(label, 11) + ("▸ " if cur else "  ") + value


def _section(sec: str, mid: str, w: int) -> list[str]:
    if sec == "try":
        return [
            _lrow("TRY", f"eawf milestone try {mid}"),
            _lrow("", "Runs the accepted bundle locally against this digest."),
        ]
    if sec != "glance":
        label, first, second = _SECTION_ROWS[sec]
        return [_lrow(label, first), _lrow("", second)]
    width = max(cell_len(c[0]) for c in _CRITERIA) + 2
    return [
        _lrow("GLANCE", "PROMISED   Every escape is recorded with its authority."),
        _lrow("", "VERDICT    3 of 4 criteria proven · 1 open"),
        thin(w),
        _lrow("CRITERIA", pad("CRITERION", width) + "PROOF"),
        *(_lrow("", pad(claim, width) + proof) for claim, proof in _CRITERIA),
    ]


def _absent_rows(mid: str, found: tuple[Track, Milestone] | None, w: int) -> list[str]:
    track, milestone = found if found else (None, None)
    return [
        thin(w),
        _lrow("TRACK", track.id if track else _UNAVAILABLE),
        _lrow("STATE", milestone.state if milestone else _UNAVAILABLE),
        _lrow("SCOPE", (track.prog if track else _UNAVAILABLE) + " on this track"),
        thin(w),
        f" ∅ No bundle, glance or criteria is recorded for {mid}.",
        "   The roadmap carries its state; nothing deeper is held for it.",
    ]


def short_digest(digest: str) -> str:
    """Return a digest as the frame prints it: its first four and last three hex cells.

    The whole digest is what ``y`` copies; the frame only has to tell two apart.
    """
    hexa = digest.removeprefix("sha256:")
    return f"{hexa[:4]}…{hexa[-3:]}"


def _bundle_text(model: AcceptanceBundleView) -> str:
    """Return the sealed bundle the rows were accepted at, or the honest absence."""
    if model.bundle_revision is None or model.bundle_digest is None:
        return NO_BUNDLE
    sealed = "" if model.sealed_at is None else f" · sealed {model.sealed_at:%Y-%m-%d %H:%M}"
    digest = short_digest(model.bundle_digest)
    return f"revision {model.bundle_revision}{sealed} · digest {digest}"


def _approval_text(model: AcceptanceBundleView) -> str:
    """Return the approval row, naming the exact head the approved digest was sealed on."""
    approval = model.approval
    if approval is None:
        return NO_APPROVAL
    return (
        f"head {approval.head_sha[:7]} · tree {approval.tree_sha[:7]} · "
        f"digest {short_digest(approval.approved_digest)} · by {approval.resolved_by}"
    )


def _criteria_rows(model: AcceptanceBundleView) -> list[str]:
    """Return the journey's criteria, one line each, under their own head."""
    proven, total = model.proven(), len(model.criteria)
    head = f" CRITERIA  {proven} of {total} proven · {len(model.blocking())} open"
    rows = [head if model.criteria else f" CRITERIA  {_no_criteria(model)}"]
    if not model.criteria:
        return rows
    rows.append(_CRITERIA_TABLE.head(["STEP", "VERDICT", "SHOWED"]))
    rows.extend(
        _CRITERIA_TABLE.row(
            [
                row.step_id,
                "proven" if row.passed else "open",
                " · ".join([row.observation, *row.evidence_keys]),
            ]
        )
        for row in model.criteria
    )
    return rows


def _tabs(sec: str) -> str:
    """Return the section strip, the focused section bracketed, so Tab's focus is visible.

    The strip marks a section, not the row the arrows walk, so it draws no caret: a caret
    would read, and be grounded, as the cursor.
    """
    return " ".join(f"[{x}]" if x == sec else f" {x} " for x in SECTIONS)


def _section_rows(sec: str, model: AcceptanceBundleView) -> list[str]:
    """Return the focused section: the journey's criteria at a glance, else what is stated."""
    if sec != "glance":
        return [lrow(sec.upper(), f"{UNKNOWN_WORD} · no producer states this section yet")]
    if not model.criteria:
        return [lrow("CRITERIA", _no_criteria(model))]
    proven, blocking = model.proven(), len(model.blocking())
    return [
        lrow("CRITERIA", f"{proven} of {len(model.criteria)} proven · {blocking} open"),
        *(
            lrow("", " · ".join([row.step_id, verdict, row.observation, *row.evidence_keys]))
            for row in model.criteria
            for verdict in ("proven" if row.passed else "open",)
        ),
    ]


def _keys(view: View, model: AcceptanceBundleView) -> str:
    """Return the native keybar; with no bundle held there is no digest for ``y`` to copy."""
    table = native_keys("milestone", windowed=view.session.windowed)
    if model.bundle_digest is None:
        table = tuple(entry for entry in table if entry != KEY["digest"])
    return route_keys_bar(view, table)


def milestone_frame(view: View, model: AcceptanceBundleView, subject: RouteRecord) -> list[str]:
    """Return one Milestone's frame: its Track and Batches, bundle, approval and section.

    Args:
        view: The render being built; its session carries the section and the caret.
        model: The Milestone's read model, holding the Batches cut under each Milestone.
        subject: The Milestone the frame is about.

    Returns:
        The full frame, keybar last.
    """
    session, w = view.session, view.w
    sec = SECTIONS[session.section % len(SECTIONS)]
    batches = children(model.rows, subject.key, Epoch2Collection.BATCH)
    done = sum(1 for batch in batches if status(batch) == BatchStatus.COMPLETED.value)
    track = subject.parent_key
    done_with = finished_subject(session, model)
    final = " · final" if done_with is not None else ""
    top = native_head(
        view,
        model,
        crumb_text=route_crumb(view, model, *([track] if track else []), subject.key),
        summary=f"Milestone {named(subject)} · {status(subject)}{final}",
        terminal=done_with is not None,
    )
    if done_with is not None:
        top += [*finished_rows(session.route, done_with, lrow), thin(w)]
    top.append(lrow("TRACK", track or "∅ filed under no Track"))
    below = [
        lrow("BUNDLE", _bundle_text(model)),
        lrow("APPROVAL", _approval_text(model)),
        lrow("BUILT", f"{dv.plural(len(batches), 'batch', 'es')} · {done} completed"),
        lrow("", _tabs(sec)),
        thin(w),
        *_section_rows(sec, model),
    ]
    cursor = child_cursor(session, [batch.key for batch in batches], subject=subject.key)
    listed = child_rows(
        view,
        "BATCHES",
        [reference(batch) for batch in batches],
        cursor,
        chrome=len(top) + len(below),
        empty="∅ no Batch is cut under this Milestone",
    )
    if not batches:
        # Enter on the Milestone itself opens its acceptance evidence, so it still acts
        session.nav_rows = None
    return build(
        view,
        [*top, *listed, *below],
        _keys(view, model),
    )


def native_frame(view: View, model: AcceptanceBundleView) -> list[str]:
    """Return the Milestone's frame, drawn from the read model the daemon served.

    Args:
        view: The render being built; its session carries the cursor and the selection.
        model: The Milestone's read model at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    session, w = view.session, view.w
    found = model.index_of(session.subj_id)
    if found is not None:
        return milestone_frame(view, model, model.rows[found])
    cursor = restore(session, model)
    rows: list[str] = [
        header_row(
            session,
            crumb=crumb(view, model),
            scope=scope_label(view, model.scope_id),
            needs=needs_count(view),
            w=w,
        ),
        " " + counts(model),
        bar(w),
        _lrow("BUNDLE", _bundle_text(model)),
        _lrow("APPROVAL", _approval_text(model)),
        thin(w),
    ]
    below = [thin(w), *_criteria_rows(model), thin(w), *unstated_rows(model)]
    rows.extend(record_rows(view, model, cursor, above=len(rows), below=len(below)))
    rows.extend(below)
    return build(view, rows, _keys(view, model))


def render(view: View) -> list[str]:
    """Return the Milestone frame, native when its read model is held."""
    model = native(view)
    if isinstance(model, AcceptanceBundleView):
        return native_frame(view, model)
    s, fx, w = view.session, view.fixture, view.w
    sec = SECTIONS[s.section % len(SECTIONS)]
    mid = dv.subj_of(s, OWN)
    found = dv.ms_of(fx, mid)
    track_name = found[0].id if found else (dv.field_of(fx, mid, "TRACK") or "∅")
    facts = [("NAME", "field"), ("STATE", "field")]
    subject = (
        f"{found[1].name} · {found[1].state}"
        if found
        else (dv.subj_facts(fx, mid, facts) or "∅ track and state unavailable")
    )
    tabs = " ".join(("▸" if x == sec else " ") + x for x in SECTIONS)
    rows = [
        header(view, f" Eä ▸ … ▸ {track_name} ▸ {mid}"),
        f" Milestone {mid} {subject}",
        bar(w),
        _lrow("TRACK", track_name),
        _lrow("BUNDLE", "sealed 14:01 · digest 7c1f…a94 · immutable"),
        _lrow("", tabs),
        thin(w),
    ]
    batches = dv.children_of(fx, mid, "BAT-", "MILESTONE")
    dv.sel_in(s, len(batches))
    for ix, batch in enumerate(batches):
        rows.append(_lrow("" if ix else "BATCHES", dv.ref_text(fx, batch), ix == s.sel))
    if not batches:
        state = found[1].state if found else (dv.field_of(fx, mid, "STATE") or "")
        rows.append(
            _lrow("BATCHES", f"∅ none cut · {state.lower().replace('_', ' ')}, nothing built yet")
        )
    dv.publish_nav(s, batches)
    rows.append(_lrow("BUILT", dv.built_of(fx, mid).text))
    rows.append(thin(w))
    rows.extend(_section(sec, mid, w))
    keys = route_keys_bar(view, ROUTE_KEYS["milestone"])
    if not dv.own_body(s, OWN):
        record = fx.record(mid)
        if record:
            body = dv.record_body(s, fx, record, entity_id=mid, subject=rows[1])
            return build(view, rows[:3] + body, route_keys_bar(view, ROUTE_KEYS["milestone"]))
        s.absent = True
        rows = rows[:3] + _absent_rows(mid, found, w)
    dv.sel_in(s, 0)
    return build(view, rows, keys)
