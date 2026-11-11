"""Static reflect pages: the overview, the session finder and one page per session.

Every page is one self-contained file. Its stylesheet and script are inlined from
the package's assets, it names no remote URL, and its palette follows the system
colour scheme. The finder draws :data:`FINDER_PAGE_ROWS` rows and pages the rest
from an inline data block, so no page grows with the history behind it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from eawf.kernel.config.layered import merge_config
from eawf.kernel.config.schema import PriceRates, ReflectConfig
from eawf.observability.reflect.analysis import (
    RANKING_LIMIT,
    Finding,
    Metric,
    SessionAnalysis,
    SessionClass,
    Severity,
)
from eawf.observability.reflect.html.session import (
    cost_text,
    session_page,
    unpriced_banner,
    unpriced_tokens,
)
from eawf.observability.reflect.html.shared import (
    DASH,
    FINDER_FILENAME,
    OVERVIEW_FILENAME,
    REFERENCE_RATE,
    SEVERITY,
    PageSources,
    Scope,
    absent,
    axis_side,
    axis_ticks,
    compact,
    compared,
    day,
    duration,
    effective,
    esc,
    exact,
    finding_card,
    identity,
    measure,
    note,
    page_name,
    pct,
    plural,
    raised,
    ratio,
    say,
    section,
    sentence,
    session_key,
    session_title,
    severity_chip,
    shell,
    spend_order,
    table,
    trim,
    usd,
)
from eawf.observability.reflect.rates import ModelRates, rate_table
from eawf.observability.telemetry.models import ObservedRequest
from eawf.observability.telemetry.pricing import (
    PriceBook,
    TokenUsage,
    active_price_book,
    request_date,
)

logger = logging.getLogger(__name__)

#: Rows the finder draws before the reader pages.
FINDER_PAGE_ROWS: Final = 40

_DOT_COLUMNS_PER_PERCENT: Final = 4
# The distribution axis label size in reflect.css, which its side margins hold.
_AXIS_FONT_PX: Final = 10.5

Progress = Callable[[str, int, int], None]
"""Receives ``("render", done, total)`` as the session pages are written."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WrittenPages:
    """Where one set of pages landed.

    Attributes:
        overview: The overview page.
        finder: The session finder.
        sessions: One page per covered session, dearest first.
    """

    overview: Path
    finder: Path
    sessions: tuple[Path, ...]


def _nav(current: str) -> str:
    links = ((OVERVIEW_FILENAME, "Overview"), (FINDER_FILENAME, "Sessions"))
    return "".join(
        f'<a href="{href}"{' aria-current="page"' if href == current else ""}>{label}</a>'
        for href, label in links
    )


# ---- headline setting -----------------------------------------------------------------

HeadlineMode = Literal["api_equivalent", "tokens"]
"""What the overview leads with; the page switch starts on it."""


def reflect_headline(repo_root: Path | None) -> HeadlineMode:
    """Return ``reflect.headline`` from the layered config.

    Args:
        repo_root: The repository whose layers compose over the global one;
            ``None`` outside a repository, where the global layer still applies.

    Returns:
        ``api_equivalent`` unless a layer sets ``tokens``.

    Raises:
        pydantic.ValidationError: The ``reflect`` block is malformed.
    """
    merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
    return ReflectConfig.model_validate(merged.get("reflect") or {}).headline


# ---- hero -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Listed:
    """The list rates and verification date one model resolves to on one day."""

    rates: PriceRates | None
    verified_on: date | None


class _ListPrices:
    """The price book's list rates per model and day, looked up once each."""

    def __init__(self, book: PriceBook) -> None:
        self._book = book
        self._seen: dict[tuple[str, date], _Listed] = {}

    def of(self, request: ObservedRequest) -> _Listed:
        key = (request.model or "", request_date(request.ts))
        found = self._seen.get(key)
        if found is None:
            model, on = key
            quote = self._book.quote(model, TokenUsage(), on=on)
            found = _Listed(self._book.list_rates(model, on=on), quote.verified_on)
            self._seen[key] = found
        return found


