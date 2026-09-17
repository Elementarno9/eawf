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
map is the one place a corrected route key or family is written.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from eawf.surfaces.tui.console.keybar import KEY, Pair, keybar
from eawf.surfaces.tui.console.keymap import ATTACH_LATER
from eawf.surfaces.tui.console.tokens import CONNECTION
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
        route_family: The port's family for ``route_id``.

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
    route_family: str | None = None

    @model_validator(mode="after")
    def _check(self) -> NormalisationEntry:
        """Reject an empty entry or a half-written classification correction."""
        if not self.golden_ids:
            raise ValueError(f"entry {self.entry!r} names no golden id")
        if not self.rulings:
            raise ValueError(f"entry {self.entry!r} names no ruling")
        parts = (self.route_id, self.route_key, self.route_family)
        if any(p is not None for p in parts) and not all(p is not None for p in parts):
            raise ValueError(
                f"entry {self.entry!r} is a half-written route correction: "
                "route_id, route_key and route_family stand or fall together"
            )
        return self

    def selects(self, contract_id: str) -> bool:
        """Return whether ``contract_id`` is one of the records this entry touches."""
        return any(fnmatch.fnmatchcase(contract_id, pattern) for pattern in self.golden_ids)


class RouteCorrection(BaseModel):
    """The port's key and family for one pack route id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    family: str


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
            e.route_id: RouteCorrection(key=e.route_key, family=e.route_family)
            for e in self.entries
            if e.route_id is not None and e.route_key is not None and e.route_family is not None
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
    """

    contract_id: str
    rows: tuple[str, ...]
    pack: Mapping[str, str]

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
    """

    entry: str
    rows: RowsRewrite | None = None
    keys: frozenset[str] = field(default_factory=frozenset)


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
_DISCONNECTED_LABEL = "disconnected · nothing is arriving"


def _slot(conn: str) -> str:
    return f"{CONNECTION[conn].unicode} {conn}"


def _disconnected_body(frame: PackFrame) -> list[str]:
    """Return the port's disconnected frame: the pack's gap frame of the route, relabelled.

    Both states refuse writes and read the same revision, so the port draws them alike
    but for the label; a route whose gap frame reads like its live frame does not read
    the connection state, and keeps the recorded frame.
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


def _unslashed_keys(frame: PackFrame) -> list[str]:
    """Name a two-key verb once, as the port's key table does, instead of a slashed pair."""
    return _renamed_pairs(frame, {"↑/↓": "↑↓", "J/K": "J K"})


REWRITES: tuple[Rewrite, ...] = (
    Rewrite(entry="entry simulator pair", rows=_entry_without_simulator, keys=frozenset("[]")),
    Rewrite(entry="help simulator row", rows=_help_without_simulator, keys=frozenset("w")),
    Rewrite(entry="disconnected body", rows=_disconnected_body),
    Rewrite(entry="window indicator", rows=_edge_markers),
    Rewrite(entry="activity keybar and rail", rows=_full_page_keys),
    Rewrite(entry="settings editor keybar", rows=_unslashed_keys),
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

    def expected(self, contract_id: str, frame: str) -> str:
        """Return the port's expected frame: the pack frame through each rewrite selecting it."""
        rows = tuple(frame.split("\n"))
        for entry, rewrite in self._rewrites:
            if rewrite.rows is not None and entry.selects(contract_id):
                page = PackFrame(contract_id=contract_id, rows=rows, pack=self.pack)
                rows = tuple(rewrite.rows(page))
        return "\n".join(rows)

    def compare(self, contract_id: str, recorded: str, rendered: str) -> Comparison:
        """Compare the port's render with the recorded frame through the map."""
        return first_diff(self.expected(contract_id, recorded), rendered)
