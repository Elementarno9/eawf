"""Powerline statusline rendering for ``eawf cc statusline``.

The orchestrator in :mod:`eawf.runtime.runtimes.claude.statusline` collects the
line-1 :class:`StatuslineSegment` records and the :class:`PowerlineBar` rows
from the modules in :mod:`eawf.runtime.runtimes.claude.statusline_modules`.
This module draws them: line 1 as arrow-ended powerline segments, then one bar
row per window, in a light or a dark 24-bit :class:`Palette`.

Public surface:

- :class:`StatuslineSegment` — typed segment dataclass.
- :class:`Palette` / :func:`load_palette` — the bundled 24-bit palettes in
  ``templates/themes.yaml``.
- :class:`PowerlineBar` — one bar row: label, used fraction, trend, detail.
- :func:`render_powerline` — draw line 1 and the bar rows within a width.
- :func:`format_countdown` / :func:`format_context_tokens` — the texts a bar
  row shows after its trend.
- :func:`sourced_segment` / :func:`unavailable_segment` — a segment whose
  value a named producer states, and the marker naming why it cannot be shown.
- :func:`budget_segment` / :func:`budget_unavailable_segment` — the active
  scope's spend against its one ceiling, and the marker naming why it
  cannot be shown.

Every segment carries a :class:`~eawf.kernel.projection.truth.TruthField`, so its
producer, freshness and measurement quality travel with the text, and a segment
that would render a bare unknown token is refused at construction. A segment
whose value cannot be sourced keeps its ``n/a(<reason>)`` marker for audit, but
the powerline omits it rather than drawing the marker.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Annotated, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, StringConstraints

from eawf.kernel.projection.truth import (
    Freshness,
    Precision,
    TruthField,
    TruthKind,
    TruthState,
)
from eawf.kernel.state.enums import MeasurementQuality
from eawf.surfaces.render.units import format_tokens

SegmentStatus = Literal["ok", "info", "warn", "missing", "degraded", "failed"]
"""Status carried on each :class:`StatuslineSegment`.