def _scope_requests(scope: Scope) -> list[ObservedRequest]:
    return [item for items in scope.requests.values() for item in items]


def _priced_total(requests: Sequence[ObservedRequest]) -> Decimal | None:
    priced = [item.cost_usd for item in requests if item.cost_usd is not None]
    return sum(priced, Decimal(0)) if priced else None


def _verified_on(requests: Sequence[ObservedRequest], prices: _ListPrices) -> date | None:
    """Return the oldest ``verified_on`` among the price rows the priced requests used."""
    dates = [
        listed.verified_on
        for item in requests
        if item.cost_usd is not None and item.model
        if (listed := prices.of(item)).verified_on is not None
    ]
    return min(dates, default=None)


def _class_costs(requests: Sequence[ObservedRequest], prices: _ListPrices) -> dict[str, Decimal]:
    """Split each priced request's recorded cost across its token classes.

    Each class is weighed at the model's list rate, 5-minute and 1-hour writes
    at their own, and the weights are scaled to the cost the request recorded,
    so a tier or modifier moves every class alike and the strip sums to the hero.
    """
    costs = dict.fromkeys(("fresh", "output", "write", "read"), Decimal(0))
    for item in requests:
        if item.cost_usd is None or not item.model:
            continue
        rates = prices.of(item).rates
        if rates is None:
            continue
        writes = rates.cache_write
        weights = {
            "fresh": item.input_tokens * (rates.input or 0),
            "output": item.output_tokens * (rates.output or 0),
            "read": item.cache_read_tokens * (rates.cache_read or 0),
            "write": (item.cache_write_tokens - item.cache_write_1h_tokens)
            * (writes.get("5m") or 0)
            + item.cache_write_1h_tokens * (writes.get("1h") or 0),
        }
        weighed = sum(weights.values(), Decimal(0))
        if not weighed:
            continue
        for cls, weight in weights.items():
            costs[cls] += item.cost_usd * weight / weighed
    return costs


def _cache_read_copy(scope: Scope, prices: _ListPrices) -> str:
    """Return how cache reads bill against base input across the models priced here."""
    shares = {
        float(rates.cache_read / rates.input * 100)
        for item in _scope_requests(scope)
        if item.cost_usd is not None and item.model
        if (rates := prices.of(item).rates) is not None
        and rates.input
        and rates.cache_read is not None
    }
    if not shares:
        return "Cache reads bill below base input"
    low, high = min(shares), max(shares)
    if low == high:
        return f"Cache reads bill at {low:g}% of base input"
    return f"Cache reads bill at {low:g}% to {high:g}% of base input, by model"


_WRITE_MEANING: Final = "5-minute and 1-hour writes at their own rates"

_CLASSES: Final = (
    ("fresh-input", "Fresh input", Metric.FRESH_INPUT_TOKENS, "fresh"),
    ("output", "Output", Metric.OUTPUT_TOKENS, "output"),
    ("cache-writes", "Cache writes", Metric.CACHE_WRITE_TOKENS, "write"),
    ("cache-reads", "Cache reads", Metric.CACHE_READ_TOKENS, "read"),
)


def _class_strip(scope: Scope, prices: _ListPrices, read_copy: str) -> str:
    costs = _class_costs(_scope_requests(scope), prices)
    spend = float(sum(costs.values(), Decimal(0)))
    reasoning = sum(measure(row, Metric.REASONING_TOKENS) for row in scope.rows)
    meanings = {
        "fresh": "New context sent",
        "output": "Reasoning is billed as output",
        "write": _WRITE_MEANING,
        "read": read_copy,
    }
    cells = []
    for key, label, metric, cls in _CLASSES:
        tokens = sum(measure(row, metric) for row in scope.rows)
        tip = (
            f"{exact(tokens)} tokens · {usd(costs[cls])} · "
            f"{pct(float(costs[cls]), spend)} of spend. {meanings[cls]}."
        )
        sub = f" <small>{compact(reasoning)} reasoning</small>" if key == "output" else ""
        cells.append(
            f'<div data-cls="{key}" data-usd="{costs[cls]}" title="{esc(tip)}"><dt>{label}</dt>'
            f'<dd class="num">{compact(tokens)}{sub}</dd></div>'
        )
    return f'<dl class="hero-secondary">{"".join(cells)}</dl>'


