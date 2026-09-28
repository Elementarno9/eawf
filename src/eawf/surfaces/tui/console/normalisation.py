"""The pack-to-port normalisation map, loaded strict, and the comparison it governs.

The design pack's recorded frames are the reference contract, and the console's render is
compared against them only through this map. Each map entry is one admitted
transformation: what the pack renders, what the port renders instead, the golden ids it
touches and the rulings that admit it. The entries the port has realised carry a
:class:`Rewrite` here, which turns the pack's frame into the port's before the comparison;
a rewrite runs only on the ids its entry selects, so a difference anywhere else is a
failure. An entry the port has not realised yet rewrites nothing, and the pack's frame is
compared as recorded.

Two entries also carry a classification correction that the route registry binds, so the
map is the one place a corrected route key or route group is written.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from eawf.kernel.projection.attention import NOTIFICATION_MATRIX, ToastPolicy
from eawf.surfaces.tui.console.keybar import KEY, Pair, keybar
from eawf.surfaces.tui.console.keymap import ATTACH_LATER
from eawf.surfaces.tui.console.prototype import SNAPSHOT_AGE
from eawf.surfaces.tui.console.reads import DISCONNECTED_CAUSE
from eawf.surfaces.tui.console.session import SessionSetup
from eawf.surfaces.tui.console.tokens import CONNECTION, RULE_THIN
from eawf.surfaces.tui.console.width import cell_len, pad


class NormalisationEntry(BaseModel):
    """One admitted transformation between a pack frame and the port's render.

    Attributes:
        entry: The transformation's name, unique across the map.
        pack: What the design pack renders.
        port: What the port renders instead.
        golden_ids: ``fnmatch`` patterns over frame ids and journey ids.
        rulings: The ruling ids that admit the transformation.
        route_id: The pack route id this entry reclassifies, for a classification
            correction.
        route_key: The port's canonical key for ``route_id``.
        route_group: The port's route group for ``route_id``.

    Raises:
        pydantic.ValidationError: a field is unknown, the entry names no golden id or no
            ruling, or it writes a classification correction only in part.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: str
    pack: str
    port: str
    golden_ids: tuple[str, ...]
    rulings: tuple[str, ...]
    route_id: str | None = None
    route_key: str | None = None
    route_group: str | None = None

    @model_validator(mode="after")
    def _check(self) -> NormalisationEntry:
        """Reject an empty entry or a half-written classification correction."""
        if not self.golden_ids:
            raise ValueError(f"entry {self.entry!r} names no golden id")
        if not self.rulings:
            raise ValueError(f"entry {self.entry!r} names no ruling")
        parts = (self.route_id, self.route_key, self.route_group)
        if any(p is not None for p in parts) and not all(p is not None for p in parts):
            raise ValueError(
                f"entry {self.entry!r} is a half-written route correction: "
                "route_id, route_key and route_group stand or fall together"
            )
        return self

    def selects(self, contract_id: str) -> bool:
        """Return whether ``contract_id`` is one of the records this entry touches."""
        return any(fnmatch.fnmatchcase(contract_id, pattern) for pattern in self.golden_ids)


class RouteCorrection(BaseModel):
    """The port's key and route group for one pack route id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    group: str


class NormalisationMap(BaseModel):
    """The whole map: the contract it implements, its provenance and its entries.

    Raises:
        pydantic.ValidationError: a field is unknown, the map is empty, or two entries
            share a name.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: str
    source: str
    entries: tuple[NormalisationEntry, ...]

    @model_validator(mode="after")
    def _check(self) -> NormalisationMap:
        """Reject an empty map or a duplicated entry name."""
        if not self.entries:
            raise ValueError("the normalisation map carries no entry")
        names = [entry.entry for entry in self.entries]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate normalisation entries: {', '.join(duplicates)}")
        return self

    def route_corrections(self) -> dict[str, RouteCorrection]:
        """Return the classification correction per pack route id."""
        return {
            e.route_id: RouteCorrection(key=e.route_key, group=e.route_group)
            for e in self.entries
            if e.route_id is not None and e.route_key is not None and e.route_group is not None
        }

    def by_name(self, name: str) -> NormalisationEntry:
        """Return the entry named ``name``.

        Raises:
            KeyError: no entry has that name.
        """
        for entry in self.entries:
            if entry.entry == name:
                return entry
        raise KeyError(f"no normalisation entry named {name!r}")


