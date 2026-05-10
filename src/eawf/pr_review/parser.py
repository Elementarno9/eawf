"""Markdown findings parser (B041).

Recognises the canonical caveman-reviewer / claude ``/review`` output
format::

    path:line: <emoji> <severity>: <problem>. <fix>.

Where ``<emoji>`` is drawn from the four-tier severity palette:

==========  ==========
Emoji       Severity
==========  ==========
"red"       blocker
"orange"    must-fix
"yellow"    should-fix
"blue"      nit
==========  ==========

Lines that do not match ``path:line:`` are treated as commentary and
silently skipped. A line that matches the leading prefix but carries
an unknown severity tag (either an unrecognised emoji or an
unrecognised text severity) raises :class:`ValueError` — malformed
input is a producer-side bug and must surface, not silently drop the
finding.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)


Severity = Literal["blocker", "must-fix", "should-fix", "nit"]


# Canonical emoji → severity mapping. Tied to the caveman-reviewer
# output format documented above. The literal emoji characters live
# only in this dict so downstream tests and prompts can re-use the
# mapping by keyword rather than by glyph.
_EMOJI_TO_SEVERITY: dict[str, Severity] = {
    "\U0001f534": "blocker",  # red circle
    "\U0001f7e0": "must-fix",  # orange circle
    "\U0001f7e1": "should-fix",  # yellow circle
    "\U0001f535": "nit",  # blue circle
}

# Allow textual severity as a fall-through when the producer omits the
# emoji prefix (e.g. plain-text reviewer output). The text severity
# itself must still be one of the four canonical tags.
_TEXT_SEVERITIES: frozenset[Severity] = frozenset(_EMOJI_TO_SEVERITY.values())


# Findings line shape: ``<path>:<line?>: <emoji-or-tag> <severity>: <message>``
# where ``<line?>`` is an optional integer (the ``path::`` form means
# the producer could not pin the finding to a specific line). The
# message captures everything after ``<severity>:`` so callers can
# render it verbatim.
_LINE_RE = re.compile(
    r"^(?P<path>[^:\s][^:]*):(?P<line>\d*):\s+"
    r"(?P<head>\S+)\s+(?P<sev>[a-z-]+):\s*(?P<msg>.*)$"
)


@dataclass(frozen=True)
class Finding:
    """One structured code-review finding.

    Attributes:
        path: File path the finding is attached to (verbatim from the
            input — relative repo paths and absolute paths are both
            accepted by the parser; the consumer is responsible for
            normalisation).
        line: 1-indexed line number, or ``None`` when the producer
            could not pin the finding to a specific line (``path::``
            form).
        severity: One of ``blocker`` / ``must-fix`` / ``should-fix``
            / ``nit``.
        message: Free-form finding text (problem + suggested fix) as
            emitted by the producer.
    """

    path: str
    line: int | None
    severity: Severity
    message: str


def parse_findings(markdown: str) -> list[Finding]:
    """Parse the caveman-reviewer Markdown output into structured findings.

    Lines that do not match the canonical
    ``path:line: <emoji> <severity>: <message>`` shape are silently
    skipped — they are treated as commentary. A line that matches the
    leading shape but carries an unrecognised severity (unknown emoji
    AND unknown textual tag) raises :class:`ValueError` so the
    producer bug surfaces.

    Args:
        markdown: Full Markdown text of the findings document.

    Returns:
        A list of :class:`Finding` records in input order. Empty when
        the input contains zero findings (and only commentary).

    Raises:
        ValueError: When a line matches the leading prefix but the
            severity tag is not recognised.
    """
    findings: list[Finding] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        # Strip optional list bullets and inline-code wrappers so a
        # producer that wraps each finding in ``- `` or ``` `…` ``` still
        # parses cleanly. We strip a single leading marker per line — a
        # finding that is itself a bullet of a deeper list (very rare in
        # practice) keeps the inner whitespace, which the regex tolerates.
        if line.startswith("- "):
            line = line[2:].lstrip()
        if line.startswith("* "):
            line = line[2:].lstrip()
        if line.startswith("`") and line.endswith("`") and len(line) >= 2:
            line = line[1:-1].strip()

        match = _LINE_RE.match(line)
        if match is None:
            # Not a finding line — treat as commentary.
            continue

        path = match.group("path")
        line_str = match.group("line")
        head = match.group("head")
        sev_tag = match.group("sev")
        message = match.group("msg").strip()

        line_no = int(line_str) if line_str else None
        severity = _resolve_severity(head=head, sev_tag=sev_tag)

        findings.append(
            Finding(
                path=path,
                line=line_no,
                severity=severity,
                message=message,
            )
        )
    return findings


def _resolve_severity(*, head: str, sev_tag: str) -> Severity:
    """Combine the emoji prefix and textual tag into a single severity.

    Strategy: when the leading token is one of the canonical emojis,
    its mapping wins (and is cross-checked against the textual tag for
    consistency — a mismatch is a producer bug and raises). When the
    leading token is not a recognised emoji, we fall back to the
    textual tag.

    Raises:
        ValueError: When neither the leading emoji nor the textual tag
            resolve to a canonical severity, or when the two disagree.
    """
    from_emoji = _EMOJI_TO_SEVERITY.get(head)
    from_text: Severity | None = sev_tag if sev_tag in _TEXT_SEVERITIES else None

    if from_emoji is None and from_text is None:
        raise ValueError(
            f"unrecognised severity tag: head={head!r} sev={sev_tag!r}; "
            f"expected one of {sorted(_TEXT_SEVERITIES)}"
        )
    if from_emoji is not None and from_text is not None and from_emoji != from_text:
        raise ValueError(
            f"severity mismatch between emoji and textual tag: "
            f"emoji={head!r} → {from_emoji!r}, text={sev_tag!r}"
        )
    return from_emoji or from_text  # type: ignore[return-value]


__all__ = ["Finding", "Severity", "parse_findings"]
