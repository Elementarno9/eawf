"""EAWF027 — the citation-scope lint: no committed text quotes a reflection row it may not.

A reflection report is local, but the figures in it are meant to be read, and an agent
that reads one can quote it into an audit, a decision record or a pull-request body.
Only this project's own statistics may cross that line. The report therefore closes every
row with a quotability mark, and this lint is where the mark is enforced: it reads every
committed artifact, rendered document and rule projection, the pull-request body when one
is named, and the local proposal packet when it is present, and fails on

- a report row whose mark is ``non-quotable``, which is how a row from another project or
  an excerpt reads;
- a report row that carries no mark, because an unmarked figure's scope is unknown;
- a session title whose source is ``scrubbed_extract``, wherever it appears, because that
  title is a bounded excerpt of the operator's own words and is local-only at any scope.

A report row is recognised by the shape the report renderer writes, and a machine-form
report by its ``title_source`` and ``quotable`` fields, so the lint and the renderer read
one format. Each finding names the file, the line and the mark that failed.

The production call-site is ``eawf hook eawf027-citation-scope [--pr-body FILE]``.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from eawf.observability.reflect.report import NON_QUOTABLE_MARK, QUOTABLE_MARK
from eawf.observability.reflect.titles import TitleSource

RULE_CODE = "EAWF027"

#: The committed paths in scope: artifacts, rendered documents and rule projections.
COMMITTED_SCOPE: Final[tuple[str, ...]] = (
    ".ea/artifacts",
    "docs",
    "AGENTS.md",
    "AGENTS.override.md",
    "README.md",
    "CHANGELOG.md",
)

#: The local proposal packet. It is never committed, and it is in scope wherever it exists.
PROPOSAL_PACKET_DIR: Final = Path(".ea/local/research/2026-08-04-v07-proposal")

#: The file kinds a quotation can sit in.
TEXT_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".txt", ".json", ".yaml", ".yml"})

_SOURCES: Final = "|".join(source.value for source in TitleSource)

#: A report row as :func:`eawf.observability.reflect.report.render_text` writes it.
_ROW_RE: Final = re.compile(
    rf"\bsession \S+ .*· title .* \[(?P<source>{_SOURCES})\]"
    rf"(?: · (?P<mark>{re.escape(NON_QUOTABLE_MARK)}|{re.escape(QUOTABLE_MARK)}))?"
)

#: The machine form of the same two facts, one field per line as the report document is
#: written.
_EXTRACT_FIELD_RE: Final = re.compile(
    rf'"title_source"\s*:\s*"{TitleSource.SCRUBBED_EXTRACT.value}"'
)
_NON_QUOTABLE_FIELD_RE: Final = re.compile(r'"quotable"\s*:\s*false\b')

#: A title's source as a report row prints it, found anywhere.
_EXTRACT_MARK: Final = f"[{TitleSource.SCRUBBED_EXTRACT.value}]"


@dataclass(frozen=True, slots=True)
class CitationFinding:
    """One quotation the lint refuses.

    Attributes:
        path: The file, as the lint was given it.
        line: The 1-based line.
        mark: The mark that failed: ``non-quotable``, ``unmarked`` or
            ``scrubbed_extract``.
    """

    path: str
    line: int
    mark: str

    def render(self) -> str:
        """Return the ``path:line: CODE mark`` diagnostic."""
        return f"{self.path}:{self.line}: {RULE_CODE} reflection row marked {self.mark}"


def scan_text(path: str, text: str) -> list[CitationFinding]:
    """Return every refused quotation in ``text``, in line order.

    Args:
        path: The name findings carry.
        text: The file's content.

    Returns:
        One finding per refused line; a line with a ``scrubbed_extract`` title is
        reported for that title whatever its mark says.
    """
    findings: list[CitationFinding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if _EXTRACT_MARK in line or _EXTRACT_FIELD_RE.search(line):
            findings.append(CitationFinding(path, number, TitleSource.SCRUBBED_EXTRACT.value))
            continue
        if _NON_QUOTABLE_FIELD_RE.search(line):
            findings.append(CitationFinding(path, number, NON_QUOTABLE_MARK))
            continue
        row = _ROW_RE.search(line)
        if row is None or row["mark"] == QUOTABLE_MARK:
            continue
        findings.append(CitationFinding(path, number, row["mark"] or "unmarked"))
    return findings


def committed_scope(repo_root: Path) -> list[Path]:
    """Return the tracked text files under :data:`COMMITTED_SCOPE`.

    Raises:
        subprocess.CalledProcessError: ``repo_root`` is not a git work tree.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", *COMMITTED_SCOPE],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [
        Path(name) for name in listed.split("\0") if name and Path(name).suffix in TEXT_SUFFIXES
    ]


def packet_scope(repo_root: Path) -> list[Path]:
    """Return the proposal packet's text files, or nothing where the packet is absent."""
    packet = repo_root / PROPOSAL_PACKET_DIR
    if not packet.is_dir():
        return []
    return sorted(
        path.relative_to(repo_root)
        for path in packet.rglob("*")
        if path.is_file() and path.suffix in TEXT_SUFFIXES
    )


def scan_paths(repo_root: Path, paths: Iterable[Path]) -> list[CitationFinding]:
    """Return the findings of every file in ``paths``, read relative to ``repo_root``."""
    findings: list[CitationFinding] = []
    for relative in paths:
        text = (repo_root / relative).read_text(encoding="utf-8", errors="replace")
        findings.extend(scan_text(relative.as_posix(), text))
    return findings
