"""Fold a root Run's delegation subtree into one account of its provider usage.

Each Run records only its own share: the difference between its stop reading and its
baseline, divided by the Runs sharing its vendor session. A provider's counters are
cumulative per session, so the subtree's usage is read per source -- each vendor session
counted once, from the earliest baseline any Run took of it to the latest stop -- and the
Runs' shares are then checked against it. Summing the shares alone would hide usage no
Run was measured for; summing each Run's cumulative reading would count a session's
history once per Run that read it.

The fold publishes the provider total, the inherited baseline, the accepted step total,
and the root's and descendants' shares side by side, with the residual between them. A
residual that is not zero means some usage of the subtree's sources was not accepted as
any of its Runs' steps -- a source shared with a Run outside the subtree, or a stretch
of a session no Run was running for -- and it is a reconciliation failure, not rounding.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated

from pydantic import Field

from eawf.kernel.state.epoch2.base import Epoch2Model
from eawf.kernel.state.epoch2.measurement import (
    CounterName,
    CounterSnapshot,
    MeasuredRuntime,
    Observed,
    Unobserved,
)
from eawf.kernel.state.epoch2.run import Run

logger = logging.getLogger(__name__)

_Amount = Annotated[Decimal, Field(ge=0)]


class CounterFold(Epoch2Model):
    """One counter of a subtree, as its sources report it and as its Runs accepted it.

    Attributes:
        provider_total: Each source's latest cumulative reading, summed.
        inherited_baseline: Each source's earliest baseline, summed: what the
            sources had counted before any Run of the subtree started.
        accepted_step_total: The root's share and the descendants' shares.
        root_share: The root Run's own share.
        descendant_share: Every descendant's share, summed.
        residual: ``provider_total - inherited_baseline - accepted_step_total``.
    """

    provider_total: _Amount
    inherited_baseline: _Amount
    accepted_step_total: _Amount
    root_share: _Amount
    descendant_share: _Amount
    residual: Decimal


class SubtreeFold(Epoch2Model):
    """A root Run and its descendants, folded per counter.

    Attributes:
        root_key: The root Run.
        descendant_keys: Every Run under it, in key order.
        sources: How many distinct vendor sessions the measured Runs read.
        unmeasured_keys: The Runs whose capture yielded no measured share, so
            they contribute to neither side of the fold.
        counters: Each counter's fold, or why it cannot be folded.
    """

    root_key: str
    descendant_keys: tuple[str, ...]
    sources: int
    unmeasured_keys: tuple[str, ...]
    counters: Mapping[CounterName, CounterFold | Unobserved]

    @property
    def unreconciled(self) -> tuple[CounterName, ...]:
        """The counters whose residual is not zero."""
        return tuple(
            name
            for name, fold in self.counters.items()
            if isinstance(fold, CounterFold) and fold.residual != 0
        )


@dataclass(frozen=True, slots=True)
class _Measured:
    """One Run that was measured, and the vendor session it was measured through."""

    key: str
    source: str
    baseline: CounterSnapshot
    captured: MeasuredRuntime


@dataclass(frozen=True, slots=True)
class _Step:
    """One measured Run's reading of one counter."""

    key: str
    source: str
    baseline: Decimal
    share: Decimal
    divisor: int

    @property
    def stop(self) -> Decimal:
        """The source's cumulative reading when the Run stopped."""
        return self.baseline + self.share * self.divisor


def _descendants(root: Run, runs: Sequence[Run]) -> tuple[Run, ...]:
    """Return every Run under *root*, in key order."""
    children: dict[str, list[Run]] = {}
    for run in runs:
        if run.parent_run_ref is not None:
            children.setdefault(str(run.parent_run_ref), []).append(run)
    found: list[Run] = []
    frontier = list(children.get(str(root.urn), ()))
    while frontier:
        run = frontier.pop()
        found.append(run)
        frontier.extend(children.get(str(run.urn), ()))
    return tuple(sorted(found, key=lambda run: run.key))


def _fold_counter(
    name: CounterName, measured: Sequence[_Measured], root_key: str
) -> CounterFold | Unobserved:
    """Fold one counter over the measured Runs, or say why it cannot be."""
    steps: list[_Step] = []
    for run in measured:
        start, share = run.baseline.counters[name], run.captured.counters[name]
        if not isinstance(start, Observed) or not isinstance(share, Observed):
            return Unobserved(reason=f"{run.key} did not observe {name.value}")
        steps.append(
            _Step(
                key=run.key,
                source=run.source,
                baseline=start.value,
                share=share.value,
                divisor=run.captured.divisor,
            )
        )
    by_source: dict[str, list[_Step]] = {}
    for step in steps:
        by_source.setdefault(step.source, []).append(step)
    provider_total = sum((max(s.stop for s in group) for group in by_source.values()), Decimal(0))
    inherited = sum((min(s.baseline for s in group) for group in by_source.values()), Decimal(0))
    root_share = sum((step.share for step in steps if step.key == root_key), Decimal(0))
    descendant_share = sum((step.share for step in steps if step.key != root_key), Decimal(0))
    accepted = root_share + descendant_share
    return CounterFold(
        provider_total=provider_total,
        inherited_baseline=inherited,
        accepted_step_total=accepted,
        root_share=root_share,
        descendant_share=descendant_share,
        residual=provider_total - inherited - accepted,
    )


def fold_subtree(root: Run, runs: Sequence[Run]) -> SubtreeFold | None:
    """Fold *root* and every Run it delegated, directly or not.

    Args:
        root: The root Run.
        runs: Every Run the tree holds; those outside the subtree are ignored.

    Returns:
        The fold, or ``None`` when *root* delegated nothing, so there is no
        subtree to reconcile beyond its own capture.
    """
    descendants = _descendants(root, runs)
    if not descendants:
        return None
    measured: list[_Measured] = []
    unmeasured: list[str] = []
    for run in (root, *descendants):
        session, baseline, captured = run.vendor_session, run.counter_baseline, run.captured_runtime
        if (
            session is not None
            and isinstance(baseline, CounterSnapshot)
            and isinstance(captured, MeasuredRuntime)
        ):
            measured.append(_Measured(run.key, session.session_digest, baseline, captured))
        else:
            unmeasured.append(run.key)
    fold = SubtreeFold(
        root_key=root.key,
        descendant_keys=tuple(run.key for run in descendants),
        sources=len({run.source for run in measured}),
        unmeasured_keys=tuple(unmeasured),
        counters={name: _fold_counter(name, measured, root.key) for name in CounterName},
    )
    if fold.unreconciled:
        unreconciled = ",".join(name.value for name in fold.unreconciled)
        logger.warning(f"fold_subtree root={root.key} unreconciled={unreconciled}")
    return fold


__all__ = ["CounterFold", "SubtreeFold", "fold_subtree"]