``info`` marks a value that is shown but makes no health claim either way, so
it keeps its module's fill. ``warn`` and ``degraded`` switch the fill to the
palette's warning and alert colours. ``failed`` is reserved for modules that
raised during render: the orchestrator wraps any module exception in a
``status="failed"`` segment carrying the ``<module>:n/a(render-failed)``
marker, which the powerline omits.
"""


#: The marker a segment renders in place of a value it cannot source, always followed
#: by the parenthesised reason.
UNAVAILABLE_MARK: Final = "n/a"

#: Tokens that say "no value" without saying why. A segment rendering one would show
#: an absence the operator cannot act on, so a segment carrying one is refused.
BARE_UNKNOWN_TOKENS: Final = frozenset({"?", "-", "unknown", "none", "null"})

#: The segment-contract revision a producer with no revision of its own is read at.
SEGMENT_CONTRACT_REVISION: Final = 1

_TOKEN_SPLIT: Final = re.compile(r"[\s:/]+")


@dataclass(frozen=True, slots=True, kw_only=True)
class SegmentSource:
    """Who states a segment's value, and how exactly.

    Attributes:
        producer: The component that states the value, such as the host payload.
        provenance: The record or field the value is read from.
        revision: The producer's revision the value was read at.
        truth_kind: How the value came to exist.
        precision: How exactly the value states its quantity when present.
        quality: The measurement quality behind the value when present.
    """

    producer: str
    provenance: str
    revision: int = SEGMENT_CONTRACT_REVISION
    truth_kind: TruthKind = TruthKind.OBSERVED
    precision: Precision = Precision.EXACT
    quality: MeasurementQuality = MeasurementQuality.EXACT


@dataclass(frozen=True)
class StatuslineSegment:
    """One module's output — what the renderer joins into the final line.

    Attributes:
        module: Stable module identifier (``scope``, ``git``,
            ``model_session_cwd``, ``context_tokens``, ``cost``, ``mcp_health``,
            ``hooks_plugins``, ``memory``, ``token_saving``). Used to look
            up the per-module glyph in the theme.
        text: User-facing body, e.g. ``"scope:EAWF-0126"`` or ``"git:feature/x*"``.
            Already pre-formatted by the module — the renderer only adds
            colour and glyph wrappers.
        truth: The truth field behind ``text``: its producer, freshness and
            measurement quality, or the reason it is unavailable.
        status: Segment status, drives the colour lookup.

    Raises:
        ValueError: ``text`` carries a bare unknown token, a known ``truth``
            states no value, or an unavailable ``truth`` is rendered without its
            ``n/a(<reason>)`` marker.
    """

    module: str
    text: str
    truth: TruthField[str]
    status: SegmentStatus = "ok"

    def __post_init__(self) -> None:
        """Refuse a segment that renders a value it cannot source."""
        bare = sorted(
            {token for token in _TOKEN_SPLIT.split(self.text) if token in BARE_UNKNOWN_TOKENS}
        )
        if bare:
            raise ValueError(f"segment {self.module!r} renders bare unknown token(s) {bare}")
        if self.truth.state is TruthState.KNOWN:
            if not self.truth.value:
                raise ValueError(f"segment {self.module!r} is known but states no value")
            return
        marker = f"{UNAVAILABLE_MARK}({self.truth.missing_reason})"
        if marker not in self.text:
            raise ValueError(f"segment {self.module!r} is unavailable but does not render {marker}")


def _known_truth(value: str, source: SegmentSource) -> TruthField[str]:
    """Return the truth field for ``value`` as ``source`` states it at render time."""
    return TruthField[str](
        value=value,
        state=TruthState.KNOWN,
        truth_kind=source.truth_kind,
        producer=source.producer,
        producer_revision=source.revision,
        precision=source.precision,
        measurement_quality=source.quality,
        freshness=Freshness.LIVE,
        provenance_refs=(source.provenance,),
    )


def sourced_segment(
    module: str,
    label: str,
    value: str,
    source: SegmentSource,
    *,
    status: SegmentStatus = "ok",
) -> StatuslineSegment:
    """Build a ``<label>:<value>`` segment whose value ``source`` states.

    Args:
        module: The segment's module id.
        label: The prefix the operator reads the value by.
        value: The rendered value.
        source: Who states the value, and how exactly.
        status: The segment status.

    Returns:
        The segment, read live from ``source``.

    Raises:
        ValueError: ``value`` is empty or is a bare unknown token.
        pydantic.ValidationError: ``source`` states an estimate as exact, or an
            empty producer or provenance.
    """
    return StatuslineSegment(
        module=module,
        text=f"{label}:{value}",
        truth=_known_truth(value, source),
        status=status,
    )


def unavailable_segment(
    module: str,
    label: str,
    reason: str,
    source: SegmentSource,
    *,
    status: SegmentStatus = "missing",
) -> StatuslineSegment:
    """Build the ``<label>:n/a(<reason>)`` segment for a value ``source`` cannot state.

    Args:
        module: The segment's module id.
        label: The prefix the operator reads the value by.
        reason: One short token naming why, such as ``no-session``.
        source: The producer that was asked for the value.
        status: The segment status; ``failed`` for a module that raised.

    Returns:
        The unavailable segment.

    Raises:
        ValueError: ``reason`` is empty or contains whitespace.
    """
    if not reason or any(char.isspace() for char in reason):
        raise ValueError(f"reason must be one non-empty token; got {reason!r}")
    truth = TruthField[str](
        value=None,
        state=TruthState.UNAVAILABLE,
        truth_kind=source.truth_kind,
        producer=source.producer,
        producer_revision=source.revision,
        precision=Precision.UNAVAILABLE,
        measurement_quality=MeasurementQuality.UNAVAILABLE,
        freshness=Freshness.LIVE,
        provenance_refs=(source.provenance,),
        missing_reason=reason,
    )
    return StatuslineSegment(
        module=module,
        text=f"{label}:{UNAVAILABLE_MARK}({reason})",
        truth=truth,
        status=status,
    )


#: The bundled palette file and the package it ships in.
_PALETTES_FILENAME: Final = "themes.yaml"
_TEMPLATES_PACKAGE: Final = "eawf.platform.templates"

Colour = Annotated[str, StringConstraints(pattern=r"^#[0-9a-f]{6}$")]
"""A 24-bit colour as lower-case ``#rrggbb``."""


