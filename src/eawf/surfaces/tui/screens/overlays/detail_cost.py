"""Per-session cost projection for the wave-detail ``$`` (cost) tab + tile.

The wave-detail overlay's ``$`` tab surfaces the metered per-attempt cost
of a wave's :class:`~eawf.kernel.state.models.SessionAttempt` rows, and the
``/metrics`` dashboard carries a matching Cost tile. Both read the same
priced telemetry: each attempt is joined back to its projected
:class:`~eawf.observability.telemetry.models.TelemetrySession` through the
canonical :func:`~eawf.observability.telemetry.join.rollup_wave_sessions`
join, so the tab and the tile agree on every figure (DRY: one cost
aggregation, two surfaces).

Honest absence is a first-class state, never a fabricated zero:

* a wave whose attempts join no telemetry session renders the exact
  :data:`NO_METERED_SESSIONS` line rather than an empty table;
* an attempt that the aggregator could not price (billable tokens but a
  :attr:`~eawf.observability.telemetry.join.WaveAttemptRollup.cost_usd` of
  zero, e.g. an unknown model absent from the pricing snapshot) renders the
  shared em-dash sentinel plus the :data:`UNBILLED_MARKER` inert marker, so
  an un-priced row stays visibly distinct from a real ``$0`` cost.

Every figure is a pure function of the joined rollup so the projection is
unit-testable without mounting Textual; the modal stays a thin view over
it.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from eawf.observability.telemetry.join import (
    DEFAULT_EU_MINUTES,
    WaveAttemptRollup,
    WaveSessionRollup,
    rollup_wave_sessions,
)
from eawf.observability.telemetry.models import TelemetrySession
from eawf.observability.telemetry.pricing import lookup_pricing
from eawf.observability.telemetry.store import metrics_db_path, open_store
from eawf.surfaces.render.link_wrap import PreMarkedText
from eawf.surfaces.render.units import format_tokens
from eawf.surfaces.tui.widgets.eu_bar import (
    DEFAULT_RENDER_MODE,
    RenderMode,
    render_eu_bar_plain,
)
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph

if TYPE_CHECKING:
    from eawf.kernel.state.models import SessionAttempt, State, Wave

logger = logging.getLogger(__name__)

#: The honest-absence line the cost tab renders when a wave has no metered
#: session -- no attempt joined a projected telemetry row, so there is no
#: cost to show. Surfacing this line (rather than an empty table or a
#: fabricated ``$0``) keeps the "surface now, data later" contract honest.
NO_METERED_SESSIONS: str = "no metered sessions yet"

#: The em-dash absence sentinel reused for an attempt the aggregator could
#: not price. Mirrors the detail-card em-dash convention so an absent cost
#: reads the same across the overlay. A real middle character (U+2014).
COST_ABSENT: str = "—"

#: The inert marker appended to an un-priced attempt's cost cell, so a row
#: the pricing snapshot could not bill is visibly un-billed rather than
#: silently zero. Pinned to the designer pin-strip (U1): the un-priced cell is
#: the em-dash sentinel plus this GLYPH (the ABANDONED circled-division-slash
#: from the ratified sigil alphabet), not the word "unbilled" -- a withheld
#: shape reads as a label without spelling one out. Sourced from the sigil home
#: so the alphabet stays single-owned; an ascii render still falls back through
#: the sigil column.
UNBILLED_MARKER: str = glyph(Sigil.ABANDONED, mode="unicode")

#: The dollar-figure precision for a per-attempt cost cell. Four decimal
#: places keep a sub-cent dispatch cost (per-token rates are fractions of a
#: cent) visible rather than rounding it away to ``$0.00``.
_COST_DP: int = 4

#: The cost-tab column order matching the success criterion: attempt id,
#: model, input/output tokens, cache create/read tokens, the priced cost,
#: and the runtime effort units.
_COST_COLUMNS: tuple[str, ...] = (
    "att",
    "model",
    "in",
    "out",
    "cache cr",
    "cache rd",
    "cost",
    "eu",
)


def attempt_is_priced(attempt: WaveAttemptRollup) -> bool:
    """Return whether *attempt* carries a real priced cost.

    An attempt is *priced* when its
    :attr:`~eawf.observability.telemetry.join.WaveAttemptRollup.cost_usd` is
    strictly positive -- the aggregator priced its billable tokens through
    the embedded snapshot. An attempt with billable tokens but a zero cost
    is *un-priced* (the model was missing from the snapshot), and an attempt
    with no billable tokens has nothing to bill; both fold onto ``False`` so
    the caller renders the em-dash + inert marker rather than a misleading
    ``$0``.

    Args:
        attempt: One joined per-attempt rollup row.

    Returns:
        ``True`` when the attempt has a positive priced cost, else ``False``.
    """
    return attempt.cost_usd > Decimal("0")


def _format_cost(cost: Decimal) -> str:
    """Return the ``$X.XXXX`` figure for a positive priced *cost*."""
    return f"${cost:.{_COST_DP}f}"


def _attempt_cost_cell(attempt: WaveAttemptRollup) -> str:
    """Return the cost cell for *attempt*: a figure, or the unbilled marker.

    Args:
        attempt: One joined per-attempt rollup row.

    Returns:
        The ``$X.XXXX`` figure when the attempt is priced, else the shared
        em-dash sentinel plus the inert :data:`UNBILLED_MARKER`.
    """
    if attempt_is_priced(attempt):
        return _format_cost(attempt.cost_usd)
    return f"{COST_ABSENT} {UNBILLED_MARKER}"


def _attempt_model(attempt: WaveAttemptRollup) -> str:
    """Return the model id for an attempt row's ``model`` column.

    The join carries the runtime id (the priced telemetry session does not
    surface a per-attempt model on the rollup), so the runtime stands in for
    the model column -- it is the billable identity the cost was charged
    against.

    Args:
        attempt: One joined per-attempt rollup row.

    Returns:
        The attempt's runtime id.
    """
    return attempt.runtime


def _attempt_eu(attempt: WaveAttemptRollup) -> str:
    """Return the effort-unit cell for an attempt row.

    Args:
        attempt: One joined per-attempt rollup row.

    Returns:
        The ``X.XX EU`` figure when the attempt joined a measured duration,
        else the em-dash sentinel (no measured runtime to convert).
    """
    if attempt.attention_eu is None:
        return COST_ABSENT
    return f"{attempt.attention_eu:.2f} EU"


def _attempt_cells(attempt: WaveAttemptRollup) -> tuple[str, ...]:
    """Return the eight ordered column cells for one attempt row."""
    return (
        str(attempt.attempt),
        _attempt_model(attempt),
        format_tokens(attempt.input_tokens),
        format_tokens(attempt.output_tokens),
        format_tokens(attempt.cache_write_tokens),
        format_tokens(attempt.cache_read_tokens),
        _attempt_cost_cell(attempt),
        _attempt_eu(attempt),
    )


def aggregate_cost_bar(rollup: WaveSessionRollup, *, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Render the aggregate cost bar: the priciest attempt against the total.

    The bar fills each attempt's share of the wave's total cost so the most
    expensive attempt reads as the consumed fraction against the aggregate
    total. There is no cost *budget* to bar against, so the total cost is
    the denominator and the single priciest attempt the numerator -- the bar
    answers "how concentrated is the spend" rather than "how much budget is
    left". An all-zero (or single-attempt) total surfaces the shared
    empty-state sentinel via :func:`render_eu_bar_plain`'s non-positive guard
    rather than a fabricated full bar.

    Args:
        rollup: The per-wave joined cost rollup.
        mode: The active render mode (``"unicode"`` / ``"ascii"``).

    Returns:
        The plain bar string, or the empty-state sentinel when the wave's
        aggregate cost is zero.
    """
    if len(rollup.attempts) < 2:
        # A single attempt is its own total, so priciest == total would paint a
        # fabricated 100% "concentration" bar that means nothing. Surface the
        # shared empty-state sentinel instead -- concentration only has meaning
        # once two or more attempts compete for the total.
        return render_eu_bar_plain(0.0, 0.0, mode=mode)
    total = float(rollup.cost_usd)
    priciest = max((float(row.cost_usd) for row in rollup.attempts), default=0.0)
    return render_eu_bar_plain(priciest, total, mode=mode)