def _switch(headline: HeadlineMode) -> str:
    """Return the headline switch: a radio pair the stylesheet's sibling selectors
    read, so the overview switches with no script."""
    tokens = " checked" if headline == "tokens" else ""
    dollars = " checked" if headline == "api_equivalent" else ""
    return (
        f'<input class="hl-in" type="radio" name="headline" id="headline-tokens"{tokens}>'
        f'<input class="hl-in" type="radio" name="headline" id="headline-usd"{dollars}>'
        '<div class="hl-switch" role="group" aria-label="Headline">'
        '<label for="headline-tokens">tokens</label>'
        '<label for="headline-usd">API-equivalent</label></div>'
    )


def _hero(scope: Scope, headline: HeadlineMode) -> str:
    prices = _ListPrices(active_price_book())
    requests = _scope_requests(scope)
    cost = _priced_total(requests)
    worth = effective(cost)
    handled = sum(measure(row, Metric.TOTAL_TOKENS) for row in scope.rows)
    unpriced = unpriced_tokens(requests)
    verified = _verified_on(requests, prices)
    dated = (
        f"list prices verified {verified.isoformat()}"
        if verified is not None
        else "priced by operator overrides"
    )
    dollars = f"{usd(cost)} API-equivalent · {dated}"
    tokens = f"{compact(worth or 0)} effective tokens"
    if cost is None:
        none = f"<span>{DASH}</span> <small>not priced: no session carries a verified rate</small>"
        lead_usd = lead_tokens = none
        sub_usd = sub_tokens = ""
    else:
        lead_usd = f'<span class="num">{usd(cost)}</span> <small>API-equivalent · {dated}</small>'
        lead_tokens = (
            f'<span class="num">{compact(worth or 0)}</span> <small>effective tokens</small>'
        )
        sub_usd = f"{tokens}. "
        sub_tokens = f"{dollars}. "
    read_copy = _cache_read_copy(scope, prices)
    caveat = (
        f" {compact(unpriced)} tokens carry no verified rate and are left out, so the total"
        " is a lower bound."
        if unpriced
        else ""
    )
    context = (
        f"{compact(handled)} tokens passed through the context (context handled, cumulative)."
        f" {read_copy}, so the weighted total is smaller.{caveat}"
    )
    label_usd = (
        "API-equivalent: every token class at the vendor's standard-tier list price in USD,"
        ' not what a subscription bills. <a href="#rates">Rates and assumptions</a>.'
    )
    label_tokens = (
        f"Effective tokens: every token class priced, then expressed back at"
        f" ${REFERENCE_RATE:.2f} per 1M base input tokens "
        '<span class="q" title="Derived from recorded per-request totals">reconstructed</span>'
    )
    return (
        '<section class="sec" data-section="hero"><div class="eyebrow">'
        f"{esc(scope.sources.scope_label)} · local artifact</div>"
        f"<h1>What these sessions cost you</h1>{_switch(headline)}"
        f'<div class="hero" data-headline="{headline}"><div class="hero-top"><div>'
        f'<div class="hl-usd"><div class="hero-val">{lead_usd}</div>'
        f'<div class="hero-lbl">{label_usd}</div><div class="hero-sub">{sub_usd}{context}'
        "</div></div>"
        f'<div class="hl-tokens"><div class="hero-val">{lead_tokens}</div>'
        f'<div class="hero-lbl">{label_tokens}</div><div class="hero-sub">{sub_tokens}'
        f"{context}</div></div></div></div>"
        f"{_class_strip(scope, prices, read_copy)}</div></section>"
        f"{unpriced_banner(requests)}"
    )