class Palette(BaseModel):
    """One 24-bit statusline palette.

    Every colour the renderer emits comes from here, so the contrast of each
    foreground and background pair can be checked against the palette alone.

    Attributes:
        base: Background of every cell outside a segment fill, separator
            gaps and bar rows included; meant to match the terminal.
        ink: Text on a segment fill.
        text: The used percentage on ``base``.
        muted: A reset countdown or the context size on ``base``.
        track: The unfilled part of a bar on ``base``.
        label: Fill of a bar row's label.
        model: Fill of the model segment.
        project: Fill of the project segment.
        active_time: Fill of the active-time segment.
        cost: Fill of the session-cost segment.
        mcp: Fill of the MCP segment.
        scope: Fill of the Run scope segment.
        budget: Fill of the Run budget segment.
        warn: Fill of a segment whose status is ``warn``.
        alert: Fill of a segment whose status is ``degraded`` or ``failed``.
        low: Bar fill below half used.
        mid: Bar fill from half to four fifths used.
        high: Bar fill from four fifths used.
        rising: A rising trend on ``base``.
        falling: A falling trend on ``base``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    base: Colour
    ink: Colour
    text: Colour
    muted: Colour
    track: Colour
    label: Colour
    model: Colour
    project: Colour
    active_time: Colour
    cost: Colour
    mcp: Colour
    scope: Colour
    budget: Colour
    warn: Colour
    alert: Colour
    low: Colour
    mid: Colour
    high: Colour
    rising: Colour
    falling: Colour


class _PaletteFile(BaseModel):
    """The bundled palette file: exactly one light and one dark palette."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    light: Palette
    dark: Palette


def load_palette(name: Literal["light", "dark"]) -> Palette:
    """Return the bundled palette called *name*.

    Args:
        name: ``light`` or ``dark``.

    Returns:
        The palette, read from ``templates/themes.yaml``.

    Raises:
        pydantic.ValidationError: The bundled file does not hold exactly a
            light and a dark palette of ``#rrggbb`` colours.
    """
    raw = (files(_TEMPLATES_PACKAGE) / _PALETTES_FILENAME).read_text(encoding="utf-8")
    palettes = _PaletteFile.model_validate(yaml.safe_load(raw))
    return palettes.light if name == "light" else palettes.dark


@dataclass(frozen=True, slots=True, kw_only=True)
class PowerlineBar:
    """One bar row under line 1: a window's used share and what qualifies it.

    Attributes:
        label: The row's name, such as ``ctx`` or ``5h``.
        ratio: The used fraction in ``[0, 1]``.
        detail: The text after the trend: the reset countdown of a rate
            window, or the used over limit tokens of the context; ``None``
            when there is nothing to show.
        trend: Change of the used percentage in points since the session's
            previous render; ``None`` when unknown.

    Raises:
        ValueError: *label* is empty or *ratio* is outside ``[0, 1]``.
    """

    label: str
    ratio: float
    detail: str | None = None
    trend: int | None = None

    def __post_init__(self) -> None:
        """Refuse a row the renderer could not draw."""
        if not self.label:
            raise ValueError("a bar row needs a label")
        if not 0.0 <= self.ratio <= 1.0:
            raise ValueError(f"ratio out of range, expected [0, 1]: {self.ratio!r}")

    @property
    def percent(self) -> int:
        """Return the used share in whole percent, rounded half up."""
        return int(self.ratio * 100 + 0.5)


