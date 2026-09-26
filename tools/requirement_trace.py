"""Trace every v0.7 requirement id to an owner, or to a typed deferral.

The requirement catalog ``.ea/requirements.json`` carries each packet id with
a short title and, beside it, a generated trace row: the waves whose text cites
the id, the test modules and the production modules that name it, and the
decision that defers it when one does. A row with neither an owning wave nor a
deferral is ``unowned``, the state a census exists to make visible.

The trace is a census, so it is generated and never hand-edited (LINT-038).
Freshness is value equality: ``check`` recomputes the trace from the committed
state and source and fails only when the values differ from the stored ones.
The stamped revision is provenance for the reader and never compared, so a
merge that moves the revision without changing a value passes.

The ids and titles come from the local proposal packet, which is not
committed, so ``extract`` runs only where the packet exists. Everywhere else,
CI included, the committed catalog is the input and only the trace is
recomputed.

Usage::

    uv run python tools/requirement_trace.py write
    uv run python tools/requirement_trace.py check [--require-owned]
    uv run python tools/requirement_trace.py extract --packet <dir>

Exit codes: ``0`` when the stored census is fresh (and, with
``--require-owned``, every id is owned or deferred), ``1`` otherwise, ``2``
when an input cannot be read or validated.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CATALOG_PATH: Final = Path(".ea/requirements.json")
STATE_PATH: Final = Path(".ea/state.json")
SCHEMA_VERSION: Final = 1

#: A wave in one of these states no longer carries the work it cited.
DEAD_WAVE_STATUSES: Final = frozenset({"abandoned", "failed"})

#: Where test citations and production citations are read from.
TEST_ROOTS: Final[tuple[str, ...]] = ("tests",)
PRODUCER_ROOTS: Final[tuple[str, ...]] = ("src", "tools")

#: A definition row is a table row under an ``ID`` (or ``Rule``) column
#: followed by one of these columns; any other table only cites ids.
DEFINITION_FIRST_COLUMNS: Final = frozenset({"ID", "Rule"})
DEFINITION_SECOND_COLUMNS: Final = frozenset(
    {"Requirement", "Decision", "Ruling", "Gate", "Assertion"}
)

TITLE_MAX_CHARS: Final = 100

#: A range wider than this is a typo or a number that is not an id suffix.
MAX_RANGE_SPAN: Final = 200

_ID_PATTERN: Final = r"^[A-Z]+-\d{3}$"
_BT: Final = r"`?"
_SEP: Final = r"\s*(?:\.\.|\u2013|\u2014|\bto\b|\bthrough\b|-(?=\s*`?(?:[A-Z]+-)?\d{3}\b))\s*"
_TAIL_ITEM_RE: Final = re.compile(r"(\d{3})\b(?:\s*\.\.\s*(\d{3})\b)?")
_ROW_ID_RE: Final = re.compile(r"^\|\s*`(?P<id>[A-Z]+-\d{3})`\s*\|(?P<rest>.*)$")
_TABLE_RULE_RE: Final = re.compile(r"^\|[\s:|-]+\|\s*$")
_LEAK_RE: Final = re.compile(r"/(?:Users|home)/|~(?=/)|[A-Za-z]:\\|[\w.+-]+@[\w-]+\.[\w.]+")


class TraceStatus(StrEnum):
    """How one requirement id is accounted for."""

    OWNED = "owned"
    DEFERRED = "deferred"
    UNOWNED = "unowned"


class Deferral(BaseModel):
    """Ids a recorded decision moves out of the current release."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: str = Field(pattern=r"^D\d+$")
    release: str = Field(min_length=1)
    ids: tuple[str, ...] = Field(min_length=1)


class RequirementRow(BaseModel):
    """One catalog id with its generated trace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_ID_PATTERN)
    title: str = Field(min_length=1, max_length=TITLE_MAX_CHARS + 3)
    status: TraceStatus = TraceStatus.UNOWNED
    owners: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    producers: tuple[str, ...] = ()
    deferral: str | None = None


class TraceSummary(BaseModel):
    """Counts derived from the rows, never written by hand."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=0)
    owned: int = Field(ge=0)
    deferred: int = Field(ge=0)
    unowned: int = Field(ge=0)