def load_map(path: Path) -> NormalisationMap:
    """Load and validate a normalisation map file.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        pydantic.ValidationError: the file carries an unknown key or a malformed entry.
    """
    return NormalisationMap.model_validate(json.loads(path.read_text(encoding="utf-8")))


def unknown_golden_ids(table: NormalisationMap, contract_ids: Sequence[str]) -> list[str]:
    """Return every ``entry: pattern`` whose pattern selects no record of the contract."""
    return [
        f"{entry.entry}: {pattern}"
        for entry in table.entries
        for pattern in entry.golden_ids
        if not any(fnmatch.fnmatchcase(cid, pattern) for cid in contract_ids)
    ]


# ---------- the rewrites the port has realised ----------


@dataclass(frozen=True, slots=True, kw_only=True)
class PackFrame:
    """One recorded frame a rewrite transforms.

    Attributes:
        contract_id: The frame id, or the journey id for a journey step.
        rows: The frame's rows.
        pack: Every recorded frame state of the contract, by id, for a rewrite that
            derives the port's frame from a sibling recording.
        mark: The roadmap marker the frame's cursor stands on, which the pack drew in
            colour alone and so did not record in the text.
    """

    contract_id: str
    rows: tuple[str, ...]
    pack: Mapping[str, str]
    mark: int = 0

    @property
    def w(self) -> int:
        """Return the frame width in cells."""
        return cell_len(self.rows[0]) if self.rows else 0


RowsRewrite = Callable[[PackFrame], list[str]]


@dataclass(frozen=True, slots=True, kw_only=True)
class Rewrite:
    """How the port realises one map entry.

    Attributes:
        entry: The map entry the rewrite realises; a rewrite outside the map is refused.
        rows: Turns the pack's rows into the port's rows.
        keys: Keys the pack pressed that the port has no binding for; the replay performs
            them as harness actions instead of keystrokes, for the entry's ids only.
        overlays: Overlays the pack's setup names that the port opens under another
            name, pack name to port name, for the entry's ids only.
    """

    entry: str
    rows: RowsRewrite | None = None
    keys: frozenset[str] = field(default_factory=frozenset)
    overlays: Mapping[str, str] = field(default_factory=dict)


_GAP = "   "
_MARGIN = " "


def _pairs(bar: str) -> list[Pair]:
    """Return a keybar row's pairs, each split at its first space."""
    pieces = [p for p in bar[len(_MARGIN) :].rstrip().split(_GAP) if p]
    return [(p.split(" ", 1)[0], p.split(" ", 1)[1] if " " in p else "") for p in pieces]


def _rebar(bar: str, pieces: Sequence[str]) -> str:
    """Return a keybar row recomposed from ``pieces`` under the port's width budget."""
    pairs = [(p.split(" ", 1)[0], p.split(" ", 1)[1] if " " in p else "") for p in pieces]
    return keybar(pairs, cell_len(bar))


def _pieces(bar: str) -> list[str]:
    return [f"{token} {label}".rstrip() for token, label in _pairs(bar)]


_SIMULATOR_PAIR = "[ ] state"
# The subtitle's state counter, whole or cut at a word by the frame's width.
_STATE_OF = re.compile(
    r"^(?P<title>.*?)(?: · state \d+ of \d+| ·(?: state(?: \d+(?: of)?)?)?…)?\s*$"
)


