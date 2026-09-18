"""Derivations every renderer and the dispatcher share.

Record lookups, containment counts, the record body, fleet lookups, the row clamps, the
bucket strip and the target helpers. Every count here is computed from the register it
describes; nothing is stored.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from eawf.surfaces.tui.console.attention import attn_list
from eawf.surfaces.tui.console.fixture import Fixture, FleetRow, Milestone, Track
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.registry import route_of
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import cell_len, pad

Row = tuple[str, str]

IN_FLIGHT = re.compile(r"\b(RUNNING|STARTING|CHECKING|INTEGRATING)\b")
# A record row that starts with a navigable entity id, optionally after a relation word.
NAVSTART = re.compile(
    r"^(?:receipt |kept with |supports )?"
    r"(RUN-[0-9a-f]{8}|EAWF-\d{4}|BAT-\d{4}|MLS-\d{4}|EVT-\d{4}|CLM-\d{4}|CAM-\d{4}"
    r"|REL-\d{4}|EVD-\d{4})\b"
)
# The record groups whose entity rows the cursor walks.
NAVLABEL = re.compile(
    r"^(BATCH|BATCHES|TASKS|RUNS|MILESTONE|SCOPE|MEMBERS|PROOF|EVIDENCE|RECEIPT|CLAIM"
    r"|SUPPORTS|CAMPAIGN|RELEASE)$"
)
_ABSENT_VALUE = re.compile(r"^\s*∅")
_ENTITY_ID = re.compile(
    r"\b(RUN-[0-9a-f]{8}|EAWF-\d{4}|BAT-\d{4}|MLS-\d{4}|CAM-\d{4}|CLM-\d{4}|REL-\d{4})\b"
)
_PARENT_ID = re.compile(r"\b(BAT-\d{4}|MLS-\d{4})\b")
_WORST = re.compile(r"fail|denied|lost|invalid|blocked", re.IGNORECASE)
_UNSURE = re.compile(r"\?|unknown|pending|not run", re.IGNORECASE)
_FACT_SEP = " · "


def plural(n: int, word: str, suffix: str = "s") -> str:
    """Return ``n word``, thousands grouped, with ``suffix`` appended unless ``n`` is one.

    Raises:
        TypeError: ``n`` is not an integer, or is a bool.
    """
    return f"{group(n)} {word}{'' if n == 1 else suffix}"


# ---------- entity records ----------


def field_of(fixture: Fixture, entity_id: str | None, label: str) -> str | None:
    """Return the first value of the record row labelled ``label``, case-insensitively."""
    rec = fixture.record(entity_id)
    if not rec:
        return None
    for lab, val in rec:
        if lab.upper() == label:
            return val
    return None


def field_token(fixture: Fixture, entity_id: str, label: str) -> str | None:
    """Return the first ``·``-separated token of a record field."""
    value = field_of(fixture, entity_id, label)
    return None if value is None else value.split(_FACT_SEP)[0]


def fleet_of(fixture: Fixture, entity_id: str | None) -> FleetRow | None:
    """Return the last fleet row whose Run or task is ``entity_id``."""
    hit = None
    for row in fixture.proto.fleet:
        if entity_id in (row.run, row.task.split(" ")[0]):
            hit = row
    return hit


def task_name_of(fixture: Fixture, entity_id: str) -> str | None:
    """Return the task name the fleet register holds for ``entity_id``."""
    row = fleet_of(fixture, entity_id)
    return " ".join(row.task.split(" ")[1:]) if row else None


def ms_of(fixture: Fixture, entity_id: str | None) -> tuple[Track, Milestone] | None:
    """Return the track and milestone of milestone ``entity_id``."""
    hit = None
    for track in fixture.proto.tracks:
        for milestone in track.milestones:
            if milestone.id == entity_id:
                hit = (track, milestone)
    return hit


def track_id_of(fixture: Fixture, name: str) -> str | None:
    """Return the track id a ``TRACK`` field names, if the register holds that track."""
    wanted = name.split(_FACT_SEP)[0].strip()
    hit = None
    for track in fixture.proto.tracks:
        if track.id == wanted:
            hit = track.id
    return hit


def ref_state(fixture: Fixture, entity_id: str) -> str | None:
    """Return the state of ``entity_id``: the fleet's, else its record's."""
    row = fleet_of(fixture, entity_id)
    if row:
        return row.state
    state = field_of(fixture, entity_id, "STATE") or field_of(fixture, entity_id, "STANDING")
    return state.split(_FACT_SEP)[0] if state else None


def ref_text(fixture: Fixture, entity_id: str) -> str:
    """Return ``entity_id`` with its name and state, as a reference row reads it."""
    name = field_of(fixture, entity_id, "NAME")
    state = ref_state(fixture, entity_id)
    return entity_id + (f" {name}" if name else "") + (f"{_FACT_SEP}{state}" if state else "")


def children_of(fixture: Fixture, parent: str, prefix: str, back_label: str) -> list[str]:
    """Return the sorted records with ``prefix`` whose ``back_label`` field names ``parent``."""
    return sorted(
        x
        for x in fixture.detail
        if x.startswith(prefix) and (field_of(fixture, x, back_label) or "").startswith(parent)
    )


@dataclass(frozen=True, slots=True)
class Built:
    """What a milestone has built: batches and tasks, with its sentence."""

    batches: int
    tasks: int
    text: str


@dataclass(frozen=True, slots=True)
class TaskCount:
    """A batch's tasks and how many are in flight, with its sentence."""

    total: int
    live: int
    text: str


