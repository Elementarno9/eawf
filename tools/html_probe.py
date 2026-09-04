"""Static geometry probe over a rendered HTML page.

Auditing a rendered surface against its mockup is a *layout* question —
"is the sidebar 240px wide, does the header overlap the body" — and
diffing the markup as text answers none of it. This probe reads the
geometry a page *declares* (inline ``style`` properties plus the
``width`` / ``height`` presentation attributes) and returns it as a
typed report: one :class:`ElementBox` per element that declares any of
``left`` / ``top`` / ``width`` / ``height``, plus the overlapping pairs
among the fully-specified boxes.

It is deliberately a *declared*-geometry probe, not a layout engine: it
runs on the standard library alone, so it stays available in CI and in a
worktree with no browser. That bounds what it can answer. A page whose
geometry comes from an external stylesheet, a percentage, or flow layout
reports no boxes — the probe reports what the document states, and a
caller that needs computed layout needs a browser instead.

Units: ``px`` and unitless numbers only. A length in any other unit
(``%``, ``em``, ``rem``, ``vh``) or a keyword (``auto``, ``inherit``)
raises :class:`ValueError` rather than being silently coerced — a
geometry report that quietly dropped half its lengths would be worse
than no report.

Usage::

    uv run python tools/html_probe.py page.html
    uv run python tools/html_probe.py page.html --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

#: CSS properties the probe reads off an element's inline ``style``.
GEOMETRY_PROPERTIES: tuple[str, ...] = ("left", "top", "width", "height")

#: Length units the probe understands. Everything else raises, because a
#: percentage or an ``em`` is only resolvable against a computed layout
#: this probe deliberately does not model.
SUPPORTED_UNITS: tuple[str, ...] = ("px",)

#: Void elements that never carry an end tag, so the parser must pop
#: their depth immediately rather than waiting for a close that never
#: arrives (an unbalanced depth counter corrupts every later row).
_VOID_TAGS: frozenset[str] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def parse_length(raw: str) -> float:
    """Return *raw* as a pixel count.

    Args:
        raw: A CSS length such as ``"240px"``, ``"240"``, or ``"0"``.
            Surrounding whitespace is ignored.

    Returns:
        The length in pixels.

    Raises:
        TypeError: *raw* is not a string.
        ValueError: *raw* is empty, carries an unsupported unit, or does
            not parse as a number.
    """
    if not isinstance(raw, str):
        raise TypeError(f"length must be a string, got {type(raw).__name__}")
    text = raw.strip().lower()
    if not text:
        raise ValueError("length is empty")
    for unit in SUPPORTED_UNITS:
        if text.endswith(unit):
            text = text[: -len(unit)].strip()
            break
    try:
        return float(text)
    except ValueError as exc:
        supported = ", ".join(SUPPORTED_UNITS)
        raise ValueError(
            f"unsupported length {raw!r}: only unitless numbers and {supported} lengths "
            "resolve without a layout engine"
        ) from exc


def parse_style(style: str) -> dict[str, str]:
    """Return the ``property -> value`` map of an inline ``style`` string.

    Args:
        style: The raw attribute value, e.g. ``"width: 240px; top:0"``.

    Returns:
        Lowercased property names mapped to their stripped values.
        Malformed declarations (no ``:``) and empty segments are skipped
        so a stray trailing ``;`` costs nothing.

    Raises:
        TypeError: *style* is not a string.
    """
    if not isinstance(style, str):
        raise TypeError(f"style must be a string, got {type(style).__name__}")
    declarations: dict[str, str] = {}
    for segment in style.split(";"):
        name, separator, value = segment.partition(":")
        if not separator:
            continue
        key = name.strip().lower()
        if key:
            declarations[key] = value.strip()
    return declarations


@dataclass(frozen=True)
class ElementBox:
    """The geometry one element declares.

    Attributes:
        tag: Lowercased tag name.
        identifier: The ``id`` attribute, or ``None``.
        classes: The ``class`` attribute split on whitespace.
        depth: Nesting depth, ``0`` for a top-level element.
        order: 0-based document order among probed elements.
        left: Declared ``left`` in pixels, or ``None``.
        top: Declared ``top`` in pixels, or ``None``.
        width: Declared ``width`` in pixels, or ``None``.
        height: Declared ``height`` in pixels, or ``None``.
    """

    tag: str
    identifier: str | None
    classes: tuple[str, ...]
    depth: int
    order: int
    left: float | None = None
    top: float | None = None
    width: float | None = None
    height: float | None = None

    @property
    def label(self) -> str:
        """Return a stable human label: ``tag#id.class`` as far as declared."""
        parts = [self.tag]
        if self.identifier:
            parts.append(f"#{self.identifier}")
        parts.extend(f".{name}" for name in self.classes)
        return "".join(parts)

    @property
    def is_positioned(self) -> bool:
        """Return ``True`` when all four edges resolve to a rectangle."""
        return None not in (self.left, self.top, self.width, self.height)

    def rectangle(self) -> tuple[float, float, float, float]:
        """Return ``(left, top, right, bottom)`` in pixels.

        Raises:
            ValueError: the element does not declare all four edges.
        """
        if not self.is_positioned:
            raise ValueError(f"{self.label} declares no complete rectangle")
        left = float(self.left or 0.0)
        top = float(self.top or 0.0)
        return (left, top, left + float(self.width or 0.0), top + float(self.height or 0.0))

    def overlaps(self, other: ElementBox) -> bool:
        """Return ``True`` when this rectangle shares area with *other*.

        Edge-adjacent boxes (one's right edge equal to the other's left)
        do NOT overlap: a shared boundary is the normal result of laying
        two panes side by side.

        Raises:
            ValueError: either element declares no complete rectangle.
        """
        left, top, right, bottom = self.rectangle()
        other_left, other_top, other_right, other_bottom = other.rectangle()
        horizontal = left < other_right and other_left < right
        vertical = top < other_bottom and other_top < bottom
        return horizontal and vertical

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable row for this box."""
        return {
            "tag": self.tag,
            "id": self.identifier,
            "classes": list(self.classes),
            "depth": self.depth,
            "order": self.order,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class GeometryReport:
    """The declared geometry of one page.

    Attributes:
        source: Label for the probed page (a path, or ``"<memory>"``).
        element_count: Every start tag the parser saw.
        boxes: One row per element declaring any geometry property, in
            document order.
    """

    source: str
    element_count: int
    boxes: tuple[ElementBox, ...]

    @property
    def positioned(self) -> tuple[ElementBox, ...]:
        """Return only the boxes that resolve to a complete rectangle."""
        return tuple(box for box in self.boxes if box.is_positioned)

    def overlapping_pairs(self) -> tuple[tuple[ElementBox, ElementBox], ...]:
        """Return every overlapping pair among the positioned boxes.

        Nested pairs are included: a child inside its parent overlaps it.
        The caller decides which overlaps are intentional — the probe
        reports geometry, it does not judge a layout.
        """
        positioned = self.positioned
        return tuple(
            (first, second)
            for index, first in enumerate(positioned)
            for second in positioned[index + 1 :]
            if first.overlaps(second)
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable report."""
        return {
            "source": self.source,
            "element_count": self.element_count,
            "boxes": [box.as_dict() for box in self.boxes],
            "overlaps": [[first.label, second.label] for first, second in self.overlapping_pairs()],
        }

    def render(self) -> str:
        """Return the human-readable geometry table."""
        lines = [
            f"geometry report: {self.source}",
            f"  elements: {self.element_count}  boxes: {len(self.boxes)}"
            f"  positioned: {len(self.positioned)}",
        ]
        if not self.boxes:
            lines.append("  (no element declares left/top/width/height)")
        for box in self.boxes:
            cells = " ".join(f"{name}={getattr(box, name)!s:>8}" for name in GEOMETRY_PROPERTIES)
            lines.append(f"  [{box.order:>3}] depth={box.depth} {box.label:<28} {cells}")
        for first, second in self.overlapping_pairs():
            lines.append(f"  overlap: {first.label} x {second.label}")
        return "\n".join(lines)