# ---- distribution ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LogScale:
    low: float
    high: float

    def at(self, value: float) -> float:
        span = math.log10(self.high) - math.log10(self.low)
        offset = math.log10(max(value, self.low)) - math.log10(self.low)
        return max(0.0, min(100.0, offset / span * 100))


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(probability * len(ordered)) - 1)]


def _ticks(scale: _LogScale) -> tuple[str, str, list[str]]:
    """Return the track gridlines, the axis and its labels: decade majors, 2 to 9 minors."""
    grid, minors, majors = [], [], []
    decade = scale.low
    while decade <= scale.high:
        for step in range(1, 10):
            tick = decade * step
            if tick > scale.high:
                break
            major = ' class="mj"' if step == 1 else ""
            line = f'<i{major} style="left:{scale.at(tick):.2f}%"></i>'
            grid.append(line)
            if step == 1:
                majors.append((scale.at(tick), compact(tick)))
            else:
                minors.append(line)
        decade *= 10
    labels = [label for _at, label in majors]
    return "".join(grid), "".join(minors) + axis_ticks(majors), labels


def _dot_tip(row: SessionAnalysis, value: float, median: float) -> str:
    return " · ".join(
        (
            session_title(row),
            row.project_label,
            day(row.started_at),
            row.model,
            f"{exact(value)} tokens",
            usd(row.cost_usd),
            plural(len(raised(row)), "finding"),
            ratio(value, median),
        )
    )


def _dots(sized: Sequence[tuple[SessionAnalysis, float]], scale: _LogScale, median: float) -> str:
    """One dot per session; sessions that share a pixel column share one dot."""
    columns: dict[tuple[int, bool], list[tuple[SessionAnalysis, float]]] = defaultdict(list)
    for row, value in sized:
        column = round(scale.at(value) * _DOT_COLUMNS_PER_PERCENT)
        columns[(column, bool(raised(row)))].append((row, value))
    dots = []
    for (column, flagged), members in sorted(columns.items()):
        if len(members) == 1:
            tip, href = _dot_tip(*members[0], median), page_name(members[0][0].session_ref)
        else:
            tip = f"{len(members)} sessions near {compact(members[0][1])} tokens"
            href = FINDER_FILENAME
        left = min(column / _DOT_COLUMNS_PER_PERCENT, 99.4)
        dots.append(
            f'<a class="dist-dot{" hi" if flagged else ""}" style="left:{left:.2f}%" '
            f'href="{href}" title="{esc(tip)}"></a>'
        )
    return "".join(dots)


def _distribution(scope: Scope) -> str:
    title = "Where these sessions sit against your own history"
    sized = [(row, value) for row in scope.rows if (value := measure(row, Metric.TOTAL_TOKENS)) > 0]
    if not sized:
        return section("distribution", title, note("No session recorded tokens."))
    values = [value for _row, value in sized]
    low = 10 ** math.floor(math.log10(min(values)))
    scale = _LogScale(low, max(10 ** math.ceil(math.log10(max(values))), low * 10))
    grid, axis, ticks = _ticks(scale)
    marks, labels = [], []
    for css, label, value in (
        ("p50", "Your median", statistics.median(values)),
        ("p95", "P95", _quantile(values, 0.95)),
        ("max", "Largest", max(values)),
    ):
        x = scale.at(value)
        marks.append(f'<div class="dist-mark {css}" style="left:{x:.2f}%"></div>')
        # Each mark's label keeps its own row of the lane above the dots, so none overlap.
        labels.append(
            f'<span class="dist-label {css}{" flip" if x > 62 else ""}" style="left:{x:.2f}%">'
            f"{label} {compact(value)}</span>"
        )
    side = axis_side(ticks, _AXIS_FONT_PX)
    body = (
        f'<div class="dist" data-chart="distribution" style="--side:{side}px">'
        f'<div class="dist-lane">{"".join(labels)}</div>'
        f'<div class="dist-track"><div class="dist-grid">{grid}</div>'
        f"{''.join(marks)}{_dots(sized, scale, scope.median_tokens)}</div>"
        f'<div class="axis">{axis}</div></div>'
    )
    return section("distribution", title, body, chip="log scale · tokens handled")