def built_of(fixture: Fixture, entity_id: str) -> Built:
    """Return what milestone ``entity_id`` has built, counted from the records."""
    batches = children_of(fixture, entity_id, "BAT-", "MILESTONE")
    tasks = sum(len(children_of(fixture, x, "EAWF-", "BATCH")) for x in batches)
    text = f"{plural(len(batches), 'batch', 'es')}{_FACT_SEP}{plural(tasks, 'task')}"
    return Built(len(batches), tasks, text)


def count_of(fixture: Fixture, entity_id: str) -> TaskCount:
    """Return batch ``entity_id``'s tasks and the ones in flight, counted from the records."""
    tasks = children_of(fixture, entity_id, "EAWF-", "BATCH")
    live = sum(1 for x in tasks if IN_FLIGHT.search(ref_state(fixture, x) or ""))
    return TaskCount(len(tasks), live, f"{plural(len(tasks), 'task')}{_FACT_SEP}{live} in flight")


def worst_of(value: str | None) -> str | None:
    """Return the most alarming ``·``-separated part of ``value``, the first on a tie."""
    if value is None:
        return None

    def rank(part: str) -> int:
        if _WORST.search(part):
            return 3
        if _UNSURE.search(part):
            return 2
        return 1

    parts = value.split(_FACT_SEP)
    best, best_rank = parts[0], rank(parts[0])
    for part in parts[1:]:
        if rank(part) > best_rank:
            best, best_rank = part, rank(part)
    return best


def _fact(fixture: Fixture, entity_id: str, label: str, kind: str) -> str | None:
    """Return one subject fact of the kind the subject line asks for."""
    if kind == "first":
        for alt in label.split("|"):
            got = field_of(fixture, entity_id, alt)
            if got is not None:
                return got
        return None
    if kind == "token":
        return field_token(fixture, entity_id, label)
    if kind == "worst":
        return worst_of(field_of(fixture, entity_id, label))
    return field_of(fixture, entity_id, label)


def subj_facts(fixture: Fixture, entity_id: str, pairs: Sequence[Row]) -> str | None:
    """Return the subject line's facts read from the entity's own record, or ``None``.

    Args:
        fixture: The registers.
        entity_id: The subject.
        pairs: ``(label, kind)`` per fact: ``field`` reads the field, ``token`` its first
            token, ``worst`` its most alarming part, ``first`` the first of ``|``-joined
            labels that is present.
    """
    out = [f for label, kind in pairs if (f := _fact(fixture, entity_id, label, kind))]
    return _FACT_SEP.join(out) if out else None


def subj_of(session: Session, default: str) -> str:
    """Return the session's subject, or the route's own entity."""
    return session.subj_id or default


def own_body(session: Session, default: str) -> bool:
    """Return whether the route renders its own authored entity."""
    return not session.subj_id or session.subj_id == default


def publish_nav(session: Session, ids: Sequence[str | None]) -> None:
    """Publish the entity ids a record frame's cursor walks."""
    session.record_nav = list(ids)


# ---------- the record body ----------


def _replace_group(
    fixture: Fixture, rows: list[Row], *, label: str, items: list[str], empty: str
) -> list[Row]:
    """Replace group ``label``'s rows by one reference row per item, or by ``empty``."""
    out: list[Row] = []
    skipping = False
    for lab, val in rows:
        upper = lab.upper()
        if upper == label and skipping:
            continue
        if upper == label:
            skipping = True
            out.extend([(lab, empty)] if not items else _group_rows(fixture, lab, items))
            continue
        if upper == "" and skipping:
            continue
        if upper != "":
            skipping = False
        out.append((lab, val))
    return out


