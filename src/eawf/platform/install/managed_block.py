"""Splice one delimited managed block into a file other people also edit.

A ``.gitignore`` or a runtime ``config.toml`` carries operator lines beside
the block eawf owns. Every write replaces only the bytes between the
block's own begin and end marker lines, so everything outside them survives
byte for byte. The splice works on raw bytes and whole lines, never on a
pattern that could run past a marker: a regex written to stop at a header
can match newlines too and swallow the operator's sections with it.

A file whose markers do not form exactly one ordered pair is refused rather
than repaired. Appending a fresh block after an orphaned begin marker would
make the next write treat every operator line between the two as the old
block and delete it.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "ManagedBlockError",
    "managed_block_lines",
    "render_managed_block",
    "splice_managed_block",
    "unmanaged_bytes",
    "unmanaged_survives",
]


class ManagedBlockError(ValueError):
    """Raised when a file's managed-block markers cannot be spliced safely."""


def _line_body(line: bytes) -> bytes:
    """Return *line* without its line terminator."""
    if line.endswith(b"\r\n"):
        return line[:-2]
    if line.endswith((b"\n", b"\r")):
        return line[:-1]
    return line


def render_managed_block(*, begin: str, end: str, body_lines: Sequence[str]) -> bytes:
    """Return the marker-framed block, one ``\\n``-terminated line each.

    Args:
        begin: The begin marker line, without a terminator.
        end: The end marker line, without a terminator.
        body_lines: The managed lines between the markers.

    Returns:
        The UTF-8 encoded block, ending with a newline.

    Raises:
        ValueError: When the markers are empty or equal, or a body line holds
            a line break or repeats a marker, since any of those would make
            the next splice misread where the block ends.
    """
    if not begin or not end or begin == end:
        raise ValueError(f"managed block markers must be distinct and non-empty: {begin!r}")
    for line in body_lines:
        if "\n" in line or "\r" in line:
            raise ValueError(f"managed block line holds a line break: {line!r}")
        if line in (begin, end):
            raise ValueError(f"managed block line repeats a marker: {line!r}")
    return ("\n".join([begin, *body_lines, end]) + "\n").encode("utf-8")


def _block_span(existing: bytes, *, begin: str, end: str) -> tuple[int, int] | None:
    """Return the byte span of the one managed block, or ``None`` when absent.

    Raises:
        ManagedBlockError: When the markers are unpaired, out of order, or
            appear more than once.
    """
    begin_marker = begin.encode("utf-8")
    end_marker = end.encode("utf-8")
    begins: list[int] = []
    ends: list[int] = []
    offset = 0
    for line in existing.splitlines(keepends=True):
        body = _line_body(line)
        if body == begin_marker:
            begins.append(offset)
        elif body == end_marker:
            ends.append(offset + len(line))
        offset += len(line)
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0] <= begins[0]:
        raise ManagedBlockError(
            f"managed block markers are not one ordered pair ({len(begins)} {begin!r}, "
            f"{len(ends)} {end!r}); repair the file by hand so no operator line is lost"
        )
    return begins[0], ends[0]


def _append_separator(existing: bytes) -> bytes:
    """Return the spacing that sets a newly appended block apart."""
    if not existing or existing.endswith((b"\n\n", b"\r\n\r\n")):
        return b""
    if existing.endswith((b"\n", b"\r")):
        return b"\n"
    return b"\n\n"


def splice_managed_block(existing: bytes, *, begin: str, end: str, block: bytes) -> bytes:
    """Return *existing* with its managed block replaced by *block*.

    When the file carries no block yet, *block* is appended after a blank
    line. Bytes before the begin marker and after the end marker's line are
    copied unchanged.

    Args:
        existing: The current file bytes; empty for a file not yet written.
        begin: The begin marker line.
        end: The end marker line.
        block: The replacement, from :func:`render_managed_block`.

    Returns:
        The new file bytes.

    Raises:
        ManagedBlockError: When the markers in *existing* are not exactly one
            ordered pair.
    """
    span = _block_span(existing, begin=begin, end=end)
    if span is None:
        return existing + _append_separator(existing) + block
    start, stop = span
    return existing[:start] + block + existing[stop:]


def unmanaged_bytes(existing: bytes, *, begin: str, end: str) -> bytes:
    """Return *existing* with its managed block cut out.

    Args:
        existing: The current file bytes.
        begin: The begin marker line.
        end: The end marker line.

    Returns:
        Every byte outside the block; *existing* itself when it has none.

    Raises:
        ManagedBlockError: When the markers in *existing* are not exactly one
            ordered pair.
    """
    span = _block_span(existing, begin=begin, end=end)
    if span is None:
        return existing
    start, stop = span
    return existing[:start] + existing[stop:]


def unmanaged_survives(prior: bytes, written: bytes, *, begin: str, end: str) -> bool:
    """Return whether every byte outside the managed block survived a write.

    Checked against the bytes read back after the write, so a splice that
    reached past a marker, or a writer that raced this one, is caught
    rather than trusted.

    Args:
        prior: The file bytes before the write; empty for a new file.
        written: The file bytes read back after the write.
        begin: The begin marker line.
        end: The end marker line.

    Returns:
        ``True`` when *written* holds exactly one block and the bytes around
        it equal those around the block of *prior*; a first write may only
        have appended a separator of line breaks and the block.

    Raises:
        ManagedBlockError: When the markers in either input are not exactly
            one ordered pair.
    """
    span = _block_span(written, begin=begin, end=end)
    if span is None:
        return False
    start, stop = span
    prior_span = _block_span(prior, begin=begin, end=end)
    if prior_span is None:
        separator = written[len(prior) : start]
        return written.startswith(prior) and not separator.strip(b"\r\n") and stop == len(written)
    prior_start, prior_stop = prior_span
    return written[:start] == prior[:prior_start] and written[stop:] == prior[prior_stop:]


def managed_block_lines(existing: bytes, *, begin: str, end: str) -> tuple[str, ...] | None:
    """Return the body lines of the one managed block, or ``None`` when absent.

    Args:
        existing: The current file bytes.
        begin: The begin marker line.
        end: The end marker line.

    Returns:
        The lines between the markers, without terminators.

    Raises:
        ManagedBlockError: When the markers in *existing* are not exactly one
            ordered pair.
    """
    span = _block_span(existing, begin=begin, end=end)
    if span is None:
        return None
    start, stop = span
    lines = existing[start:stop].decode("utf-8").splitlines()
    return tuple(lines[1:-1])