def format_countdown(seconds: float) -> str:
    """Return the time until a reset as ``<d>d<h>h``, ``<h>h<mm>m`` or ``<m>m``.

    Args:
        seconds: Seconds until the reset; part of a minute is dropped.

    Returns:
        The countdown, such as ``6d14h``, ``2h05m`` or ``42m``.

    Raises:
        ValueError: *seconds* is negative.
    """
    if seconds < 0:
        raise ValueError(f"seconds must be non-negative: {seconds!r}")
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def format_context_tokens(count: int) -> str:
    """Return a token count as ``950``, ``64K`` or ``1.0M``.

    Args:
        count: The non-negative token count.

    Returns:
        The count in whole thousands below a million, in millions to one
        decimal from there.

    Raises:
        ValueError: *count* is negative.
    """
    if count < 0:
        raise ValueError(f"count must be non-negative: {count!r}")
    if count < 1_000:
        return str(count)
    thousands = int(count / 1_000 + 0.5)
    if thousands < 1_000:
        return f"{thousands}K"
    return f"{count / 1_000_000:.1f}M"


_ARROW: Final = ""
_RESET: Final = "\x1b[0m"
_SGR: Final = re.compile(r"\x1b\[[0-9;]*m")
_ELLIPSIS: Final = "…"

#: Line-1 segments in the order they are dropped when the row is too wide;
#: model and cost are never dropped, only clipped.
_DROP_ORDER: Final = ("mcp_health", "active_time", "project", "hooks_plugins", "budget", "scope")

#: The palette fill of each line-1 module whose status makes no claim.
_FILL: Final = {
    "model": "model",
    "project": "project",
    "active_time": "active_time",
    "cost": "cost",
    "mcp_health": "mcp",
    "scope": "scope",
    "budget": "budget",
}

#: The prefix a line-1 module's value is read by, where the value alone is ambiguous.
_PREFIX: Final = {"mcp_health": "MCP: ", "budget": "budget: "}

_HOOK_ERRORS: Final = re.compile(r"hook-errors:\d+")

_BAR_WIDE: Final = 10
_BAR_NARROW: Final = 5
_BAR_FULL: Final = "█"
_BAR_PARTIAL: Final = "▏▎▍▌▋▊▉"
_BAR_TRACK: Final = "░"
_LOW_BELOW: Final = 0.5
_MID_BELOW: Final = 0.8


def _fg(colour: str) -> str:
    """Return the 24-bit foreground escape for *colour*."""
    return f"\x1b[38;2;{int(colour[1:3], 16)};{int(colour[3:5], 16)};{int(colour[5:7], 16)}m"


def _bg(colour: str) -> str:
    """Return the 24-bit background escape for *colour*."""
    return f"\x1b[48;2;{int(colour[1:3], 16)};{int(colour[3:5], 16)};{int(colour[5:7], 16)}m"


def _cells(text: str) -> int:
    """Return the terminal cells *text* occupies; wide glyphs take two, marks none."""
    total = 0
    for char in _SGR.sub("", text):
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return total


def _clip(text: str, width: int) -> str:
    """Return *text* cut to *width* cells, ending in an ellipsis when cut."""
    if _cells(text) <= width:
        return text
    kept = ""
    for char in text:
        if _cells(kept) + _cells(char) > width - 1:
            break
        kept += char
    return f"{kept}{_ELLIPSIS}"


@dataclass(frozen=True, slots=True)
class _Piece:
    """One line-1 segment as it is drawn: its module, fill and text."""

    module: str
    fill: str
    text: str


def _display(segment: StatuslineSegment) -> str | None:
    """Return the text line 1 shows for *segment*, or ``None`` to omit it.

    A segment with no sourced value is omitted rather than drawn as a
    marker, and the hooks segment shows only its failure token.
    """
    if segment.truth.state is not TruthState.KNOWN or segment.truth.value is None:
        return None
    if segment.module == "hooks_plugins":
        match = _HOOK_ERRORS.search(segment.text)
        return match.group() if match else None
    return f"{_PREFIX.get(segment.module, '')}{segment.truth.value}"


