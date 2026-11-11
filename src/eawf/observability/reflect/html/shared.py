"""What every reflect page shares: its inputs, number formats, vocabulary and chrome.

Money is shown only where a verified rate prices it. Effective tokens express
that cost back as base input tokens at :data:`REFERENCE_RATE` dollars per
million, so sessions on different models and cache mixes compare on one scale.
"""

from __future__ import annotations

import html
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache
from importlib import resources
from typing import Final

from eawf.observability.reflect.analysis import (
    COHORT_LABELS,
    METRIC_NOUNS,
    Finding,
    HistoryAnalysis,
    Metric,
    SessionAnalysis,
    Severity,
)
from eawf.observability.reflect.generations import SessionContext
from eawf.observability.reflect.rates import RateRow
from eawf.observability.telemetry.models import ObservedRequest
from eawf.observability.telemetry.sources.session_history import SessionDetail

#: USD per million base input tokens that effective tokens are expressed at.
REFERENCE_RATE: Final = Decimal(5)

#: The overview page's file name.
OVERVIEW_FILENAME: Final = "index.html"

#: The session finder's file name.
FINDER_FILENAME: Final = "sessions.html"

#: What a page shows for a value nothing recorded.
DASH: Final = chr(0x2013)

#: Tokens per unit of a per-million rate.
PER_MILLION: Final = Decimal(1_000_000)

#: Advance width of one monospace glyph as a share of the font size.
GLYPH_EM: Final = 0.6

#: Compact count units, smallest first; :func:`compact` moves up while a value reaches 1000.
_SCALES: Final = ((1e3, "k"), (1e6, "M"), (1e9, "B"))

_TIMES: Final = chr(0xD7)
_ABOUT: Final = chr(0x2248)

#: Severity to its CSS suffix and its reader word.
SEVERITY: Final[dict[Severity, tuple[str, str]]] = {
    Severity.CONFIRMED_ISSUE: ("issue", "Issue"),
    Severity.INVESTIGATE: ("inv", "Investigate"),
    Severity.ANOMALY: ("anom", "Unusual"),
    Severity.NOTICE: ("notice", "Context"),
}

# Reader language for every code a page can show; anything else falls back to
# the code with its underscores spaced.
_VOCABULARY: Final[dict[str, str]] = {
    "cache_write_unavailable": "Cache writes were not recorded by this harness",
    "cache_read_unavailable": "Cache reads were not recorded by this harness",
    "no_event_chronology": "This harness recorded no per-event timestamps",
    "pricing_rate_missing": "No verified price for this model, so no cost is shown",
    "compaction_churn": "Context compacted more than once in one session",
    "orchestration_scale": "Subagent work is counted in the parent session",
    "minimal_input": "Session opened on a minimal input",
    "baseline_cold_start": "Baseline too small",
    "baseline_chronology_unavailable": "No start time to compare against",
    "impossible_token_totals": "Token totals do not add up",
    "timestamp_regression": "Request timestamps run backwards",
    "automated_review": "automated review pass",
    "no_operator_work": "no operator work",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class PageSources:
    """What one set of pages is drawn from.

    Attributes:
        analysis: The history analysis.
        session_refs: The sessions the pages cover; the rest of the analysis
            is left out of every page.
        requests: Request rows; those of the covered sessions are read.
        details: Session detail by ref, for the sessions read in full.
        contexts: Context generations by ref, for the same sessions.
        history_files: History files found on the machine.
        scope_label: What the covered set is called, such as a project code.
    """

    analysis: HistoryAnalysis
    session_refs: frozenset[str]
    requests: Sequence[ObservedRequest]
    details: Mapping[str, SessionDetail]
    contexts: Mapping[str, SessionContext]
    history_files: int
    scope_label: str


@dataclass(frozen=True, slots=True)
class Scope:
    """The covered sessions and everything every page derives from them once.

    Attributes:
        sources: What the pages are drawn from.
        rows: The covered sessions, oldest first.
        requests: Their request rows by session ref.
        rates: The rate table over those requests.
        must_see: The flagged sessions the overview promotes, dearest first.
        flagged: Every listed session with a finding above a notice.
        median_tokens: The covered sessions' median of tokens handled.
    """

    sources: PageSources
    rows: tuple[SessionAnalysis, ...]
    requests: Mapping[str, tuple[ObservedRequest, ...]]
    rates: tuple[RateRow, ...]
    must_see: tuple[SessionAnalysis, ...]
    flagged: tuple[SessionAnalysis, ...]
    median_tokens: float


# ---- numbers --------------------------------------------------------------------------


def esc(value: object) -> str:
    """Return *value* as text safe inside an element or a quoted attribute."""
    return html.escape(str(value), quote=True)


def compact(value: float) -> str:
    """Return a count in compact form: ``950``, ``50k``, ``1.3M`` or ``10B``.

    One decimal below 100 of a unit, none from there, and never a trailing
    ``.0``; a value that rounds up to 1000 of a unit moves to the next one, so
    999,950 reads ``1M``, not ``1000k``.

    Args:
        value: The count; a negative count keeps its sign.

    Returns:
        The compact text.

    Raises:
        TypeError: When *value* is not a number.
    """
    magnitude = abs(value)
    text = f"{magnitude:.0f}"
    if float(text) >= 1000:
        for size, suffix in _SCALES:
            scaled = magnitude / size
            shown = f"{scaled:.0f}" if scaled >= 100 else f"{round(scaled, 1):g}"
            text = f"{shown}{suffix}"
            if float(shown) < 1000 or suffix == _SCALES[-1][1]:
                break
    return f"-{text}" if value < 0 and text != "0" else text


def exact(value: float) -> str:
    """Return a count with thousands separators."""
    return f"{round(value):,}"


def usd(value: Decimal | None) -> str:
    """Return dollars rounded by magnitude, or the dash for an unpriced value."""
    if value is None:
        return DASH
    if value == 0:
        return "$0"
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.4f}"