def _entry_without_simulator(frame: PackFrame) -> list[str]:
    """Drop the pack's ``[ ] state`` pair and ``· state N of 8`` counter from an entry frame.

    The pair sat before ``/ attach later``, so the port's bar is the state's own pairs and
    then ``/ attach later``, which may now fit where the pack had dropped it.
    """
    rows = list(frame.rows)
    if len(rows) < 2:
        return rows
    found = _STATE_OF.match(rows[1])
    rows[1] = pad(found.group("title") if found else rows[1], frame.w)
    pieces = _pieces(rows[-1])
    own = pieces[: pieces.index(_SIMULATOR_PAIR)] if _SIMULATOR_PAIR in pieces else pieces
    attach = " ".join(ATTACH_LATER.pair())
    own = [piece for piece in own if piece != attach]
    rows[-1] = _rebar(rows[-1], [*own, attach])
    return rows


_SIMULATOR_ROW = re.compile(r"^   w {10}cycle frame size \(a property of this document\)\s*$")


def _help_without_simulator(frame: PackFrame) -> list[str]:
    """Drop the pack's ``w`` row from the help overlay; the rows below it move up."""
    rows = list(frame.rows)
    kept = [row for row in rows[:-1] if not _SIMULATOR_ROW.match(row)]
    if len(kept) == len(rows) - 1:
        return rows
    return [*kept, " " * frame.w, rows[-1]]


_CONN_ID = re.compile(r"^conn/DISCONNECTED/(?P<route>.+)$")
_GAP_LABEL = "no count can be called complete for the gap"
_DISCONNECTED_LABEL = f"disconnected · {DISCONNECTED_CAUSE}"
_ATTACHED_REVISION = re.compile(r"^(?P<line> ATTACHED  revision \S+)\s*$")


def _slot(conn: str) -> str:
    return f"{CONNECTION[conn].unicode} {conn}"


def _disconnected_body(frame: PackFrame) -> list[str]:
    """Return the port's disconnected frame: the pack's gap frame of the route, relabelled.

    Both states refuse writes and read the same revision, so the port draws them alike
    but for the cause line and the age: a lost link is read at its last revision, and
    the ``ATTACHED`` line says how old that revision is. A route whose gap frame reads
    like its live frame does not read the connection state, and keeps the recorded frame.
    """
    found = _CONN_ID.match(frame.contract_id)
    route = found.group("route") if found else ""
    gap = frame.pack.get(f"conn/GAP-DETECTED/{route}")
    live = frame.pack.get(f"conn/LIVE/{route}")
    if gap is None or live is None or gap.split("\n")[1:] == live.split("\n")[1:]:
        return list(frame.rows)
    rows = gap.split("\n")
    w = frame.w
    rows[0] = rows[0].replace(_slot("GAP DETECTED"), _slot("DISCONNECTED"))
    rows[1] = pad(rows[1].rstrip().replace(_GAP_LABEL, _DISCONNECTED_LABEL), w)
    for i, row in enumerate(rows):
        found_line = _ATTACHED_REVISION.match(row)
        if found_line is not None:
            rows[i] = pad(f"{found_line.group('line')} · {SNAPSHOT_AGE} old", w)
    return rows


_MORE = re.compile(r"^   \.\.\. \((?P<n>\d+) more\)\s*$")
# The palette's first window row, right under its prompt and rule.
_WINDOW_TOP = 3


def _edge_markers(frame: PackFrame) -> list[str]:
    """Rewrite the pack's ``... (N more)`` rows as the port's ``… N above`` and ``… N below``."""
    out: list[str] = []
    for i, row in enumerate(frame.rows):
        found = _MORE.match(row)
        if found is None:
            out.append(row)
            continue
        edge = "above" if i == _WINDOW_TOP else "below"
        out.append(pad(f"   … {int(found.group('n')):,} {edge}", frame.w))
    return out


