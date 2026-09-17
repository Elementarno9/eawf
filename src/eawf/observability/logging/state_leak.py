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

The commit-time lints scan the lines a commit adds; the state writers apply
the same set earlier, to the strings a write adds (:func:`diff_state_leaks`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
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

# A workspace index records where each linked repo is checked out, so that
# field holds a local absolute path by design. Only the home-path shape is
# waived there; an email or token in it is still a leak.
_LOCAL_CHECKOUT_FIELD: Final[re.Pattern[str]] = re.compile(r"workspace\.repos\.[^.\[]+\.path")


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


@dataclass(frozen=True)
class StateStringLeak:
    """A leak shape carried by one string a state write adds or changes.

    Attributes:
        field_path: Where the string sits in the state payload: object keys
            joined by dots, list positions in brackets, such as
            ``waves.P01-I01-W01.success_criteria[0].text``.
        hit: The leak shape the string carries.
    """

    field_path: str
    hit: StateLeakHit


def diff_state_leaks(
    old: object, new: object, *, allowed_emails: frozenset[str]
) -> list[StateStringLeak]:
    """Return the leak shapes in the strings a state write adds or changes.

    A state write re-serialises the whole payload, so scanning every string
    would re-flag text that predates the write and cost a full walk of a
    multi-megabyte payload. Only a string that differs from the value at the
    same place in ``old`` is scanned. A subtree equal to its old counterpart
    is skipped without being walked, and a list item equal to any item of the
    old list is treated as moved rather than new, so an insertion does not
    re-flag the items it shifts.

    Args:
        old: The decoded JSON payload on disk before the write.
        new: The decoded JSON payload about to be written. Neither argument
            is modified.
        allowed_emails: Casefolded addresses that are not leaks, normally
            :func:`default_allowed_emails`.

    Returns:
        One entry per hit, in payload walk order. Empty when every added or
        changed string is clean. Dict keys are not scanned; only values are.
        A home path in a workspace repo checkout path
        (``workspace.repos.<code>.path``) is not reported.
    """
    leaks: list[StateStringLeak] = []
    _collect_string_leaks(old, new, field_path="", allowed_emails=allowed_emails, leaks=leaks)
    return leaks


def _collect_string_leaks(
    old: object,
    new: object,
    *,
    field_path: str,
    allowed_emails: frozenset[str],
    leaks: list[StateStringLeak],
) -> None:
    """Append the leaks of ``new``'s added or changed strings to ``leaks``.

    Args:
        old: The counterpart of ``new`` in the old payload, or ``None`` when
            the location is new.
        new: The value about to be written at ``field_path``.
        field_path: The location of ``new``; empty for the payload root.
        allowed_emails: Casefolded addresses that are not leaks.
        leaks: The accumulator the hits are appended to.
    """
    if new == old:
        return
    if isinstance(new, str):
        checkout_path = _LOCAL_CHECKOUT_FIELD.fullmatch(field_path) is not None
        leaks.extend(
            StateStringLeak(field_path=field_path, hit=hit)
            for hit in scan_state_leaks(new, allowed_emails=allowed_emails)
            if not (checkout_path and hit.kind is StateLeakKind.HOME_PATH)
        )
    elif isinstance(new, dict):
        old_object = old if isinstance(old, dict) else {}
        for key, value in new.items():
            _collect_string_leaks(
                old_object.get(key),
                value,
                field_path=f"{field_path}.{key}" if field_path else str(key),
                allowed_emails=allowed_emails,
                leaks=leaks,
            )
    elif isinstance(new, list):
        old_list = old if isinstance(old, list) else []
        for index, item in enumerate(new):
            counterpart = old_list[index] if index < len(old_list) else None
            # The positional check runs first so an unchanged list never pays
            # the quadratic membership scan.
            if item == counterpart or item in old_list:
                continue
            _collect_string_leaks(
                counterpart,
                item,
                field_path=f"{field_path}[{index}]",
                allowed_emails=allowed_emails,
                leaks=leaks,
            )


def describe_state_leaks(leaks: Sequence[StateStringLeak]) -> str:
    """Return the refusal detail for a state write that adds leak shapes.

    The matched text is left out on purpose: the refusal travels to
    terminals and daemon logs, and repeating the leak there would spread it.
    The field path and the leak kind are enough to find what to remove.

    Args:
        leaks: The non-empty result of :func:`diff_state_leaks`.

    Returns:
        ``state_leak_refused:`` followed by each distinct field path, in
        first-seen order, with its leak kinds in parentheses.

    Raises:
        ValueError: When ``leaks`` is empty, because there is nothing to
            refuse.
    """
    if not leaks:
        raise ValueError("describe_state_leaks needs at least one leak")
    kinds_by_path: dict[str, list[str]] = {}
    for leak in leaks:
        kinds = kinds_by_path.setdefault(leak.field_path, [])
        if leak.hit.kind.value not in kinds:
            kinds.append(leak.hit.kind.value)
    fields = "; ".join(f"{path} ({', '.join(kinds)})" for path, kinds in kinds_by_path.items())
    return f"state_leak_refused: {fields}"


def state_leak_refusal(old: object, new: object) -> str | None:
    """Return why a state write must be refused for leaks, if it must be.

    The one check every canonical state writer runs between validating the
    new payload and writing it, so the writers cannot drift apart.

    Args:
        old: The decoded JSON payload on disk before the write.
        new: The decoded JSON payload about to be written.

    Returns:
        The :func:`describe_state_leaks` detail when an added or changed
        string carries a leak shape, or ``None`` when the write may proceed.
    """
    leaks = diff_state_leaks(old, new, allowed_emails=default_allowed_emails())
    return describe_state_leaks(leaks) if leaks else None


__all__ = [
    "EMAIL_PATTERN",
    "HOME_PATH_PATTERNS",
    "TOKEN_PATTERNS",
    "StateLeakHit",
    "StateLeakKind",
    "StateStringLeak",
    "default_allowed_emails",
    "describe_state_leaks",
    "diff_state_leaks",
    "is_placeholder_or_nonemail",
    "is_placeholder_path",
    "scan_state_leaks",
    "state_leak_refusal",
]