#: The canonical model whose per-class pricing ratios weight the cache-adjusted
#: ``eff`` headline. The ratios (output ~5x an input token, a cache-read ~0.1x,
#: a cache-write ~1.25x) are family-stable, so ``eff`` reuses the metering
#: :class:`~eawf.observability.telemetry.pricing.ModelPricing` weights rather
#: than forking a second ratio table; the current default model anchors them.
_EFF_REFERENCE_MODEL: str = "claude-opus-4-8"


def effective_tokens(rollup: WaveSessionRollup) -> int | None:
    """Return the cache-adjusted effective input-token-equivalent for a rollup.

    Weights each summed per-class token count by the SAME metering pricing
    ratios the ``$`` figure uses (output costs ~5x an input token, a cache-read
    ~0.1x, a cache-write ~1.25x), normalised to input-token-equivalents. Cheap
    cache reads contribute little and expensive output much, so ``eff`` reads as
    the wave's effective token spend rather than a flat raw sum. The single
    cache-write tier weights against the 5-minute rate (the rollup carries no
    TTL split, mirroring the adapter's conservative prior).

    Args:
        rollup: The per-wave joined cost rollup.

    Returns:
        The rounded effective input-token-equivalent, or ``None`` when the
        reference pricing row cannot be resolved (no ratios to weight against).
    """
    pricing = lookup_pricing(_EFF_REFERENCE_MODEL)
    if pricing is None or pricing.input_per_token <= 0:
        return None
    base = float(pricing.input_per_token)
    eff = (
        rollup.input_tokens
        + rollup.output_tokens * float(pricing.output_per_token) / base
        + rollup.cache_write_tokens * float(pricing.cache_write_5m_per_token) / base
        + rollup.cache_read_tokens * float(pricing.cache_read_per_token) / base
    )
    return round(eff)