# ---- must-see, expected and findings --------------------------------------------------


def _why(row: SessionAnalysis, median: float) -> str:
    tokens = measure(row, Metric.TOTAL_TOKENS)
    children = int(measure(row, Metric.CHILD_COUNT))
    steps = int(measure(row, Metric.MODEL_STEPS))
    parts = [f"{plural(len(raised(row)), 'finding')} to decide on"]
    if tokens:
        parts.append(f"{compact(tokens)} tokens handled ({ratio(tokens, median)})")
    if children:
        parts.append(plural(children, "subagent"))
    if steps:
        span = duration(row.metrics.get(Metric.CALENDAR_MS))
        parts.append(f"{plural(steps, 'request')} over {span}")
    return " · ".join(parts) + "."


def _session_cost(row: SessionAnalysis, scope: Scope) -> str | None:
    """Return a session's cost text from its requests, else its recorded cost, else ``None``."""
    requests = scope.requests.get(row.session_ref, ())
    if requests:
        return cost_text(requests)
    return None if row.cost_usd is None else usd(row.cost_usd)


def _must_see_row(rank: int, row: SessionAnalysis, scope: Scope) -> str:
    worst = max(raised(row), key=lambda item: item.severity.rank).severity
    cost = _session_cost(row, scope)
    money = esc(cost) if cost is not None else '<span class="faint">not priced</span>'
    median = scope.median_tokens
    chip = f'<span class="sv sv-{SEVERITY[worst][0]}">{plural(len(raised(row)), "finding")}</span>'
    return (
        f'<a href="{page_name(row.session_ref)}"><span class="no">{rank}</span><span>'
        f'<h3>{esc(session_title(row))}</h3><span class="id">{esc(identity(row))}{chip}</span>'
        f'<span class="why">{esc(_why(row, median))}</span></span><span class="amt">'
        f'<span class="money">{money}</span><span class="sub">'
        f"{compact(measure(row, Metric.TOTAL_TOKENS))} tokens</span></span></a>"
    )


def _must_see(scope: Scope) -> str:
    items = "".join(
        _must_see_row(rank, row, scope) for rank, row in enumerate(scope.must_see, start=1)
    )
    total = sum((row.cost_usd or Decimal(0) for row in scope.must_see), Decimal(0))
    chip = (
        f"{plural(len(scope.flagged), 'session')} carry findings · {usd(total)} in these"
        f" {len(scope.must_see)}"
    )
    body = f'<div class="rank">{items}</div>' if items else note("No session carries a finding.")
    return section("must-see", "Must-see: findings first, ranked by spend", body, chip=chip)


def _expected(scope: Scope) -> str:
    """Coalesce neutral notices: one card per fact, not one per session."""
    notices: dict[str, list[Finding]] = defaultdict(list)
    for row in scope.rows:
        for item in row.findings:
            if item.severity is Severity.NOTICE:
                notices[item.finding_type].append(item)
    cards = "".join(
        f'<article class="find is-notice"><div class="find-h">{severity_chip(Severity.NOTICE)}'
        f'<h3>{esc(say(kind))}</h3><span class="chip">{plural(len(items), "session")}</span>'
        f'</div><p class="find-say prose">{sentence(items[0].summary)}</p></article>'
        for kind, items in sorted(notices.items(), key=lambda pair: -len(pair[1]))
    )
    body = f'<div class="grid">{cards}</div>' if cards else note("Nothing expected to note.")
    return section("expected", "Large, but expected", body)


def _finding_groups(scope: Scope) -> list[tuple[Finding, list[SessionAnalysis]]]:
    groups: dict[str, list[tuple[Finding, SessionAnalysis]]] = defaultdict(list)
    for row in scope.rows:
        for item in raised(row):
            groups[item.finding_type].append((item, row))
    ordered = [
        (
            max(members, key=lambda pair: pair[0].severity.rank)[0],
            sorted((row for _item, row in members), key=spend_order),
        )
        for members in groups.values()
    ]
    ordered.sort(key=lambda pair: (-pair[0].severity.rank, -len(pair[1]), pair[0].finding_type))
    return ordered