def _renamed_pairs(frame: PackFrame, renames: Mapping[str, str]) -> list[str]:
    """Rename keybar tokens and recompose the bar; other rows are left alone."""
    rows = list(frame.rows)
    bar = rows[-1]
    pieces = _pieces(bar)
    renamed = [_rename(piece, renames) for piece in pieces]
    if renamed != pieces:
        rows[-1] = _rebar(bar, renamed)
    return rows


def _rename(piece: str, renames: Mapping[str, str]) -> str:
    for old, new in renames.items():
        if piece.startswith(f"{old} "):
            return new + piece[len(old) :]
    return piece


_PAGE_PACK = "PgUp PgDn"
_HELP_PAGE = re.compile(r"^   PgUp PgDn  (?P<label>\S.*?)\s*$")


def _full_page_keys(frame: PackFrame) -> list[str]:
    """Spell the page keys in full in the keybar and in the help overlay's route table."""
    full = KEY["page"].token
    rows = _renamed_pairs(frame, {_PAGE_PACK: full})
    for i, row in enumerate(rows):
        found = _HELP_PAGE.match(row)
        if found is not None:
            rows[i] = pad(f"   {full}  {found.group('label')}", frame.w)
    return rows


_ROW_PAIR = " ".join(KEY["up"].pair())
_OPEN_PAIR = " ".join(KEY["open"].pair())
_BUCKETS_PAIR = " ".join(KEY["tab"].pair())


def _advertised_open(frame: PackFrame) -> list[str]:
    """Advertise the Attention route's bound Enter after its row pair, and recompose the bar.

    The pack binds Enter on the route but leaves it off the bar; the port advertises every
    route verb it binds, and the globals then drop in their fixed order to make room. The
    Attention bar is the one whose row pair is followed straight by its buckets pair, so a
    journey step on another route keeps its bar.
    """
    rows = list(frame.rows)
    pieces = _pieces(rows[-1])
    at = pieces.index(_ROW_PAIR) + 1 if _ROW_PAIR in pieces else 0
    if at and pieces[at : at + 1] == [_BUCKETS_PAIR]:
        rows[-1] = _rebar(rows[-1], [*pieces[:at], _OPEN_PAIR, *pieces[at:]])
    return rows


def _unslashed_keys(frame: PackFrame) -> list[str]:
    """Name a two-key verb once, as the port's key table does, instead of a slashed pair."""
    return _renamed_pairs(frame, {"↑/↓": "↑↓", "J/K": "J K"})


# A quality marker the pack sets apart from its numeral, and the numeral it qualifies.
_SPACED_MARKER = re.compile(r"(?<![\w.])(?P<marker>[~≈]) (?P<numeral>\d[\d,.]*(?:%|[a-z]+\b)?)")


def _joined_marker(row: str) -> str:
    """Return ``row`` with each quality marker against its numeral, every column kept.

    The space the marker loses is given back at the next run of padding, so a value in a
    padded column keeps the next column where it was and a trailing value keeps the row
    its width.
    """
    found = _SPACED_MARKER.search(row)
    while found is not None:
        joined = found.group("marker") + found.group("numeral")
        row = row[: found.start()] + joined + row[found.end() :]
        end = found.start() + len(joined)
        gap = row.find("  ", end)
        row = row[:gap] + " " + row[gap:] if gap >= 0 else row + " "
        found = _SPACED_MARKER.search(row, end)
    return row


def _quality_prefix(frame: PackFrame) -> list[str]:
    """Set every quality marker against the numeral it qualifies, with no space between."""
    return [_joined_marker(row) for row in frame.rows]


# The pack's bare queue percentages and the named numerators the port's queue states.
_QUEUE_PROGRESS: Mapping[str, str] = {"~62%": "~5 of 8 steps", "~18%": "~2 of 11 steps"}
_QUEUE_ROW = re.compile(r"RUNNING\s+(?P<progress>~\d+%)\s*$")