def _blended_token_rows(rollup: WaveSessionRollup) -> tuple[tuple[str, str], ...]:
    """Return the ``eff`` headline + per-class breakdown rows for a rollup.

    The blended token view: a cache-adjusted ``eff`` headline (no raw-sum
    headline) plus the raw per-class breakdown (in / out / cache-write /
    cache-read). Empty when ``eff`` cannot be derived so the tab degrades to the
    per-attempt table alone rather than a fabricated figure.

    Args:
        rollup: The per-wave joined cost rollup.

    Returns:
        Two ``(label, value)`` rows (the ``eff`` headline + the breakdown), or
        an empty tuple when ``eff`` is unresolvable.
    """
    eff = effective_tokens(rollup)
    if eff is None:
        return ()
    breakdown = (
        f"in {format_tokens(rollup.input_tokens)}  "
        f"out {format_tokens(rollup.output_tokens)}  "
        f"cache cr {format_tokens(rollup.cache_write_tokens)}  "
        f"cache rd {format_tokens(rollup.cache_read_tokens)}"
    )
    return (
        ("eff", format_tokens(eff)),
        ("tokens", breakdown),
    )


def cost_tab_rows(
    rollup: WaveSessionRollup, *, mode: RenderMode = DEFAULT_RENDER_MODE
) -> tuple[tuple[str, str], ...]:
    """Build the ``$`` (cost) tab ``(label, value)`` rows for a wave rollup.

    Surfaces the columnar per-attempt cost table (attempt id, model,
    in/out tokens, cache create/read tokens, priced cost, effort units) plus
    an aggregate cost bar. A wave whose attempts join no telemetry session
    (an empty rollup) yields a single honest :data:`NO_METERED_SESSIONS`
    row rather than an empty table.

    Args:
        rollup: The per-wave joined cost rollup.
        mode: The active render mode (``"unicode"`` / ``"ascii"``) the
            aggregate bar honours.

    Returns:
        Ordered ``(label, value)`` rows for the cost tab; a single
        ``sessions`` row carrying :data:`NO_METERED_SESSIONS` when the wave
        has no metered attempt.
    """
    if not rollup.attempts:
        return (("sessions", NO_METERED_SESSIONS),)
    raw_rows = [_attempt_cells(attempt) for attempt in rollup.attempts]
    widths = [len(col) for col in _COST_COLUMNS]
    for raw in raw_rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, raw, strict=True)]
    # The header rides the metrics-title accent-bold convention; the value
    # therefore carries its own markup (PreMarkedText opts out of the
    # detail overlay's escaping) with the data lines escaped here instead.
    header = f"[$accent][b]{escape(_format_cost_row(_COST_COLUMNS, widths))}[/][/]"
    table = [header]
    table.extend(escape(_format_cost_row(raw, widths)) for raw in raw_rows)
    rendered = PreMarkedText("\n" + "\n".join(f"  {line}" for line in table))
    return (
        ("attempts", rendered),
        ("total", _aggregate_total_cell(rollup)),
        ("cost", aggregate_cost_bar(rollup, mode=mode)),
        *_blended_token_rows(rollup),
    )