def _finding_group_card(item: Finding, members: Sequence[SessionAnalysis]) -> str:
    priced = [row.cost_usd for row in members if row.cost_usd is not None]
    tokens = sum(measure(row, Metric.TOTAL_TOKENS) for row in members)
    money = f" · {usd(sum(priced, Decimal(0)))} spent in them" if priced else ""
    links = ", ".join(
        f'<a href="{page_name(row.session_ref)}">{esc(trim(session_title(row), 48))}</a>'
        for row in members[:3]
    )
    more = f" and {len(members) - 3} more" if len(members) > 3 else ""
    count = plural(len(members), "session")
    return finding_card(
        item,
        says=f"{count}: {esc(item.summary)}.",
        impact=f"{count} · {compact(tokens)} tokens handled{money}",
        how=compared(item, members[0]),
        chip=count,
        also=f'<p class="also">Sessions: {links}{more}</p>',
    )


def _findings(scope: Scope) -> str:
    cards = [_finding_group_card(item, members) for item, members in _finding_groups(scope)]
    body = f'<div class="grid">{"".join(cards)}</div>' if cards else note("No finding was raised.")
    return section("findings", "Findings", body, chip=plural(len(cards), "finding"))


# ---- coverage and rates ---------------------------------------------------------------


def _coverage(scope: Scope) -> str:
    sources = scope.sources
    parsed = len(sources.analysis.sessions)
    covered = len(scope.rows)
    eligible = [row for row in scope.rows if row.session_class is SessionClass.LISTED]
    priced = sum(1 for row in eligible if row.cost_usd is not None)
    unread = max(0, sources.history_files - parsed)
    stages = (
        ("Sources seen", sources.history_files, "history files found on this machine"),
        ("Parsed", parsed, f"{plural(unread, 'file')} named no session of its own"),
        ("In scope", covered, f"{plural(parsed - covered, 'session')} outside the scope"),
        ("Eligible", len(eligible), f"{covered - len(eligible)} excluded: noise, not real work"),
        ("Priced", priced, f"{plural(len(eligible) - priced, 'session')} with no verified rate"),
    )
    funnel = "".join(
        f'<div><dt>{label}</dt><dd class="num">{exact(value)}<small>{reason}</small></dd></div>'
        for label, value, reason in stages
    )
    grid = "".join(
        f'<div><dt>{label}</dt><dd class="num">{exact(value)}</dd></div>'
        for label, value in (
            ("Requests read", sum(len(rows) for rows in scope.requests.values())),
            ("Noise excluded", covered - len(eligible)),
            ("Projects", len({row.project_key for row in scope.rows})),
            ("Models", len(scope.rates)),
        )
    )
    body = f'<dl class="funnel">{funnel}</dl><dl class="cov-grid">{grid}</dl>'
    return section("coverage", "Coverage and trust", body)


def _rate(rates: ModelRates | None, name: str, digits: int) -> str:
    return absent("unpriced") if rates is None else f"${getattr(rates, name):,.{digits}f}"


def _rates(scope: Scope) -> str:
    rows = [
        (
            f'<span title="{esc(row.unpriced_reason or "")}">{esc(row.model)}</span>',
            exact(row.sessions),
            compact(row.total_tokens),
            _rate(row.rates, "input", 2),
            _rate(row.rates, "cache_write_5m", 2),
            _rate(row.rates, "cache_write_1h", 2),
            _rate(row.rates, "cache_read", 3),
            _rate(row.rates, "output", 2),
            usd(row.cost_usd)
            if row.cost_usd is not None
            else absent(row.unpriced_reason or "unpriced"),
        )
        for row in scope.rates[:FINDER_PAGE_ROWS]
    ]
    head = (
        ("Model", False),
        ("Sessions", True),
        ("Tokens", True),
        ("$/1M input", True),
        ("$/1M 5-minute write", True),
        ("$/1M 1-hour write", True),
        ("$/1M cache read", True),
        ("$/1M output", True),
        ("Cost", True),
    )
    total = sum(row.total_tokens for row in scope.rates)
    priced = sum(row.total_tokens for row in scope.rates if row.rates is not None)
    provenance = note(
        "Rates come from the shipped price files: standard tier, USD, 5-minute and 1-hour"
        f" cache writes at their own rates. {pct(priced, total)} of tokens handled were"
        " priced; an unpriced model shows no cost, never zero."
    )
    return section("rates", "Rates", table(head, rows) + provenance)