def _group_rows(fixture: Fixture, label: str, items: list[str]) -> list[Row]:
    """Return a group's reference rows, the label on the first one only."""
    return [("" if k else label, ref_text(fixture, x)) for k, x in enumerate(items)]


def _derived_groups(fixture: Fixture, rows: list[Row], entity_id: str | None) -> list[Row]:
    """Replace a milestone's batches and a batch's tasks by the ones that point back."""
    if not entity_id:
        return rows
    state = (field_of(fixture, entity_id, "STATE") or "").lower().replace("_", " ")
    if entity_id.startswith("MLS-"):
        return _replace_group(
            fixture,
            rows,
            label="BATCHES",
            items=children_of(fixture, entity_id, "BAT-", "MILESTONE"),
            empty=f"∅ none cut{_FACT_SEP}{state}, nothing built yet",
        )
    if entity_id.startswith("BAT-"):
        return _replace_group(
            fixture,
            rows,
            label="TASKS",
            items=children_of(fixture, entity_id, "EAWF-", "BATCH"),
            empty=f"∅ none cut{_FACT_SEP}{state}, nothing cut yet",
        )
    return rows


def _parent_fact(fixture: Fixture, row: Row) -> Row:
    """Return a ``BATCH`` or ``MILESTONE`` row with its parent's reference and figure."""
    label = row[0].upper()
    if label not in ("BATCH", "MILESTONE"):
        return row
    found = _PARENT_ID.search(row[1])
    pid = found.group(1) if found else None
    if not pid or pid not in fixture.detail:
        return row
    figure = count_of(fixture, pid).text if label == "BATCH" else built_of(fixture, pid).text
    return (row[0], f"{ref_text(fixture, pid)}{_FACT_SEP}{figure}")


def _drop_subject_rows(rows: list[Row], subject: str) -> list[Row]:
    """Drop the rows the subject line already says, with their continuation rows."""
    kept: list[Row] = []
    dropping = False
    for lab, val in rows:
        if lab.upper() != "":
            dropping = bool(val and val in subject)
        if not dropping:
            kept.append((lab, val))
    return kept


def _set_row(rows: list[Row], *, label: str, value: str, after: str) -> list[Row]:
    """Set row ``label`` to ``value``, inserting it after row ``after`` when it is missing."""
    if any(lab.upper() == label for lab, _val in rows):
        return [(lab, value if lab.upper() == label else val) for lab, val in rows]
    out: list[Row] = []
    placed = False
    for row in rows:
        out.append(row)
        if not placed and row[0].upper() == after:
            out.append((label, value))
            placed = True
    if not placed:
        out.append((label, value))
    return out


def _derived_counts(fixture: Fixture, rows: list[Row], entity_id: str | None) -> list[Row]:
    """Set a batch's ``COUNT`` and a milestone's ``BUILT`` from the records."""
    if entity_id and entity_id.startswith("BAT-"):
        count = count_of(fixture, entity_id)
        if count.total:
            return _set_row(rows, label="COUNT", value=count.text, after="MILESTONE")
    if entity_id and entity_id.startswith("MLS-"):
        built = built_of(fixture, entity_id)
        if built.batches:
            return _set_row(rows, label="BUILT", value=built.text, after="BATCHES")
    return rows


def _nav_indexes(rows: list[Row]) -> list[int]:
    """Return the indexes of the rows the cursor walks."""
    group_label = ""
    found: list[int] = []
    for i, (lab, val) in enumerate(rows):
        if lab.upper():
            group_label = lab.upper()
        if NAVLABEL.match(group_label) and NAVSTART.match(val.strip()):
            found.append(i)
    return found


def _nav_id(value: str) -> str | None:
    found = NAVSTART.match(value.strip())
    return found.group(1) if found else None


def _land_cursor(session: Session, rows: list[Row], nav: list[int], entity_id: str | None) -> None:
    """Clamp the cursor, landing on the newest Run the first time an entity is shown."""
    sel_in(session, len(nav))
    if session.rec_seen is None:
        session.rec_seen = {}
    if entity_id and not session.rec_seen.get(entity_id):
        # a Run in the first nav row does not stop the search, so the cursor lands on the
        # first Run after it; the recorded frames were drawn with exactly this landing
        runs = [k for k in range(1, len(nav)) if rows[nav[k]][1].strip().startswith("RUN-")]
        session.sel = runs[0] if runs else 0
        session.rec_seen[entity_id] = 1


