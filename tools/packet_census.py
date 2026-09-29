"""The proposal packet's census lint: scan the specification and regenerate its census.

The v0.7 proposal packet defines every requirement id in a table row and cites ids
everywhere else, and it records a census of how many ids each family defines. Every
hand-maintained count in it has drifted at least once, so the census is generated here
and a stored census that disagrees with the regeneration fails.

A definition row is a table row whose first cell is a backticked ``FAM-NNN`` inside a
table headed ``ID`` (or ``Rule``) followed by ``Requirement``, ``Decision``, ``Ruling``,
``Gate`` or ``Assertion``; any other table only cites ids. The scan fails on

- a duplicate definition, and a family defined in more than one home file;
- a dangling citation: an id cited anywhere, ranges expanded, and defined nowhere; a gap
  below its family's highest number named on its own is that gap's record, but a range
  spanning it still claims it and fails;
- a headerless definition-row fragment, which is what a blank line inside a table makes;
- a cycle, a node without a file, or a file without a node in the authority file's
  dependency graph;
- a scrub hit (a local path, host, local URL or address);
- a stored census row or distinct-id total, in the newest regenerated-census section,
  that disagrees with the regeneration.

Fenced blocks are read for citations only: a table-shaped line inside a fence is neither
a definition nor a fragment, and an id there needs no backticks. Dated amendment rows are
history, so a count inside one is never compared. A gap below a family's highest number is
recorded in the census and never reported as a defect.

The packet is local and gitignored, so where it is absent -- CI included -- the lint says
so and passes. Its output carries the revision it was read at.

Usage::

    uv run python tools/packet_census.py [--packet DIR]

Exit codes: ``0`` clean or skipped, ``1`` on any finding.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from eawf.platform.lint.eawf027_citation_scope import PROPOSAL_PACKET_DIR
from eawf.platform.scrub.scan import scan_text

_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from requirement_trace import (  # noqa: E402
    DEFINITION_FIRST_COLUMNS,
    DEFINITION_SECOND_COLUMNS,
    cited_ids,
)

#: The file whose dependency graph names every numbered file.
AUTHORITY_PREFIX: Final = "00"

_ROW_ID_RE: Final = re.compile(r"^\|\s*`(?P<id>(?P<fam>[A-Z]+)-(?P<num>\d{3}))`\s*\|")
_TABLE_RULE_RE: Final = re.compile(r"^\|[\s:|-]+\|\s*$")
_FENCE_RE: Final = re.compile(r"^\s*(```|~~~)")
_DATED_ROW_RE: Final = re.compile(r"^\|\s*\d{4}-\d{2}-\d{2}\s*\|")
_NODE_RE: Final = re.compile(r'(?P<node>\b[A-Z]{1,3})\["(?P<num>\d{2}) ')
_EDGE_RE: Final = re.compile(r"\b(?P<src>[A-Z]{1,3})(?:\[[^\]]*\])?\s*-->\s*(?P<dst>[A-Z]{1,3})\b")
_CENSUS_HEADING_RE: Final = re.compile(r"^#{2,4} Regenerated censuses")
_CENSUS_ROW_RE: Final = re.compile(
    r"^\|\s*`(?P<fam>[A-Z]+)`\s*\|\s*`(?P<home>\d{2})`\s*\|\s*(?P<defined>\d+)\s*\|"
    r"\s*(?P<max>\d{3})\s*\|\s*(?P<next>\d{3})\s*\|\s*(?P<gaps>[^|]*)\|"
)
_TOTAL_RE: Final = re.compile(
    r"Distinct defined requirement identifiers across the numbered files: \*\*(?P<n>\d+)\*\*"
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One census defect.

    Attributes:
        file: The packet file, by name.
        line: The 1-based line, or 0 for a defect of the whole packet.
        subject: The offending id, family, node or census row.
        reason: What is wrong.
    """

    file: str
    line: int
    subject: str
    reason: str

    def render(self) -> str:
        """Return the ``file:line: subject: reason`` diagnostic."""
        return f"{self.file}:{self.line}: {self.subject}: {self.reason}"


@dataclass(frozen=True, slots=True)
class FamilyCensus:
    """One family's regenerated census row.

    Attributes:
        family: The id prefix.
        home: The two-digit prefix of the one file defining it.
        defined: Distinct ids defined.
        highest: The highest number defined.
        gaps: Numbers below the highest that no row defines.
    """

    family: str
    home: str
    defined: int
    highest: int
    gaps: tuple[int, ...]


