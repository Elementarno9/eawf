"""Inline markdown in console text: the visible words, and where each style falls on them.

Titles and criteria are authored with inline markdown -- bold, italic, code and links --
and a row that showed the markup would print its backticks and asterisks. The parser here
keeps only the visible text, so every width, pad and clip still measures exactly what is
drawn, and carries each styled run beside it as a span the row painter applies. Nothing
is ever read as Rich markup, so a ``[bracket]`` in the text stays a bracket.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from os.path import commonprefix

from markdown_it import MarkdownIt

from eawf.surfaces.tui.console.frame import Fixed
from eawf.surfaces.tui.console.width import pad

# CommonMark only: the console renders the inline subset authors write in titles, and
# the GFM extras (tables, strikethrough) have no inline meaning on a row.
_MARKDOWN = MarkdownIt("commonmark")


class Inline(StrEnum):
    """The inline styles a row may carry."""

    BOLD = "bold"
    ITALIC = "italic"
    CODE = "code"
    LINK = "link"


@dataclass(frozen=True, slots=True)
class Span:
    """One styled run of a row, as string indices into its visible text.

    Attributes:
        start: The first index the style covers.
        end: The index one past the last it covers.
        style: The style drawn over the run.
    """

    start: int
    end: int
    style: Inline


# The opening token of each paired style, and the closing token that ends it.
_OPENS = {"strong_open": Inline.BOLD, "em_open": Inline.ITALIC, "link_open": Inline.LINK}
_CLOSES = {"strong_close": Inline.BOLD, "em_close": Inline.ITALIC, "link_close": Inline.LINK}


class Marked(Fixed):
    """Visible text with the inline styles its markdown asked for.

    The string itself is the plain text a frame measures; the markup it was parsed from is
    gone. A row laid out by :func:`lay` is exactly frame-wide, which is why this is a
    fixed row: the frame must neither snap nor clip it, or the spans would point past it.

    Attributes:
        spans: The styled runs, in the order their text was read.
    """

    # a str subclass can hold no non-empty slots, so the spans live in the instance dict
    spans: tuple[Span, ...]

    def __new__(cls, text: str, spans: tuple[Span, ...] = ()) -> Marked:
        row = super().__new__(cls, text)
        row.spans = spans
        return row


def inline_markdown(text: str) -> Marked:
    """Return ``text`` as the words inline markdown draws, with a span per styled run.

    Code, bold, italic and links are styled; a soft or hard break reads as one space; raw
    inline HTML stays as written, and an image reads as its alt text. Markup CommonMark
    does not close, such as a lone backtick, stays literal, as it would in a viewer.

    Args:
        text: The authored text.

    Returns:
        The visible text; empty for an empty ``text``.
    """
    # an empty text parses to no block at all, so there is no inline token to walk
    blocks = _MARKDOWN.parseInline(text)
    children = (blocks[0].children or []) if blocks else []
    out: list[str] = []
    spans: list[Span] = []
    open_at: dict[Inline, list[int]] = {style: [] for style in Inline}
    at = 0
    for token in children:
        if token.type in _OPENS:
            open_at[_OPENS[token.type]].append(at)
            continue
        if token.type in _CLOSES:
            style = _CLOSES[token.type]
            spans.append(Span(open_at[style].pop(), at, style))
            continue
        piece = " " if token.type in ("softbreak", "hardbreak") else token.content
        if token.type == "code_inline":
            spans.append(Span(at, at + len(piece), Inline.CODE))
        out.append(piece)
        at += len(piece)
    return Marked("".join(out), tuple(span for span in spans if span.start < span.end))


def prefixed(prefix: str, value: Marked) -> Marked:
    """Return ``prefix`` followed by ``value``, its spans moved past the prefix."""
    shift = len(prefix)
    return Marked(
        prefix + value,
        tuple(Span(s.start + shift, s.end + shift, s.style) for s in value.spans),
    )


def lay(row: Marked, w: int) -> Marked:
    """Return ``row`` padded or word-clipped to ``w`` cells, its spans cut to what remains.

    A clipped row ends in an ellipsis that was never part of the text, so a span stops
    where the row stops agreeing with the text it was laid from.

    Raises:
        ValueError: ``w`` is negative.
    """
    laid = pad(row, w)
    kept = len(commonprefix([laid, str(row)]))
    return Marked(
        laid,
        tuple(Span(s.start, min(s.end, kept), s.style) for s in row.spans if s.start < kept),
    )