def record_body(
    session: Session,
    fixture: Fixture,
    rows_in: Sequence[Row],
    *,
    entity_id: str | None,
    subject: str | None,
) -> list[str]:
    """Return the record body and publish what the record frame's keys read.

    Groups and counts are derived from the records, rows the subject line already says
    are dropped, and the cursor marks the entity it selects. The navigation ids and the
    recorded facts are published on the session for the frame builder and the dispatcher.

    Args:
        session: The session whose cursor is clamped and whose published fields are set.
        fixture: The registers.
        rows_in: The entity's stored record rows.
        entity_id: The entity the record belongs to.
        subject: The subject line, whose facts are not repeated below it.
    """
    rows = _derived_groups(fixture, list(rows_in), entity_id)
    rows = [_parent_fact(fixture, row) for row in rows]
    rows = _drop_subject_rows(rows, subject or "")
    rows = _derived_counts(fixture, rows, entity_id)
    nav = _nav_indexes(rows)
    _land_cursor(session, rows, nav, entity_id)
    marked = nav[session.sel] if nav else -1
    out = [
        "  " + pad(lab, 11) + ("▸ " if i == marked else "  ") + val
        for i, (lab, val) in enumerate(rows)
    ]
    publish_nav(session, [_nav_id(rows[i][1]) for i in nav])
    session.record_facts = [lab.lower() for lab, val in rows if not _ABSENT_VALUE.match(val)]
    return out


def absent(
    session: Session, fixture: Fixture, rows: list[str], *, entity_id: str, what: str, w: int
) -> list[str]:
    """Return a frame body for an entity this route has not authored.

    The entity's stored record when one is held, else a fleet-derived record, else the
    absence stated in place.

    Args:
        session: The session the absent flag is raised on.
        fixture: The registers.
        rows: The route's rows; the header, subtitle and rule are kept.
        entity_id: The entity the frame is about.
        what: What the route would have shown, as the absence names it.
        w: The frame width in cells.
    """
    stored = fixture.record(entity_id)
    record: list[Row] | None = list(stored) if stored else None
    fleet = fleet_of(fixture, entity_id) if not record else None
    if fleet:
        record = [
            ("PROVIDER", f"{fleet.prov}{_FACT_SEP}as of {fleet.as_of}"),
            ("STATE", f"{fleet.state}{_FACT_SEP}{fleet.reason}"),
            ("TASK", fleet.task),
            ("RUN", fleet.run),
            ("BUCKET", fleet.bucket),
        ]
    if record:
        body = record_body(session, fixture, record, entity_id=entity_id, subject=rows[1])
        return [*rows[:3], *body]
    session.absent = True
    return [
        *rows[:3],
        "─" * w,
        f" ∅ no {what} recorded for {entity_id}",
        "   the fleet register carries its state; nothing deeper is held for it",
    ]


# ---------- cursors ----------


def sel_in(session: Session, n: int) -> int:
    """Publish the row count and clamp the cursor into it; every route uses this clamp."""
    session.count = n
    session.sel = 0 if n <= 0 else max(0, min(session.sel, n - 1))
    return session.sel


def sel_by_id(session: Session, ids: Sequence[str]) -> int:
    """Clamp the cursor on a re-sorting list by entity id, never by row offset."""
    session.count = len(ids)
    if not ids:
        session.sel = 0
        session.sel_id = None
        return 0
    if session.sel_id in ids:
        index = ids.index(session.sel_id)
    else:
        index = max(0, min(session.sel, len(ids) - 1))
        session.sel_id = ids[index]
    session.sel = index
    return index


def filter_of(session: Session) -> str:
    """Return the route's own filter text."""
    return session.filters.get(session.route, "")


def set_filter(session: Session, value: str) -> None:
    """Set the route's own filter text."""
    session.filters[session.route] = value


def fresh_arrival(session: Session, route: str) -> None:
    """Start a route reached by name clean: no bucket, no filter, no scroll."""
    session.bucket = None
    session.filters[route] = ""
    session.filter = ""
    session.scroll = 0
    session.typing = False
    if route == "activity":
        session.pane_sel = 0


# ---------- fleet ----------


def fleet_bucket_count(fixture: Fixture, key: str) -> int:
    """Return the Runs the fleet register files under bucket ``key``."""
    return sum(1 for row in fixture.proto.fleet if row.bucket == key)


def current_fleet_row(session: Session, fixture: Fixture) -> FleetRow | None:
    """Return the Activity row under the cursor, after the bucket and the filter."""
    rows = [f for f in fixture.proto.fleet if not session.bucket or f.bucket == session.bucket]
    if session.filter:
        query = session.filter.lower()
        rows = [f for f in rows if query in f"{f.run} {f.task} {f.reason}".lower()]
    if not rows:
        return None
    return rows[min(session.sel, len(rows) - 1)]