def _named_progress(frame: PackFrame) -> list[str]:
    """Replace a queue row's bare percentage with the named numerator the port renders."""
    rows = list(frame.rows)
    for i, row in enumerate(rows):
        found = _QUEUE_ROW.search(row)
        if found is not None and found.group("progress") in _QUEUE_PROGRESS:
            named = _QUEUE_PROGRESS[found.group("progress")]
            rows[i] = pad(row[: found.start("progress")] + named, frame.w)
    return rows


_REMAINING_CHECK = "≈6m left"
_TYPICAL_CHECK = "typical ~6m"


def _typical_not_remaining(frame: PackFrame) -> list[str]:
    """Replace the checking phase's remaining time with the typical duration of the phase."""
    return [
        pad(row.replace(_REMAINING_CHECK, _TYPICAL_CHECK).rstrip(), frame.w)
        if _REMAINING_CHECK in row
        else row
        for row in frame.rows
    ]


_REPLAYING = "► "
_RUNNING = "⋯ "


def _running_step(frame: PackFrame) -> list[str]:
    """Draw a running campaign step with the running mark; ``►`` belongs to the state slot."""
    head, *body = frame.rows
    return [head, *(row.replace(_REPLAYING, _RUNNING) for row in body)]


# The notice row's em dash and the four spaces the declared no-value phrase takes from it.
_NOTICE_DUE = re.compile(r"(?<=OPEN {6})— {4}")
_NO_DEADLINE = "due –"  # noqa: RUF001


def _absent_deadline(frame: PackFrame) -> list[str]:
    """Write a notice row's absent deadline the way every other row writes its no-value."""
    return [_NOTICE_DUE.sub(_NO_DEADLINE, row) for row in frame.rows]


_PURGED_TITLE = "┌─ ✗ "
_ERROR_TITLE = "┌─ ! "


def _error_toast(frame: PackFrame) -> list[str]:
    """Lead an error toast's title with the error kind's ``!`` instead of the purged token."""
    return [row.replace(_PURGED_TITLE, _ERROR_TITLE) for row in frame.rows]


# The row a pack overlay splices its state model into, just above its keybar.
_STATE_ROW = re.compile(r"^ (?:STATE      |ENDS WHEN  |IMPOSSIBLE )")
_PACK_EVIDENCE = " Eä ▸ evidence · "
_PORT_ACCEPTANCE = " Eä ▸ acceptance evidence · "


def _recrumb(row: str, old: str, new: str) -> str:
    """Return header ``row`` with crumb ``old`` renamed ``new``, the right side kept in place."""
    grown = row.replace(old, new, 1)
    extra = cell_len(grown) - cell_len(row)
    gap = grown.find(" " * (extra + 2), len(new))
    return grown[:gap] + grown[gap + extra :] if gap >= 0 else pad(grown, cell_len(row))


def _backlog_open(frame: PackFrame) -> list[str]:
    """Name the Backlog's Enter for the card it opens rather than the card's own verb."""
    rows = list(frame.rows)
    pieces = _pieces(rows[-1])
    renamed = ["Enter open" if piece == "Enter promote" else piece for piece in pieces]
    if renamed != pieces:
        rows[-1] = _rebar(rows[-1], renamed)
    return rows


def _without_harness_rows(frame: PackFrame) -> list[str]:
    """Blank a decision overlay's review-harness rows: its stepped state and impossible legend.

    The pack stepped each overlay through its state model and narrated the transitions it
    can never take; the port states the record's own state from the projection instead.
    """
    return [" " * frame.w if _STATE_ROW.match(row) else row for row in frame.rows]


def _acceptance_evidence(frame: PackFrame) -> list[str]:
    """Name the milestone's evidence as the acceptance evidence overlay."""
    rows = list(frame.rows)
    rows[0] = _recrumb(rows[0], _PACK_EVIDENCE, _PORT_ACCEPTANCE)
    return rows


