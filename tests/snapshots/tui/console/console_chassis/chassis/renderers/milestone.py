"""milestone: the record path's row geometry; six sections cycled by Tab; a subject other
than the fixture's own milestone renders its stored record or states the absence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import bar, bar_, build, header_row, thin
from ...chassis.keys import ROUTE_KEYS
from ...chassis.registry import SECTIONS
from ...chassis.width import cell_len, pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_OWN = "MLS-0004"


def _lrow(label: str, value: str, cur: bool = False) -> str:
    return "  " + pad(label, 11) + ("▸ " if cur else "  ") + value


def _crow(value: str) -> str:
    return _lrow("", value)


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    sec = SECTIONS[s.section % len(SECTIONS)]
    mid = dv.subj_of(s, _OWN)
    mh = dv.ms_of(fixture, mid)
    track_name = mh[0].id if mh else (dv.rec_field(fixture, mid, "TRACK") or "∅")
    subject = (
        f"{mh[1].name} · {mh[1].state}"
        if mh
        else (
            dv.subj_facts(fixture, mid, [("NAME", "field"), ("STATE", "field")])
            or "∅ track and state unavailable"
        )
    )
    rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ … ▸ {track_name} ▸ {mid}", w),
        f" Milestone {mid} {subject}",
        bar(w),
        _lrow("TRACK", track_name),
        _lrow("BUNDLE", "sealed 14:01 · digest 7c1f…a94 · immutable"),
        _crow(" ".join(("▸" + x if x == sec else " " + x) for x in SECTIONS)),
        thin(w),
    ]
    mkids = dv.children_of(fixture, mid, "BAT-", "MILESTONE")
    dv.sel_in(s, len(mkids))
    for ix, b in enumerate(mkids):
        rows.append(_lrow("" if ix else "BATCHES", dv.ref_text(fixture, b), ix == s.sel))
    if not mkids:
        state = (
            str(mh[1].state if mh else (dv.rec_field(fixture, mid, "STATE") or ""))
            .lower()
            .replace("_", " ")
        )
        rows.append(_lrow("BATCHES", f"∅ none cut · {state}, nothing built yet"))
    dv.publish_nav(s, list(mkids))
    rows.append(_lrow("BUILT", str(dv.built_of(fixture, mid)["text"])))
    rows.append(thin(w))
    if sec == "glance":
        crit = [
            ("Every escape has an authority reference.", "Receipt EVT-2201"),
            ("No escape bypasses the ledger.", "Receipt EVT-2204"),
            ("Replay of the ledger is exact.", "Receipt EVT-2209"),
            ("The audit sign-off is recorded.", "≠ Denied to your class"),
        ]
        cw = max(cell_len(c[0]) for c in crit) + 2
        rows.append(_lrow("GLANCE", "PROMISED   Every escape is recorded with its authority."))
        rows.append(_crow("VERDICT    3 of 4 criteria proven · 1 open"))
        rows.append(thin(w))
        rows.append(_lrow("CRITERIA", pad("CRITERION", cw) + "PROOF"))
        for c in crit:
            rows.append(_crow(pad(c[0], cw) + c[1]))
    elif sec == "try":
        rows.append(_lrow("TRY", f"eawf milestone try {mid}"))
        rows.append(_crow("Runs the accepted bundle locally against this digest."))
    elif sec == "changes":
        rows.append(_lrow("CHANGES", "2 reverted · both re-landed · 1 repair requested"))
        rows.append(_crow("No change is hidden; a revert keeps its own receipt."))
    elif sec == "evidence":
        rows.append(_lrow("EVIDENCE", "EVT-2201 · EVT-2204 · EVT-2209"))
        rows.append(_crow("Each receipt is quotable at this exact digest."))
    elif sec == "risks":
        rows.append(_lrow("RISKS", "1 open · repair requested once, resolved"))
        rows.append(_crow("Resolved history is kept, not hidden."))
    else:
        rows.append(_lrow("RAW", "The sealed bundle as stored, bounded and scrubbed."))
        rows.append(_crow("Raw is for quoting a runner, never for learning state."))
    if not dv.own_body(s, _OWN):
        rec = fixture.record(mid)
        if rec:
            rows = rows[:3] + dv.record_body(s, fixture, list(rec), mid, rows[1])
            return build(s, rows, bar_(s, ROUTE_KEYS["milestone"], w), w, h)
        s.absent = True
        rows = rows[:3] + [
            thin(w),
            "  " + pad("TRACK", 11) + "  " + (mh[0].id if mh else "∅ unavailable"),
            "  " + pad("STATE", 11) + "  " + (mh[1].state if mh else "∅ unavailable"),
            "  "
            + pad("SCOPE", 11)
            + "  "
            + (mh[0].prog if mh else "∅ unavailable")
            + " on this track",
            thin(w),
            f" ∅ No bundle, glance or criteria is recorded for {mid}.",
            "   The roadmap carries its state; nothing deeper is held for it.",
        ]
    dv.sel_in(s, 0)
    return build(s, rows, bar_(s, ROUTE_KEYS["milestone"], w), w, h)
