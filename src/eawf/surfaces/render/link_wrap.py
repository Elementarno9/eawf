"""Clickable EAWF reference wrapping for rich/Textual render surfaces.

The TUI consumes this module for two separate paths:

* render text with ``@click`` action markup for recognised entity refs;
* scan the same text to build hover previews and ``/goto`` candidates.

The catalog intentionally covers the 14 C01 operator-facing entity kinds.
Each kind has its own compiled regex so additions stay explicit and tests
can pin the catalog size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from rich.markup import escape

ReferenceKind = Literal[
    "repo",
    "project",
    "phase",
    "iter",
    "wave",
    "hypothesis",
    "decision",
    "audit",
    "artifact",
    "memory",
    "report",
    "event",
    "profile",
    "spec",
]

REFERENCE_KINDS: tuple[ReferenceKind, ...] = (
    "repo",
    "project",
    "phase",
    "iter",
    "wave",
    "hypothesis",
    "decision",
    "audit",
    "artifact",
    "memory",
    "report",
    "event",
    "profile",
    "spec",
)

_TOKEN_END = r"(?![\w-])"
_TOKEN_START = r"(?<![\w-])"
_URN_TAIL = r"[^\s\]\[()<>,'\"`]+"
_PROJECT_CODE = r"[A-Z][A-Z0-9_-]{1,15}"
_LIFECYCLE = r"P\d{2,}(?:-I\d{2,}(?:-W\d{2,})?)?"


@dataclass(frozen=True)
class LinkPattern:
    """One reference-kind regex row."""

    kind: ReferenceKind
    regex: re.Pattern[str]


@dataclass(frozen=True)
class LinkRef:
    """A matched clickable reference in a text span."""

    kind: ReferenceKind
    target: str
    label: str
    start: int
    end: int

    @property
    def action_name(self) -> str:
        """Return the Textual action name for this ref kind."""
        return f"open_{self.kind}_ref"


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile a reference regex with named ``ref`` group."""
    return re.compile(pattern)


LINK_PATTERNS: tuple[LinkPattern, ...] = (
    LinkPattern(
        "repo",
        _compile(rf"(?P<ref>urn:eawf:v1:repo:{_URN_TAIL}|repo:{_PROJECT_CODE})"),
    ),
    LinkPattern(
        "project",
        _compile(rf"(?P<ref>project:{_PROJECT_CODE}|urn:eawf:v1:project:{_URN_TAIL})"),
    ),
    LinkPattern(
        "spec",
        _compile(rf"(?P<ref>urn:eawf:v1:spec:{_URN_TAIL}|spec:{_LIFECYCLE})"),
    ),
    LinkPattern(
        "wave",
        _compile(
            rf"(?P<ref>urn:eawf:v1:wave:{_URN_TAIL}|{_TOKEN_START}P\d{{2,}}-I\d{{2,}}-W\d{{2,}}{_TOKEN_END})"
        ),
    ),
    LinkPattern(
        "iter",
        _compile(
            rf"(?P<ref>urn:eawf:v1:iter:{_URN_TAIL}|{_TOKEN_START}P\d{{2,}}-I\d{{2,}}(?!-W){_TOKEN_END})"
        ),
    ),
    LinkPattern(
        "phase",
        _compile(
            rf"(?P<ref>urn:eawf:v1:phase:{_URN_TAIL}|{_TOKEN_START}P\d{{2,}}(?!-I){_TOKEN_END})"
        ),
    ),
    LinkPattern(
        "hypothesis",
        _compile(
            rf"(?P<ref>urn:eawf:v1:hypothesis:{_URN_TAIL}|"
            rf"{_TOKEN_START}(?:{_PROJECT_CODE}-)?H\d{{2,}}-\d{{2,}}{_TOKEN_END})"
        ),
    ),
    LinkPattern(
        "decision",
        _compile(
            rf"(?P<ref>urn:eawf:v1:decision:{_URN_TAIL}|"
            rf"{_TOKEN_START}(?:DEC-\d{{3,}}|D\d{{2,}}){_TOKEN_END})"
        ),
    ),
    LinkPattern(
        "audit",
        _compile(
            rf"(?P<ref>urn:eawf:v1:audit:{_URN_TAIL}|"
            rf"{_TOKEN_START}(?:AUD-\d{{3,}}|A\d{{2,}}(?:-[A-Za-z0-9][A-Za-z0-9_.-]*)?){_TOKEN_END})"
        ),
    ),
    LinkPattern(
        "artifact",
        _compile(
            rf"(?P<ref>urn:eawf:v1:artifact:{_URN_TAIL}|"
            rf"{_TOKEN_START}(?:ART-\d{{3,}}|artifact:[A-Za-z0-9_./:#-]+){_TOKEN_END})"
        ),
    ),
    LinkPattern(
        "memory",
        _compile(
            rf"(?P<ref>urn:eawf:v1:memory:{_URN_TAIL}|"
            rf"{_TOKEN_START}MEM-\d{{3,}}{_TOKEN_END})"
        ),
    ),
    LinkPattern(
        "report",
        _compile(
            rf"(?P<ref>urn:eawf:v1:report:{_URN_TAIL}|"
            rf"{_TOKEN_START}(?:REPORT|RPT)-\d{{3,}}{_TOKEN_END}|report:[A-Za-z0-9_./:-]+)"
        ),
    ),
    LinkPattern(
        "event",
        _compile(
            rf"(?P<ref>urn:eawf:v1:event:{_URN_TAIL}|"
            rf"{_TOKEN_START}(?:EVENT|EVT)-\d{{3,}}{_TOKEN_END}|event:[A-Za-z0-9_./:-]+)"
        ),
    ),
    LinkPattern(
        "profile",
        _compile(rf"(?P<ref>urn:eawf:v1:profile:{_URN_TAIL}|profile:[A-Za-z0-9_.-]+)"),
    ),
)