# Each cursor overlay the pack recorded: its ids, its table's head and what its foot calls
# one of the rows the cursor walks.
_CURSOR_TABLES: tuple[tuple[str, str, str], ...] = (
    ("overlay/evidence@*", "EVIDENCE   RECEIPT", "RECEIPT"),
    ("overlay/readiness@*", "READINESS SIGNAL", "SIGNAL"),
    ("overlay/draft@*", "WHAT IT ANSWERS", "FIELD"),
)
_FOOT_LABEL = 9


def _cursor_foot(frame: PackFrame) -> list[str]:
    """Name the row a cursor overlay's cursor is on, of how many, in the row after its body.

    Raises:
        StopIteration: the frame is none of the cursor overlays, or it draws no cursor.
    """
    rows = list(frame.rows)
    head, label = next(
        (head, label)
        for pattern, head, label in _CURSOR_TABLES
        if fnmatch.fnmatchcase(frame.contract_id, pattern)
    )
    at = next(i for i, row in enumerate(rows) if head in row)
    table: list[str] = []
    for row in rows[at + 1 :]:
        if row.startswith(RULE_THIN):
            break
        table.append(row)
    n = next(i for i, row in enumerate(table) if "▸" in row) + 1
    body = range(1, len(rows) - 1)
    last = max(i for i in body if rows[i].strip() and not _STATE_ROW.match(rows[i]))
    rows[last + 1] = pad(f" {pad(label, _FOOT_LABEL)} {n} of {len(table)}", frame.w)
    return rows


# The pack's card body, row by row, and what the presentation matrix states in its place:
# the three matrix columns, no settings owner, and no second ``NEEDS YOU`` on the frame.
_MATRIX_HEAD = "CLASS               TOAST               DECIDED BY"
_MATRIX_ROWS = tuple(
    f"{row.notification_class.value.replace('_', ' '):<19}{row.may_interrupt.value:<20}"
    f"{row.decided_by}"
    for row in NOTIFICATION_MATRIX.classes
)
_PACK_BODY = (
    "CLASS               MAY INTERRUPT   DECIDED BY",
    "needs permission   yes             policy",
    "needs your answer  yes             policy",
    "stopped responding yes             policy",
    "run finished       no              profile",
    "budget passed      no              ratified · R23",
    "Read only · settings ▸ interface owns this policy.",
    "A muted class still counts in !N NEEDS YOU.",
)
_PORT_BODY = (
    _MATRIX_HEAD,
    *_MATRIX_ROWS,
    "Read only · a toast is the one interruption; nothing takes focus.",
    "A class that raises no toast still counts in the header's count.",
)
_PACK_FOOT = " A run in this class "
_EFFECT = {
    ToastPolicy.YES: "may raise a toast",
    ToastPolicy.NO: "raises no toast",
    ToastPolicy.ONCE_PER_REVISION: "may raise one toast per revision",
}


def _matrix_body(frame: PackFrame) -> list[str]:
    """Draw the notifications card from the presentation matrix the port projects."""
    swap = dict(zip(_PACK_BODY, _PORT_BODY, strict=True))
    rows: list[str] = []
    selected = 0
    for row in frame.rows:
        inner = row[2:-1] if row.startswith("│ ") and row.endswith("│") else None
        if inner is None:
            rows.append(row)
            continue
        text = inner.rstrip()
        mark, bare = (
            (text[0], text[1:]) if text[:1] in ("▸", " ") and text[1:] in swap else ("", text)
        )
        if mark == "▸":
            selected = _PACK_BODY.index(bare) - 1
        if bare in swap:
            text = mark + swap[bare]
        rows.append("│ " + pad(text, cell_len(inner)) + "│")
    chosen = NOTIFICATION_MATRIX.classes[selected]
    name = chosen.notification_class.value.replace("_", " ")
    foot = f" A {name} {_EFFECT[chosen.may_interrupt]}, as the {chosen.decided_by} decided."
    return [pad(foot, frame.w) if row.startswith(_PACK_FOOT) else row for row in rows]


