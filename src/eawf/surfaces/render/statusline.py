"""Powerline statusline rendering for ``eawf cc statusline``.

The orchestrator in :mod:`eawf.runtime.runtimes.claude.statusline` collects the
line-1 :class:`StatuslineSegment` records and the :class:`PowerlineBar` rows
from the modules in :mod:`eawf.runtime.runtimes.claude.statusline_modules`.
This module draws them: line 1 as filled segments joined by chained
separators, then one meter row per window on a fixed column grid, in a
:class:`Theme` or without colour.

Public surface:

- :class:`StatuslineSegment` — typed segment dataclass.
- :class:`Theme` / :func:`load_theme` — the bundled themes in
  ``templates/themes.yaml``.
- :class:`LineStyle` — the theme, glyph set and bar width one render uses.
- :class:`PowerlineBar` — one bar row: label, used fraction, pace or cache
  share, detail.
- :func:`render_powerline` — draw line 1 and the meter rows within a width.
- :func:`format_countdown` / :func:`format_context_tokens` — the texts a bar
  row shows after its pace column.
- :func:`round_half_up` — the rounding every percentage on the line uses.
- :data:`COST_LABEL` — what the session cost is labelled, since it is
  priced at list rates rather than billed.
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
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

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


#: The bundled theme file and the package it ships in.
_THEMES_FILENAME: Final = "themes.yaml"
_TEMPLATES_PACKAGE: Final = "eawf.platform.templates"

Fill = Annotated[str, StringConstraints(pattern=r"^(#[0-9a-f]{6}|4[0-7]|10[0-7])$")]
"""A background: an SGR background code (40-47, 100-107) or a lower-case ``#rrggbb``."""

Ink = Annotated[str, StringConstraints(pattern=r"^(#[0-9a-f]{6}|3[0-7]|9[0-7])$")]
"""A foreground: an SGR foreground code (30-37, 90-97) or a lower-case ``#rrggbb``."""

AutoInk = Literal["auto"]
"""The ink that picks white or black text per fill, whichever stands out on it."""

ColourTheme = Literal[
    "ansi", "catppuccin-latte", "catppuccin-frappe", "catppuccin-macchiato", "catppuccin-mocha"
]
"""A theme that paints; ``none`` draws without colour and has no :class:`Theme`."""

GlyphSet = Literal["nerd", "unicode", "ascii"]
"""The glyphs separators and bars are drawn with."""


class Theme(BaseModel):
    """One statusline theme: a colour per role.

    Meter-row text, gaps and the cell after the last separator are not
    roles: they stay on the terminal's own background in every theme. The
    pace marker is not a role either: it is drawn white or black, whichever
    stands out on the cell under it, so it never vanishes into a fill.

    Attributes:
        ink: Text on a segment fill; ``auto`` picks white or black per fill.
        model: Fill of the model segment.
        project: Fill of the project segment.
        active_time: Fill of the session segment.
        cost: Fill of the session-cost segment.
        mcp: Fill of the MCP segment, and of a segment with no fill of its own.
        scope: Fill of the Run scope segment.
        budget: Fill of the Run budget segment.
        warn: Fill of a segment whose status is ``warn``.
        alert: Fill of a segment whose status is ``degraded`` or ``failed``.
        track: The unfilled cells of a bar.
        calm: Bar fill below half used, and of a limit window on pace.
        caution: Bar fill from half to four fifths used, and of a limit
            window 4 to 10 points over pace.
        danger: Bar fill from four fifths used, and of a limit window 11 or
            more points over pace or 90% used.
        under: Bar fill of a limit window 4 or more points under pace.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ink: Ink | AutoInk
    model: Fill
    project: Fill
    active_time: Fill
    cost: Fill
    mcp: Fill
    scope: Fill
    budget: Fill
    warn: Fill
    alert: Fill
    track: Fill
    calm: Fill
    caution: Fill
    danger: Fill
    under: Fill


class _ThemeFile(BaseModel):
    """The bundled theme file: exactly the five colour themes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ansi: Theme
    catppuccin_latte: Theme = Field(alias="catppuccin-latte")
    catppuccin_frappe: Theme = Field(alias="catppuccin-frappe")
    catppuccin_macchiato: Theme = Field(alias="catppuccin-macchiato")
    catppuccin_mocha: Theme = Field(alias="catppuccin-mocha")