def plural(count: int, noun: str) -> str:
    """Return ``count noun`` with the noun pluralised by an ``s`` when needed."""
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def pct(part: float, whole: float) -> str:
    """Return *part* as a share of *whole* to one decimal, or the dash when *whole* is zero."""
    return f"{part / whole * 100:.1f}%" if whole else DASH


def duration(milliseconds: float | None) -> str:
    """Return a span as ``45s``, ``12min``, ``3h 05m`` or ``2d 4h``.

    Each form rounds to its smallest unit; a span that rounds up to the next
    unit takes that unit's form, so 59.6 seconds reads ``1min``.

    Args:
        milliseconds: The span, or ``None`` when nothing recorded it.

    Returns:
        The span text, or the dash for ``None``.

    Raises:
        TypeError: When *milliseconds* is neither a number nor ``None``.
    """
    if milliseconds is None:
        return DASH
    seconds = milliseconds / 1000
    if round(seconds) < 60:
        return f"{round(seconds)}s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}min"
    if minutes < 24 * 60:
        hours, rest = divmod(minutes, 60)
        return f"{hours}h {rest:02d}m"
    days, hours = divmod(round(seconds / 3600), 24)
    return f"{days}d {hours}h"


def elapsed(seconds: float) -> str:
    """Return an elapsed offset as ``m:ss``, or ``h:mm:ss`` past an hour."""
    whole = round(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def clock(moment: datetime) -> str:
    """Return the UTC clock time, without a zone suffix."""
    return moment.astimezone(UTC).strftime("%H:%M:%S")


def day(moment: datetime | None) -> str:
    """Return the UTC date as ``dd/mm/yy``, or ``undated``."""
    return "undated" if moment is None else moment.astimezone(UTC).strftime("%d/%m/%y")


def ratio(value: float, median: float) -> str:
    """Return *value* against *median*: a percentage below 5x, a multiple from there."""
    if median <= 0:
        return "no median yet"
    multiple = value / median
    if multiple >= 5:
        return f"{_ABOUT}{multiple:,.0f}{_TIMES} median"
    return f"{(multiple - 1) * 100:+.0f}% vs median"


def effective(cost: Decimal | None) -> float | None:
    """Return *cost* as base input tokens at the reference rate, or ``None`` unpriced."""
    return None if cost is None else float(cost / REFERENCE_RATE * PER_MILLION)


def say(code: str) -> str:
    """Return the reader language for a code; an unlisted code is spaced and capitalised."""
    text = _VOCABULARY.get(code)
    if text is not None:
        return text
    spaced = code.replace("_", " ").strip()
    return spaced[:1].upper() + spaced[1:]


def trim(text: str, limit: int) -> str:
    """Return *text* flattened and cut at a word boundary within *limit* characters."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[: limit - 1].rsplit(" ", 1)[0].rstrip(" -,:;@·")
    return f"{cut}…"


def absent(reason: str) -> str:
    """Return the dash for an unrecorded value, with *reason* on hover."""
    return f'<span class="unk" title="{esc(reason)}">{DASH}</span>'


# ---- chart geometry -------------------------------------------------------------------


def label_width(text: str, font_px: float) -> float:
    """Return the width in px of *text* set in the pages' monospace font.

    Args:
        text: The label.
        font_px: The font size in px.

    Returns:
        The width: every monospace glyph advances by :data:`GLYPH_EM` of the font size.

    Raises:
        TypeError: When *text* is not a sized string.
    """
    return len(text) * GLYPH_EM * font_px


def axis_side(labels: Sequence[str], font_px: float) -> int:
    """Return a chart's side margin in px: half its widest tick label plus 4px.

    Args:
        labels: The tick labels.
        font_px: Their font size in px.

    Returns:
        The margin, rounded up to a whole pixel; 4 with no labels.
    """
    widest = max((label_width(label, font_px) for label in labels), default=0.0)
    return math.ceil(widest / 2 + 4)


def axis_ticks(ticks: Sequence[tuple[float, str]]) -> str:
    """Return an axis's tick marks and labels, the end labels anchored inward.

    The first label starts at its tick (``t-first``) and the last ends at it
    (``t-last``), so neither overhangs the axis box; inner labels centre.

    Args:
        ticks: Offsets in percent of the axis width with their labels, in order.

    Returns:
        The markup; empty with no ticks.

    Raises:
        ValueError: When an offset is not a number.
    """
    last = len(ticks) - 1
    marks = []
    for index, (at, label) in enumerate(ticks):
        end = ' class="t-first"' if index == 0 else ' class="t-last"' if index == last else ""
        marks.append(
            f'<i class="mj" style="left:{at:.2f}%"></i><b{end} style="left:{at:.2f}%">'
            f"{esc(label)}</b>"
        )
    return "".join(marks)


# ---- session identity -----------------------------------------------------------------


def session_key(ref: str) -> str:
    """Return the stable key a session's page is named by."""
    return ref.removeprefix("vsid-")


def page_name(ref: str) -> str:
    """Return the file name of a session's page."""
    return f"session-{session_key(ref)}.html"


def measure(row: SessionAnalysis, metric: Metric) -> float:
    """Return one of a session's measurements, zero when unobserved."""
    return row.metrics.get(metric, 0.0)


def identity(row: SessionAnalysis) -> str:
    """Return the structural line: date, project, model, scale and short key."""
    children = int(measure(row, Metric.CHILD_COUNT))
    scale = (
        plural(children, "subagent")
        if children
        else plural(int(measure(row, Metric.MODEL_STEPS)), "request")
    )
    key = f"#{session_key(row.session_ref)[:8]}"
    return " · ".join((day(row.started_at), row.project_label, row.model, scale, key))


def session_title(row: SessionAnalysis) -> str:
    """Return the recorded title, or a structural one when it is too short to be a handle."""
    recorded = None if row.title is None else row.title.title
    if recorded and sum(char.isalpha() for char in recorded) >= 10:
        return trim(recorded, 90)
    base = f"{row.runtime.capitalize()} session · {day(row.started_at)} · {row.model}"
    return f"{base} · “{trim(recorded, 40)}”" if recorded else base


def raised(row: SessionAnalysis) -> tuple[Finding, ...]:
    """Return the findings above a notice; they gate the must-see list and the filters."""
    return tuple(item for item in row.findings if item.severity is not Severity.NOTICE)


def spend_order(row: SessionAnalysis) -> tuple[bool, Decimal, float, str]:
    """Order sessions dearest first, unpriced after priced, then by tokens handled."""
    return (
        row.cost_usd is None,
        -(row.cost_usd or Decimal(0)),
        -measure(row, Metric.TOTAL_TOKENS),
        row.session_ref,
    )


# ---- chrome ---------------------------------------------------------------------------


@cache
def _asset(name: str) -> str:
    return (resources.files(__package__) / "assets" / name).read_text(encoding="utf-8")


def shell(title: str, nav: str, body: str, scope: Scope, *, script: bool) -> str:
    """Return a whole page: the inlined stylesheet, the header, *body* and the script.

    Args:
        title: The document title.
        nav: The header navigation markup.
        body: The main content markup.
        scope: The covered sessions, for the scope chips.
        script: Whether the page inlines the finder and zoom script.

    Returns:
        The page.
    """
    chips = (
        f'<span class="chip on">{esc(scope.sources.scope_label)}</span>'
        f'<span class="chip">{plural(len(scope.rows), "session")}</span>'
    )
    tail = f"<script>{_asset('reflect.js')}</script>" if script else ""
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{esc(title)}</title><style>{_asset('reflect.css')}</style></head><body>"
        '<a class="skip" href="#main">Skip to report</a>'
        '<header class="hd"><div class="hd-in"><div class="brand"><b>Eä</b>'
        f'<span class="slash">/</span>reflect</div><nav aria-label="Pages">{nav}</nav>'
        f'<div class="hd-r">{chips}</div></div></header>'
        f'<main class="wrap" id="main">{body}</main>{tail}</body></html>\n'
    )