def _overview(scope: Scope, headline: HeadlineMode) -> str:
    parts = (_distribution, _must_see, _expected, _findings, _coverage, _rates)
    body = _hero(scope, headline) + "".join(part(scope) for part in parts)
    return shell("Reflection · Eä", _nav(OVERVIEW_FILENAME), body, scope, script=False)


# ---- finder ---------------------------------------------------------------------------


def _finder_row(row: SessionAnalysis, must: frozenset[str], scope: Scope) -> dict[str, object]:
    flags = raised(row)
    worst = max((item.severity for item in flags), key=lambda item: item.rank, default=None)
    tokens = measure(row, Metric.TOTAL_TOKENS)
    requests = scope.requests.get(row.session_ref, ())
    cost = _priced_total(requests) if requests else row.cost_usd
    return {
        "key": session_key(row.session_ref),
        "t": session_title(row),
        "s": identity(row),
        "dd": day(row.started_at),
        "d": None if row.started_at is None else row.started_at.timestamp(),
        "p": row.project_label,
        "m": row.model,
        "c": None if cost is None else float(cost),
        "cs": _session_cost(row, scope),
        "tk": tokens,
        "tks": compact(tokens),
        "f": len(flags),
        "sv": "" if worst is None else SEVERITY[worst][0],
        "must": row.session_ref in must,
    }


def _finder_html(item: Mapping[str, object]) -> str:
    """Draw one row exactly as the page script draws it, so paging changes nothing."""
    count = item["f"]
    badge = (
        f'<span class="sv sv-{item["sv"]}">{count} {"finding" if count == 1 else "findings"}</span>'
        if count
        else '<span class="faint">none</span>'
    )
    cost = absent("no verified rate for this model") if item["cs"] is None else esc(item["cs"])
    return (
        f'<tr><td class="t stick"><a href="session-{item["key"]}.html">{esc(item["t"])}</a>'
        f"<small>{esc(item['s'])}</small></td><td>{esc(item['dd'])}</td>"
        f'<td>{esc(item["p"])}</td><td>{esc(item["m"])}</td><td class="n">{cost}</td>'
        f'<td class="n">{esc(item["tks"])}</td><td>{badge}</td></tr>'
    )


_FINDER_COLUMNS: Final = (
    ("t", "Session", False),
    ("d", "Started", False),
    ("p", "Project", False),
    ("m", "Model", False),
    ("c", "Cost", True),
    ("tk", "Tokens", True),
    ("f", "Findings", False),
)

_FINDER_TOOLS: Final = (
    '<div class="tbl-tools"><input id="finder-q" type="search" aria-label="Search sessions"'
    ' placeholder="Search titles, project, model, date">'
    '<button class="btn" type="button" data-filter="must" aria-pressed="false">Must-see</button>'
    '<button class="btn" type="button" data-filter="has" aria-pressed="false">Has findings'
    '</button><button class="btn" type="button" data-filter="reset">Reset</button>'
    '<span class="chip" id="finder-scope">sorted across every project</span></div>'
)