def load_theme(name: ColourTheme, *, ink: Ink | AutoInk | None = None) -> Theme:
    """Return the bundled theme called *name*.

    Args:
        name: The theme's name.
        ink: The segment text colour the ``ansi`` theme draws with in place of
            its own, ``auto`` included; the Catppuccin flavours keep their own ink.

    Returns:
        The theme, read from ``templates/themes.yaml``.

    Raises:
        pydantic.ValidationError: The bundled file does not hold exactly the
            five themes, or a role is not a colour of its kind; or *ink* is
            not a foreground.
    """
    raw = (files(_TEMPLATES_PACKAGE) / _THEMES_FILENAME).read_text(encoding="utf-8")
    themes = _ThemeFile.model_validate(yaml.safe_load(raw))
    theme: Theme = getattr(themes, name.replace("-", "_"))
    if name != "ansi" or ink is None:
        return theme
    return Theme.model_validate({**theme.model_dump(), "ink": ink})


#: The narrowest bar a meter row draws, and the widest one configurable.
_MIN_BAR: Final = 10
_MAX_BAR: Final = 40


@dataclass(frozen=True, slots=True, kw_only=True)
class LineStyle:
    """How one render draws: its theme, glyph set and bar width.

    Attributes:
        theme: The colours; ``None`` draws without any escape sequence.
        glyphs: The separator and bar glyphs.
        bar_width: Bar cells per meter row before any shrink to fit.

    Raises:
        ValueError: *bar_width* is outside 10 to 40.
    """

    theme: Theme | None
    glyphs: GlyphSet = "nerd"
    bar_width: int = 20

    def __post_init__(self) -> None:
        """Refuse a bar width the grid does not draw."""
        if not _MIN_BAR <= self.bar_width <= _MAX_BAR:
            raise ValueError(
                f"bar_width out of range, expected {_MIN_BAR} to {_MAX_BAR}: {self.bar_width!r}"
            )


def round_half_up(value: float) -> int:
    """Return *value* rounded to the nearest whole number, halves away from zero.

    Python's ``round`` sends halves to the even neighbour, so ``2.5`` would
    print as ``2`` on one row and ``3.5`` as ``4`` on the next.

    Args:
        value: The number to round.

    Returns:
        ``sign(value) * floor(|value| + 0.5)``.

    Raises:
        ValueError: *value* is NaN.
        OverflowError: *value* is infinite.
    """
    whole = int(abs(value) + 0.5)
    return -whole if value < 0 else whole


#: What the session cost is labelled: the host prices usage at list rates,
#: which is what the API would charge, not what a subscription bills.
COST_LABEL: Final = "API-equivalent"
#: The cost label line 1 falls back to before it drops any segment.
_COST_LABEL_NARROW: Final = "API-eq"


@dataclass(frozen=True, slots=True, kw_only=True)
class PowerlineBar:
    """One bar row under line 1: a window's used share and what qualifies it.

    Attributes:
        label: The row's name, such as ``ctx`` or ``5h``.
        ratio: The used fraction in ``[0, 1]``.
        detail: The text after the pace column: the reset countdown of a rate
            window, or the used over limit tokens of the context; ``None``
            when there is nothing to show.
        pace: Points the used share runs ahead of (positive) or behind
            (negative) an even spend across a limit window, with ``0``
            standing for on pace; ``None`` when unknown or not a limit window.
        expected: The share in percent an even spend would have used by now,
            where the bar draws its marker; ``None`` draws no marker.
        cache_share: The percent of the context's last call read from cache,
            shown in the pace column of a row with no pace; ``None`` when
            unknown.

    Raises:
        ValueError: *label* is empty, *ratio* is outside ``[0, 1]``, or
            *expected* or *cache_share* is outside ``[0, 100]``.
    """

    label: str
    ratio: float
    detail: str | None = None
    pace: int | None = None
    expected: float | None = None
    cache_share: int | None = None

    def __post_init__(self) -> None:
        """Refuse a row the renderer could not draw."""
        if not self.label:
            raise ValueError("a bar row needs a label")
        if not 0.0 <= self.ratio <= 1.0:
            raise ValueError(f"ratio out of range, expected [0, 1]: {self.ratio!r}")
        if self.expected is not None and not 0.0 <= self.expected <= 100.0:
            raise ValueError(f"expected out of range, expected [0, 100]: {self.expected!r}")
        if self.cache_share is not None and not 0 <= self.cache_share <= 100:
            raise ValueError(f"cache_share out of range, expected [0, 100]: {self.cache_share!r}")

    @property
    def percent(self) -> int:
        """Return the used share in whole percent, rounded half up."""
        return round_half_up(self.ratio * 100)


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