class Catalog(BaseModel):
    """The committed requirement catalog and its census."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=SCHEMA_VERSION, le=SCHEMA_VERSION)
    revision: str | None = None
    summary: TraceSummary
    deferrals: tuple[Deferral, ...] = ()
    requirements: tuple[RequirementRow, ...] = Field(min_length=1)


class _WaveView(BaseModel):
    # A projection of the state's wave record. The state document has its own
    # strict validator; this reader takes only the fields the trace cites, so
    # it must tolerate the rest.
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    status: str
    title: str = ""
    description: str | None = None
    intent: Any = None
    success_criteria: list[dict[str, Any]] | None = None
    outcome: Any = None

    def cited_text(self) -> str:
        """Return the text a wave states its scope in."""
        criteria = [row.get("text") for row in self.success_criteria or []]
        return json.dumps(
            [self.title, self.description, self.intent, criteria, self.outcome],
            ensure_ascii=False,
            default=str,
        )


class _DecisionView(BaseModel):
    # Projection of a state decision record; see _WaveView for why extra fields pass.
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    status: str
    superseded_by: str | None = None

    @property
    def in_force(self) -> bool:
        """Whether the decision still binds."""
        return self.status == "active" and self.superseded_by is None


class StateView(BaseModel):
    """The slice of ``.ea/state.json`` the trace reads."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    waves: dict[str, _WaveView] = Field(default_factory=dict)
    decisions: dict[str, _DecisionView] = Field(default_factory=dict)


class TraceInputError(ValueError):
    """An input the trace cannot be built from."""


@functools.cache
def _citation_re(families: frozenset[str]) -> re.Pattern[str]:
    alternatives = "|".join(sorted(families, key=lambda family: (-len(family), family)))
    return re.compile(
        rf"\b(?P<fam>{alternatives})-(?P<start>\d{{3}})\b{_BT}"
        rf"(?:{_SEP}{_BT}(?:(?P=fam)-)?(?P<end>\d{{3}})\b{_BT})?"
        rf"(?P<tail>(?:\s*,\s*(?:and\s+)?\d{{3}}\b(?:\s*\.\.\s*\d{{3}}\b)?)*)"
    )


def cited_ids(text: str, families: frozenset[str]) -> set[str]:
    """Return every requirement id a text cites, with ranges expanded.

    ``FAM-031..039``, ``FAM-031 to FAM-039``, ``FAM-031``-``FAM-039`` and the
    continuation form ``FAM-001..037, 041, 046..049`` all expand. A range
    that runs backwards or spans more than :data:`MAX_RANGE_SPAN` keeps only
    its first id.

    Args:
        text: Any prose.
        families: The id prefixes that name requirements; others are ignored.

    Returns:
        The cited ids, family-prefixed and zero-padded.

    Raises:
        ValueError: When *families* is empty.
    """
    if not families:
        raise ValueError("cited_ids needs at least one family")
    found: set[str] = set()
    for match in _citation_re(families).finditer(text):
        family = match["fam"]
        found.update(_expand(family, match["start"], match["end"]))
        for item in _TAIL_ITEM_RE.finditer(match["tail"] or ""):
            found.update(_expand(family, item[1], item[2]))
    return found


def _expand(family: str, start: str, end: str | None) -> list[str]:
    first = int(start)
    last = int(end) if end is not None else first
    if last < first or last - first > MAX_RANGE_SPAN:
        last = first
    return [f"{family}-{number:03d}" for number in range(first, last + 1)]


