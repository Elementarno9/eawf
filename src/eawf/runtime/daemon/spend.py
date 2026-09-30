"""What a root has spent against its governor, and what one Run has spent against its caps.

Both answers are folds over the run ledger and nothing else: a Run's spend is what its
own ``usage_observed`` lines add up to, a Run's caps are the ones its binding sealed,
and a stop is a budget notice whose control effect was confirmed. Nothing is counted
twice -- a reading is folded by :func:`~eawf.kernel.runtime.usage.aggregate_usage`,
which keeps the largest running total -- and nothing unread is stated as zero: a cost
no reading priced stays ``None``, which the console renders unmetered.

A stop the ledger cannot vouch for -- a notice whose control no confirmed effect
answers -- is reported as not confirmed rather than as stopped, because a notice alone
proves the crossing and not the reap.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from eawf.kernel.economics.governor import AdmissionAxis, InFlightGovernor
from eawf.kernel.economics.spend import (
    CostCeilingView,
    Pricing,
    ProviderSpend,
    RunUsageView,
    StoppedRun,
)
from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.budget_notice import BudgetNotice
from eawf.kernel.runtime.control import TERMINAL_RUN_STATUSES, ControlPhase
from eawf.kernel.runtime.usage import UsagePayload, UsageTotals, aggregate_usage
from eawf.kernel.state.epoch2.run import Run, RunStatus
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import LedgerRecord, effective_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.control.reducer import reduce_run_control
from eawf.runtime.daemon.admission import in_flight_reservations
from eawf.runtime.daemon.native_dispatch import control_facts_of, run_binding_of
from eawf.runtime.daemon.run_events import run_events_of

logger = logging.getLogger(__name__)


#: A reading's quality, from the most to the least trusted.
_QUALITY_RANK: Final[Mapping[str, int]] = {
    "measured": 0,
    "derived": 1,
    "estimated": 2,
    "unavailable": 3,
}

#: The provider a Run is attributed to when nothing on the ledger names one.
UNATTRIBUTED: Final = "unattributed"


def _runs(document: Mapping[str, object], records: Sequence[LedgerRecord]) -> dict[str, Run]:
    """Return every Run the tree holds, live ones from the document, ended ones compacted."""
    held: dict[str, Run] = {}
    for item in effective_records(tuple(records)):
        if item.record_key.startswith("RUN-") and "payload_kind" not in item.payload:
            held[item.record_key] = Run.model_validate(item.payload)
    for key, row in document_rows(dict(document), Epoch2Collection.RUN).items():
        held[key] = Run.model_validate(row)
    return held


def _status(run: Run, records: Sequence[LedgerRecord]) -> RunStatus:
    """Return the Run's status as its control effects support it."""
    return reduce_run_control(
        status=run.status, facts=control_facts_of(tuple(records), run.urn)
    ).status


def _readings(records: Sequence[LedgerRecord], urn: QualifiedUrn) -> tuple[UsagePayload, ...]:
    """Return the Run's derived usage readings."""
    return tuple(
        event.payload
        for event in run_events_of(records, urn)
        if event.quarantine is None and isinstance(event.payload, UsagePayload)
    )


def _sum(values: Sequence[int | None]) -> int | None:
    """Return the reported values summed, or ``None`` when none was reported."""
    reported = [value for value in values if value is not None]
    return sum(reported) if reported else None


def _provider(records: Sequence[LedgerRecord], run: Run) -> str:
    """Return the provider a Run was routed to: its dispatch, else its host session."""
    routed = [
        str(item.payload["provider_kind"])
        for item in records
        if item.payload.get("payload_kind") == "dispatch_attempt"
        and item.payload.get("run_ref") == str(run.urn)
    ]
    if routed:
        return routed[-1]
    return run.vendor_session.harness if run.vendor_session is not None else UNATTRIBUTED


def _stops(records: Sequence[LedgerRecord], runs: Mapping[str, Run]) -> tuple[StoppedRun, ...]:
    """Return every budget crossing, confirmed when a control effect answered it."""
    stops: list[StoppedRun] = []
    for item in records:
        if item.payload.get("payload_kind") != "budget_notice":
            continue
        notice = BudgetNotice.model_validate(item.payload)
        run = runs.get(notice.run_ref.entity_key)
        facts = control_facts_of(tuple(records), notice.run_ref) if run is not None else ()
        effected = any(
            fact.control_request_ref == notice.control_request_ref
            and fact.phase is ControlPhase.EFFECTED
            for fact in facts
        )
        confirmed = effected and run is not None and _status(run, records) in TERMINAL_RUN_STATUSES
        stops.append(
            StoppedRun(
                run_key=notice.run_ref.entity_key,
                noticed_at=notice.noticed_at,
                confirmed=confirmed,
                observed_tokens=notice.observed_tokens,
                cap_tokens=notice.cap_tokens,
            )
        )
    return tuple(sorted(stops, key=lambda stop: stop.noticed_at, reverse=True))