#: The glyph between two line-1 segments; ``ascii`` draws none, so the fills abut.
_SEPARATOR: Final[dict[GlyphSet, str]] = {"nerd": "", "unicode": "▌", "ascii": ""}
#: How segments are joined when the line is drawn without colour.
_PLAIN_SEPARATOR: Final = " | "
_RESET: Final = "\x1b[0m"
#: The terminal's own background, which gaps and meter-row text stay on.
_DEFAULT_BG: Final = "49"
_SGR: Final = re.compile(r"\x1b\[[0-9;]*m")
_ELLIPSIS: Final = "…"
#: The empty row between line 1 and the meter rows, so the bars read apart
#: from the segments above them.
_SPACER: Final = ""

#: Line-1 segments in the order they are dropped when the row is too wide;
#: model and cost are never dropped, only clipped.
_DROP_ORDER: Final = ("mcp_health", "active_time", "project", "hooks_plugins", "budget", "scope")

#: The theme fill of each line-1 module whose status makes no claim.
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

_HOOK_ERRORS: Final = re.compile(r"hooks!\d+\+?")

#: The glyphs a bar is drawn with when it has no colour: full, the eighths, track.
_BAR_FULL: Final[dict[GlyphSet, str]] = {"nerd": "█", "unicode": "█", "ascii": "#"}
_BAR_PARTIAL: Final = "▏▎▍▌▋▊▉"
_BAR_TRACK: Final[dict[GlyphSet, str]] = {"nerd": "░", "unicode": "░", "ascii": "."}
_BAR_MARKER: Final[dict[GlyphSet, str]] = {"nerd": "│", "unicode": "│", "ascii": "|"}
_CALM_BELOW: Final = 0.5
_CAUTION_BELOW: Final = 0.8
#: Pace within this many points either way prints ``~``: the host reports
#: whole percents, so a few points off an even spend is noise.
_ON_PACE: Final = 3
#: Pace from this many points over fills a limit bar with the danger colour.
_FAR_OVER: Final = 11
#: A limit window this full fills with the danger colour whatever its pace.
_LIMIT_DANGER_FROM: Final = 0.9

#: Meter-row field widths; one space separates each field from the next.
_LABEL_CELLS: Final = 3
_PERCENT_CELLS: Final = 4
_PACE_CELLS: Final = 5
#: The cells of a meter row around its bar up to where the detail starts:
#: label, percent and pace, and the four spaces between the five fields.
_GRID_CELLS: Final = _LABEL_CELLS + _PERCENT_CELLS + _PACE_CELLS + 4

#: The meter-row text attributes: bold for label and percent, dim for detail.
_BOLD: Final = "1"
_DIM: Final = "2"
_DEFAULT_FG: Final = "39"
_UNDER_FG: Final = "32"
_OVER_FG: Final = "33"
_FAR_OVER_FG: Final = "31"


def _sgr(*params: str) -> str:
    """Return the Select Graphic Rendition escape setting *params*."""
    return f"\x1b[{';'.join(params)}m"


def _rgb(colour: str) -> str:
    """Return the ``r;g;b`` parameters of a ``#rrggbb`` colour."""
    return f"{int(colour[1:3], 16)};{int(colour[3:5], 16)};{int(colour[5:7], 16)}"


def _bg(fill: str) -> str:
    """Return the SGR background parameters of *fill*."""
    return f"48;2;{_rgb(fill)}" if fill.startswith("#") else fill


def _fg(ink: str) -> str:
    """Return the SGR foreground parameters of *ink*."""
    return f"38;2;{_rgb(ink)}" if ink.startswith("#") else ink


#: The 16-colour backgrounds a terminal palette draws dark: black, red, blue,
#: magenta and bright black; text on them reads in white, on the rest in black.
_DARK_FILLS: Final = frozenset({"40", "41", "44", "45", "100"})
_WHITE: Final = "97"
_BLACK: Final = "30"
_WHITE_RGB: Final = "38;2;255;255;255"
_BLACK_RGB: Final = "38;2;0;0;0"
#: The relative luminance at which black and white stand out equally (WCAG 2.x).
_EVEN_CONTRAST: Final = 0.179