def _aggregate_total_cell(rollup: WaveSessionRollup) -> str:
    """Return the aggregate-total cost cell for the cost tab.

    Args:
        rollup: The per-wave joined cost rollup.

    Returns:
        The summed ``$X.XXXX`` figure, or the em-dash sentinel plus the
        inert :data:`UNBILLED_MARKER` when the aggregate priced to zero (no
        attempt carried a billable, priceable cost).
    """
    if rollup.cost_usd > Decimal("0"):
        return _format_cost(rollup.cost_usd)
    return f"{COST_ABSENT} {UNBILLED_MARKER}"


def _format_cost_row(row: tuple[str, ...], widths: list[int]) -> str:
    """Format one cost-table row with left-justified padded columns."""
    cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=True)]
    return "  ".join(cells)


def aggregate_session_cost(sessions: list[TelemetrySession]) -> tuple[Decimal, int]:
    """Sum the priced cost across *sessions* and count the metered rows.

    The shared cost aggregation the ``/metrics`` Cost tile reads, summing the
    same priced
    :attr:`~eawf.observability.telemetry.models.TelemetrySession.total_cost_usd`
    figure the wave-detail ``$`` tab quotes per attempt -- so the tile and the
    tab agree on every dollar.

    Args:
        sessions: The metered telemetry session rows to aggregate.

    Returns:
        A ``(total_cost, sample_count)`` pair: the summed priced cost and the
        count of sessions the sum spans (``Decimal("0"), 0`` for an empty
        list, which the tile folds onto the honest absence line).
    """
    total = sum((session.total_cost_usd for session in sessions), Decimal("0"))
    return total, len(sessions)


def render_cost_tile(total_cost: Decimal, *, sample_count: int) -> str:
    """Render the ``/metrics`` Cost tile body from an aggregate cost.

    Matches the ``$`` tab's aggregate: the dashboard Cost tile sums the same
    priced ``cost_usd`` figure across the windowed telemetry sessions. A
    window with no priced session renders the honest
    :data:`NO_METERED_SESSIONS` line (never a fabricated ``$0``).

    Args:
        total_cost: The summed priced cost across the windowed sessions.
        sample_count: The count of metered sessions the sum spans.

    Returns:
        A two-line tile body (``total $X.XXXX`` + ``sessions N``), or the
        honest absence line when no session was metered.
    """
    if sample_count <= 0:
        return NO_METERED_SESSIONS
    return "\n".join([f"total {_format_cost(total_cost)}", f"sessions {sample_count}"])


def wave_cost_rows(
    wave: Wave,
    cost_rollup: WaveSessionRollup | None,
) -> tuple[tuple[str, str], ...]:
    """Build the ``cost`` tab rows for a wave from its joined cost rollup.

    A wave with no session attempts has no metering surface at all, so the
    group is left empty and the modal builds no cost tab. A wave that
    carries session attempts always builds the tab: with a joined rollup it
    renders the per-attempt cost columns + aggregate bar, and with no joined
    telemetry (``cost_rollup`` is ``None`` or carries no matched attempt) it
    renders the honest :data:`NO_METERED_SESSIONS` line -- the attempts exist
    but none priced.

    Args:
        wave: The resolved wave.
        cost_rollup: The wave's joined per-attempt cost rollup, or ``None``
            when no telemetry DB is reachable.

    Returns:
        The cost tab's ``(label, value)`` rows; ``()`` for a wave with no
        session attempts so the modal builds no cost tab.
    """
    if not wave.sessions:
        return ()
    if cost_rollup is None:
        cost_rollup = WaveSessionRollup(wave_id=wave.id)
    return cost_tab_rows(cost_rollup)


