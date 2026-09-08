"""Derivations every renderer and the dispatcher share, ported from the prototype's
module scope: record lookups, containment counts, the record body, fleet lookups, the
row clamp, the bucket strip and the copy/target helpers.

Every count here is computed from the register it describes; nothing is stored.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..chassis.registry import ROUTE_OF, route_word
from ..chassis.width import cell_len, pad

if TYPE_CHECKING:
    from ..chassis.fixture import Fixture, FleetRow, Milestone, Track
    from ..chassis.session import Session

IN_FLIGHT = re.compile(r"\b(RUNNING|STARTING|CHECKING|INTEGRATING)\b")
NAVSTART = re.compile(
    r"^(?:receipt |kept with |supports )?(RUN-[0-9a-f]{8}|EAWF-\d{4}|BAT-\d{4}|MLS-\d{4}|EVT-\d{4}|CLM-\d{4}|CAM-\d{4}|REL-\d{4}|EVD-\d{4})\b"
)
NAVLABEL = re.compile(
    r"^(BATCH|BATCHES|TASKS|RUNS|MILESTONE|SCOPE|MEMBERS|PROOF|EVIDENCE|RECEIPT|CLAIM|SUPPORTS|CAMPAIGN|RELEASE)$"
)
_ABSENT_VALUE = re.compile(r"^\s*∅")
_ENTITY_ID = re.compile(
    r"\b(RUN-[0-9a-f]{8}|EAWF-\d{4}|BAT-\d{4}|MLS-\d{4}|CAM-\d{4}|CLM-\d{4}|REL-\d{4})\b"
)


def num(n: int) -> str:
    """Thousands grouping with a fixed comma, never the machine's locale."""
    return f"{n:,}"


# ---------- entity records ----------


def field_of(fixture: Fixture, entity_id: str | None, label: str) -> str | None:
    rec = fixture.record(entity_id)
    if not rec:
        return None
    for lab, val in rec:
        if str(lab).upper() == label:
            return val
    return None


rec_field = field_of


def rec_token(fixture: Fixture, entity_id: str, label: str) -> str | None:
    v = rec_field(fixture, entity_id, label)
    return None if v is None else str(v).split(" · ")[0]


def fleet_of(fixture: Fixture, entity_id: str | None) -> FleetRow | None:
    hit = None
    for f in fixture.proto.fleet:
        if f.run == entity_id or str(f.task or "").split(" ")[0] == entity_id:
            hit = f
    return hit


def task_name_of(fixture: Fixture, entity_id: str) -> str | None:
    f = fleet_of(fixture, entity_id)
    return " ".join(str(f.task or "").split(" ")[1:]) if f else None


def ms_of(fixture: Fixture, entity_id: str | None) -> tuple[Track, Milestone] | None:
    hit = None
    for t in fixture.proto.tracks:
        for m in t.milestones:
            if m.id == entity_id:
                hit = (t, m)
    return hit


def track_id_of(fixture: Fixture, name: str) -> str | None:
    n = str(name).split(" · ")[0].strip()
    hit = None
    for t in fixture.proto.tracks:
        if t.id == n:
            hit = t.id
    return hit


def ref_state(fixture: Fixture, entity_id: str) -> str | None:
    f = fleet_of(fixture, entity_id)
    if f:
        return f.state
    st = field_of(fixture, entity_id, "STATE") or field_of(fixture, entity_id, "STANDING")
    return str(st).split(" · ")[0] if st else None


def ref_text(fixture: Fixture, entity_id: str) -> str:
    nm = field_of(fixture, entity_id, "NAME")
    st = ref_state(fixture, entity_id)
    return entity_id + (f" {nm}" if nm else "") + (f" · {st}" if st else "")


def children_of(fixture: Fixture, parent: str, prefix: str, back_label: str) -> list[str]:
    out = [
        x
        for x in fixture.detail
        if x.startswith(prefix) and str(field_of(fixture, x, back_label) or "").startswith(parent)
    ]
    return sorted(out)


