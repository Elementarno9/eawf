"""The leak shapes a line of committed state text must not carry.

``.ea/state.json`` is committed, and its free-text fields take whatever an
agent or operator wrote. detect-secrets cannot guard it -- its line offsets
churn against the baseline on every mutation, so the file is excluded there --
which leaves the leak lints as the only scan between that text and a public
commit. This module is the one pattern set every scanner of state text applies,
so the shapes cannot drift between them:

- the macOS, Windows and Linux home-directory anchors, selected from the
  emit-time scrubber's own table;
- the scrubber's email pattern, minus the canonical allowlist and the reserved
  example domains; and
- credential-shaped tokens (Anthropic, OpenAI, GitHub classic and fine-grained,
  AWS access key ids).

The bare ``~/`` shape is deliberately absent: state text legitimately names
``~/.eawf/...`` locations, and a tilde path carries no username.

Every token pattern is anchored on its left edge, because an unanchored
``sk-`` run also matches inside ordinary words such as ``task-...``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from eawf.observability.logging.scrub import (
    _DEFAULT_ALLOWED_EMAILS,
    SensitiveScrubber,
    _eawf_author_emails,
)

logger = logging.getLogger(__name__)

HOME_PATH_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    pattern
    for pattern in SensitiveScrubber.PATTERNS
    if "Users" in pattern.pattern or "home" in pattern.pattern
)

EMAIL_PATTERN: Final[re.Pattern[str]] = next(
    pattern for pattern in SensitiveScrubber.PATTERNS if "@" in pattern.pattern
)

# A token must not continue a word on its left. The generic ``sk-`` shape
# refuses an ``ant-`` tail so an Anthropic key is reported once, as itself.
TOKEN_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-(?!ant-)[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{36,}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{22,}"),
    # An AWS key id has a fixed width, so it is anchored on both edges.
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"),
)

# An angle bracket marks a name to be filled in (``/Users/<name>``) and an
# ellipsis an elided tail (``C:\Users\...``): documentation, not a leak.
_PLACEHOLDER_TOKENS: Final[tuple[str, ...]] = ("<", ">", "...")

# RFC 2606 / 6761 reserved domains used in fixtures and docs; never real PII.
_RESERVED_EMAIL_DOMAINS: Final = frozenset(
    {"example.com", "example.org", "example.net", "example.edu"}
)
_RESERVED_EMAIL_TLDS: Final = (".example", ".invalid", ".localhost", ".test")


class StateLeakKind(StrEnum):
    """The family a leak hit belongs to, so a gate can report only its own."""

    HOME_PATH = "home_path"
    EMAIL = "email"
    TOKEN = "token"


@dataclass(frozen=True)
class StateLeakHit:
    """One leak shape found in scanned text.

    Attributes:
        kind: The pattern family that matched.
        snippet: The matched substring, verbatim so the writer can find and
            remove it.
    """

    kind: StateLeakKind
    snippet: str


def is_placeholder_path(snippet: str) -> bool:
    """Report whether a matched home path is a documented placeholder.

    Args:
        snippet: The substring a home-directory anchor matched.

    Returns:
        ``True`` when the match carries an angle bracket or an ellipsis. A
        concrete path with a real username carries neither.
    """
    return any(token in snippet for token in _PLACEHOLDER_TOKENS)


def is_placeholder_or_nonemail(addr: str) -> bool:
    """Report whether an address-shaped match is a placeholder or not an email.

    The scrubber's address pattern is loose on purpose, so two classes of match
    are not leaks: reserved example domains (``test@example.com``) and version
    pins such as ``setup-uv@v8.1.0``, whose last label is not an alphabetic
    top-level domain.

    Args:
        addr: The substring the email pattern matched.

    Returns:
        ``True`` when ``addr`` is a reserved placeholder or not an email.
    """
    _, _, domain = addr.partition("@")
    domain = domain.casefold()
    if not domain:
        return True
    if domain in _RESERVED_EMAIL_DOMAINS or domain.endswith(_RESERVED_EMAIL_TLDS):
        return True
    tld = domain.rsplit(".", 1)[-1]
    return not (tld.isalpha() and len(tld) >= 2)


def default_allowed_emails() -> frozenset[str]:
    """Return the casefolded addresses a leak scan lets through.

    Returns:
        The no-reply co-author addresses plus eawf's ``pyproject.toml`` author
        rows: exactly the addresses the emit-time scrubber preserves.
    """
    return frozenset(email.casefold() for email in _DEFAULT_ALLOWED_EMAILS | _eawf_author_emails())


def scan_state_leaks(text: str, *, allowed_emails: frozenset[str]) -> list[StateLeakHit]:
    """Return every leak shape ``text`` carries.

    Args:
        text: The text to scan, such as one line of the state file or one
            string value about to be written to it.
        allowed_emails: Casefolded addresses that are not leaks, normally
            :func:`default_allowed_emails`. It is a parameter so a caller that
            scans many lines resolves the allowlist once.

    Returns:
        The hits in pattern-family order (home paths, emails, tokens), then
        match order. Documented placeholders, allowlisted addresses and
        non-email ``@`` references are left out.

    Raises:
        TypeError: When ``text`` is not a ``str``.
    """
    hits: list[StateLeakHit] = []
    for pattern in HOME_PATH_PATTERNS:
        hits.extend(
            StateLeakHit(kind=StateLeakKind.HOME_PATH, snippet=match.group(0))
            for match in pattern.finditer(text)
            if not is_placeholder_path(match.group(0))
        )
    hits.extend(
        StateLeakHit(kind=StateLeakKind.EMAIL, snippet=match.group(0))
        for match in EMAIL_PATTERN.finditer(text)
        if match.group(0).casefold() not in allowed_emails
        and not is_placeholder_or_nonemail(match.group(0))
    )
    for pattern in TOKEN_PATTERNS:
        hits.extend(
            StateLeakHit(kind=StateLeakKind.TOKEN, snippet=match.group(0))
            for match in pattern.finditer(text)
        )
    return hits


__all__ = [
    "EMAIL_PATTERN",
    "HOME_PATH_PATTERNS",
    "TOKEN_PATTERNS",
    "StateLeakHit",
    "StateLeakKind",
    "default_allowed_emails",
    "is_placeholder_or_nonemail",
    "is_placeholder_path",
    "scan_state_leaks",
]
