"""The cutover's home-path scan, and why it reports instead of rewriting.

A home-directory path in the corpus is a leak, and the obvious fix --
substitute it on the way through -- is the one thing the importer may not
do. Every source string is carried verbatim so an imported record can be
compared against the row it came from; a rewritten string would make that
comparison fail for a reason nobody recorded. So this module reports, and
the cutover refuses. The operator ends up with a corpus to fix rather
than an import that edited their data.

The scan reuses the emit-time scrubber's three home anchors
(:data:`~eawf.observability.logging.scrub.SensitiveScrubber.PATTERNS`), so
the gate and the scrubber cannot drift apart. It also reuses the
repository path-leak lint's placeholder exemption, for a reason specific
to this corpus: the epoch-1 document carries the text of the project's own
secrets-hygiene rule, whose whole job is to show what a leak looks like.
``/Users/<name>`` inside a rule that forbids committing ``/Users/<name>``
is documentation. A match is therefore exempt when it carries an angle
bracket -- the convention for a name to be filled in -- or an ellipsis --
the convention for an elided tail. A concrete path with a real username
carries neither and is reported.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import Field

from eawf.kernel.migration.epoch2.errors import MigrationHomePathLeakError
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)


#: The tokens that mark a matched home path as a documented placeholder
#: rather than a leak. Kept as a tuple so the exemption is one readable
#: declaration shared by the three anchors.
PLACEHOLDER_TOKENS: Final[tuple[str, ...]] = ("<", ">", "...")

#: How many findings a refusal names before it truncates. A message that
#: lists every hit in a corpus is unreadable; the count is always exact.
MAX_NAMED_FINDINGS: Final = 10


def home_path_patterns() -> tuple[re.Pattern[str], ...]:
    """Return the three home-directory anchors, reused from the scrubber.

    Returns:
        The macOS, Windows and Linux home patterns, in scrubber order.
        Selecting them from the scrubber's own table rather than restating
        them is what keeps the cutover gate and the emit-time redaction
        from drifting.
    """
    from eawf.observability.logging.scrub import SensitiveScrubber

    return tuple(
        pattern
        for pattern in SensitiveScrubber.PATTERNS
        if "Users" in pattern.pattern or "home" in pattern.pattern
    )


def is_documented_placeholder(snippet: str) -> bool:
    """Report whether one matched home path is documentation, not a leak.

    Args:
        snippet: The matched substring.

    Returns:
        ``True`` when the match carries a placeholder token, which is the
        same exemption the repository's own path-leak lint applies.
    """
    return any(token in snippet for token in PLACEHOLDER_TOKENS)


class ScrubFinding(StrictMigrationModel):
    """One concrete home-directory path the staged corpus carries.

    Attributes:
        locator: Where the string sits, as a dotted path through the
            scanned payload such as ``waves.W01.intent``.
        snippet: The matched substring, reported verbatim so the operator
            can find and fix it in the source.
    """

    locator: Annotated[str, Field(min_length=1, max_length=300)]
    snippet: Annotated[str, Field(min_length=1, max_length=300)]


def scan_text(*, locator: str, text: str) -> tuple[ScrubFinding, ...]:
    """Return every non-exempt home path one string carries.

    Args:
        locator: Where the string sits, for the finding.
        text: The string to scan.

    Returns:
        One finding per concrete home path, in match order. A string whose
        only matches are documented placeholders yields nothing.
    """
    findings: list[ScrubFinding] = []
    for pattern in home_path_patterns():
        for match in pattern.finditer(text):
            snippet = match.group(0)
            if is_documented_placeholder(snippet):
                continue
            findings.append(ScrubFinding(locator=locator, snippet=snippet[:300]))
    return tuple(findings)


def _walk_strings(value: Any, prefix: str) -> Iterator[tuple[str, str]]:
    """Yield every ``(locator, string)`` pair reachable inside ``value``.

    Args:
        value: Any decoded JSON value.
        prefix: The locator of ``value`` itself.

    Yields:
        One pair per string, including the object keys, because a source
        row keyed by a path leaks exactly as much as one valued by it.
    """
    if isinstance(value, str):
        yield prefix, value
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            yield child, str(key)
            yield from _walk_strings(item, child)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{prefix}[{index}]")


def scan_payload(*, locator: str, payload: Any) -> tuple[ScrubFinding, ...]:
    """Return every non-exempt home path a decoded JSON value carries.

    Args:
        locator: The locator the payload is rooted at.
        payload: Any decoded JSON value.

    Returns:
        The findings, in traversal order.
    """
    return tuple(
        finding
        for child, text in _walk_strings(payload, locator)
        for finding in scan_text(locator=child, text=text)
    )


def scan_staged_tree(
    state_path: Path, *, ledger_paths: Sequence[Path] = ()
) -> tuple[ScrubFinding, ...]:
    """Scan everything a staged write left on disk.

    Args:
        state_path: The staged tree's ``state.json``. A missing file
            contributes nothing, which is the state before the first
            write.
        ledger_paths: The ledger files the write appended to, in write
            order.

    Returns:
        The findings, document first and then the ledgers in the order
        given.

    Raises:
        json.JSONDecodeError: A scanned file is not the JSON the writer
            left behind.
    """
    findings: list[ScrubFinding] = []
    if state_path.exists():
        findings.extend(
            scan_payload(locator="document", payload=json.loads(state_path.read_text("utf-8")))
        )
    for path in ledger_paths:
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
            findings.extend(scan_payload(locator=f"{path.name}:{number}", payload=json.loads(line)))
    return tuple(findings)


def require_no_home_path_leaks(findings: Sequence[ScrubFinding]) -> None:
    """Refuse a staged tree that carries a concrete home-directory path.

    Args:
        findings: What the scan reported.

    Raises:
        MigrationHomePathLeakError: When ``findings`` is non-empty. The
            message names the first :data:`MAX_NAMED_FINDINGS` hits and
            always reports the exact total, so a large corpus stays
            readable without under-reporting.
    """
    if not findings:
        return
    named = ", ".join(
        f"{finding.locator}: {finding.snippet}" for finding in findings[:MAX_NAMED_FINDINGS]
    )
    suffix = "" if len(findings) <= MAX_NAMED_FINDINGS else ", ..."
    raise MigrationHomePathLeakError(
        f"the staged tree carries {len(findings)} concrete home-directory paths, which "
        f"the importer reports rather than rewrites: {named}{suffix}"
    )


__all__ = [
    "MAX_NAMED_FINDINGS",
    "PLACEHOLDER_TOKENS",
    "ScrubFinding",
    "home_path_patterns",
    "is_documented_placeholder",
    "require_no_home_path_leaks",
    "scan_payload",
    "scan_staged_tree",
    "scan_text",
]
