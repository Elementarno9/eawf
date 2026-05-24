"""Portable PII/local-token scanner for artifact prose."""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_EMAIL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "noreply@anthropic.com",
        "noreply@openai.com",
    }
)

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "absolute_posix_path",
        re.compile(r"(?<![\w.])/(?:Users|home|var|tmp|private)/[^\s)`]+"),
        "<local-path>",
    ),
    ("absolute_windows_path", re.compile(r"[A-Za-z]:\\Users\\[^\s)`]+"), "<local-path>"),
    ("home_path", re.compile(r"~/[^\s)`]+"), "<local-path>"),
    (
        "local_url",
        re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d+)?[^\s)`]*"),
        "<local-url>",
    ),
    (
        "private_ip",
        re.compile(
            r"\b(?:10|127)\.(?:\d{1,3}\.){2}\d{1,3}\b"
            r"|\b192\.168\.\d{1,3}\.\d{1,3}\b"
            r"|\b172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}\b"
        ),
        "<host>",
    ),
    (
        "local_hostname",
        re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}\.(?:local|lan|internal|corp)\b", re.I),
        "<host>",
    ),
)


@dataclass(frozen=True)
class ScrubFinding:
    """One scanner finding."""

    kind: str
    start: int
    end: int
    text: str


def scan_text(
    text: str,
    *,
    allowed_emails: set[str] | frozenset[str] | None = DEFAULT_EMAIL_ALLOWLIST,
) -> list[ScrubFinding]:
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
    email_allowlist = {email.casefold() for email in (allowed_emails or set())}
    for match in _EMAIL_RE.finditer(text):
        if match.group(0).casefold() in email_allowlist:
            continue
        findings.append(
            ScrubFinding(
                kind="email",
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