def section(name: str, heading: str, body: str, *, chip: str = "") -> str:
    """Return one ``data-section`` section with its heading and an optional chip."""
    tag = f'<span class="chip">{chip}</span>' if chip else ""
    return (
        f'<section class="sec" id="{name}" data-section="{name}">'
        f'<div class="sec-h"><h2>{heading}</h2>{tag}</div>{body}</section>'
    )


def note(text: str) -> str:
    """Return one muted paragraph."""
    return f'<p class="note">{text}</p>'


def table(head: Sequence[tuple[str, bool]], rows: Sequence[Sequence[str]]) -> str:
    """Return a table; each head cell is ``(label, numeric)`` and every row matches it.

    Raises:
        ValueError: A row has a different number of cells than the head.
    """
    cells = "".join(
        f'<th class="n">{label}</th>' if numeric else f"<th>{label}</th>" for label, numeric in head
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="n">{cell}</td>' if numeric else f"<td>{cell}</td>"
            for cell, (_label, numeric) in zip(row, head, strict=True)
        )
        + "</tr>"
        for row in rows
    )
    return (
        f'<div class="tbl-scroll"><table class="rt"><thead><tr>{cells}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


# ---- findings, the one component the overview and the session page share --------------


def severity_chip(severity: Severity) -> str:
    """Return the severity mark: shape, fill and word, never colour alone."""
    css, word = SEVERITY[severity]
    return f'<span class="sv sv-{css}">{word}</span>'


def _finding_title(item: Finding) -> str:
    """Return a finding's heading in reader language."""
    if item.finding_type in _VOCABULARY:
        return _VOCABULARY[item.finding_type]
    if item.metric is not None and item.finding_type.endswith("_anomaly"):
        noun = METRIC_NOUNS[item.metric]
        return f"{noun[:1].upper()}{noun[1:]} above your own history"
    return say(item.finding_type)


def compared(item: Finding, row: SessionAnalysis | None) -> str:
    """Return how a finding was compared: its cohort, sample, deviation and baseline."""
    if item.cohort is None:
        return '<dl class="kv"><dt>Baseline</dt><dd>Raised from this session alone</dd></dl>'
    assessment = (
        None if row is None else next((a for a in row.anomalies if a.metric is item.metric), None)
    )
    deviation = (
        DASH
        if assessment is None or assessment.robust_z is None
        else f"{assessment.robust_z:.1f} robust z against a median of {compact(assessment.median)}"
    )
    return (
        f'<dl class="kv"><dt>Cohort</dt><dd>{esc(COHORT_LABELS[item.cohort.rung])}</dd>'
        f"<dt>Sample</dt><dd>{plural(item.cohort.sample_count, 'prior comparable session')}</dd>"
        f"<dt>Deviation</dt><dd>{deviation}</dd>"
        "<dt>Baseline</dt><dd>prior sessions only</dd></dl>"
    )


def finding_card(
    item: Finding, *, says: str, impact: str, how: str, chip: str = "", also: str = ""
) -> str:
    """Return one finding card.

    Args:
        item: The finding, the strongest of its group on the overview.
        says: The plain-language sentence, already escaped.
        impact: The impact line, carrying money where it is known.
        how: The "how this was compared" markup.
        chip: The session-count chip text, or empty.
        also: Markup naming where else it is referenced, or empty.

    Returns:
        The card.
    """
    css, _word = SEVERITY[item.severity]
    step = item.recommendation or "Open the sessions it names and set them against their cohort."
    count = f'<span class="chip">{chip}</span>' if chip else ""
    return (
        f'<article class="find is-{css}"><div class="find-h">{severity_chip(item.severity)}'
        f"<h3>{esc(_finding_title(item))}</h3>{count}</div>"
        f'<p class="find-say prose">{says}</p>'
        f'<div class="find-meta">Impact <b>{impact}</b></div>'
        "<details><summary>Why this might not be a problem</summary>"
        f'<dl class="kv"><dt>Guard</dt><dd>{esc(item.guard)}</dd>'
        f"<dt>Next step</dt><dd>{esc(step)}</dd></dl></details>"
        f"<details><summary>How this was compared</summary>{how}</details>{also}</article>"
    )


def sentence(text: str) -> str:
    """Return *text* escaped, capitalised and closed with a full stop."""
    return f"{esc(text[:1].upper())}{esc(text[1:])}."


__all__ = [
    "DASH",
    "FINDER_FILENAME",
    "OVERVIEW_FILENAME",
    "PER_MILLION",
    "REFERENCE_RATE",
    "SEVERITY",
    "PageSources",
    "Scope",
    "absent",
    "clock",
    "compact",
    "compared",
    "day",
    "duration",
    "effective",
    "elapsed",
    "esc",
    "exact",
    "finding_card",
    "identity",
    "measure",
    "note",
    "page_name",
    "pct",
    "plural",
    "raised",
    "ratio",
    "say",
    "section",
    "sentence",
    "session_key",
    "session_title",
    "severity_chip",
    "shell",
    "spend_order",
    "table",
    "trim",
    "usd",
]