def _fill(segment: StatuslineSegment, palette: Palette) -> str:
    """Return the fill colour *segment* is drawn on."""
    if segment.status in {"degraded", "failed"}:
        return palette.alert
    if segment.status == "warn":
        return palette.warn
    field_name = _FILL.get(segment.module)
    return str(getattr(palette, field_name)) if field_name else palette.label


def _line_one(pieces: Sequence[_Piece], palette: Palette) -> str:
    """Draw *pieces* as arrow-ended segments separated by one ``base`` cell."""
    drawn = [
        f"{_bg(piece.fill)}{_fg(palette.ink)} {piece.text} "
        f"{_bg(palette.base)}{_fg(piece.fill)}{_ARROW}"
        for piece in pieces
    ]
    return f"{' '.join(drawn)}{_RESET}"


def _fit_line_one(pieces: list[_Piece], palette: Palette, width: int) -> list[_Piece]:
    """Return the pieces that fit *width*: drop by priority, then clip the first."""
    kept = list(pieces)
    for module in _DROP_ORDER:
        if _cells(_line_one(kept, palette)) <= width:
            return kept
        kept = [piece for piece in kept if piece.module != module]
    while kept and (excess := _cells(_line_one(kept, palette)) - width) > 0:
        head = kept[0]
        room = _cells(head.text) - excess
        if room >= 2:
            kept[0] = _Piece(head.module, head.fill, _clip(head.text, room))
        else:
            kept.pop(0)
    return kept


def _bar_glyphs(ratio: float, width: int) -> tuple[str, str]:
    """Return the filled and the unfilled run of a *width*-cell bar at *ratio*."""
    units = int(ratio * width * 8 + 0.5)
    full, remainder = divmod(units, 8)
    filled = _BAR_FULL * full + (_BAR_PARTIAL[remainder - 1] if remainder else "")
    return filled, _BAR_TRACK * (width - len(filled))


def _bar_row(
    bar: PowerlineBar, palette: Palette, *, bar_width: int, trend: bool, detail: bool
) -> str:
    """Draw one bar row: label, bar, percentage, then the trend and detail asked for."""
    if bar.ratio < _LOW_BELOW:
        level = palette.low
    elif bar.ratio < _MID_BELOW:
        level = palette.mid
    else:
        level = palette.high
    filled, track = _bar_glyphs(bar.ratio, bar_width)
    row = (
        f"{_bg(palette.label)}{_fg(palette.ink)} {bar.label:<3} "
        f"{_bg(palette.base)}{_fg(palette.label)}{_ARROW} "
        f"{_fg(level)}{filled}{_fg(palette.track)}{track}"
        f"{_fg(palette.text)} {bar.percent:>3}%"
    )
    if trend and bar.trend:
        mark, colour = ("↑+", palette.rising) if bar.trend > 0 else ("↓", palette.falling)
        row += f"{_fg(colour)} {mark}{bar.trend}"
    if detail and bar.detail:
        row += f"{_fg(palette.muted)} {bar.detail}"
    return f"{row}{_RESET}"


def _fit_bar_row(bar: PowerlineBar, palette: Palette, width: int | None) -> str | None:
    """Return the widest form of *bar*'s row that fits *width*, or ``None`` when none does.

    The bar shrinks to five cells first, then the trend goes, then the detail.
    """
    forms = (
        (_BAR_WIDE, True, True),
        (_BAR_NARROW, True, True),
        (_BAR_NARROW, False, True),
        (_BAR_NARROW, False, False),
    )
    for bar_width, trend, detail in forms:
        row = _bar_row(bar, palette, bar_width=bar_width, trend=trend, detail=detail)
        if width is None or _cells(row) <= width:
            return row
    return None