def _title_of(cell: str) -> str:
    """Condense a requirement cell to a one-line title."""
    bold = re.match(r"\s*\*\*(.+?)\*\*", cell)
    text = bold[1] if bold else re.split(r"(?<=[.;])\s+(?=[A-Z`*])", cell.strip(), maxsplit=1)[0]
    text = re.sub(r"[`*]", "", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    if len(text) > TITLE_MAX_CHARS:
        text = f"{text[:TITLE_MAX_CHARS].rsplit(' ', 1)[0]}..."
    return text


def _definition_rows(lines: Sequence[str]) -> Iterable[tuple[str, str]]:
    """Yield ``(id, first requirement cell)`` for each definition-table row."""
    header: list[str] | None = None
    for index, line in enumerate(lines):
        if not line.startswith("|"):
            header = None
            continue
        if index + 1 < len(lines) and _TABLE_RULE_RE.match(lines[index + 1]):
            header = [cell.strip() for cell in line.strip().strip("|").split("|")]
            continue
        row = _ROW_ID_RE.match(line)
        if row is None or header is None or len(header) < 2:
            continue
        if header[0] in DEFINITION_FIRST_COLUMNS and header[1] in DEFINITION_SECOND_COLUMNS:
            yield row["id"], row["rest"].split(" | ", 1)[0].strip().rstrip("|")


def extract_catalog(packet_dir: Path) -> dict[str, str]:
    """Read the definition rows of a proposal packet.

    Only the numbered files (``NN-*.md``) are read, and only a row whose table
    header opens with a definition column pair counts.

    Args:
        packet_dir: The proposal directory.

    Returns:
        Id to title, in id order.

    Raises:
        TraceInputError: When the directory holds no numbered file, when an id
            is defined twice, or when a title would carry a path or an address.
    """
    files = sorted(packet_dir.glob("[0-9][0-9]-*.md"))
    if not files:
        raise TraceInputError(f"no numbered packet files under {packet_dir.name}")
    titles: dict[str, str] = {}
    for path in files:
        for req_id, cell in _definition_rows(path.read_text(encoding="utf-8").splitlines()):
            if req_id in titles:
                raise TraceInputError(f"{req_id} is defined twice (again in {path.name})")
            title = _title_of(cell)
            if _LEAK_RE.search(title):
                raise TraceInputError(f"{req_id} title carries a path or an address")
            titles[req_id] = title
    return dict(sorted(titles.items()))


def load_state(path: Path) -> StateView:
    """Read the trace's slice of a state document.

    Args:
        path: The ``state.json`` file.

    Returns:
        The validated state slice.

    Raises:
        TraceInputError: When the file is missing, not JSON, or malformed.
    """
    try:
        return StateView.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise TraceInputError(f"cannot read state {path.name}: {exc}") from exc


def load_catalog(path: Path) -> Catalog:
    """Read and validate the committed catalog.

    Args:
        path: The committed requirements catalog file.

    Returns:
        The validated catalog.

    Raises:
        TraceInputError: When the file is missing, not JSON, or malformed.
    """
    try:
        return Catalog.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise TraceInputError(f"cannot read catalog {path.name}: {exc}") from exc


def scan_citations(
    repo_root: Path, roots: Iterable[str], families: frozenset[str]
) -> dict[str, set[str]]:
    """Map each cited id to the Python modules under *roots* that name it.

    Args:
        repo_root: The repository the roots are relative to.
        roots: Top-level directories to read; a missing one is skipped.
        families: The id prefixes that name requirements.

    Returns:
        Id to repo-relative POSIX paths.
    """
    cited: dict[str, set[str]] = defaultdict(set)
    for root in roots:
        base = repo_root / root
        if not base.is_dir():
            continue
        for module in base.rglob("*.py"):
            relative = module.relative_to(repo_root).as_posix()
            for req_id in cited_ids(module.read_text(encoding="utf-8", errors="replace"), families):
                cited[req_id].add(relative)
    return cited


def _deferred_ids(
    deferrals: Sequence[Deferral], state: StateView, known: set[str]
) -> dict[str, str]:
    """Resolve each deferral against the decision it names.

    A deferral whose decision is no longer in force defers nothing, so its ids
    fall back to their owners.

    Raises:
        TraceInputError: When a deferral names a decision the state lacks or an
            id the catalog lacks.
    """
    deferred: dict[str, str] = {}
    for deferral in deferrals:
        decision = state.decisions.get(deferral.decision)
        if decision is None:
            raise TraceInputError(f"deferral names {deferral.decision}, which state does not carry")
        unknown = sorted(set(deferral.ids) - known)
        if unknown:
            raise TraceInputError(
                f"{deferral.decision} defers ids the catalog lacks: {', '.join(unknown)}"
            )
        if decision.in_force:
            deferred.update(dict.fromkeys(deferral.ids, deferral.decision))
    return deferred


def build_trace(
    *,
    titles: Mapping[str, str],
    deferrals: Sequence[Deferral],
    state: StateView,
    repo_root: Path,
) -> Catalog:
    """Recompute the census from state and source.

    Args:
        titles: Id to title, the catalog's authored half.
        deferrals: The deferral bindings the catalog declares.
        state: The committed state slice.
        repo_root: Where tests and producers are read from.

    Returns:
        The catalog with every row traced and the summary derived.

    Raises:
        TraceInputError: When a deferral cannot be resolved.
    """
    known = set(titles)
    families = frozenset(req_id.rsplit("-", 1)[0] for req_id in known)
    deferred = _deferred_ids(deferrals, state, known)
    owners: dict[str, set[str]] = defaultdict(set)
    for wave in state.waves.values():
        if wave.status in DEAD_WAVE_STATUSES:
            continue
        for req_id in cited_ids(wave.cited_text(), families) & known:
            owners[req_id].add(wave.id)
    tests = scan_citations(repo_root, TEST_ROOTS, families)
    producers = scan_citations(repo_root, PRODUCER_ROOTS, families)
    rows = []
    for req_id in sorted(titles):
        if req_id in deferred:
            status = TraceStatus.DEFERRED
        elif owners.get(req_id):
            status = TraceStatus.OWNED
        else:
            status = TraceStatus.UNOWNED
        rows.append(
            RequirementRow(
                id=req_id,
                title=titles[req_id],
                status=status,
                owners=tuple(sorted(owners.get(req_id, ()))),
                tests=tuple(sorted(tests.get(req_id, ()))),
                producers=tuple(sorted(producers.get(req_id, ()))),
                deferral=deferred.get(req_id),
            )
        )
    counts = {status: sum(row.status is status for row in rows) for status in TraceStatus}
    return Catalog(
        schema_version=SCHEMA_VERSION,
        revision=_head_revision(repo_root),
        summary=TraceSummary(
            total=len(rows),
            owned=counts[TraceStatus.OWNED],
            deferred=counts[TraceStatus.DEFERRED],
            unowned=counts[TraceStatus.UNOWNED],
        ),
        deferrals=tuple(deferrals),
        requirements=tuple(rows),
    )


def _head_revision(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def stale_ids(stored: Catalog, fresh: Catalog) -> list[str]:
    """Return the ids whose stored trace differs from the recomputed one.

    The revision is provenance and never compared. A changed summary or
    deferral table with no changed row reports ``summary`` or ``deferrals``.

    Args:
        stored: The committed catalog.
        fresh: The catalog recomputed from the current tree.

    Returns:
        The sorted changed requirement ids, plus ``summary`` / ``deferrals``.
    """
    before = {row.id: row for row in stored.requirements}
    after = {row.id: row for row in fresh.requirements}
    changed = sorted(i for i in before.keys() | after.keys() if before.get(i) != after.get(i))
    if stored.summary != fresh.summary and not changed:
        changed.append("summary")
    if stored.deferrals != fresh.deferrals:
        changed.append("deferrals")
    return changed


def render(catalog: Catalog) -> str:
    """Serialise the catalog with one requirement per line for reviewable diffs.

    Args:
        catalog: The catalog to serialise.

    Returns:
        The JSON text, newline-terminated.
    """
    head = catalog.model_dump(mode="json", exclude={"requirements"})
    rows = [
        json.dumps(row.model_dump(mode="json"), ensure_ascii=False) for row in catalog.requirements
    ]
    body = json.dumps(head, indent=2, ensure_ascii=False)[:-2]
    return f'{body},\n  "requirements": [\n    ' + ",\n    ".join(rows) + "\n  ]\n}\n"


def _recompute(repo_root: Path, stored: Catalog) -> Catalog:
    return build_trace(
        titles={row.id: row.title for row in stored.requirements},
        deferrals=stored.deferrals,
        state=load_state(repo_root / STATE_PATH),
        repo_root=repo_root,
    )


def _report(catalog: Catalog) -> str:
    s = catalog.summary
    return f"{s.total} ids: {s.owned} owned, {s.deferred} deferred, {s.unowned} unowned"


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch the ``write``, ``check`` and ``extract`` verbs.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description="requirement id trace census")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("write", help="recompute the trace and store it")
    check = verbs.add_parser("check", help="fail when the stored trace is stale")
    check.add_argument("--require-owned", action="store_true", help="also fail on any unowned id")
    extract = verbs.add_parser("extract", help="re-read ids and titles from the packet")
    extract.add_argument("--packet", type=Path, required=True)
    args = parser.parse_args(argv)
    repo_root: Path = args.repo_root
    catalog_path = repo_root / CATALOG_PATH
    try:
        if args.verb == "extract":
            # The first extract runs before any catalog exists; a later one
            # keeps the authored deferral table.
            deferrals = load_catalog(catalog_path).deferrals if catalog_path.exists() else ()
            fresh = build_trace(
                titles=extract_catalog(args.packet),
                deferrals=deferrals,
                state=load_state(repo_root / STATE_PATH),
                repo_root=repo_root,
            )
        else:
            stored = load_catalog(catalog_path)
            fresh = _recompute(repo_root, stored)
    except TraceInputError as exc:
        print(f"requirement-trace: {exc}", file=sys.stderr)
        return 2
    if args.verb in {"write", "extract"}:
        catalog_path.write_text(render(fresh), encoding="utf-8")
        print(f"requirement-trace: wrote {CATALOG_PATH.as_posix()} ({_report(fresh)})")
        return 0
    status = 0
    stale = stale_ids(stored, fresh)
    if stale:
        shown = ", ".join(stale[:20]) + (" ..." if len(stale) > 20 else "")
        print(
            f"requirement-trace: stale census, {len(stale)} row(s) differ: {shown}; "
            "run `uv run python tools/requirement_trace.py write`",
            file=sys.stderr,
        )
        status = 1
    unowned = [row.id for row in fresh.requirements if row.status is TraceStatus.UNOWNED]
    if args.require_owned and unowned:
        print(
            f"requirement-trace: {len(unowned)} unowned id(s): {', '.join(unowned)}",
            file=sys.stderr,
        )
        status = 1
    if status == 0:
        print(f"requirement-trace: census fresh ({_report(fresh)})")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