if len(LINK_PATTERNS) != len(REFERENCE_KINDS):  # pragma: no cover - import-time guard
    raise RuntimeError("link pattern catalog drifted from reference kind catalog")


def _target_from_label(kind: ReferenceKind, label: str) -> str:
    """Return the modal target id for a matched label."""
    prefixes = {
        "repo": "repo:",
        "project": "project:",
        "artifact": "artifact:",
        "report": "report:",
        "event": "event:",
        "profile": "profile:",
        "spec": "spec:",
    }
    prefix = prefixes.get(kind)
    if prefix is not None and label.lower().startswith(prefix):
        return label.split(":", 1)[1]
    return label


def iter_refs(text: str) -> tuple[LinkRef, ...]:
    """Return non-overlapping references in *text*.

    Longest refs win when patterns start at the same position, so
    ``P28-I03-W35`` becomes one wave ref instead of phase + iter + wave.
    Earlier-starting refs win over nested refs, so ``spec:P28-I03`` stays
    one spec ref instead of also linking its lifecycle suffix.
    """
    matches: list[tuple[int, int, int, LinkRef]] = []
    for priority, pattern in enumerate(LINK_PATTERNS):
        for match in pattern.regex.finditer(text):
            label = match.group("ref")
            ref = LinkRef(
                kind=pattern.kind,
                target=_target_from_label(pattern.kind, label),
                label=label,
                start=match.start("ref"),
                end=match.end("ref"),
            )
            matches.append((ref.start, -(ref.end - ref.start), priority, ref))
    refs: list[LinkRef] = []
    occupied: list[range] = []
    for _start, _neg_len, _priority, ref in sorted(matches):
        span = range(ref.start, ref.end)
        if any(_overlaps(span, used) for used in occupied):
            continue
        refs.append(ref)
        occupied.append(span)
    refs.sort(key=lambda ref: ref.start)
    return tuple(refs)


def _overlaps(left: range, right: range) -> bool:
    """Return whether two half-open ranges overlap."""
    return left.start < right.stop and right.start < left.stop


def action_markup(ref: LinkRef) -> str:
    """Return Textual ``@click`` action markup for *ref*."""
    return f"app.{ref.action_name}({ref.target!r})"


def linkify_text(text: str) -> str:
    """Return *text* with recognised refs wrapped in clickable markup."""
    refs = iter_refs(text)
    if not refs:
        return escape(text)
    chunks: list[str] = []
    cursor = 0
    for ref in refs:
        chunks.append(escape(text[cursor : ref.start]))
        chunks.append(f"[@click={action_markup(ref)}][u]{escape(ref.label)}[/][/]")
        cursor = ref.end
    chunks.append(escape(text[cursor:]))
    return "".join(chunks)


def tooltip_summary(text: str, *, max_refs: int = 3) -> str | None:
    """Return a generic hover tooltip for refs in *text*."""
    refs = iter_refs(text)
    if not refs:
        return None
    lines = [f"{ref.kind} {ref.target}" for ref in refs[:max_refs]]
    if len(refs) > max_refs:
        lines.append(f"+{len(refs) - max_refs} more")
    return "\n".join(lines)


__all__ = [
    "LINK_PATTERNS",
    "REFERENCE_KINDS",
    "LinkPattern",
    "LinkRef",
    "ReferenceKind",
    "action_markup",
    "iter_refs",
    "linkify_text",
    "tooltip_summary",
]