def render_powerline(
    segments: Sequence[StatuslineSegment],
    bars: Sequence[PowerlineBar],
    palette: Palette,
    *,
    width: int | None,
) -> list[str]:
    """Draw line 1 from *segments* and one row per bar, all in *palette*.

    Line 1 shows each segment with a sourced value, in the order given; a
    segment with none is omitted, never drawn as a marker. Every cell carries
    an explicit foreground and background from the palette.

    Args:
        segments: The line-1 segments, in display order.
        bars: The bar rows, in display order.
        palette: The colours to draw with.
        width: The terminal width in cells, or ``None`` to draw every row
            whole. Line 1 drops MCP, then active time, then project, then
            hooks, budget and scope, and clips the model last; a bar row
            shrinks its bar, then drops the trend, then the detail. A row
            that still does not fit is left out, so no row ever wraps.

    Returns:
        The rows, top to bottom, each ending in an attribute reset; empty when
        nothing could be sourced.
    """
    pieces = [
        _Piece(segment.module, _fill(segment, palette), text)
        for segment in segments
        if (text := _display(segment)) is not None
    ]
    if width is not None:
        pieces = _fit_line_one(pieces, palette, width)
    rows = [_line_one(pieces, palette)] if pieces else []
    for bar in bars:
        row = _fit_bar_row(bar, palette, width)
        if row is not None:
            rows.append(row)
    return rows


_BUDGET_MODULE: str = "budget"
"""Stable module id for the spend-against-ceiling segment."""

_LIMIT_NOTICE_MARK: str = "!limit"
"""Suffix shown while the scope's limit-reached notice is open."""


def budget_segment(
    *, spent: int, limit: int, notice_open: bool, source: SegmentSource
) -> StatuslineSegment:
    """Build the segment showing *spent* against the scope's one ceiling *limit*.

    The spend stays readable continuously; only the open limit-reached
    notice changes the segment's status, because that notice is the event,
    and a fraction short of the ceiling is not.

    Args:
        spent: Tokens consumed by the scope so far.
        limit: The scope's one ceiling, in tokens.
        notice_open: Whether the scope's limit-reached notice is open.
        source: Who states the spend and the ceiling.

    Returns:
        A ``budget:<spent>/<limit>`` segment, suffixed ``!limit`` with status
        ``warn`` while the notice is open.

    Raises:
        ValueError: *spent* or *limit* is negative.
    """
    if spent < 0 or limit < 0:
        raise ValueError(f"spent and limit must be non-negative; got {spent}/{limit}")
    value = f"{format_tokens(spent)}/{format_tokens(limit)}"
    if notice_open:
        return sourced_segment(
            _BUDGET_MODULE, _BUDGET_MODULE, f"{value} {_LIMIT_NOTICE_MARK}", source, status="warn"
        )
    return sourced_segment(_BUDGET_MODULE, _BUDGET_MODULE, value, source)


def budget_unavailable_segment(reason: str, source: SegmentSource) -> StatuslineSegment:
    """Build the budget segment for a scope whose spend or ceiling is unknown.

    No ratio is drawn across an unknown side, so the segment names why
    instead of showing a number it cannot source.

    Args:
        reason: A short, single-token reason such as ``no-budget``.
        source: The producer that was asked for the spend.

    Returns:
        A ``budget:n/a(<reason>)`` segment with status ``missing``.

    Raises:
        ValueError: *reason* is empty or contains whitespace.
    """
    return unavailable_segment(_BUDGET_MODULE, _BUDGET_MODULE, reason, source)


__all__ = [
    "BARE_UNKNOWN_TOKENS",
    "SEGMENT_CONTRACT_REVISION",
    "UNAVAILABLE_MARK",
    "Palette",
    "PowerlineBar",
    "SegmentSource",
    "SegmentStatus",
    "StatuslineSegment",
    "budget_segment",
    "budget_unavailable_segment",
    "format_context_tokens",
    "format_countdown",
    "load_palette",
    "render_powerline",
    "sourced_segment",
    "unavailable_segment",
]