def _linear(channel: int) -> float:
    """Return the linear-light value of one sRGB channel, 0 to 255."""
    value = channel / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _contrast(background: str) -> str:
    """Return the foreground, white or black, that stands out most on *background*.

    A 16-colour fill answers in the palette's own 97 or 30, since the palette
    decides how dark it is; a 24-bit fill answers in 24-bit white or black by
    its relative luminance.
    """
    if not background.startswith("48;2;"):
        return _WHITE if background in _DARK_FILLS else _BLACK
    red, green, blue = (int(part) for part in background.split(";")[2:])
    luminance = 0.2126 * _linear(red) + 0.7152 * _linear(green) + 0.0722 * _linear(blue)
    return _BLACK_RGB if luminance > _EVEN_CONTRAST else _WHITE_RGB


def _ink(theme: Theme, background: str) -> str:
    """Return the foreground of segment text on *background* in *theme*."""
    return _contrast(background) if theme.ink == "auto" else _fg(theme.ink)


def _as_fg(background: str) -> str:
    """Return the foreground parameters naming the colour of *background*.

    ``4x`` becomes ``3x``, ``10x`` becomes ``9x`` and ``48;2;…`` becomes
    ``38;2;…``, so a separator draws in the fill it closes.
    """
    if background.startswith("48;2;"):
        return f"38{background[2:]}"
    return str(int(background) - 10)


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


def _fill(segment: StatuslineSegment, theme: Theme | None) -> str:
    """Return the SGR background parameters *segment* is drawn on; empty without colour."""
    if theme is None:
        return ""
    if segment.status in {"degraded", "failed"}:
        return _bg(theme.alert)
    if segment.status == "warn":
        return _bg(theme.warn)
    return _bg(str(getattr(theme, _FILL.get(segment.module, "mcp"))))


def _line_one(pieces: Sequence[_Piece], style: LineStyle) -> str:
    """Draw *pieces* as filled segments joined by chained separators.

    Each separator is drawn in the fill it closes on the fill it opens, the
    last one on the terminal's own background, so no gap cell is painted.
    """
    theme = style.theme
    if theme is None:
        return _PLAIN_SEPARATOR.join(piece.text for piece in pieces)
    separator = _SEPARATOR[style.glyphs]
    drawn = ""
    for index, piece in enumerate(pieces):
        drawn += f"{_sgr(_ink(theme, piece.fill), piece.fill)} {piece.text} "
        if separator:
            following = pieces[index + 1].fill if index + 1 < len(pieces) else _DEFAULT_BG
            drawn += f"{_sgr(_as_fg(piece.fill), following)}{separator}"
    return f"{drawn}{_RESET}"


def _narrow(piece: _Piece) -> _Piece:
    """Return *piece* with the short cost label when it is the cost segment."""
    long_label = f" {COST_LABEL}"
    if piece.module != "cost" or not piece.text.endswith(long_label):
        return piece
    text = f"{piece.text.removesuffix(long_label)} {_COST_LABEL_NARROW}"
    return _Piece(piece.module, piece.fill, text)


def _fit_line_one(pieces: list[_Piece], style: LineStyle, width: int) -> list[_Piece]:
    """Return the pieces that fit *width*: narrow the cost, drop by priority, clip the first."""
    kept = list(pieces)
    if _cells(_line_one(kept, style)) > width:
        kept = [_narrow(piece) for piece in kept]
    for module in _DROP_ORDER:
        if _cells(_line_one(kept, style)) <= width:
            return kept
        kept = [piece for piece in kept if piece.module != module]
    while kept and (excess := _cells(_line_one(kept, style)) - width) > 0:
        head = kept[0]
        room = _cells(head.text) - excess
        if room >= 2:
            kept[0] = _Piece(head.module, head.fill, _clip(head.text, room))
        else:
            kept.pop(0)
    return kept