@dataclass(slots=True)
class _Scan:
    """What one pass over the numbered files collects."""

    definitions: dict[str, list[tuple[str, int]]] = field(default_factory=lambda: defaultdict(list))
    citations: list[tuple[str, int, str]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _scan_file(name: str, lines: Sequence[str], scan: _Scan) -> None:
    """Collect ``name``'s definitions, citing lines, fragments and scrub hits into ``scan``."""
    header: list[str] | None = None
    fenced = False
    for index, line in enumerate(lines):
        number = index + 1
        for hit in scan_text(line):
            scan.findings.append(Finding(name, number, hit.kind, "scrub hit"))
        if _FENCE_RE.match(line):
            fenced = not fenced
            header = None
            continue
        scan.citations.append((name, number, line))
        if fenced:
            continue
        if not line.startswith("|"):
            header = None
            continue
        if index + 1 < len(lines) and _TABLE_RULE_RE.match(lines[index + 1]):
            header = [cell.strip() for cell in line.strip().strip("|").split("|")]
            continue
        row = _ROW_ID_RE.match(line)
        if row is None or _TABLE_RULE_RE.match(line):
            continue
        if header is None:
            scan.findings.append(
                Finding(name, number, row["id"], "headerless definition-row fragment")
            )
        elif (
            len(header) >= 2
            and header[0] in DEFINITION_FIRST_COLUMNS
            and header[1] in DEFINITION_SECOND_COLUMNS
        ):
            scan.definitions[row["id"]].append((name, number))


def _graph_findings(name: str, lines: Sequence[str], files: Iterable[str]) -> list[Finding]:
    """Return the dependency-graph defects of the authority file's mermaid block."""
    block: list[str] = []
    inside = False
    for line in lines:
        if line.strip().startswith("```mermaid"):
            inside = True
            continue
        if inside and _FENCE_RE.match(line):
            break
        if inside:
            block.append(line)
    if not block:
        return [Finding(name, 0, "graph", "the authority file has no dependency graph")]
    nodes = {m["node"]: m["num"] for line in block for m in _NODE_RE.finditer(line)}
    edges: dict[str, set[str]] = defaultdict(set)
    for line in block:
        for m in _EDGE_RE.finditer(line):
            edges[m["src"]].add(m["dst"])
    prefixes = {file[:2] for file in files}
    findings = [
        Finding(name, 0, f"node {node}", f"names file {num}, which does not exist")
        for node, num in sorted(nodes.items())
        if num not in prefixes
    ]
    findings += [
        Finding(name, 0, f"file {prefix}", "has no node in the dependency graph")
        for prefix in sorted(prefixes - set(nodes.values()))
    ]
    findings += [
        Finding(name, 0, f"node {node}", "is on a cycle") for node in _cyclic(nodes, edges)
    ]
    return findings


def _cyclic(nodes: dict[str, str], edges: dict[str, set[str]]) -> list[str]:
    """Return the nodes on some cycle, sorted."""
    on_cycle: set[str] = set()
    for start in nodes:
        stack = list(edges.get(start, ()))
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == start:
                on_cycle.add(start)
                break
            if node not in seen:
                seen.add(node)
                stack.extend(edges.get(node, ()))
    return sorted(on_cycle)


def regenerate(definitions: dict[str, list[tuple[str, int]]]) -> dict[str, FamilyCensus]:
    """Return each family's census from its first definition of every id."""
    by_family: dict[str, set[int]] = defaultdict(set)
    homes: dict[str, str] = {}
    for req_id, places in definitions.items():
        family, number = req_id.rsplit("-", 1)
        by_family[family].add(int(number))
        homes.setdefault(family, places[0][0][:2])
    census: dict[str, FamilyCensus] = {}
    for family, numbers in sorted(by_family.items()):
        highest = max(numbers)
        gaps = tuple(n for n in range(1, highest) if n not in numbers)
        census[family] = FamilyCensus(family, homes[family], len(numbers), highest, gaps)
    return census


def _stored_census_findings(
    name: str, lines: Sequence[str], census: dict[str, FamilyCensus]
) -> list[Finding]:
    """Compare the newest regenerated-census section of ``name`` with ``census``."""
    starts = [i for i, line in enumerate(lines) if _CENSUS_HEADING_RE.match(line)]
    if not starts:
        return []
    findings: list[Finding] = []
    total = sum(row.defined for row in census.values())
    for index in range(starts[-1] + 1, len(lines)):
        line = lines[index]
        if line.startswith("#"):
            break
        stored = _TOTAL_RE.search(line)
        if stored is not None and int(stored["n"]) != total:
            findings.append(
                Finding(name, index + 1, "distinct ids", f"states {stored['n']}, census {total}")
            )
        row = _CENSUS_ROW_RE.match(line)
        if row is None or _DATED_ROW_RE.match(line):
            continue
        fresh = census.get(row["fam"])
        if fresh is None:
            findings.append(Finding(name, index + 1, row["fam"], "no row defines this family"))
            continue
        if (int(row["defined"]), int(row["max"]), int(row["next"])) != (
            fresh.defined,
            fresh.highest,
            fresh.highest + 1,
        ):
            findings.append(
                Finding(
                    name,
                    index + 1,
                    row["fam"],
                    f"states defined {row['defined']} max {row['max']} next {row['next']}; "
                    f"census defined {fresh.defined} max {fresh.highest:03d} "
                    f"next {fresh.highest + 1:03d}",
                )
            )
    return findings


def check_packet(packet: Path) -> tuple[dict[str, FamilyCensus], list[Finding]]:
    """Return the regenerated census of ``packet`` and every defect found in it.

    Raises:
        FileNotFoundError: ``packet`` holds no numbered file.
    """
    files = sorted(packet.glob("[0-9][0-9]-*.md"))
    if not files:
        raise FileNotFoundError(f"no numbered packet files under {packet.name}")
    texts = {path.name: path.read_text(encoding="utf-8").splitlines() for path in files}
    scan = _Scan()
    for name, lines in texts.items():
        _scan_file(name, lines, scan)
    findings = scan.findings
    homes: dict[str, set[str]] = defaultdict(set)
    for req_id, places in sorted(scan.definitions.items()):
        homes[req_id.rsplit("-", 1)[0]].update(place[0] for place in places)
        if len(places) > 1:
            where = ", ".join(f"{file}:{line}" for file, line in places)
            findings.append(Finding(places[1][0], places[1][1], req_id, f"defined twice ({where})"))
    findings += [
        Finding(sorted(names)[1], 0, family, f"defined in {len(names)} home files")
        for family, names in sorted(homes.items())
        if len(names) > 1
    ]
    census = regenerate(scan.definitions)
    gaps = {f"{row.family}-{gap:03d}" for row in census.values() for gap in row.gaps}
    families = frozenset(homes)
    for name, number, line in scan.citations:
        findings += [
            Finding(name, number, req_id, "cited but defined nowhere")
            for req_id in sorted(cited_ids(line, families) - set(scan.definitions))
            if req_id not in gaps or not re.search(rf"\b{req_id}\b", line)
        ]
    authority = next((name for name in texts if name.startswith(AUTHORITY_PREFIX)), None)
    if authority is not None:
        findings += _graph_findings(authority, texts[authority], texts)
    for name, lines in texts.items():
        findings += _stored_census_findings(name, lines, census)
    return census, findings


def _revision(root: Path) -> str:
    """Return the revision of the tree the packet was read in, or ``unknown``."""
    done = subprocess.run(
        ["git", "rev-parse", "--short=8", "HEAD"], cwd=root, capture_output=True, text=True
    )
    return done.stdout.strip() if done.returncode == 0 else "unknown"


def render_census(census: dict[str, FamilyCensus], revision: str) -> str:
    """Return the census as the packet's regenerated-census table, stamped with ``revision``."""
    rows = [
        f"| `{row.family}` | `{row.home}` | {row.defined} | {row.highest:03d} | "
        f"{row.highest + 1:03d} | {', '.join(f'{gap:03d}' for gap in row.gaps) or 'none'} |"
        for row in census.values()
    ]
    total = sum(row.defined for row in census.values())
    return "\n".join(
        [
            f"Read at revision `{revision}`.",
            "",
            "| Family | Home | Defined | Max | Next free | Gaps below max |",
            "|---|---|---|---|---|---|",
            *rows,
            "",
            f"Distinct defined requirement identifiers across the numbered files: **{total}**",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Scan the packet and print its census, or say it is absent.

    Returns:
        ``0`` when the packet is clean or absent, ``1`` on any finding.
    """
    parser = argparse.ArgumentParser(description="proposal packet census lint")
    parser.add_argument("--packet", type=Path, default=PROPOSAL_PACKET_DIR)
    args = parser.parse_args(argv)
    if not args.packet.is_dir():
        print(f"packet-census: skipped: {args.packet.as_posix()} is absent (local, gitignored)")
        return 0
    census, findings = check_packet(args.packet)
    print(render_census(census, _revision(args.packet)))
    for finding in findings:
        print(f"packet-census: {finding.render()}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