def wave_cost_rollup_for_wave(
    state: State,
    wave_id: str,
    state_path: Path,
) -> WaveSessionRollup | None:
    """Join the wave's priced cost from the metrics DB, else the runtime snapshot.

    Loads the projected :class:`~eawf.observability.telemetry.models.TelemetrySession`
    rows from the local telemetry DB and joins them back to the wave's
    :class:`~eawf.kernel.state.models.SessionAttempt` rows through the
    canonical :func:`~eawf.observability.telemetry.join.rollup_wave_sessions`
    join, so the cost tab quotes the same priced ``cost_usd`` figure the
    close-time telemetry rollup does.

    When no telemetry session joins -- a headless spawn stamps its priced cost
    onto the wave runtime snapshot (``runtime_latest``) rather than a
    per-runtime session log the telemetry projector parses, and a fresh repo
    may carry no metrics DB at all -- the join falls back to
    :func:`_runtime_snapshot_rollup`, which surfaces that stored cost instead
    of folding to the honest-absence line for a cost that genuinely exists. A
    wave with neither a metered session nor a captured runtime cost still
    yields the empty rollup so the honest-absence line renders.

    Args:
        state: The bound state holding the wave table.
        wave_id: The selected wave id.
        state_path: The path the local telemetry DB is resolved from.

    Returns:
        The joined per-wave cost rollup (telemetry-session-priced when
        available, else the runtime-snapshot cost), or ``None`` when the wave
        is unknown.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        return None
    telemetry_rollup = _telemetry_session_rollup(wave, state_path)
    if telemetry_rollup is not None and telemetry_rollup.attempts:
        return telemetry_rollup
    runtime_rollup = _runtime_snapshot_rollup(wave)
    return runtime_rollup if runtime_rollup is not None else telemetry_rollup


def _telemetry_session_rollup(wave: Wave, state_path: Path) -> WaveSessionRollup | None:
    """Return the wave's telemetry-session cost rollup, or ``None`` when absent.

    Reads the projected ``telemetry_sessions`` rows from the local metrics DB
    and joins them to the wave's session attempts. A missing DB (a fresh repo)
    or any read failure yields ``None`` so the caller falls back to the runtime
    snapshot rather than crashing the drill-in seam.
    """
    db_path = metrics_db_path(state_path)
    if not db_path.is_file():
        return None
    store = open_store("sqlite", db_path)
    try:
        rows = store.fetch_all("telemetry_sessions", TelemetrySession)
    except Exception as exc:
        logger.debug(f"_telemetry_session_rollup fallback wave={wave.id!r} cause={exc!r}")
        return None
    finally:
        store.close()
    sessions = [row for row in rows if isinstance(row, TelemetrySession)]
    return rollup_wave_sessions(wave, sessions, eu_minutes=DEFAULT_EU_MINUTES)


def _runtime_snapshot_rollup(wave: Wave) -> WaveSessionRollup | None:
    """Build a cost rollup from the wave's stored spawn cost (no telemetry DB).

    A headless spawn's priced cost never reaches the telemetry projector, so
    the tab would fold to :data:`NO_METERED_SESSIONS` despite a real, stored
    cost. Two stored sources back the fallback, in priority order:

    - **Per-attempt** (:attr:`~eawf.kernel.state.models.SessionAttempt.cost_usd`):
      each genuine dispatch attempt stamps its own priced cost, so every
      attempt row carries its OWN figure and the aggregate is their sum. This
      is the honest per-attempt view for a wave dispatched more than once.
    - **Wave snapshot** (``runtime_baseline`` / ``runtime_latest``): the legacy
      single whole-wave delta for a wave whose attempts predate per-attempt
      capture. Only the latest attempt is priced from it; earlier attempts
      render un-priced (so every dispatch still shows, honestly marked as not
      separately metered).

    Returns ``None`` when no cost is captured either way (so a genuinely
    un-metered wave folds to the honest-absence line) -- the surfaced cost is
    always a real stored figure, never a fabricated zero.
    """
    from eawf.workflow.lifecycle.wave import compute_runtime_delta

    if not wave.sessions:
        return None
    # Per-attempt path: any attempt carrying its own priced cost wins.
    if any(sess.cost_usd is not None for sess in wave.sessions.values()):
        rows = [_attempt_row_from_session(no, wave.sessions[no]) for no in sorted(wave.sessions)]
        return _aggregate_rows(wave.id, rows)
    # Legacy wave-snapshot fallback: one delta, priced onto the latest attempt.
    delta = compute_runtime_delta(
        wave.runtime_baseline,
        wave.runtime_latest,
        carry=wave.runtime_carry,
        eu_minutes=DEFAULT_EU_MINUTES,
    )
    if delta is None or delta.actual_cost_usd <= 0.0:
        return None
    latest_no = max(wave.sessions)
    baseline = wave.runtime_baseline
    latest = wave.runtime_latest

    def _token_delta(name: str) -> int:
        base = getattr(baseline, name, 0) or 0
        now = getattr(latest, name, 0) or 0
        return max(0, int(now) - int(base))

    rows = []
    for attempt_no in sorted(wave.sessions):
        attempt = wave.sessions[attempt_no]
        priced = attempt_no == latest_no
        rows.append(
            WaveAttemptRollup(
                attempt=attempt_no,
                runtime=attempt.runtime,
                session_id=attempt.session_id,
                duration_ms=(delta.api_duration_ms or None) if priced else None,
                attention_eu=delta.elapsed_eu if priced else None,
                input_tokens=_token_delta("input_tokens") if priced else 0,
                output_tokens=_token_delta("output_tokens") if priced else 0,
                cache_read_tokens=_token_delta("cache_read_input_tokens") if priced else 0,
                cache_write_tokens=_token_delta("cache_creation_input_tokens") if priced else 0,
                cost_usd=Decimal(str(delta.actual_cost_usd)) if priced else Decimal("0"),
            )
        )
    return _aggregate_rows(wave.id, rows)


def _attempt_row_from_session(attempt_no: int, sess: SessionAttempt) -> WaveAttemptRollup:
    """Build one cost row from a session attempt's OWN captured figures."""
    duration_ms: int | None = None
    attention_eu: float | None = None
    if sess.ended_at is not None:
        duration_ms = max(0, int((sess.ended_at - sess.started_at).total_seconds() * 1000))
        attention_eu = (duration_ms / 60_000.0) / DEFAULT_EU_MINUTES
    return WaveAttemptRollup(
        attempt=attempt_no,
        runtime=sess.runtime,
        session_id=sess.session_id,
        duration_ms=duration_ms,
        attention_eu=attention_eu,
        input_tokens=sess.input_tokens or 0,
        output_tokens=sess.output_tokens or 0,
        cache_read_tokens=sess.cache_read_input_tokens or 0,
        cache_write_tokens=sess.cache_creation_input_tokens or 0,
        cost_usd=Decimal(str(sess.cost_usd)) if sess.cost_usd is not None else Decimal("0"),
    )


def _aggregate_rows(wave_id: str, rows: list[WaveAttemptRollup]) -> WaveSessionRollup:
    """Fold per-attempt rows into a wave rollup summing every attempt's figures."""
    return WaveSessionRollup(
        wave_id=wave_id,
        attempts=rows,
        duration_ms=sum((r.duration_ms or 0 for r in rows), 0) or None,
        attention_eu=sum((r.attention_eu or 0.0 for r in rows), 0.0) or None,
        input_tokens=sum(r.input_tokens for r in rows),
        output_tokens=sum(r.output_tokens for r in rows),
        cache_read_tokens=sum(r.cache_read_tokens for r in rows),
        cache_write_tokens=sum(r.cache_write_tokens for r in rows),
        cost_usd=sum((r.cost_usd for r in rows), Decimal("0")),
    )


__all__ = [
    "COST_ABSENT",
    "NO_METERED_SESSIONS",
    "UNBILLED_MARKER",
    "aggregate_cost_bar",
    "aggregate_session_cost",
    "attempt_is_priced",
    "cost_tab_rows",
    "render_cost_tile",
    "wave_cost_rollup_for_wave",
    "wave_cost_rows",
]