def _finder(scope: Scope) -> str:
    must = frozenset(row.session_ref for row in scope.must_see)
    items = [_finder_row(row, must, scope) for row in sorted(scope.rows, key=spend_order)]
    first = "".join(_finder_html(item) for item in items[:FINDER_PAGE_ROWS]) or (
        '<tr><td colspan="7">No session in this scope yet.</td></tr>'
    )
    head = "".join(
        f'<th data-sort="{key}"{' class="n"' if numeric else ""}'
        f"{' aria-sort="descending"' if key == 'c' else ''}>"
        f'<button type="button">{label}</button></th>'
        for key, label, numeric in _FINDER_COLUMNS
    )
    payload = json.dumps({"page": FINDER_PAGE_ROWS, "rows": items}, separators=(",", ":"))
    shown = min(len(items), FINDER_PAGE_ROWS)
    status = f"1-{shown} of {len(items)}" if items else "0 of 0"
    following = "" if len(items) > FINDER_PAGE_ROWS else " disabled"
    body = (
        f'{_FINDER_TOOLS}<div class="tbl-scroll"><table class="rt"><thead><tr>{head}</tr>'
        f'</thead><tbody id="finder-rows">{first}</tbody></table></div>'
        f'<div class="pager"><span id="finder-status">{status}</span><span>'
        '<button class="btn" type="button" data-page="prev" disabled>Previous</button> '
        f'<button class="btn" type="button" data-page="next"{following}>Next</button></span></div>'
        '<script type="application/json" id="finder-data">'
        f"{payload.replace('</', '<\\/')}</script>"
    )
    page = unpriced_banner(_scope_requests(scope)) + section(
        "finder", "Sessions", body, chip=plural(len(items), "session")
    )
    return shell("Sessions · Eä", _nav(FINDER_FILENAME), page, scope, script=True)


# ---- writing --------------------------------------------------------------------------


def _scope(sources: PageSources) -> Scope:
    refs = sources.session_refs
    rows = tuple(row for row in sources.analysis.sessions if row.session_ref in refs)
    requests: dict[str, list[ObservedRequest]] = defaultdict(list)
    for request in sources.requests:
        if request.vendor_session_ref in refs:
            requests[request.vendor_session_ref].append(request)
    listed = (row for row in rows if row.session_class is SessionClass.LISTED and raised(row))
    flagged = tuple(sorted(listed, key=spend_order))
    tokens = [value for row in rows if (value := measure(row, Metric.TOTAL_TOKENS)) > 0]
    return Scope(
        sources=sources,
        rows=rows,
        requests={ref: tuple(items) for ref, items in requests.items()},
        rates=rate_table([item for items in requests.values() for item in items]),
        must_see=flagged[:RANKING_LIMIT],
        flagged=flagged,
        median_tokens=statistics.median(tokens) if tokens else 0.0,
    )


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{secrets.token_hex(4)}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_pages(
    output_dir: Path,
    sources: PageSources,
    *,
    headline: HeadlineMode = "api_equivalent",
    progress: Progress | None = None,
) -> WrittenPages:
    """Write the overview, the finder and one page per covered session.

    Args:
        output_dir: The directory the pages land in; created when missing.
        sources: What the pages are drawn from.
        headline: What the overview leads with, as :func:`reflect_headline`
            reads it; the page switch starts on it.
        progress: Called with ``("render", done, total)`` at the start, the end
            and about every tenth session page.

    Returns:
        Where each page landed.

    Raises:
        OSError: ``output_dir`` is not a directory or cannot be written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    scope = _scope(sources)
    overview = output_dir / OVERVIEW_FILENAME
    finder = output_dir / FINDER_FILENAME
    _write(overview, _overview(scope, headline))
    _write(finder, _finder(scope))
    ordered = sorted(scope.rows, key=spend_order)
    total = len(ordered)
    written = []
    if progress is not None:
        progress("render", 0, total)
    for done, row in enumerate(ordered, start=1):
        path = output_dir / page_name(row.session_ref)
        _write(path, session_page(row, scope))
        written.append(path)
        if progress is not None and (done == total or done % max(1, total // 10) == 0):
            progress("render", done, total)
    logger.info(f"write_pages sessions={total} must_see={len(scope.must_see)}")
    return WrittenPages(overview=overview, finder=finder, sessions=tuple(written))


__all__ = [
    "FINDER_PAGE_ROWS",
    "HeadlineMode",
    "PageSources",
    "WrittenPages",
    "reflect_headline",
    "write_pages",
]
