"""Portable PII/local-token scanner for artifact prose."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "absolute_posix_path",
        re.compile(r"(?<![\w.])/(?:Users|home|var|tmp|private)/[^\s)`]+"),
        "<local-path>",
    ),
    ("absolute_windows_path", re.compile(r"[A-Za-z]:\\Users\\[^\s)`]+"), "<local-path>"),
    ("home_path", re.compile(r"~/[^\s)`]+"), "<local-path>"),
    (
        "email",
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
        "<email>",
    ),
    (
        "local_url",
        re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d+)?[^\s)`]*"),
        "<local-url>",
    ),
)


@dataclass(frozen=True)
class ScrubFinding:
    """One scanner finding."""

    kind: str
    start: int
    end: int
    text: str


def scan_text(text: str) -> list[ScrubFinding]:
    """Return scanner findings sorted by byte position."""
    findings: list[ScrubFinding] = []
    for kind, pattern, _replacement in _PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                ScrubFinding(
                    kind=kind,
                    start=match.start(),
                    end=match.end(),
                    text=match.group(0),
                )
            )
    findings.sort(key=lambda finding: (finding.start, finding.end, finding.kind))
    return findings


def rewrite_text(text: str) -> str:
    """Replace local/sensitive tokens with generic placeholders."""
    rewritten = text
    for _kind, pattern, replacement in _PATTERNS:
        rewritten = pattern.sub(replacement, rewritten)
    return rewritten
