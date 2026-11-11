"""One session's page: timeline, compaction Sankey, context generations and requests.

The page keeps its size budget by binning, never by dropping: a timeline lane
draws at most :data:`LANE_BARS` bars, each the activity that filled most of its
slot, and a context strip at most :data:`STRIP_SEGMENTS` segments, adjacent
small blocks binned together and marked as binned.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from eawf.observability.reflect.analysis import (
    METRIC_NOUNS,
    UNKNOWN,
    Metric,
    SessionAnalysis,
    SessionClass,
)
from eawf.observability.reflect.generations import (
    ContextGeneration,
    GenerationBlock,
    GenerationSummary,
    SessionContext,
)
from eawf.observability.reflect.html.shared import (
    DASH,
    FINDER_FILENAME,
    OVERVIEW_FILENAME,
    PER_MILLION,
    Scope,
    absent,
    axis_side,
    axis_ticks,
    clock,
    compact,
    compared,
    day,
    duration,
    effective,
    elapsed,
    esc,
    exact,
    finding_card,
    label_width,
    measure,
    note,
    pct,
    plural,
    say,
    section,
    sentence,
    session_key,
    session_title,
    shell,
    table,
    trim,
    usd,
)
from eawf.observability.reflect.rates import NON_MODELS, UNKNOWN_MODEL, PricingQuality
from eawf.observability.telemetry.models import DurationKind, ObservedRequest
from eawf.observability.telemetry.sources.session_history import SessionDetail, ToolSpan

#: Bars one timeline lane draws at most.
LANE_BARS: Final = 40

#: Segments one context strip draws at most.
STRIP_SEGMENTS: Final = 60

_SUBAGENT_LANES: Final = 24
_SANKEY_LIMIT: Final = 6
_GENERATION_LIMIT: Final = 12
_LEDGER_ROWS: Final = 10
_MODEL_GAP_SECONDS: Final = 300.0
_RULER_STEPS: Final = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)
_RULER_STEPS_LONG: Final = (10800, 21600, 43200, 86400, 172800, 604800)
_RULER_UNITS: Final = ((86400, "d"), (3600, "h"), (60, "min"), (1, "s"))
_TOKEN_UNITS: Final = ((10**9, "B"), (10**6, "M"), (10**3, "k"), (1, ""))
_ARROW: Final = "→"
# The Sankey label size in reflect.css (.wf svg text), which its gutter and label spacing hold.
_SANKEY_FONT_PX: Final = 10.5
# Kinds under this share of a generation fold into one "Other (n)" row.
_SANKEY_FOLD: Final = 0.02
# The ruler and token axis label size in reflect.css, which their side margins hold.
_AXIS_FONT_PX: Final = 10.0
# Track percent past which a compaction chip ends at its line instead of starting there.
_MARK_FLIP: Final = 85.0
_CRUMB: Final = '<span class="deco">&rsaquo;</span>'

_KINDS: Final[dict[str, tuple[str, str]]] = {
    "user": ("k-user", "Your messages"),
    "assistant": ("k-asst", "Model replies"),
    "assistant_reasoning": ("k-reason", "Reasoning"),
    "tool_call": ("k-call", "Tool calls"),
    "tool_result": ("k-tool", "Tool results"),
    "system": ("k-sys", "System prompt"),
    "runtime_injection": ("k-inj", "Runtime injections"),
    "project_instruction": ("k-proj", "Project instructions"),
    "tool_definition": ("k-def", "Tool definitions"),
    "compaction_summary": ("k-compact", "Compacted history"),
    "unknown": ("k-other", "Unclassified"),
}

_ACTIVITIES: Final[dict[str, tuple[str, str]]] = {
    "think": ("Thinking", "Model reasoning before it acted, billed as output tokens"),
    "write": ("Writing", "Model producing a message or a patch"),
    "tool": ("Tool call", "Shell, search or file read running outside the model"),
    "agent": ("Delegated work", "A subagent working on what it was asked to do"),
    "idle": ("Idle", "No recorded activity in this lane"),
}
_ACTIVE: Final = ("tool", "agent", "think", "write")


def _kind(name: str) -> tuple[str, str]:
    return _KINDS.get(name, _KINDS["unknown"])


# ---- timeline -------------------------------------------------------------------------


@dataclass(slots=True)
class _Lane:
    label: str
    intervals: list[tuple[datetime, datetime, str]] = field(default_factory=list)


def _closed(spans: Sequence[ToolSpan], activity: str) -> list[tuple[datetime, datetime, str]]:
    return [
        (span.started_at, span.ended_at, activity)
        for span in spans
        if span.started_at is not None and span.ended_at is not None
    ]


def _main_lane(detail: SessionDetail) -> _Lane:
    """Model steps fill the gap since the step before; tool spans run on top of them."""
    main = _Lane("<b>Main session</b>")
    previous: datetime | None = None
    for step in detail.steps:
        if step.ts is None:
            continue
        gap = None if previous is None else (step.ts - previous).total_seconds()
        if previous is not None and gap is not None and 0 <= gap <= _MODEL_GAP_SECONDS:
            activity = "think" if step.reasoning_tokens else "write"
            main.intervals.append((previous, step.ts, activity))
        previous = step.ts
    main.intervals.extend(_closed(detail.thinking_spans, "think"))
    for kind in (DurationKind.TOOL_CALL, DurationKind.COMMAND):
        main.intervals.extend(_closed(detail.tool_spans.get(kind, ()), "tool"))
    return main


def _lanes(detail: SessionDetail) -> list[_Lane]:
    named = sorted(
        (
            (span.started_at, span.ended_at, span.label)
            for span in detail.tool_spans.get(DurationKind.SUBAGENT, ())
            if span.started_at is not None and span.ended_at is not None
        ),
        key=lambda item: item[0],
    )
    own = named if len(named) <= _SUBAGENT_LANES else named[: _SUBAGENT_LANES - 1]
    lanes = [_main_lane(detail)]
    for begin, end, label in own:
        name = "Subagent" if label == "other" else f"{label} subagent"
        lanes.append(_Lane(f"\u21b3 {esc(name)}", [(begin, end, "agent")]))
    rest = [(begin, end, "agent") for begin, end, _label in named[len(own) :]]
    if rest:
        lanes.append(_Lane(f"\u21b3 {plural(len(rest), 'more subagent')}", rest))
    return lanes


def _slot_activities(lane: _Lane, start: datetime, window: float) -> list[str]:
    """Return each slot's activity: the active activity that covered most of it, else idle."""
    width = window / LANE_BARS
    cover: list[dict[str, float]] = [defaultdict(float) for _ in range(LANE_BARS)]
    for begin, end, activity in lane.intervals:
        low = (begin - start).total_seconds()
        high = max((end - start).total_seconds(), low + width * 0.01)
        first = max(0, int(low // width))
        for index in range(first, min(LANE_BARS - 1, int(high // width)) + 1):
            overlap = min(high, (index + 1) * width) - max(low, index * width)
            if overlap > 0:
                cover[index][activity] += overlap
    return [
        max(_ACTIVE, key=lambda name: (slot.get(name, 0.0), -_ACTIVE.index(name)))
        if slot
        else "idle"
        for slot in cover
    ]


def _bars(activities: Sequence[str]) -> list[tuple[int, int, str]]:
    bars: list[tuple[int, int, str]] = []
    for index, activity in enumerate(activities):
        if bars and bars[-1][2] == activity:
            bars[-1] = (bars[-1][0], index, activity)
        else:
            bars.append((index, index, activity))
    return bars


def _ruler(window: float) -> tuple[int, list[tuple[float, str]]]:
    """Return the ruler step and its ticks, every label in the one unit the step divides."""
    step = next(
        (step for step in (*_RULER_STEPS, *_RULER_STEPS_LONG) if window / step <= 8),
        _RULER_STEPS_LONG[-1],
    )
    unit, suffix = next((size, name) for size, name in _RULER_UNITS if step % size == 0)
    ticks = [(at / window * 100, f"{at // unit}{suffix}") for at in range(0, int(window) + 1, step)]
    return step, ticks


def _covered(intervals: Iterable[tuple[datetime, datetime]]) -> float:
    """Return the seconds the union of *intervals* covers, an overlap counted once."""
    total, reach = 0.0, None
    for begin, end in sorted(intervals):
        if end <= begin or (reach is not None and end <= reach):
            continue
        total += (end - (begin if reach is None or begin > reach else reach)).total_seconds()
        reach = end
    return total


def _totals(lanes: Sequence[_Lane], window: float) -> tuple[dict[str, float], float]:
    """Return seconds per activity from the lanes' real intervals, and the main lane's busy time.

    The slots only draw: an interval shorter than a slot still counts its own
    length, and overlapping intervals of one activity in one lane count once.
    Idle is the main lane's wall-clock its intervals leave uncovered.
    """
    totals: dict[str, float] = {}
    for lane in lanes:
        for activity in _ACTIVE:
            spent = _covered(
                (begin, end) for begin, end, name in lane.intervals if name == activity
            )
            if spent:
                totals[activity] = totals.get(activity, 0.0) + spent
    busy = _covered((begin, end) for begin, end, _name in lanes[0].intervals)
    if window > busy:
        totals["idle"] = window - busy
    return totals, busy


def _bar(first: int, last: int, activity: str, window: float) -> str:
    seconds = (last - first + 1) * window / LANE_BARS
    begin = first * window / LANE_BARS
    word, meaning = _ACTIVITIES[activity]
    tip = (
        f"{word} · {elapsed(begin)} {_ARROW} {elapsed(begin + seconds)} · "
        f"{duration(seconds * 1000)}. {meaning}."
    )
    return (
        f'<span class="tl-bar ph-{activity}" tabindex="0" '
        f'style="left:{first / LANE_BARS * 100:.2f}%;'
        f'width:{(last - first + 1) / LANE_BARS * 100:.2f}%" title="{esc(tip)}"></span>'
    )


def _compaction_marks(detail: SessionDetail, start: datetime, window: float) -> list[str]:
    marks = []
    for cut in detail.compactions:
        if cut.ts is None:
            continue
        before = "" if cut.pre_tokens is None else f", {exact(cut.pre_tokens)} tokens before"
        tip = f"Compaction at {clock(cut.ts)} UTC, {cut.trigger or 'trigger not recorded'}{before}"
        left = (cut.ts - start).total_seconds() / window * 100
        flip = " flip" if left > _MARK_FLIP else ""
        marks.append(
            f'<span class="tl-mark{flip}" style="--x:{left:.2f}%" title="{esc(tip)}">'
            "<b>compaction</b></span>"
        )
    return marks


def _timeline(detail: SessionDetail | None) -> str:
    if detail is None:
        return section(
            "timeline",
            "Timeline",
            note(
                "Per-event detail was not read for this session; only the covered"
                " project's sessions are read in full."
            ),
        )
    lanes = _lanes(detail)
    moments = [moment for lane in lanes for interval in lane.intervals for moment in interval[:2]]
    moments += [cut.ts for cut in detail.compactions if cut.ts is not None]
    if not moments:
        return section("timeline", "Timeline", note(f"{say('no_event_chronology')}."))
    start, end = min(moments), max(moments)
    window = max((end - start).total_seconds(), 1.0)
    step, ticks = _ruler(window)
    side = axis_side([label for _at, label in ticks], _AXIS_FONT_PX)
    totals, busy = _totals(lanes, window)
    rows = []
    for index, lane in enumerate(lanes):
        bars = [
            _bar(first, last, activity, window)
            for first, last, activity in _bars(_slot_activities(lane, start, window))
        ]
        marks = _compaction_marks(detail, start, window) if index == 0 else []
        rows.append(
            f'<div class="tl-row"><div class="tl-lane">{lane.label}</div>'
            f'<div class="tl-track{" marked" if marks else ""}" '
            f'style="--gridw:{step / window * 100:.2f}%">{"".join(bars + marks)}</div></div>'
        )
    legend = "".join(
        f'<span title="{esc(meaning)}"><i class="ph-{activity}"></i>{word} '
        f"{duration(totals.get(activity, 0.0) * 1000)}</span>"
        for activity, (word, meaning) in _ACTIVITIES.items()
    )
    footer = "".join(
        f"<span>{_ACTIVITIES[activity][0]} <b>{duration(seconds * 1000)}</b></span>"
        for activity, seconds in sorted(totals.items())
    )
    cuts = ", ".join(clock(cut.ts) for cut in detail.compactions if cut.ts is not None)
    if cuts:
        footer += f"<span>compaction at <b>{cuts}</b> UTC</span>"
    # A span that crosses midnight names both dates, or its clock times read backwards.
    dated = start.astimezone(UTC).date() != end.astimezone(UTC).date()
    span = (
        f"{start.astimezone(UTC):%Y-%m-%d} {clock(start)} {_ARROW} "
        f"{end.astimezone(UTC):%Y-%m-%d} {clock(end)}"
        if dated
        else f"{clock(start)} {_ARROW} {clock(end)}"
    )
    body = (
        f'<div class="tl" data-chart="timeline" style="--side:{side}px"><div class="tl-head">'
        f"<b>{plural(len(lanes), 'lane')}</b>"
        f"<span>{span} UTC</span>"
        f"<span>main lane busy {pct(busy, window)} of wall-clock</span></div>"
        f'<div class="tl-legend">{legend}</div>'
        '<div class="tl-ruler"><div class="sp">elapsed</div>'
        f'<div class="rl">{axis_ticks(ticks)}</div></div>'
        f'{"".join(rows)}<div class="tl-foot">{footer}</div></div>'
    )
    return section("timeline", "Timeline", body)


# ---- compaction Sankey ----------------------------------------------------------------


def _ribbon(x0: float, y0: float, x1: float, y1: float, height: float) -> str:
    mid = (x0 + x1) / 2
    return (
        f"M{x0:.1f},{y0:.1f}C{mid:.1f},{y0:.1f} {mid:.1f},{y1:.1f} {x1:.1f},{y1:.1f}"
        f"L{x1:.1f},{y1 + height:.1f}C{mid:.1f},{y1 + height:.1f} {mid:.1f},{y0 + height:.1f}"
        f" {x0:.1f},{y0 + height:.1f}Z"
    )


@dataclass(frozen=True, slots=True)
class _Flow:
    """One Sankey's geometry: every width is tokens at one shared scale.

    ``left`` is the label gutter; the gate and the next generation's node sit
    at fixed distances right of it.
    """

    total: int
    carried: int
    scale: float
    left: float
    node: float = 12.0
    gap: float = 4.0

    @property
    def gate(self) -> float:
        return self.left + 260.0

    @property
    def right(self) -> float:
        return self.left + 530.0

    @property
    def share(self) -> float:
        return self.carried / self.total

    @property
    def dropped_top(self) -> float:
        return self.carried * self.scale + 14.0


@dataclass(frozen=True, slots=True)
class _SankeyRow:
    """One left-hand node: a kind, or the kinds under the fold share together."""

    css: str
    word: str
    value: int
    tip: str

    @property
    def label(self) -> str:
        return f"{self.word} {compact(self.value)}"


def _sankey_rows(kinds: Sequence[tuple[str, int]], total: int) -> list[_SankeyRow]:
    """Return the left-hand nodes: each kind at or over the fold share, then ``Other (n)``."""
    rows = [
        _SankeyRow(*_kind(kind), value, _kind(kind)[1])
        for kind, value in kinds
        if value >= total * _SANKEY_FOLD
    ]
    folded = [(kind, value) for kind, value in kinds if value < total * _SANKEY_FOLD]
    if folded:
        word = f"Other ({len(folded)})"
        names = ", ".join(_kind(kind)[1] for kind, _value in folded)
        value = sum(value for _kind_name, value in folded)
        rows.append(_SankeyRow("k-other", word, value, f"{word}: {names}"))
    return rows


def _relax(wanted: Sequence[float], gap: float, floor: float) -> list[float]:
    """Return label baselines in order, each at least *gap* below the one before.

    A label keeps its wanted baseline unless that would crowd the label above
    it, or sit above *floor*; then it moves down just far enough.
    """
    placed: list[float] = []
    for y in wanted:
        placed.append(max(y, placed[-1] + gap if placed else floor))
    return placed


def _row_labels(flow: _Flow, centres: Sequence[tuple[float, str]]) -> tuple[str, float]:
    """Return the left labels by their nodes' centres, with a leader line to each moved one.

    Returns:
        The markup and the lowest baseline, which the viewBox must reach.
    """
    ordered = sorted(centres, key=lambda pair: pair[0])
    wanted = [centre + 4 for centre, _text in ordered]
    placed = _relax(wanted, 1.2 * _SANKEY_FONT_PX, _SANKEY_FONT_PX)
    marks = []
    for (centre, text), want, y in zip(ordered, wanted, placed, strict=True):
        if y - want > 0.5:
            marks.append(
                f'<line class="lead" x1="{flow.left - 5:.1f}" y1="{y - 3.5:.1f}" '
                f'x2="{flow.left:.1f}" y2="{centre:.1f}"></line>'
            )
        marks.append(
            f'<text x="{flow.left - 6:.1f}" y="{y:.1f}" text-anchor="end">{esc(text)}</text>'
        )
    return "".join(marks), placed[-1] if placed else 0.0


def _kind_flows(flow: _Flow, rows: Sequence[_SankeyRow], index: int) -> tuple[str, float, float]:
    """Return the left nodes, their ribbons and labels, the nodes' bottom and the last label."""
    shapes, centres, y, kept_y = [], [], 0.0, 0.0
    dropped_y = flow.dropped_top
    for row in rows:
        css, word, value = row.css, row.word, row.value
        tall = value * flow.scale
        keep = tall * flow.share
        shapes.append(
            f'<rect class="{css}" x="{flow.left:.1f}" y="{y:.1f}" width="{flow.node}" '
            f'height="{max(tall, 0.5):.1f}"><title>{esc(row.tip)}: {exact(value)} tokens, '
            f"{pct(value, flow.total)} of generation {index}</title></rect>"
        )
        centres.append((y + tall / 2, row.label))
        start = flow.left + flow.node
        if keep > 0:
            shapes.append(
                f'<path class="flow {css}" d="{_ribbon(start, y, flow.gate, kept_y, keep)}">'
                f"<title>{esc(word)}: {compact(value * flow.share)} carried</title></path>"
            )
        if tall - keep > 0:
            path = _ribbon(start, y + keep, flow.gate, dropped_y, tall - keep)
            shapes.append(
                f'<path class="flow {css}" d="{path}"><title>{esc(word)}: '
                f"{compact(value * (1 - flow.share))} dropped</title></path>"
            )
        kept_y += keep
        dropped_y += tall - keep
        y += tall + flow.gap
    labels, last = _row_labels(flow, centres)
    return "".join(shapes) + labels, y, last


def _gates(flow: _Flow, after: int) -> str:
    dropped = flow.total - flow.carried
    high = flow.carried * flow.scale
    low = dropped * flow.scale
    text_x = flow.gate + flow.node + 6
    carried_label = (
        f"carried {compact(flow.carried)} ({pct(flow.carried, flow.total)})"
        if flow.carried
        else "summary not found"
    )
    markup = (
        f'<rect class="gate-carried" x="{flow.gate:.1f}" y="0" width="{flow.node}" '
        f'height="{max(high, 0.5):.1f}"><title>Carried into generation {after}: '
        f"{exact(flow.carried)} tokens, {pct(flow.carried, flow.total)}</title></rect>"
        f'<rect class="gate-dropped" x="{flow.gate:.1f}" y="{flow.dropped_top:.1f}" '
        f'width="{flow.node}" height="{max(low, 0.5):.1f}"><title>Dropped at compaction: '
        f"{exact(dropped)} tokens, {pct(dropped, flow.total)}</title></rect>"
        f'<text x="{text_x:.1f}" y="{max(high / 2, 6) + 4:.1f}">{carried_label}</text>'
        f'<text x="{text_x:.1f}" y="{flow.dropped_top + low / 2 + 4:.1f}">dropped '
        f"{compact(dropped)}</text>"
    )
    if flow.carried:
        band = _ribbon(flow.gate + flow.node, 0, flow.right, 0, high)
        markup += (
            f'<path class="flow k-compact" d="{band}">'
            f"<title>{compact(flow.carried)} compacted history</title></path>"
            f'<rect class="k-compact" x="{flow.right:.1f}" y="0" width="{flow.node}" '
            f'height="{high:.1f}"><title>Generation {after} compacted history: '
            f"{exact(flow.carried)} tokens</title></rect>"
            f'<text x="{flow.right - 4:.1f}" y="{max(high / 2, 6) + 18:.1f}" text-anchor="end">'
            f"generation {after}</text>"
        )
    return markup


def _sankey(before: GenerationSummary, after: GenerationSummary, rate: Decimal | None) -> str:
    kinds = sorted(
        ((kind, value) for kind, value in before.tokens_by_kind.items() if value > 0),
        key=lambda pair: -pair[1],
    )
    total = sum(value for _kind, value in kinds)
    head = f"Generation {before.index} {_ARROW} generation {after.index}"
    if not total:
        return (
            f'<div class="wf"><div class="wf-h"><h3>{head}</h3>'
            "<span>no sized blocks before this compaction</span></div></div>"
        )
    carried = min(after.tokens_by_kind.get("compaction_summary", 0), total)
    rows = _sankey_rows(kinds, total)
    flow = _Flow(
        total=total,
        carried=carried,
        scale=(220.0 - 4.0 * (len(rows) - 1)) / total,
        left=max(label_width(row.label, _SANKEY_FONT_PX) for row in rows) + 12,
    )
    shapes, bottom, last_label = _kind_flows(flow, rows, before.index)
    # The viewBox reaches the last relaxed label, not only the last node.
    tall = max(bottom, flow.dropped_top + (total - carried) * flow.scale, last_label) + 8
    price = (
        ""
        if rate is None or not carried
        else f" · re-sending the carried band costs {usd(carried * rate / PER_MILLION)}"
        " at base input rates"
    )
    # With no summary read there is no next generation to draw, so the
    # right half collapses to the gates and their labels.
    wide = flow.right + flow.node + 4 if carried else flow.gate + flow.node + 130
    sub = (
        f"{compact(carried)} carried, {compact(total - carried)} dropped"
        if carried
        else f"summary not found, {compact(total)} dropped"
    )
    return (
        f'<div class="wf" data-chart="sankey"><div class="wf-h"><h3>{head}</h3><span>{sub}, '
        f"one scale on both sides{price}</span></div>"
        f'<div class="wf-vp"><svg width="{wide:.0f}" height="{tall:.0f}" '
        f'viewBox="0 0 {wide:.0f} {tall:.0f}" role="img" '
        f'aria-label="{esc(head)}">{shapes}{_gates(flow, after.index)}</svg></div></div>'
    )


def _compaction(row: SessionAnalysis, rate: Decimal | None) -> str:
    count = int(measure(row, Metric.COMPACTION_COUNT))
    title = "What compaction carried forward"
    if len(row.context) < 2:
        text = (
            "This session never compacted its context."
            if count == 0
            else f"This session compacted {plural(count, 'time')}, but its context was not"
            " read, so what was carried cannot be shown."
        )
        return section("compaction", title, note(text))
    pairs = list(zip(row.context, row.context[1:], strict=False))
    body = "".join(_sankey(before, after, rate) for before, after in pairs[:_SANKEY_LIMIT])
    if len(pairs) > _SANKEY_LIMIT:
        body += note(
            f"{len(pairs) - _SANKEY_LIMIT} later compactions appear in the generations below."
        )
    return section("compaction", title, body, chip=plural(len(pairs), "compaction"))


# ---- context generations --------------------------------------------------------------


@dataclass(slots=True)
class _Segment:
    tokens: int
    kinds: dict[str, int]
    blocks: int

    @property
    def kind(self) -> str:
        return max(self.kinds, key=lambda name: self.kinds[name])

    def absorb(self, other: _Segment) -> None:
        self.tokens += other.tokens
        self.blocks += other.blocks
        for name, value in other.kinds.items():
            self.kinds[name] = self.kinds.get(name, 0) + value


def _segments(blocks: Sequence[GenerationBlock], cap: int) -> list[_Segment]:
    """Bin adjacent small blocks so a strip never draws more than *cap* segments."""
    sized = [_Segment(block.tokens or 0, {block.kind: block.tokens or 0}, 1) for block in blocks]
    sized = [segment for segment in sized if segment.tokens]
    floor = sum(segment.tokens for segment in sized) * 3 / cap
    segments: list[_Segment] = []
    pending: _Segment | None = None
    for segment in sized:
        if segment.tokens >= floor:
            if pending is not None:
                segments.append(pending)
                pending = None
            segments.append(segment)
            continue
        if pending is None:
            pending = _Segment(0, {}, 0)
        pending.absorb(segment)
        if pending.tokens >= floor:
            segments.append(pending)
            pending = None
    if pending is not None:
        segments.append(pending)
    while len(segments) > cap:
        index = min(
            range(len(segments) - 1), key=lambda i: segments[i].tokens + segments[i + 1].tokens
        )
        segments[index].absorb(segments.pop(index + 1))
    return segments


def _token_ticks(total: int) -> list[tuple[float, str]]:
    """Return the token axis ticks, every label in the unit of the largest one.

    The step is 1, 2 or 5 times a power of ten and at least an eighth of the
    largest tick, so a label needs one decimal at most: ``0``, ``0.5M``, ``1M``.
    """
    step = next(
        mantissa * 10**exponent
        for exponent in range(16)
        for mantissa in (1, 2, 5)
        if total / (mantissa * 10**exponent) <= 8
    )
    stops = range(0, total + 1, step)
    unit, suffix = next(
        ((size, name) for size, name in _TOKEN_UNITS if stops[-1] >= size), _TOKEN_UNITS[-1]
    )
    return [(at / total * 100, f"{round(at / unit, 1):g}{suffix}") for at in stops]


def _strip_buttons(segments: Sequence[_Segment], total: int) -> str:
    buttons = []
    for segment in segments:
        css, word = _kind(segment.kind)
        width = segment.tokens / total * 100
        tip = f"{word} · {exact(segment.tokens)} tokens · {pct(segment.tokens, total)}"
        tip += " of the generation"
        if segment.blocks > 1:
            tip += f" · {plural(segment.blocks, 'block')} binned"
        buttons.append(
            f'<button type="button" class="{css}{" binned" if segment.blocks > 1 else ""}" '
            f'style="width:{width:.3f}%" title="{esc(tip)}">{esc(word) if width >= 8 else ""}'
            "</button>"
        )
    return "".join(buttons)


def _ledger(generation: ContextGeneration, total: int) -> str:
    largest = sorted(
        (block for block in generation.blocks if block.tokens),
        key=lambda block: -(block.tokens or 0),
    )[:_LEDGER_ROWS]
    rows = [
        (
            str(block.ordinal),
            _kind(block.kind)[1],
            str(block.request_ordinal),
            exact(block.tokens or 0),
            pct(block.tokens or 0, total),
        )
        for block in largest
    ]
    head = (("Block", True), ("Kind", False), ("Request", True), ("Tokens", True), ("Share", True))
    return (
        f"<details><summary>{plural(len(rows), 'largest block')}</summary>"
        f"{table(head, rows)}</details>"
    )


def _strip(generation: ContextGeneration, cap: int, anchor: str) -> str:
    summary = generation.summary
    segments = _segments(generation.blocks, cap)
    total = sum(segment.tokens for segment in segments)
    if not total:
        return (
            f'<div class="gen"><div class="gen-h"><h3>Generation {summary.index}</h3>'
            "<span>no sized blocks</span></div></div>"
        )
    legend = "".join(
        f'<span><i class="{_kind(kind)[0]}"></i>{_kind(kind)[1]} {compact(value)}</span>'
        for kind, value in sorted(summary.tokens_by_kind.items(), key=lambda pair: -pair[1])
        if value
    )
    read = (
        ""
        if summary.final_request_input is None
        else f" · last request read {compact(summary.final_request_input)}"
    )
    ticks = _token_ticks(total)
    side = axis_side([label for _at, label in ticks], _AXIS_FONT_PX)
    return (
        f'<div class="gen"><div class="gen-h"><h3>Generation {summary.index} · '
        f"{esc(summary.reason)}</h3><span>{compact(total)} tokens appended · "
        f"{plural(summary.requests, 'request')}{read}</span>"
        '<label>zoom <input type="range" min="1" max="12" step="1" value="1" '
        f'data-zoom="{anchor}"></label></div>'
        f'<div class="strip-vp"><div class="strip-in" id="{anchor}" data-chart="strip" '
        f'style="--side:{side}px">'
        f'<div class="strip">{_strip_buttons(segments, total)}</div>'
        f'<div class="tokaxis">{axis_ticks(ticks)}</div>'
        f'</div></div><div class="strip-legend">{legend}</div>{_ledger(generation, total)}</div>'
    )


def _generations(context: SessionContext | None) -> str:
    title = "Context generations"
    if context is None or not context.generations:
        return section("generations", title, note("The context of this session was not read."))
    shown = context.generations[:_GENERATION_LIMIT]
    cap = max(12, min(STRIP_SEGMENTS, 600 // len(shown)))
    body = "".join(
        _strip(generation, cap, f"zoom-{index}") for index, generation in enumerate(shown, start=1)
    )
    if len(context.generations) > len(shown):
        left_out = len(context.generations) - len(shown)
        body += note(f"{plural(left_out, 'later generation')} left out to keep the page light.")
    peak = (
        ""
        if context.peak_request_input is None
        else f"peak request input {compact(context.peak_request_input)}"
    )
    return section("generations", title, body, chip=peak)


# ---- requests, findings and limitations -----------------------------------------------


def _request_cost(rows: Sequence[ObservedRequest]) -> str:
    costs = [row.cost_usd for row in rows if row.cost_usd is not None]
    return usd(sum(costs, Decimal(0))) if costs else absent("no verified rate for this model")


def unpriced_tokens(requests: Sequence[ObservedRequest]) -> int:
    """Return the tokens of the requests no verified rate priced.

    Args:
        requests: Request rows.

    Returns:
        The summed ``total_tokens`` of the rows with a null cost; zero for none.
    """
    return sum(row.total_tokens for row in requests if row.cost_usd is None)


def cost_text(requests: Sequence[ObservedRequest]) -> str:
    """Return what a set of requests cost: ``$X``, or ``≥ $X + N unpriced tokens``.

    The priced requests are summed and the unpriced ones counted, so the text is
    never the dash: a set with no priced request reads ``≥ $0 + N unpriced tokens``.

    Args:
        requests: Request rows, such as one session's.

    Returns:
        The cost text; ``$0`` for no rows.
    """
    priced = sum((row.cost_usd for row in requests if row.cost_usd is not None), Decimal(0))
    unpriced = unpriced_tokens(requests)
    if not unpriced:
        return usd(priced)
    return f"≥ {usd(priced)} + {compact(unpriced)} unpriced tokens"


def unpriced_banner(requests: Sequence[ObservedRequest]) -> str:
    """Return the banner naming each unpriced model of *requests*, or nothing.

    Models sort by unpriced request count, most first. A name that is no model,
    such as ``unknown`` or ``<synthetic>``, is left out, and so is a request
    with no tokens, which costs nothing whatever its rate.

    Args:
        requests: The request rows the page draws.

    Returns:
        The banner markup, or the empty string when every model is priced.
    """
    counts: dict[str, int] = defaultdict(int)
    tokens = 0
    for item in requests:
        model = item.model or UNKNOWN_MODEL
        if item.cost_usd is None and item.total_tokens and model not in NON_MODELS:
            counts[model] += 1
            tokens += item.total_tokens
    if not counts:
        return ""
    named = ", ".join(
        f"{model} ({plural(count, 'request')})"
        for model, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    )
    share = pct(tokens, sum(item.total_tokens for item in requests))
    text = (
        f"Unpriced: {named}, {share} of tokens. Add a rate under reflect.pricing.overrides"
        " or update the price file."
    )
    return f'<div class="callout" role="note" data-banner="unpriced">{esc(text)}</div>'


def _classes(rows: Sequence[ObservedRequest], *, exact_counts: bool = False) -> tuple[str, ...]:
    shown = exact if exact_counts else compact
    return (
        shown(sum(item.input_tokens for item in rows)),
        shown(sum(item.output_tokens for item in rows)),
        shown(sum(item.cache_write_tokens for item in rows)),
        shown(sum(item.cache_read_tokens for item in rows)),
        _request_cost(rows),
    )


def _effort(row: SessionAnalysis) -> str:
    """Return the effort band, or the dash: no record names it, so it is never a value."""
    if row.effort_band == UNKNOWN:
        return absent("no record names this session's effort")
    return esc(row.effort_band)


def _requests(row: SessionAnalysis, requests: Sequence[ObservedRequest]) -> str:
    classes = (
        ("Fresh input", True),
        ("Output", True),
        ("Cache write", True),
        ("Cache read", True),
        ("Cost", True),
    )
    if not requests:
        return section(
            "requests",
            "Requests",
            note("No request was recorded for this session.") + table(classes, []),
        )
    grouped: dict[str, list[ObservedRequest]] = defaultdict(list)
    for request in requests:
        grouped[request.model or "unknown"].append(request)
    by_cost = sorted(
        grouped.items(), key=lambda pair: -sum(item.cost_usd or Decimal(0) for item in pair[1])
    )
    aggregate = [
        (esc(model), _effort(row), exact(len(members)), *_classes(members))
        for model, members in by_cost
    ]
    top = sorted(
        requests,
        key=lambda item: (
            item.cost_usd is None,
            -(item.cost_usd or Decimal(0)),
            -item.total_tokens,
        ),
    )[:10]
    dearest = [
        (
            str(item.ordinal),
            DASH if item.ts is None else clock(item.ts),
            esc(item.model or "unknown"),
            *_classes((item,), exact_counts=True),
        )
        for item in top
    ]
    body = (
        table((("Model", False), ("Effort", False), ("Requests", True), *classes), aggregate)
        + '<h3 style="margin-top:18px;font-size:13px">Ten most expensive requests</h3>'
        + table((("#", True), ("Time UTC", False), ("Model", False), *classes), dearest)
    )
    chip = f"{plural(len(requests), 'request')} · {cost_text(requests)}"
    return section("requests", "Requests", body, chip=chip)


def _findings(row: SessionAnalysis) -> str:
    cards = [
        finding_card(
            item,
            says=sentence(item.summary),
            impact=(
                f"{compact(item.value)} {METRIC_NOUNS[item.metric]}"
                if item.metric is not None and item.value is not None
                else usd(row.cost_usd)
            ),
            how=compared(item, row),
        )
        for item in row.findings
    ]
    body = (
        f'<div class="grid">{"".join(cards)}</div>'
        if cards
        else note("No finding was raised for this session.")
    )
    return section("findings", "Findings", body, chip=plural(len(cards), "finding"))


def _limitations(row: SessionAnalysis) -> str:
    items = [
        "Priced from a published rate."
        if row.pricing_quality is PricingQuality.VERIFIED
        else "No verified rate for this model, so no cost is shown; unpriced is not free.",
        *(f"{esc(say(code))}." for code in row.context_limitations),
    ]
    if row.title is not None:
        items.append(f"Title source: {esc(say(str(row.title.source)))}.")
    listed = "".join(f"<li>{item}</li>" for item in items)
    return section("coverage", "Coverage and limitations", f'<ul class="note">{listed}</ul>')


def _read_first(
    row: SessionAnalysis, detail: SessionDetail | None, requests: Sequence[ObservedRequest]
) -> str:
    compactions = int(measure(row, Metric.COMPACTION_COUNT))
    children = int(measure(row, Metric.CHILD_COUNT))
    unpriced = unpriced_tokens(requests)
    facts = [
        fact
        for condition, fact in (
            (
                compactions > 0,
                f"The context was compacted {plural(compactions, 'time')}; history before each"
                " boundary was rewritten and sent again.",
            ),
            (
                children > 0,
                f"Work was delegated to {plural(children, 'subagent')}; their tokens are already"
                " inside this session's totals.",
            ),
            (
                detail is None,
                "Per-event detail was not read, so the timeline and context strips are empty.",
            ),
            (
                any(item.finding_type == "baseline_cold_start" for item in row.findings),
                "Fewer than eight comparable earlier sessions exist, so only notices are raised.",
            ),
            (
                bool(unpriced) or (not requests and row.cost_usd is None),
                f"{compact(unpriced)} tokens have no verified rate, so the cost is a lower bound;"
                " unpriced is not free."
                if requests
                else "No verified rate prices this model, so no cost is shown.",
            ),
            (
                row.session_class is SessionClass.NOISE,
                "Excluded from baselines as noise: "
                f"{', '.join(say(reason.value) for reason in row.noise_reasons)}.",
            ),
        )
        if condition
    ]
    items = "".join(f"<li>{esc(fact)}</li>" for fact in facts) or (
        "<li>Nothing unusual shapes how to read this session.</li>"
    )
    return f'<div class="callout"><b>Read this first</b><ul>{items}</ul></div>'


def _summary(row: SessionAnalysis, requests: Sequence[ObservedRequest]) -> str:
    priced = [item.cost_usd for item in requests if item.cost_usd is not None]
    cost = sum(priced, Decimal(0)) if priced else None
    worth = effective(cost if requests else row.cost_usd)
    tools = exact(measure(row, Metric.TOOL_CALLS)) if Metric.TOOL_CALLS in row.metrics else DASH
    cells = (
        ("Effective tokens", DASH if worth is None else compact(worth)),
        ("API-equivalent", cost_text(requests) if requests else usd(row.cost_usd)),
        ("Context handled", compact(measure(row, Metric.TOTAL_TOKENS))),
        ("Wall-clock", duration(row.metrics.get(Metric.CALENDAR_MS))),
        ("Requests", exact(len(requests) or measure(row, Metric.MODEL_STEPS))),
        ("Tool calls", tools),
        ("Compactions", exact(measure(row, Metric.COMPACTION_COUNT))),
        ("Subagents", exact(measure(row, Metric.CHILD_COUNT))),
    )
    listed = "".join(
        f'<div><dt>{label}</dt><dd class="num">{value}</dd></div>' for label, value in cells
    )
    return f'<dl class="cells">{listed}</dl>'


def _input_rate(scope: Scope, model: str) -> Decimal | None:
    found = next((row for row in scope.rates if row.model == model), None)
    return None if found is None or found.rates is None else found.rates.input


def session_page(row: SessionAnalysis, scope: Scope) -> str:
    """Return one session's page.

    Args:
        row: The session.
        scope: The covered sessions, their requests, detail and rates.

    Returns:
        The page.
    """
    detail = scope.sources.details.get(row.session_ref)
    requests = scope.requests.get(row.session_ref, ())
    started = (
        DASH
        if row.started_at is None
        else row.started_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    )
    shape = "orchestrated" if measure(row, Metric.CHILD_COUNT) else "single agent"
    chips = "".join(
        f'<span class="chip">{esc(value)}</span>'
        for value in (
            started,
            row.project_label,
            row.runtime,
            row.model,
            *(() if row.effort_band == UNKNOWN else (f"effort {row.effort_band}",)),
            shape,
        )
    )
    head = (
        '<section class="sec" data-section="identity"><div class="eyebrow">Session '
        f"#{session_key(row.session_ref)[:8]} · stable across re-runs</div>"
        f'<h1>{esc(session_title(row))}</h1><div class="chips">{chips}</div>'
        f"{_summary(row, requests)}{_read_first(row, detail, requests)}</section>"
    )
    body = "".join(
        (
            head,
            unpriced_banner(requests),
            _timeline(detail),
            _compaction(row, _input_rate(scope, row.model)),
            _generations(scope.sources.contexts.get(row.session_ref)),
            _requests(row, requests),
            _findings(row),
            _limitations(row),
        )
    )
    nav = (
        f'<a href="{OVERVIEW_FILENAME}">Overview</a>{_CRUMB}'
        f'<a href="{FINDER_FILENAME}">Sessions</a>{_CRUMB}<b>this session</b>'
    )
    title = f"{day(row.started_at)} · {row.project_label} · {trim(session_title(row), 40)}"
    return shell(title, nav, body, scope, script=True)


__all__ = ["LANE_BARS", "STRIP_SEGMENTS", "session_page"]
