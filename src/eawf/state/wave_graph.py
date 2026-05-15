"""Typed Wave DAG accessors (P20-W15 / B026).

Exposes the Wave dependency graph to downstream consumers (the TUI
wave-board in W03, the ``eawf wave graph`` CLI, future automation) as
typed tuple-returning helpers so callers can read the edges off the
state model in O(1) per wave from a single call site — no inline
walk over sibling ``Wave.deps`` lists at every consumer.

Three edge views are exposed:

- :func:`deps` — sorted tuple of waves THIS wave depends on (static
  plan-time predecessors; mirrors :attr:`Wave.deps` but as an
  immutable sorted tuple).
- :func:`blocks` — sorted tuple of waves THIS wave blocks (static
  reverse-index; mirrors :attr:`Wave.blocks` but as an immutable
  sorted tuple).
- :func:`blocked_by` — runtime live view: deps that are not yet
  ``CLOSED``. Shrinks as deps close. This is the typed surface the
  TUI wave-board renders to highlight what is actively blocking each
  pending wave right now.

Plus :func:`edges` returns a typed :class:`WaveDagEdges` record so
consumers get all three views with one call.

Design decision: derived helpers, no new state field. ``Wave.deps``
and ``Wave.blocks`` already persist the static graph; adding a
runtime-mirror field on ``Wave`` invites drift (every close mutation
would have to fan out to child waves' ``blocked_by``). Derivation
keeps the storage minimal and the runtime view authoritative.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from eawf.state.enums import WaveStatus
from eawf.state.models import State, WaveIdStr

logger = logging.getLogger(__name__)


class WaveDagEdges(BaseModel):
    """Typed view of one wave's DAG edges.

    ``deps`` is the static predecessor set; ``blocks`` is the static
    reverse-index; ``blocked_by`` is the live runtime subset of deps
    whose own status is not ``CLOSED``. All three are sorted tuples
    so consumers may rely on stable iteration order and immutability.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    wave_id: WaveIdStr
    deps: tuple[WaveIdStr, ...]
    blocks: tuple[WaveIdStr, ...]
    blocked_by: tuple[WaveIdStr, ...]


def deps(wave_id: str, state: State) -> tuple[WaveIdStr, ...]:
    """Return *wave_id*'s static predecessor set as a sorted tuple.

    Args:
        wave_id: Wave id (e.g. ``"P01-I01-W02"``).
        state: Validated :class:`State` document.

    Returns:
        Sorted tuple of wave ids that *wave_id* declares as deps.

    Raises:
        KeyError: when *wave_id* is not in ``state.waves``.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise KeyError(f"unknown wave: {wave_id!r}")
    return tuple(sorted(wave.deps))


def blocks(wave_id: str, state: State) -> tuple[WaveIdStr, ...]:
    """Return *wave_id*'s persisted forward reverse-index as a sorted tuple.

    Mirrors :attr:`Wave.blocks` but returned sorted + immutable so
    consumers may iterate deterministically. The forward index is
    maintained by lifecycle mutators (``plan_wave``, ``set_wave_deps``,
    ``remove_wave_plan``) and rebuildable via
    ``eawf wave blocks-rebuild`` when state ages predate the index.

    Args:
        wave_id: Wave id (e.g. ``"P01-I01-W01"``).
        state: Validated :class:`State` document.

    Returns:
        Sorted tuple of wave ids that declare *wave_id* as a dep.

    Raises:
        KeyError: when *wave_id* is not in ``state.waves``.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise KeyError(f"unknown wave: {wave_id!r}")
    return tuple(sorted(wave.blocks))


def blocked_by(wave_id: str, state: State) -> tuple[WaveIdStr, ...]:
    """Return *wave_id*'s LIVE blocked-by view: deps not yet ``CLOSED``.

    Differs from :func:`deps` which returns the static predecessor
    set: this view shrinks as predecessor waves close. Missing dep
    references are silently skipped — referential drift is reported
    separately by :func:`eawf.validate.invariants.check_parent_ids`.

    Args:
        wave_id: Wave id to inspect.
        state: Validated :class:`State` document.

    Returns:
        Sorted tuple of wave ids that currently block *wave_id* from
        becoming ready (their status is not ``WaveStatus.CLOSED``).

    Raises:
        KeyError: when *wave_id* is not in ``state.waves``.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise KeyError(f"unknown wave: {wave_id!r}")
    live: list[str] = []
    for dep_id in wave.deps:
        dep_wave = state.waves.get(dep_id)
        if dep_wave is None:
            # Referential drift — skip; check_parent_ids surfaces it.
            continue
        if dep_wave.status != WaveStatus.CLOSED:
            live.append(dep_id)
    return tuple(sorted(live))


def edges(wave_id: str, state: State) -> WaveDagEdges:
    """Return a typed :class:`WaveDagEdges` with all three views.

    Single-call-site accessor for consumers that need both directions
    of the DAG plus the runtime blocked-by view (e.g. the TUI
    wave-board in W03 renders all three per row).

    Args:
        wave_id: Wave id to inspect.
        state: Validated :class:`State` document.

    Returns:
        :class:`WaveDagEdges` instance with ``deps``, ``blocks``, and
        ``blocked_by`` populated.

    Raises:
        KeyError: when *wave_id* is not in ``state.waves``.
    """
    if wave_id not in state.waves:
        raise KeyError(f"unknown wave: {wave_id!r}")
    return WaveDagEdges(
        wave_id=wave_id,
        deps=deps(wave_id, state),
        blocks=blocks(wave_id, state),
        blocked_by=blocked_by(wave_id, state),
    )


def edges_for_iter(iter_id: str, state: State) -> dict[str, WaveDagEdges]:
    """Return :class:`WaveDagEdges` for every wave under *iter_id*.

    Iteration-scoped helper for the TUI wave-board (W03) which renders
    one iter at a time. Result is keyed by wave id; missing iter
    returns an empty mapping (a separate validator surfaces dangling
    iter pointers).

    Args:
        iter_id: Iter id (e.g. ``"P01-I01"``).
        state: Validated :class:`State` document.

    Returns:
        Mapping from wave id to :class:`WaveDagEdges`, filtered to
        waves whose ``iter_id`` matches *iter_id*.
    """
    out: dict[str, WaveDagEdges] = {}
    for wid, w in state.waves.items():
        if w.iter_id != iter_id:
            continue
        out[wid] = edges(wid, state)
    logger.info(f"edges_for_iter iter={iter_id} count={len(out)}")
    return out


__all__ = [
    "WaveDagEdges",
    "blocked_by",
    "blocks",
    "deps",
    "edges",
    "edges_for_iter",
]
