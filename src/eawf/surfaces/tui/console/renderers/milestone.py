"""milestone: the record-path row geometry, with six sections Tab cycles.

A subject other than the fixture's own milestone renders its stored record or states the
absence.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.fixture import Milestone, Track
from eawf.surfaces.tui.console.frame import View, bar, build, header, route_keys_bar, thin
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.registry import SECTIONS
from eawf.surfaces.tui.console.width import cell_len, pad

OWN = "MLS-0004"
_UNAVAILABLE = "∅ unavailable"
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


def render(view: View) -> list[str]:
    """Return the Milestone frame."""
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