# The frame rows the roadmap's lanes occupy: under the week header, three lanes of two rows.
_LANES = range(4, 10)
_LANE_BAR = re.compile(r"^▸\S")
_LANE_IDS = re.compile(r"\d{4}")
_MARKER_GLYPHS = frozenset("●○")
_MORE_BELOW = re.compile(r"^   \d+ more rows? below")


def recorded_mark(keys: Sequence[str]) -> int:
    """Return the marker a recorded frame's keys leave the roadmap cursor on.

    The pack recorded a frame's setup and keys but not its marker, which it drew in colour
    alone; the marker walks one step per arrow, so the keys state it.
    """
    return max(0, sum((key == "ArrowRight") - (key == "ArrowLeft") for key in keys))


def _text_marker(frame: PackFrame) -> list[str]:
    """Draw the roadmap's marker cursor in text, and the ``MARKER`` row naming it.

    The pack drew the focused marker in colour alone, so the same frame answered for
    every marker. The port brackets the focused marker on the focused lane, adds the row
    naming the milestone, its lane and its position under the lanes, and gives back the
    blank row the new one takes.
    """
    rows = list(frame.rows)
    focused = next((i for i in _LANES if i < len(rows) and _LANE_BAR.match(rows[i])), None)
    if focused is None:
        return rows
    ids = _LANE_IDS.findall(rows[focused + 1])
    glyphs = [i for i, ch in enumerate(rows[focused]) if ch in _MARKER_GLYPHS]
    if not ids or len(ids) != len(glyphs):
        return rows
    at = min(frame.mark, len(glyphs) - 1)
    cell = glyphs[at]
    row = rows[focused]
    rows[focused] = row[: cell - 1] + "[" + row[cell] + "]" + row[cell + 2 :]
    lane = row[1:].split(" ", 1)[0]
    marker = f" MARKER    MLS-{ids[at]} · {lane} · {at + 1} of {len(ids)}"
    # the new row takes the room a blank row gave, or pushes one more row under a pane
    below = next((i for i, row in enumerate(rows) if _MORE_BELOW.match(row)), None)
    blanks = [i for i, row in enumerate(rows[:-1]) if not row.strip()]
    if below is not None:
        del rows[below - 1]
    elif blanks:
        del rows[blanks[-1]]
    else:
        return list(frame.rows)
    rows.insert(_LANES.stop, pad(marker, frame.w))
    return rows


REWRITES: tuple[Rewrite, ...] = (
    Rewrite(entry="entry simulator pair", rows=_entry_without_simulator, keys=frozenset("[]")),
    Rewrite(entry="help simulator row", rows=_help_without_simulator, keys=frozenset("w")),
    Rewrite(entry="disconnected body", rows=_disconnected_body),
    Rewrite(entry="window indicator", rows=_edge_markers),
    Rewrite(entry="activity keybar and rail", rows=_full_page_keys),
    Rewrite(entry="attention keybar", rows=_advertised_open),
    Rewrite(entry="settings editor keybar", rows=_unslashed_keys),
    Rewrite(entry="quality prefix", rows=_quality_prefix),
    Rewrite(entry="unattended progress", rows=_named_progress),
    Rewrite(entry="verification time basis", rows=_typical_not_remaining),
    Rewrite(entry="campaign step glyph", rows=_running_step),
    Rewrite(entry="absent deadline", rows=_absent_deadline),
    Rewrite(entry="error toast glyph", rows=_error_toast),
    Rewrite(entry="backlog open verb", rows=_backlog_open),
    Rewrite(entry="decision overlay harness rows", rows=_without_harness_rows),
    Rewrite(
        entry="acceptance evidence overlay",
        rows=_acceptance_evidence,
        overlays={"evidence": "acceptance"},
    ),
    Rewrite(entry="cursor overlay foot", rows=_cursor_foot),
    Rewrite(entry="notifications matrix", rows=_matrix_body),
    Rewrite(entry="roadmap marker cursor", rows=_text_marker),
)