def _bar_fill(bar: PowerlineBar, theme: Theme) -> str:
    """Return the colour a bar fills with: by pace for a limit window, else by used share.

    A row without a pace, the context or a window whose reset is unknown,
    fills by how full it is.
    """
    if bar.pace is None:
        if bar.ratio < _CALM_BELOW:
            return theme.calm
        return theme.caution if bar.ratio < _CAUTION_BELOW else theme.danger
    if bar.ratio >= _LIMIT_DANGER_FROM or bar.pace >= _FAR_OVER:
        return theme.danger
    if bar.pace > _ON_PACE:
        return theme.caution
    return theme.under if bar.pace < -_ON_PACE else theme.calm


def _bar(bar: PowerlineBar, cells: int, style: LineStyle) -> str:
    """Draw *bar* *cells* wide in eighth-cell units, with its pace marker.

    In colour the cells take a background fill: full cells, then one eighth
    glyph drawn in the fill on the track, then track. ``ascii`` has no
    eighth glyph, so its partial cell is left as track. The marker replaces
    the glyph of the cell at the expected share, keeps that cell's own
    background and is drawn white or black, whichever stands out on it.
    """
    units = int(bar.ratio * cells * 8 + 0.5)
    full, eighths = divmod(units, 8)
    partial = _BAR_PARTIAL[eighths - 1] if eighths and style.glyphs != "ascii" else ""
    rest = cells - full - len(partial)
    marker = min(int(bar.expected * cells / 100), cells - 1) if bar.expected is not None else None
    theme = style.theme
    if theme is None:
        glyphs = [_BAR_FULL[style.glyphs]] * full + [partial] * len(partial)
        glyphs += [_BAR_TRACK[style.glyphs]] * rest
        if marker is not None:
            glyphs[marker] = _BAR_MARKER[style.glyphs]
        return "".join(glyphs)
    fill = _bg(_bar_fill(bar, theme))
    track = _bg(theme.track)
    runs = [(fill, " ")] * full + [(f"{_as_fg(fill)};{track}", partial)] * len(partial)
    runs += [(track, " ")] * rest
    if marker is not None:
        own = fill if marker < full else track
        runs[marker] = (f"{_contrast(own)};{own}", _BAR_MARKER[style.glyphs])
    drawn = ""
    previous = None
    for params, glyph in runs:
        if params != previous:
            drawn += _sgr(params)
            previous = params
        drawn += glyph
    return f"{drawn}{_RESET}"


def _text(text: str, style: LineStyle, *params: str) -> str:
    """Draw meter-row *text* with *params* on the terminal's own background."""
    if style.theme is None:
        return text
    return f"{_sgr(*params, _DEFAULT_BG)}{text}{_RESET}"


def _pace_column(bar: PowerlineBar) -> tuple[str, tuple[str, ...]] | None:
    """Return the pace column's text and SGR parameters, or ``None`` when blank.

    A limit window prints ``+N%`` over pace, ``-N%`` under it and ``~`` within
    three points; a row without a pace prints its cache-read share.
    """
    if bar.pace is not None:
        if bar.pace > _ON_PACE:
            return f"+{bar.pace}%", (_FAR_OVER_FG if bar.pace >= _FAR_OVER else _OVER_FG,)
        if bar.pace < -_ON_PACE:
            return f"{bar.pace}%", (_UNDER_FG,)
        return "~", (_DIM, _DEFAULT_FG)
    if bar.cache_share is not None:
        return f"{bar.cache_share}%c", (_DIM, _DEFAULT_FG)
    return None


def _meter_row(
    bar: PowerlineBar, style: LineStyle, *, bar_cells: int, pace: bool, detail: bool
) -> str:
    """Draw one meter row on the grid: label, bar, percent, pace or cache, detail.

    A blank inner field keeps its cells so the next field stays in its
    column; a blank trailing field is not drawn.
    """
    fields = [
        _text(bar.label.ljust(_LABEL_CELLS), style, _BOLD, _DEFAULT_FG),
        _bar(bar, bar_cells, style),
        _text(f"{bar.percent:>3}%", style, _BOLD, _DEFAULT_FG),
    ]
    shown_detail = bar.detail if detail else None
    column = _pace_column(bar) if pace else None
    if column is not None:
        text, params = column
        fields.append(_text(text.rjust(_PACE_CELLS), style, *params))
    elif pace and shown_detail:
        fields.append(" " * _PACE_CELLS)
    if shown_detail:
        fields.append(_text(shown_detail, style, _DIM, _DEFAULT_FG))
    return " ".join(fields)