def built_of(fixture: Fixture, entity_id: str) -> dict[str, object]:
    b = children_of(fixture, entity_id, "BAT-", "MILESTONE")
    t = sum(len(children_of(fixture, x, "EAWF-", "BATCH")) for x in b)
    return {
        "batches": len(b),
        "tasks": t,
        "text": f"{len(b)} batch{'' if len(b) == 1 else 'es'} · {t} task{'' if t == 1 else 's'}",
    }


def count_of(fixture: Fixture, entity_id: str) -> dict[str, object]:
    t = children_of(fixture, entity_id, "EAWF-", "BATCH")
    live = sum(1 for x in t if IN_FLIGHT.search(str(ref_state(fixture, x) or "")))
    return {
        "total": len(t),
        "live": live,
        "text": f"{len(t)} task{'' if len(t) == 1 else 's'} · {live} in flight",
    }


def in_flight_of(fixture: Fixture, entity_id: str) -> dict[str, int] | None:
    rec = fixture.record(entity_id)
    if not rec:
        return None
    total = live = 0
    in_list = False
    for lab, val in rec:
        label = str(lab).upper()
        if label == "TASKS":
            in_list = True
        elif label != "":
            in_list = False
        if not in_list:
            continue
        total += 1
        if IN_FLIGHT.search(str(val)):
            live += 1
    return {"total": total, "live": live} if total else None


def worst_of(value: str | None) -> str | None:
    if value is None:
        return None
    parts = str(value).split(" · ")

    def rank(s: str) -> int:
        if re.search(r"fail|denied|lost|invalid|blocked", s, re.I):
            return 3
        if re.search(r"\?|unknown|pending|not run", s, re.I):
            return 2
        return 1

    best, r = parts[0], rank(parts[0])
    for p in parts[1:]:
        x = rank(p)
        if x > r:
            r, best = x, p
    return best


def subj_facts(fixture: Fixture, entity_id: str, pairs: list[tuple[str, str]]) -> str | None:
    """The subject line's facts, read from the entity's own record."""
    out: list[str] = []
    for label, kind in pairs:
        if kind == "first":
            got = None
            for alt in str(label).split("|"):
                got = rec_field(fixture, entity_id, alt)
                if got is not None:
                    break
            if got is not None and len(str(got)):
                out.append(str(got))
            continue
        if kind == "token":
            v = rec_token(fixture, entity_id, label)
        elif kind == "worst":
            v = worst_of(rec_field(fixture, entity_id, label))
        else:
            v = rec_field(fixture, entity_id, label)
        if v is not None and len(str(v)):
            out.append(str(v))
    return " · ".join(out) if out else None


def subj_of(session: Session, default: str) -> str:
    return session.subj_id or default


def own_body(session: Session, default: str) -> bool:
    return not session.subj_id or session.subj_id == default


def publish_nav(session: Session, ids: list[str | None]) -> None:
    session.record_nav = list(ids)


