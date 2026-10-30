"""The markdown brief a ``/research`` invocation leaves under ``.ea/local/research/``.

A brief is one page: an H1 title, then exactly :data:`BRIEF_SECTIONS` as
``##`` headings in that order. Its References section is a numbered table
whose first column is the citation number and whose second column is the
reference, which must be repo-relative, a URL or an Eawf URN; every ``[N]``
marker in the prose resolves to a row and every row is cited.
:func:`check_research_brief` is the one judgement of that shape; the skill
page names the same sections, so the page and the check cannot disagree.

A brief is a local research artifact. Promoting one out of ``.ea/local/``
needs the artifact chassis instead (Summary, References, Provenance and
Scrub sections), per ``docs/rules/artifact-chassis.md``.
"""

from __future__ import annotations

import logging
import re
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.platform.artifacts.references import (
    Citation,
    CitationValidationError,
    validate_dense_citation_refs,
)

logger = logging.getLogger(__name__)

#: The ``##`` sections of a brief, in the order they must appear.
BRIEF_SECTIONS: Final[tuple[str, ...]] = (
    "Question",
    "Findings",
    "Alternatives",
    "Recommendation",
    "Open gaps",
    "References",
)

_REFERENCES: Final = "References"
_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^## (?P<title>.+?)\s*$", re.MULTILINE)
_SEPARATOR_CELL_RE: Final[re.Pattern[str]] = re.compile(r"^:?-{3,}:?$")


class BriefCheck(BaseModel):
    """The verdict on one brief.

    Attributes:
        ok: Whether the brief holds the required shape.
        errors: One sentence per defect, in document order; empty when ``ok``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    errors: tuple[str, ...] = ()


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _reference_rows(section: str) -> tuple[list[Citation], list[str]]:
    """Read the References table into citations, with one error per bad row."""
    lines = [line for line in section.splitlines() if line.strip()]
    if len(lines) < 3 or not all(line.lstrip().startswith("|") for line in lines):
        return [], ["References is not a table with a header, a separator and at least one row"]
    header = _cells(lines[0])
    if len(header) < 2 or header[0] != "#":
        return [], ["References table header must start with the columns # and Reference"]
    if len(_cells(lines[1])) != len(header) or not all(
        _SEPARATOR_CELL_RE.match(cell) for cell in _cells(lines[1])
    ):
        return [], ["References table separator row does not match its header"]
    citations: list[Citation] = []
    errors: list[str] = []
    for number, line in enumerate(lines[2:], start=1):
        cells = _cells(line)
        if len(cells) != len(header):
            errors.append(
                f"References row {number} has {len(cells)} cells, header has {len(header)}"
            )
            continue
        if not cells[0].isdigit():
            errors.append(f"References row {number} number {cells[0]!r} is not a number")
            continue
        ref = cells[1].strip("`")
        try:
            citations.append(Citation.from_legacy_source(int(cells[0]), ref))
        except ValidationError:
            errors.append(
                f"References row {number} ref {ref!r} is not repo-relative, a URL or an Eawf URN"
            )
    return citations, errors


def check_research_brief(text: str) -> BriefCheck:
    """Judge whether *text* is a well-formed research brief.

    Args:
        text: The brief's markdown.

    Returns:
        The verdict; each error names the section or row it concerns.
    """
    if not text.strip():
        return BriefCheck(ok=False, errors=("brief is empty",))
    errors: list[str] = []
    if not text.lstrip().startswith("# "):
        errors.append("brief does not start with an H1 title")
    matches = list(_HEADING_RE.finditer(text))
    headings = [match.group("title") for match in matches]
    errors.extend(f"missing section: {name}" for name in BRIEF_SECTIONS if name not in headings)
    errors.extend(f"unexpected section: {name}" for name in headings if name not in BRIEF_SECTIONS)
    known = [name for name in headings if name in BRIEF_SECTIONS]
    if len(set(known)) != len(known):
        errors.append(f"a section repeats: {known}")
    elif known != [name for name in BRIEF_SECTIONS if name in known]:
        errors.append(f"sections {known} are not in the order {list(BRIEF_SECTIONS)}")
    bodies: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        bodies.setdefault(match.group("title"), text[match.end() : end].strip())
    errors.extend(
        f"section {name} is empty" for name in BRIEF_SECTIONS if name in bodies and not bodies[name]
    )
    if bodies.get(_REFERENCES):
        citations, row_errors = _reference_rows(bodies[_REFERENCES])
        errors.extend(row_errors)
        if citations and not row_errors:
            start = matches[headings.index(_REFERENCES)].start()
            prose = text[:start]
            try:
                validate_dense_citation_refs(prose, citations)
            except CitationValidationError as exc:
                errors.append(f"References: {exc}")
    logger.debug(f"check_research_brief errors={len(errors)}")
    return BriefCheck(ok=not errors, errors=tuple(errors))


__all__ = ["BRIEF_SECTIONS", "BriefCheck", "check_research_brief"]