def _meter_grid(
    bars: Sequence[PowerlineBar], bar_width: int, width: int | None
) -> tuple[int, bool, bool] | None:
    """Return the bar cells and whether pace and detail print, or ``None`` for no row.

    Without a width nothing shrinks. With one, the bar shrinks to fit the
    widest detail; below the narrowest bar the detail drops on every row,
    then the pace column, and then no meter row prints.
    """
    if width is None:
        return bar_width, True, True
    widest = max((_cells(bar.detail) for bar in bars if bar.detail), default=0)
    forms = (
        (width - _GRID_CELLS - widest, True, True),
        (width - _GRID_CELLS + 1, True, False),
        (width - _LABEL_CELLS - _PERCENT_CELLS - 2, False, False),
    )
    for room, pace, detail in forms:
        cells = min(bar_width, room)
        if cells >= _MIN_BAR:
            return cells, pace, detail
    return None


def render_powerline(
    segments: Sequence[StatuslineSegment],
    bars: Sequence[PowerlineBar],
    style: LineStyle,
    *,
    width: int | None,
) -> list[str]:
    """Draw line 1 from *segments* and one meter row per bar, in *style*.

    Line 1 shows each segment with a sourced value, in the order given; a
    segment with none is omitted, never drawn as a marker. One blank row
    separates line 1 from the meter rows, and only when both are drawn.
    Meter rows share one column grid, so each detail starts at the same
    offset.

    Args:
        segments: The line-1 segments, in display order.
        bars: The meter rows, in display order.
        style: The theme, glyphs and bar width to draw with.
        width: The terminal width in cells, or ``None`` to draw every row
            whole. Line 1 drops MCP, then active time, then project, then
            hooks, budget and scope, and clips the model last; the meter
            rows shrink their bar, then drop the detail, then the pace
            column, and print no row below the narrowest bar, so no row
            ever wraps.

    Returns:
        The rows, top to bottom; empty when nothing could be sourced.
    """
    pieces = [
        _Piece(segment.module, _fill(segment, style.theme), text)
        for segment in segments
        if (text := _display(segment)) is not None
    ]
    if width is not None:
        pieces = _fit_line_one(pieces, style, width)
    rows = [_line_one(pieces, style)] if pieces else []
    grid = _meter_grid(bars, style.bar_width, width)
    if grid is not None and bars:
        bar_cells, pace, detail = grid
        if rows:
            rows.append(_SPACER)
        rows.extend(
            _meter_row(bar, style, bar_cells=bar_cells, pace=pace, detail=detail) for bar in bars
        )
    if style.glyphs == "ascii":
        return [_ascii_only(row) for row in rows]
    return rows


#: Fixed spellings of the non-ASCII characters the segments and the clip emit.
_ASCII_SPELLING: Final = ((" · ", " "), ("·", "-"), (_ELLIPSIS, "~"))
_UNKNOWN_CHAR: Final = "?"


def _ascii_only(row: str) -> str:
    """Return *row* in ASCII alone, for a terminal that may show nothing else.

    The ``ascii`` glyph set is chosen for a C locale or a Linux console,
    where any other byte shows as mojibake. Known characters get a fixed
    spelling, accented letters lose their marks (``ä`` reads ``a``) and any
    other character becomes ``?``. Each replacement is at most as wide as the
    character it replaces, so a row fitted to the width still fits.
    """
    for char, spelling in _ASCII_SPELLING:
        row = row.replace(char, spelling)
    if row.isascii():
        return row
    return "".join(_ascii_char(char) for char in row)


def _ascii_char(char: str) -> str:
    """Return *char* in ASCII: itself, its base letter, ``?``, or nothing for a mark."""
    if char.isascii():
        return char
    if unicodedata.combining(char):
        return ""
    base = unicodedata.normalize("NFD", char)[0]
    return base if base.isascii() else _UNKNOWN_CHAR


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
    "COST_LABEL",
    "SEGMENT_CONTRACT_REVISION",
    "UNAVAILABLE_MARK",
    "ColourTheme",
    "GlyphSet",
    "LineStyle",
    "PowerlineBar",
    "SegmentSource",
    "SegmentStatus",
    "StatuslineSegment",
    "Theme",
    "budget_segment",
    "budget_unavailable_segment",
    "format_context_tokens",
    "format_countdown",
    "load_theme",
    "render_powerline",
    "round_half_up",
    "sourced_segment",
    "unavailable_segment",
]