def record_body(
    session: Session,
    fixture: Fixture,
    rows_in: list[tuple[str, str]],
    entity_id: str | None,
    subj: str | None,
) -> list[str]:
    """The record body: derived groups, derived counts, the subject's rows dropped, the
    cursor before the entity it selects, the navigation list published."""
    D: list[tuple[str, str]] = [(str(a), str(b)) for a, b in rows_in]

    def replace_group(
        rows: list[tuple[str, str]], label: str, items: list[str], empty_text: str
    ) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        skipping = False
        for lab, val in rows:
            lb = lab.upper()
            if lb == label:
                if skipping:
                    continue
                skipping = True
                if not items:
                    out.append((lab, empty_text))
                    continue
                for k, x in enumerate(items):
                    out.append(("" if k else lab, ref_text(fixture, x)))
                continue
            if lb == "":
                if skipping:
                    continue
                out.append((lab, val))
                continue
            skipping = False
            out.append((lab, val))
        return out

    if entity_id and entity_id.startswith("MLS-"):
        state = str(field_of(fixture, entity_id, "STATE") or "").lower().replace("_", " ")
        D = replace_group(
            D,
            "BATCHES",
            children_of(fixture, entity_id, "BAT-", "MILESTONE"),
            f"∅ none cut · {state}, nothing built yet",
        )
    if entity_id and entity_id.startswith("BAT-"):
        state = str(field_of(fixture, entity_id, "STATE") or "").lower().replace("_", " ")
        D = replace_group(
            D,
            "TASKS",
            children_of(fixture, entity_id, "EAWF-", "BATCH"),
            f"∅ none cut · {state}, nothing cut yet",
        )

    def parent_fact(row: tuple[str, str]) -> tuple[str, str]:
        lb = row[0].upper()
        if lb not in ("BATCH", "MILESTONE"):
            return row
        m = re.search(r"\b(BAT-\d{4}|MLS-\d{4})\b", row[1])
        pid = m.group(1) if m else None
        if not pid or pid not in fixture.detail:
            return row
        figure = count_of(fixture, pid)["text"] if lb == "BATCH" else built_of(fixture, pid)["text"]
        return (row[0], f"{ref_text(fixture, pid)} · {figure}")

    D = [parent_fact(r) for r in D]
    subj_text = "" if subj is None else str(subj)
    kept: list[tuple[str, str]] = []
    dropping = False
    for lab, val in D:
        if lab.upper() == "":
            if not dropping:
                kept.append((lab, val))
            continue
        dropping = bool(val and val in subj_text)
        if not dropping:
            kept.append((lab, val))
    D = kept

    def set_row(
        rows: list[tuple[str, str]], label: str, value: str, after: str
    ) -> list[tuple[str, str]]:
        seen = False
        out0: list[tuple[str, str]] = []
        for lab, val in rows:
            if lab.upper() == label:
                seen = True
                out0.append((lab, value))
            else:
                out0.append((lab, val))
        if seen:
            return out0
        out: list[tuple[str, str]] = []
        placed = False
        for r in rows:
            out.append(r)
            if not placed and r[0].upper() == after:
                out.append((label, value))
                placed = True
        if not placed:
            out.append((label, value))
        return out

    if entity_id and entity_id.startswith("BAT-"):
        ct = count_of(fixture, entity_id)
        if ct["total"]:
            D = set_row(D, "COUNT", str(ct["text"]), "MILESTONE")
    if entity_id and entity_id.startswith("MLS-"):
        bt = built_of(fixture, entity_id)
        if bt["batches"]:
            D = set_row(D, "BUILT", str(bt["text"]), "BATCHES")

    nav_lab = ""
    nav: list[tuple[str, str]] = []
    nav_idx: set[int] = set()
    for i, (lab, val) in enumerate(D):
        lb = lab.upper()
        if lb:
            nav_lab = lb
        if NAVLABEL.match(nav_lab) and NAVSTART.match(val.strip()):
            nav.append((lab, val))
            nav_idx.add(i)
    sel_in(session, len(nav))
    if session.rec_seen is None:
        session.rec_seen = {}
    if entity_id and not session.rec_seen.get(entity_id):
        newest = 0
        for i, (_lab, val) in enumerate(nav):
            if not newest and val.strip().startswith("RUN-"):
                newest = i
        session.sel = newest
        session.rec_seen[entity_id] = 1
    ni = -1
    out_rows: list[str] = []
    for i, (lab, val) in enumerate(D):
        is_nav = i in nav_idx
        if is_nav:
            ni += 1
        out_rows.append(
            "  " + pad(lab, 11) + ("▸ " if (is_nav and ni == session.sel) else "  ") + val
        )

    def nav_id(val: str) -> str | None:
        m = NAVSTART.match(val.strip())
        return m.group(1) if m else None

    publish_nav(session, [nav_id(val) for _lab, val in nav])
    session.record_facts = [lab.lower() for lab, val in D if not _ABSENT_VALUE.match(val)]
    return out_rows