@dataclass(frozen=True, slots=True)
class Comparison:
    """The outcome of comparing one recorded frame with the port's render.

    Attributes:
        ok: Whether the frames match once the map's rewrites are applied.
        detail: Why they differ; empty when they match.
        row: The first differing row, ``-1`` for a row-count mismatch, ``None`` on a match.
        expected: The expected row at ``row``.
        actual: The rendered row at ``row``.
    """

    ok: bool
    detail: str = ""
    row: int | None = None
    expected: str = ""
    actual: str = ""


def first_diff(expected: str, actual: str) -> Comparison:
    """Compare two frames whole, row for row, with trailing spaces kept."""
    want, got = expected.split("\n"), actual.split("\n")
    if len(want) != len(got):
        return Comparison(False, f"row count {len(got)} expected {len(want)}", -1)
    for i, (a, b) in enumerate(zip(want, got, strict=True)):
        if a != b:
            return Comparison(False, f"row {i} differs", i, a, b)
    return Comparison(True)


class Normaliser:
    """Apply a normalisation map's realised rewrites and compare through them.

    Args:
        table: The loaded map.
        pack: Every recorded frame state of the contract, by id.
        rewrites: The realised rewrites, each naming a map entry.

    Raises:
        ValueError: a rewrite names no map entry, or two rewrites realise one entry.
    """

    def __init__(
        self,
        table: NormalisationMap,
        pack: Mapping[str, str],
        rewrites: Sequence[Rewrite] = REWRITES,
    ) -> None:
        names = [r.entry for r in rewrites]
        repeated = sorted({n for n in names if names.count(n) > 1})
        if repeated:
            raise ValueError(f"entries realised twice: {', '.join(repeated)}")
        outside = sorted(set(names) - {e.entry for e in table.entries})
        if outside:
            raise ValueError(f"rewrites outside the normalisation map: {', '.join(outside)}")
        self.table = table
        self.pack = pack
        self._rewrites = [(table.by_name(r.entry), r) for r in rewrites]

    def applied(self, contract_id: str) -> list[str]:
        """Return the names of the realised entries that touch ``contract_id``."""
        return [entry.entry for entry, _r in self._rewrites if entry.selects(contract_id)]

    def simulated_keys(self, contract_id: str) -> frozenset[str]:
        """Return the pack keys the replay performs as harness actions for ``contract_id``."""
        keys: set[str] = set()
        for entry, rewrite in self._rewrites:
            if entry.selects(contract_id):
                keys |= rewrite.keys
        return frozenset(keys)

    def setup(self, contract_id: str, setup: SessionSetup) -> SessionSetup:
        """Return the setup the port replays for ``contract_id``: its overlay renamed as mapped."""
        overlay = setup.overlay
        for entry, rewrite in self._rewrites:
            if overlay is not None and overlay in rewrite.overlays and entry.selects(contract_id):
                overlay = rewrite.overlays[overlay]
        return setup if overlay == setup.overlay else setup.model_copy(update={"overlay": overlay})

    def expected(self, contract_id: str, frame: str, *, mark: int = 0) -> str:
        """Return the port's expected frame: the pack frame through each rewrite selecting it.

        Args:
            contract_id: The frame or journey id, which selects the rewrites.
            frame: The pack's recorded frame.
            mark: The roadmap marker the cursor stands on when the frame was drawn.
        """
        rows = tuple(frame.split("\n"))
        for entry, rewrite in self._rewrites:
            if rewrite.rows is not None and entry.selects(contract_id):
                page = PackFrame(contract_id=contract_id, rows=rows, pack=self.pack, mark=mark)
                rows = tuple(rewrite.rows(page))
        return "\n".join(rows)

    def compare(
        self, contract_id: str, recorded: str, rendered: str, *, mark: int = 0
    ) -> Comparison:
        """Compare the port's render with the recorded frame through the map."""
        return first_diff(self.expected(contract_id, recorded, mark=mark), rendered)