@dataclass(frozen=True, slots=True)
class StripItem:
    """One bucket of a bucket strip: its key (``None`` for all), label and count."""

    key: str | None
    label: str
    n: int


def strip_row(session: Session, items: Sequence[StripItem], w: int) -> str:
    """Return the bucket strip: grown around the focused bucket while it fits, edges counted.

    Raises:
        IndexError: ``items`` is empty.
    """
    keys = [x.key for x in items]
    focus = keys.index(session.bucket) if session.bucket in keys else 0
    lead = " BUCKETS   "

    def cell(x: StripItem, on: bool) -> str:
        return ("▸" if on else "") + f"{x.label} {x.n}"

    def edges(start: int, end: int, body: str) -> str:
        left = f"‹{start}{_FACT_SEP}" if start else ""  # noqa: RUF001
        after = len(items) - 1 - end
        right = f"{_FACT_SEP}{after}›" if after else ""  # noqa: RUF001
        return left + body + right

    start = end = focus
    text = cell(items[focus], True)
    while True:
        if end + 1 < len(items):
            grown = f"{text}{_FACT_SEP}{cell(items[end + 1], False)}"
            if cell_len(lead + edges(start, end + 1, grown)) <= w:
                text, end = grown, end + 1
                continue
        if start > 0:
            grown = f"{cell(items[start - 1], False)}{_FACT_SEP}{text}"
            if cell_len(lead + edges(start - 1, end, grown)) <= w:
                text, start = grown, start - 1
                continue
        return lead + edges(start, end, text)


# ---------- scope home tree ----------


def home_track(session: Session, fixture: Fixture) -> int:
    """Return the scope-home tree's expanded track, clamped into the register."""
    return max(0, min(len(fixture.proto.tracks) - 1, session.home_track))


def home_step(session: Session, fixture: Fixture, direction: int) -> None:
    """Move the tree cursor one milestone, crossing to the next track with milestones."""
    tracks = fixture.proto.tracks
    milestones = tracks[home_track(session, fixture)].milestones
    at = session.home_ms
    if direction > 0 and at + 1 < len(milestones):
        session.home_ms = at + 1
        return
    if direction < 0 and at > 0:
        session.home_ms = at - 1
        return
    for n in range(1, len(tracks) + 1):
        t = (home_track(session, fixture) + direction * n + len(tracks) * n) % len(tracks)
        kids = len(tracks[t].milestones)
        if kids:
            session.home_track = t
            session.home_ms = 0 if direction > 0 else kids - 1
            return


# ---------- targets ----------


def target_id(session: Session, fixture: Fixture) -> str:
    """Return the id of the entity a verb on this frame acts on."""
    proto = fixture.proto
    sel = session.sel
    if session.route == "scope.home":
        return proto.tracks[sel].id if sel < len(proto.tracks) else proto.scope
    if session.route == "activity":
        return proto.fleet[sel].run if sel < len(proto.fleet) else "the fleet"
    if session.route == "attention":
        rows = attn_list(session, fixture)
        return rows[sel].id if sel < len(rows) else "no action selected"
    if session.subj_id:
        return session.subj_id
    return {
        "run.detail": "RUN-9e3779b1",
        "batch.detail": "BAT-0001",
        "milestone": "MLS-0004",
    }.get(session.route, session.route)


def urn(session: Session, fixture: Fixture) -> str:
    """Return the stable URN of the frame's subject."""
    tail = f":{session.subj_id}" if session.subj_id else ""
    return f"urn:eawf:{fixture.scope}:{session.route}{tail}"


def wrap_pane(label: str, text: str, w: int) -> list[str]:
    """Return consequence prose wrapped inside its labelled pane rather than truncated."""
    out: list[str] = []
    lead = " " + pad(label, 10)
    cont = " " + pad("", 10)
    line = ""
    for word in text.split(" "):
        if cell_len(f"{line} {word}".strip()) > w - 12:
            out.append((cont if out else lead) + line.strip())
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append((cont if out else lead) + line)
    return out


def entity_id_in(text: str) -> str | None:
    """Return the first entity id mentioned in ``text``."""
    found = _ENTITY_ID.search(text)
    return found.group(1) if found else None


def route_of_id(entity_id: str | None) -> str | None:
    """Return the route the id-prefix table files ``entity_id`` under."""
    return route_of(entity_id) if entity_id else None
