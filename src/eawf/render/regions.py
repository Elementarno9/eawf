"""Managed-region markers + parsing for rendered files (AGENTS.md, .claude/, ...).

Marker format (line-anchored HTML comment, mirrors ``ea-proposal.md`` §"render"):

    <!-- BEGIN EAWF:managed id=<ID> version=<MAJOR.MINOR> hash=<16-hex> -->
    <body>
    <!-- END EAWF:managed id=<ID> -->

- ``id`` matches ``[A-Za-z0-9_.-]+``.
- ``version`` is ``<major>.<minor>``, integers only.
- ``hash`` is a 16-character lowercase hex digest of the BODY (NOT including the
  marker lines or the trailing newline that separates body from END marker) —
  computed via ``blake2b(body.encode(), digest_size=8).hexdigest()``.

The hash on the BEGIN marker is the *declared* hash of the rendered output. A
hand-edit is detected when ``compute_hash(extracted_body) != declared_hash``.
This module provides only the marker mechanics; drift comparison against the
sidecar manifest lives in :mod:`eawf.render.drift`.

Public API:

    compute_hash(body) -> str
    Region                              # dataclass
    RegionParseError                    # raised by find_regions on malformed input
    find_regions(text) -> list[Region]
    extract_region(text, id) -> Region | None
    replace_region(text, *, id, version, body) -> str
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


_ID_PATTERN = r"[A-Za-z0-9_.-]+"
_VERSION_PATTERN = r"[0-9]+\.[0-9]+"
_HASH_PATTERN = r"[a-f0-9]{16}"

MARKER_BEGIN = re.compile(
    r"<!-- BEGIN EAWF:managed id=(?P<id>" + _ID_PATTERN + r") "
    r"version=(?P<version>" + _VERSION_PATTERN + r") "
    r"hash=(?P<hash>" + _HASH_PATTERN + r") -->"
)
MARKER_END = re.compile(r"<!-- END EAWF:managed id=(?P<id>" + _ID_PATTERN + r") -->")

# Loose recogniser: any line that *looks like* a marker. Used to detect
# nested BEGIN tokens or stray END tokens that would otherwise be silently
# ignored by a strict-only walker.
_BEGIN_LOOSE = re.compile(r"<!-- BEGIN EAWF:managed\b[^>]*-->")
_END_LOOSE = re.compile(r"<!-- END EAWF:managed\b[^>]*-->")


class RegionParseError(Exception):
    """Raised when a managed-region marker block is malformed.

    Cases covered:
    - BEGIN marker without a matching END.
    - Duplicate ``id`` across two BEGIN/END blocks.
    - Nested BEGIN markers (BEGIN seen before the prior block's END).
    - END marker for an id different from the most recent unmatched BEGIN.
    - A loose marker line that fails the strict regex (malformed).
    """


@dataclass(frozen=True)
class Region:
    """One parsed managed region.

    Attributes:
        id: Region identifier (e.g. ``"rules"``, ``"profile.python.skills"``).
        version: ``<major>.<minor>`` declared on the BEGIN marker.
        declared_hash: 16-hex hash recorded on the BEGIN marker. Compare to
            :func:`compute_hash` of :attr:`body` to detect hand-edits.
        body: Region body text *between* the BEGIN and END marker lines, with
            the boundary newlines stripped (so a body of ``"foo"`` round-trips
            faithfully without acquiring an internal newline).
        span: ``(start, end)`` byte offsets covering the entire BEGIN…END span
            in the source string, suitable for ``text[start:end]`` slicing.
    """

    id: str
    version: str
    declared_hash: str
    body: str
    span: tuple[int, int]


def compute_hash(body: str) -> str:
    """Return the 16-character hex digest used in BEGIN marker ``hash=`` field.

    Stable: ``blake2b(body.encode("utf-8"), digest_size=8).hexdigest()``.
    Identical bodies produce identical hashes; differing bodies produce
    different hashes with overwhelming probability (2**-64 collision space is
    plenty for the small population of rendered regions in a project).
    """
    return hashlib.blake2b(body.encode("utf-8"), digest_size=8).hexdigest()


def find_regions(text: str) -> list[Region]:
    """Parse all managed regions in *text*, in source order.

    Walks ``text`` linearly. Whenever a BEGIN marker is seen, the matching END
    marker MUST appear before any other BEGIN; otherwise a
    :exc:`RegionParseError` is raised.

    Raises:
        RegionParseError: Missing END marker, duplicate id, nested markers,
            END id mismatch, or a marker-shaped line that fails the strict
            regex.
    """
    regions: list[Region] = []
    seen_ids: set[str] = set()
    pos = 0
    text_len = len(text)

    while pos < text_len:
        # Find the next loose-BEGIN OR loose-END from here onward. If a loose
        # END appears before any BEGIN, that's a stray END → malformed.
        next_begin_loose = _BEGIN_LOOSE.search(text, pos)
        next_end_loose = _END_LOOSE.search(text, pos)

        if next_begin_loose is None:
            if next_end_loose is not None:
                raise RegionParseError(
                    f"stray END marker at offset {next_end_loose.start()} with no preceding BEGIN"
                )
            break  # no more managed regions

        if next_end_loose is not None and next_end_loose.start() < next_begin_loose.start():
            raise RegionParseError(
                f"stray END marker at offset {next_end_loose.start()} with no preceding BEGIN"
            )

        begin_match = MARKER_BEGIN.match(text, next_begin_loose.start())
        if begin_match is None:
            raise RegionParseError(
                f"malformed BEGIN marker at offset {next_begin_loose.start()}: "
                f"{next_begin_loose.group()!r}"
            )

        region_id = begin_match.group("id")
        version = begin_match.group("version")
        declared_hash = begin_match.group("hash")

        # Look for the next loose-BEGIN or loose-END *after* the current BEGIN.
        body_start = begin_match.end()
        inner_begin = _BEGIN_LOOSE.search(text, body_start)
        inner_end = _END_LOOSE.search(text, body_start)

        if inner_end is None:
            raise RegionParseError(
                f"missing END marker for region id={region_id!r} starting at "
                f"offset {begin_match.start()}"
            )

        if inner_begin is not None and inner_begin.start() < inner_end.start():
            raise RegionParseError(
                f"nested BEGIN marker at offset {inner_begin.start()} inside "
                f"region id={region_id!r}"
            )

        end_match = MARKER_END.match(text, inner_end.start())
        if end_match is None:
            raise RegionParseError(
                f"malformed END marker at offset {inner_end.start()}: {inner_end.group()!r}"
            )

        end_id = end_match.group("id")
        if end_id != region_id:
            raise RegionParseError(
                f"END id={end_id!r} does not match BEGIN id={region_id!r} "
                f"(BEGIN at {begin_match.start()}, END at {end_match.start()})"
            )

        if region_id in seen_ids:
            raise RegionParseError(
                f"duplicate id={region_id!r} (second BEGIN at offset {begin_match.start()})"
            )
        seen_ids.add(region_id)

        # Body is everything between BEGIN's end and END's start, with the
        # boundary newlines stripped so the round-trip is exact.
        raw_body = text[body_start : end_match.start()]
        body = raw_body
        if body.startswith("\n"):
            body = body[1:]
        if body.endswith("\n"):
            body = body[:-1]

        regions.append(
            Region(
                id=region_id,
                version=version,
                declared_hash=declared_hash,
                body=body,
                span=(begin_match.start(), end_match.end()),
            )
        )
        pos = end_match.end()

    return regions


def extract_region(text: str, id: str) -> Region | None:
    """Return the parsed region with the given *id*, or ``None`` if absent.

    Raises:
        RegionParseError: The document as a whole is malformed.
    """
    for region in find_regions(text):
        if region.id == id:
            return region
    return None


def _format_region(region_id: str, version: str, body: str) -> str:
    """Return the marker-wrapped block for a freshly-rendered body."""
    h = compute_hash(body)
    return (
        f"<!-- BEGIN EAWF:managed id={region_id} version={version} hash={h} -->\n"
        f"{body}\n"
        f"<!-- END EAWF:managed id={region_id} -->"
    )


def replace_region(
    text: str,
    *,
    id: str,
    version: str,
    body: str,
) -> str:
    """Insert or replace the managed region *id* in *text*.

    - If a region with this *id* already exists, its entire BEGIN…END span is
      replaced with the freshly-rendered block (new ``hash=``, possibly new
      ``version=``).
    - Otherwise the new block is appended. If *text* is non-empty and does not
      already end with a newline, exactly one newline is inserted between the
      old content and the new BEGIN marker so the marker stays line-anchored.

    Raises:
        RegionParseError: ``text`` already contains a malformed marker block.
    """
    new_block = _format_region(id, version, body)
    existing = extract_region(text, id)
    if existing is not None:
        start, end = existing.span
        return text[:start] + new_block + text[end:]

    if text == "":
        return new_block
    separator = "" if text.endswith("\n") else "\n"
    return text + separator + new_block
