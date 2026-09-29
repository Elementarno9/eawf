"""Wave effort roll-up and realized-EU accessors.

Every wave is costed at one effort constant, :data:`EFFORT_EU`. The five
size labels it replaced were measured non-monotonic, with a declared span
of 14x against a measured 1.31x, and no size proxy separated them, so a
label left on a wave is a narrative annotation: nothing multiplies by it
and no schedule or capacity figure derives from it. With no observable to
re-fit against, the per-label calibration retired with the ladder.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from eawf.kernel.state.models import ActualSummary, State, Wave

#: Effort of one wave in EU, whatever size label it carries.
EFFORT_EU: Final[float] = 0.8

#: Minutes of agent-driven session time in one EU.
EU_MINUTES: Final[float] = 30.0

#: :data:`EFFORT_EU` in minutes -- the expected duration of one wave.
EFFORT_MINUTES: Final[float] = EFFORT_EU * EU_MINUTES

#: The measured dispersion of per-wave effort, in minutes. The p90 is the
#: pessimistic budget a wave without an explicit estimate is held to.
EFFORT_DISPERSION_MINUTES: Final[Mapping[str, float]] = MappingProxyType(
    {"p10": 8.8, "p50": 23.9, "p90": 123.1}
)


def sum_wave_eu(waves: list[Wave]) -> float:
    """Return the serial effort of *waves* in EU.

    Args:
        waves: The waves to sum.

    Returns:
        ``len(waves) * EFFORT_EU``, rounded to 2 dp.
    """
    return round(len(waves) * EFFORT_EU, 2)


def critical_path_eu(waves: list[Wave]) -> float:
    """Return the effort of the longest dependency chain through *waves*, in EU.

    A dependency outside *waves* is ignored, and a cycle is cut where it
    closes rather than followed forever.

    Args:
        waves: The waves forming one dependency graph.

    Returns:
        The longest chain's wave count times :data:`EFFORT_EU`, rounded to
        2 dp; ``0.0`` for no waves.
    """
    by_id = {wave.id: wave for wave in waves}
    memo: dict[str, int] = {}

    def _depth(wave: Wave, seen: set[str]) -> int:
        if wave.id in memo:
            return memo[wave.id]
        if wave.id in seen:
            return 1
        dep_depths = [_depth(by_id[dep], seen | {wave.id}) for dep in wave.deps if dep in by_id]
        depth = 1 + (max(dep_depths) if dep_depths else 0)
        memo[wave.id] = depth
        return depth

    if not waves:
        return 0.0
    return round(max(_depth(wave, set()) for wave in waves) * EFFORT_EU, 2)


def resolve_wave_actual(state: State, wave_id: str) -> ActualSummary | None:
    """Resolve a wave's :class:`ActualSummary` — the single resolution path.

    This is the **one** place that maps a wave id onto its actual row, so
    every realized-EU reader (the :func:`actual_eu_for_wave` /
    :func:`actual_eu_for_iter` / :func:`actual_eu_for_phase` accessors, plus
    any caller that needs the full row for the pessimistic bound or existence
    check) shares one resolution semantics instead of re-deriving it.

    Resolution handles both keying conventions that appear in ``state.actuals``
    on disk: an actual stored under its wave-id dict key, **and** an actual
    whose dict key differs but whose ``scope_id`` is the wave id. The dict-key
    match wins when both are present (it is the canonical key); the
    ``scope_id`` scan is the fallback. Returns ``None`` when neither resolves.

    Args:
        state: Loaded typed :class:`State` snapshot (read-only).
        wave_id: The wave whose actual is wanted.

    Returns:
        The resolved :class:`ActualSummary`, or ``None`` when *wave_id* has no
        actual under either keying convention.
    """
    actuals = state.actuals or {}
    direct = actuals.get(wave_id)
    if direct is not None:
        return direct
    for actual in actuals.values():
        if actual.scope_id == wave_id:
            return actual
    return None


def actual_eu_for_wave(state: State, wave_id: str) -> float:
    """Return realized EU for one wave — the single realized-EU accessor.

    Reads ``elapsed_eu`` off the actual resolved by :func:`resolve_wave_actual`
    (the realized-EU counterpart to :data:`EFFORT_EU` on the planned side).
    A wave with no matching actual contributes ``0.0`` — there is no
    realized effort to report yet.

    Args:
        state: Loaded typed :class:`State` snapshot (read-only).
        wave_id: The wave whose realized EU is wanted.

    Returns:
        The wave's ``ActualSummary.elapsed_eu``, or ``0.0`` when no actual
        resolves to *wave_id*.
    """
    actual = resolve_wave_actual(state, wave_id)
    return actual.elapsed_eu if actual is not None else 0.0


def actual_eu_for_iter(state: State, iter_id: str) -> float:
    """Return realized EU summed across an iter's waves.

    Sums :func:`actual_eu_for_wave` over every wave whose ``iter_id`` matches
    *iter_id*. An iter with no waves, or whose waves carry no actuals, returns
    ``0.0``.

    Args:
        state: Loaded typed :class:`State` snapshot (read-only).
        iter_id: The iter whose realized EU is wanted.

    Returns:
        The summed realized EU across the iter's waves, rounded to 2 dp.
    """
    total = sum(
        actual_eu_for_wave(state, wave.id)
        for wave in state.waves.values()
        if wave.iter_id == iter_id
    )
    return round(total, 2)


def actual_eu_for_phase(state: State, phase_id: str) -> float:
    """Return realized EU summed across a phase's waves.

    Resolves the phase's iters (``Iter.phase_id == phase_id``) and sums
    :func:`actual_eu_for_wave` over every wave under those iters. A phase with
    no waves, or whose waves carry no actuals, returns ``0.0``.

    This is the accessor a phase-scoped EU / status pane reads for its
    consumed-EU numerator; the return shape (a plain ``float`` keyed by phase
    id) is a drop-in for an inline ``sum(elapsed_eu ...)`` so the consumer
    needs no reshape.

    Args:
        state: Loaded typed :class:`State` snapshot (read-only).
        phase_id: The phase whose realized EU is wanted.

    Returns:
        The summed realized EU across the phase's waves, rounded to 2 dp.
    """
    phase_iter_ids = {
        iter_row.id for iter_row in state.iters.values() if iter_row.phase_id == phase_id
    }
    total = sum(
        actual_eu_for_wave(state, wave.id)
        for wave in state.waves.values()
        if wave.iter_id in phase_iter_ids
    )
    return round(total, 2)