class _GeometryCollector(HTMLParser):
    """Collect declared geometry while walking a page's start tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.boxes: list[ElementBox] = []
        self.element_count = 0
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record *tag*'s declared geometry and descend one level."""
        self.element_count += 1
        attributes = {name.lower(): (value or "") for name, value in attrs}
        lengths = self._lengths(attributes)
        if lengths:
            self.boxes.append(
                ElementBox(
                    tag=tag.lower(),
                    identifier=attributes.get("id") or None,
                    classes=tuple(attributes.get("class", "").split()),
                    depth=self._depth,
                    order=len(self.boxes),
                    **lengths,
                )
            )
        if tag.lower() not in _VOID_TAGS:
            self._depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Handle a self-closing tag without touching the depth counter."""
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        """Ascend one level, clamping at the root for unbalanced markup."""
        if tag.lower() not in _VOID_TAGS:
            self._depth = max(0, self._depth - 1)

    @staticmethod
    def _lengths(attributes: dict[str, str]) -> dict[str, float]:
        """Return the declared ``left`` / ``top`` / ``width`` / ``height``.

        Inline ``style`` wins over the presentation attribute: a
        stylesheet declaration is what a browser would apply.
        """
        declarations = parse_style(attributes.get("style", ""))
        lengths: dict[str, float] = {}
        for prop in GEOMETRY_PROPERTIES:
            raw = declarations.get(prop) or attributes.get(prop)
            if raw:
                lengths[prop] = parse_length(raw)
        return lengths


def probe_html(markup: str, *, source: str = "<memory>") -> GeometryReport:
    """Return the declared-geometry report for *markup*.

    Args:
        markup: The page source.
        source: Label recorded on the report.

    Returns:
        A :class:`GeometryReport`. An empty page yields a report with no
        boxes rather than an error — "this page declares no geometry" is
        a finding, not a failure.

    Raises:
        TypeError: *markup* or *source* is not a string.
        ValueError: an element declares a length the probe cannot
            resolve (see :func:`parse_length`).
    """
    if not isinstance(markup, str):
        raise TypeError(f"markup must be a string, got {type(markup).__name__}")
    if not isinstance(source, str):
        raise TypeError(f"source must be a string, got {type(source).__name__}")
    collector = _GeometryCollector()
    collector.feed(markup)
    collector.close()
    return GeometryReport(
        source=source,
        element_count=collector.element_count,
        boxes=tuple(collector.boxes),
    )


def probe_file(path: Path) -> GeometryReport:
    """Return the declared-geometry report for the page at *path*.

    Args:
        path: Filesystem path to an HTML page.

    Returns:
        A :class:`GeometryReport` labelled with the given path.

    Raises:
        FileNotFoundError: no file exists at *path*.
        ValueError: the page declares an unresolvable length.
    """
    return probe_html(path.read_text(encoding="utf-8"), source=str(path))


def main(argv: list[str] | None = None) -> int:
    """Print the geometry report for one page.

    Args:
        argv: Argument vector excluding the program name.

    Returns:
        ``0`` on success, ``2`` when the page is missing or declares a
        length the probe cannot resolve.
    """
    parser = argparse.ArgumentParser(
        prog="html_probe",
        description="Report the geometry an HTML page declares.",
    )
    parser.add_argument("page", type=Path, help="path to the HTML page")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)
    try:
        report = probe_file(args.page)
    except (OSError, ValueError) as exc:
        print(f"html_probe: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.as_dict(), indent=2) if args.json else report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
