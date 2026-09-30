"""A root Run's captured actuals and its own usage events say the same thing.

PRX-041 holds the runtime-counter capture against the Run's usage stream on the worked
journey: ``eawf run start`` inside a Claude Code host session, a turn of work in the
session transcript, then ``eawf run finish``. The finish banks the Run's measured share
on its record and states it on its stream as ``usage_observed`` before the stop commits,
so folding the stream reproduces the captured tokens and cost.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from eawf.kernel.identity import EntityKind, parse_qualified_urn
from eawf.kernel.runtime.events import RunEventKind
from eawf.kernel.runtime.usage import UsagePayload, aggregate_usage
from eawf.kernel.state.epoch2.measurement import CounterName, Observed, Unobserved
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.run_events import run_events_of
from tests.integration.runtime.daemon._delivery_verb_fixtures import RUN_URN
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import document_path
from tests.integration.runtime.daemon.test_root_run_host_session import (
    _after_start,
    _finish,
    _head,
    _hosted,
    _measured,
    _queued,
    _start,
    _transcript,
    cli,  # noqa: F401  -- the autouse fixture routing the CLI into the verbs
)

pytestmark = pytest.mark.integration


def _observed(value: Observed | Unobserved) -> int | None:
    return int(value.value) if isinstance(value, Observed) else None


def test_prx_041_the_captured_actuals_reconcile_with_the_runs_usage_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: Path,  # noqa: F811
) -> None:
    """PRX-041: the actuals row is non-zero and the Run's usage events fold to it."""
    _hosted(monkeypatch)
    _transcript(cli, _head())
    canary = _queued(tmp_path)
    _start(canary, tmp_path)
    _transcript(cli, [*_head(), *_after_start()])

    _finish(canary, tmp_path)

    captured = _measured(canary)
    output = _observed(captured.counters[CounterName.OUTPUT_TOKENS])
    assert output is not None and output > 0
    records = read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))
    urn = parse_qualified_urn(RUN_URN, expected_kind=EntityKind.RUN)
    usage = [
        event
        for event in run_events_of(records, urn)
        if event.event_kind is RunEventKind.USAGE_OBSERVED
    ]
    assert usage, "the finish stated no usage on the Run's stream"
    assert all(event.quarantine is None for event in usage)
    totals = aggregate_usage(
        event.payload for event in usage if isinstance(event.payload, UsagePayload)
    )
    assert totals.output_tokens == output
    assert totals.input_tokens == _observed(captured.counters[CounterName.INPUT_TOKENS])
    cost = captured.counters[CounterName.COST_USD]
    expected_cost = int(cost.value * Decimal(1_000_000)) if isinstance(cost, Observed) else None
    assert totals.cost_microusd == expected_cost