def absent(
    session: Session, fixture: Fixture, rows: list, entity_id: str, what: str, w: int
) -> list:
    """A frame whose body is not this entity's: the record if one is stored, else the absence."""
    from ..chassis.frame import thin

    rec = fixture.record(entity_id)
    D: list[tuple[str, str]] | None = list(rec) if rec else None
    if not D:
        f = fleet_of(fixture, entity_id)
        if f:
            D = [
                ("PROVIDER", f"{f.prov} · as of {f.as_}"),
                ("STATE", f"{f.state} · {f.reason}"),
                ("TASK", f.task),
                ("RUN", f.run),
                ("BUCKET", f.bucket),
            ]
    if D:
        out = list(rows[:3])
        return out + record_body(session, fixture, D, entity_id, rows[1])
    session.absent = True
    return list(rows[:3]) + [
        thin(w),
        f" ∅ no {what} recorded for {entity_id}",
        "   the fleet register carries its state; nothing deeper is held for it",
    ]


# ---------- cursors ----------


def sel_in(session: Session, n: int) -> int:
    """One clamp for every route: publish the row count, get a valid cursor back."""
    session.count = n
    if n <= 0:
        session.sel = 0
        return 0
    if session.sel >= n:
        session.sel = n - 1
    if session.sel < 0:
        session.sel = 0
    return session.sel


def sel_by_id(session: Session, ids: list[str]) -> int:
    """Selection on a re-sorting list is an entity id, never a row offset."""
    session.count = len(ids)
    if not ids:
        session.sel = 0
        session.sel_id = None
        return 0
    i = ids.index(session.sel_id) if session.sel_id in ids else -1
    if i < 0:
        i = max(0, min(session.sel, len(ids) - 1))
        session.sel_id = ids[i]
    session.sel = i
    return i


def filter_of(session: Session) -> str:
    return session.filters.get(session.route, "")


def set_filter(session: Session, value: str) -> None:
    session.filters[session.route] = value


def fresh_arrival(session: Session, route: str) -> None:
    """A route reached by name starts clean: no bucket, no filter, no scroll."""
    session.bucket = None
    session.filters[route] = ""
    session.filter = ""
    session.scroll = 0
    session.typing = False
    if route == "activity":
        session.pane_sel = 0


# ---------- fleet ----------


def bucket_count(fixture: Fixture, key: str) -> int:
    return sum(1 for f in fixture.proto.fleet if f.bucket == key)


def current_fleet_row(session: Session, fixture: Fixture) -> FleetRow | None:
    rows = [f for f in fixture.proto.fleet if not session.bucket or f.bucket == session.bucket]
    if session.filter:
        q = session.filter.lower()
        rows = [f for f in rows if q in f"{f.run} {f.task} {f.reason}".lower()]
    if not rows:
        return None
    return rows[min(session.sel, max(0, len(rows) - 1))]


def strip_row(session: Session, items: list[dict[str, object]], w: int) -> str:
    """The bucket strip: grow around the focused entry while the row fits, then count
    what is left off each edge."""
    keys = [x["key"] for x in items]
    fi = keys.index(session.bucket) if session.bucket in keys else 0
    lead = " BUCKETS   "

    def cell(x: dict[str, object], on: bool) -> str:
        return ("▸" if on else "") + f"{x['label']} {x['n']}"

    def edges(s: int, e: int, body: str) -> str:
        left = f"‹{s} · " if s else ""
        right = f" · {len(items) - 1 - e}›" if len(items) - 1 - e else ""
        return left + body + right

    start = end = fi
    txt = cell(items[fi], True)
    while True:
        nxt = txt + " · " + cell(items[end + 1], False) if end + 1 < len(items) else None
        prev = cell(items[start - 1], False) + " · " + txt if start > 0 else None
        if nxt is not None and cell_len(lead + edges(start, end + 1, nxt)) <= w:
            txt = nxt
            end += 1
            continue
        if prev is not None and cell_len(lead + edges(start - 1, end, prev)) <= w:
            txt = prev
            start -= 1
            continue
        return lead + edges(start, end, txt)


# ---------- scope home tree ----------


def home_track(session: Session, fixture: Fixture) -> int:
    return max(0, min(len(fixture.proto.tracks) - 1, session.home_track or 0))