def _providers(
    records: Sequence[LedgerRecord], runs: Mapping[str, Run]
) -> tuple[ProviderSpend, ...]:
    """Return spend by provider over every Run that reported usage."""
    grouped: dict[str, list[UsageTotals]] = {}
    for run in runs.values():
        readings = _readings(records, run.urn)
        if readings:
            grouped.setdefault(_provider(records, run), []).append(aggregate_usage(readings))
    rows: list[ProviderSpend] = []
    for provider, totals in sorted(grouped.items()):
        costs = [total.cost_microusd for total in totals]
        priced = sum(cost is not None for cost in costs)
        pricing: Pricing = (
            "priced" if priced == len(costs) else "unmetered" if priced == 0 else "partial"
        )
        rows.append(
            ProviderSpend(
                provider=provider,
                runs=len(totals),
                tokens=_sum([total.tokens for total in totals]),
                cost_microusd=_sum(costs),
                pricing=pricing,
            )
        )
    return tuple(rows)


def cost_ceiling_view(
    document: Mapping[str, object], records: Sequence[LedgerRecord], *, governor: InFlightGovernor
) -> CostCeilingView:
    """Fold the run ledger into the governor ceiling's spent, held and stopped Runs.

    Args:
        document: The tree's document, which holds the live Runs.
        records: Every line the run ledger holds.
        governor: The ceilings in force.

    Returns:
        The view the ``cost.ceiling`` route renders.
    """
    runs = _runs(document, records)

    def status_of(urn: RunUrn) -> RunStatus:
        run = runs.get(urn.entity_key)
        return _status(run, records) if run is not None else RunStatus.CANCELLED

    live = in_flight_reservations(records, status_of=status_of, excluding=None)
    view = CostCeilingView(
        ceiling_tokens=governor.max_in_flight_tokens,
        ceiling_cost_microusd=governor.max_in_flight_cost_microusd,
        live_runs=len(live),
        spent_tokens=_sum([row.accrued_tokens for row in live]) if live else 0,
        held_tokens=_held(row.held(AdmissionAxis.TOKENS) for row in live),
        spent_cost_microusd=_sum([row.accrued_cost_microusd for row in live]) if live else 0,
        held_cost_microusd=_held(row.held(AdmissionAxis.COST) for row in live),
        stopped=_stops(records, runs),
        providers=_providers(records, runs),
    )
    logger.debug(
        f"cost_ceiling_view live={view.live_runs} stopped={len(view.stopped)} "
        f"providers={len(view.providers)}"
    )
    return view


def _held(values: Iterable[int | None]) -> int | None:
    """Return what the live Runs hold together, or ``None`` when one holds no cap."""
    held = list(values)
    bounded = [value for value in held if value is not None]
    return None if len(bounded) < len(held) else sum(bounded)


def run_usage_view(
    document: Mapping[str, object], records: Sequence[LedgerRecord], *, urn: QualifiedUrn
) -> RunUsageView:
    """Fold one Run's readings beside the caps its binding sealed and its kind's typical time.

    The typical time is the median duration of the Runs of the same purpose that
    completed: a Run a cap or an operator stopped says how long it ran before it was
    stopped, not how long the work takes.

    Args:
        document: The tree's document.
        records: Every line the run ledger holds.
        urn: The Run.

    Returns:
        The view the Run's usage pane renders.

    Raises:
        KeyError: The tree holds no Run *urn* names.
    """
    runs = _runs(document, records)
    run = runs[urn.entity_key]
    readings = _readings(records, urn)
    totals = aggregate_usage(readings)
    binding = run_binding_of(tuple(records), urn)
    budget = binding.capsule.budget if binding is not None and binding.capsule else None
    durations = [
        int((other.ended_at - other.started_at).total_seconds())
        for other in runs.values()
        if other.key != run.key
        and other.status is RunStatus.COMPLETED
        and other.scope.purpose is run.scope.purpose
        and other.started_at is not None
        and other.ended_at is not None
    ]
    worst = max(
        (reading.measurement_quality for reading in readings),
        key=lambda quality: _QUALITY_RANK[quality],
        default=None,
    )
    return RunUsageView(
        run_key=run.key,
        tokens=totals.tokens,
        cost_microusd=totals.cost_microusd,
        quality=worst,
        cap_tokens=budget.tokens if budget is not None else None,
        cap_cost_microusd=budget.cost_microusd if budget is not None else None,
        wall_seconds=budget.wall_seconds if budget is not None else None,
        typical_seconds=int(statistics.median(durations)) if durations else None,
        typical_runs=len(durations),
    )


__all__ = [
    "UNATTRIBUTED",
    "cost_ceiling_view",
    "run_usage_view",
]