def home_ms_list(session: Session, fixture: Fixture) -> tuple[Milestone, ...]:
    t = fixture.proto.tracks[home_track(session, fixture)]
    return t.milestones


def home_step(session: Session, fixture: Fixture, direction: int) -> None:
    m = session.home_ms
    ms = home_ms_list(session, fixture)
    if direction > 0 and m + 1 < len(ms):
        session.home_ms = m + 1
        return
    if direction < 0 and m > 0:
        session.home_ms = m - 1
        return
    tracks = fixture.proto.tracks
    for n in range(1, len(tracks) + 1):
        t = (home_track(session, fixture) + direction * n + len(tracks) * n) % len(tracks)
        kids = len(tracks[t].milestones)
        if kids:
            session.home_track = t
            session.home_ms = 0 if direction > 0 else kids - 1
            return


# ---------- copy and target ----------


def target_id(session: Session, fixture: Fixture) -> str:
    from ..chassis.attention import attn_list

    P = fixture.proto
    if session.route == "scope.home":
        return P.tracks[session.sel].id if session.sel < len(P.tracks) else P.scope
    if session.route == "activity":
        return P.fleet[session.sel].run if session.sel < len(P.fleet) else "the fleet"
    if session.route == "attention":
        rows = attn_list(session, fixture)
        return rows[session.sel].id if session.sel < len(rows) else "no action selected"
    if session.subj_id:
        return session.subj_id
    if session.route == "run.detail":
        return "RUN-9e3779b1"
    if session.route == "batch.detail":
        return "BAT-0001"
    if session.route == "milestone":
        return "MLS-0004"
    return session.route


def urn(session: Session, fixture: Fixture) -> str:
    return f"urn:eawf:{fixture.scope}:{session.route}" + (
        f":{session.subj_id}" if session.subj_id else ""
    )


def copy_target(session: Session, fixture: Fixture) -> str:
    """What `y` copies about the frame's subject; a route module may override via COPY."""
    from ..chassis.renderers import copy_for

    override = copy_for(session.route)
    if override is not None:
        return override(session, fixture)
    P = fixture.proto
    if session.overlay == "evidence" and session.route == "campaign":
        opts = ["EVT-9101 EVT-9104 EVT-9108", "EVT-9120 EVT-9126", "EVT-9131"]
        return opts[session.sel] if session.sel < len(opts) else opts[0]
    if session.route == "milestone":
        rec = fixture.record(session.subj_id)
        bundle = (
            next((val for lab, val in rec if str(lab).upper() == "BUNDLE"), None) if rec else None
        )
        m = re.search(r"digest\s+(\S+)", str(bundle)) if bundle else None
        return f"digest {m.group(1)}" if m else "digest 7c1f…a94"
    if session.route == "activity":
        return P.fleet[session.sel].run if session.sel < len(P.fleet) else "nothing selected"
    if session.route == "run.detail":
        return "RUN-9e3779b1"
    if session.route == "scope.home":
        return P.tracks[session.sel].id if session.sel < len(P.tracks) else P.scope
    return urn(session, fixture)


def wrap_pane(label: str, text: str, w: int) -> list[str]:
    """Consequence prose wraps inside its pane rather than truncating."""
    out: list[str] = []
    lead = " " + pad(label, 10)
    cont = " " + pad("", 10)
    mx = w - 12
    line = ""
    for word in str(text).split(" "):
        if cell_len((line + " " + word).strip()) > mx:
            out.append((cont if out else lead) + line.strip())
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        out.append((cont if out else lead) + line)
    return out


def entity_id_in(text: str) -> str | None:
    m = _ENTITY_ID.search(str(text or ""))
    return m.group(1) if m else None


def route_of_id(entity_id: str | None) -> str | None:
    if not entity_id:
        return None
    return ROUTE_OF.get(entity_id[:4]) or ROUTE_OF.get(entity_id[:3] + "-")


__all__ = [name for name in dir() if not name.startswith("_")] + ["route_word"]
