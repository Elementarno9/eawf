"""The native dispatch states what a Run spends on its own stream, and how it stopped.

ECON-014 needs the governor to hold a live Run to what it has actually accrued, which
admission folds out of the Run's ``usage_observed`` lines; RUN-024 needs a cap crossing
to reach the Run's stream as ``budget_exhausted`` carrying the typed budget payload. Both
are driven here through the real ``dispatch_run`` with the scripted streaming launcher
the in-flight meter suite uses: a real child in its own process group, relaying
cumulative usage readings through the request's sink. No model is called.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.economics.notice_policy import DEFAULT_NOTICE_POLICY
from eawf.kernel.runtime.compiled import canonical_digest
from eawf.kernel.runtime.events import RunEventKind
from eawf.kernel.runtime.usage import BudgetPayload, UsagePayload
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.runtime.budget.notices import load_notice_ledger, notices_path
from eawf.runtime.daemon.admission import in_flight_reservations
from eawf.runtime.daemon.run_events import run_events_of
from tests.integration.runtime.daemon.test_native_dispatch import ledger_records, run_urn
from tests.integration.runtime.test_run_meter_producer import (
    BASE_INPUT,
    CAP,
    StreamingLauncher,
    dispatch,
    dispatch_refused,
    kinds,
    readings,
)

pytestmark = pytest.mark.integration


def test_econ_014_a_live_run_holds_what_its_stream_says_it_spent(tmp_path: Path) -> None:
    """ECON-014: the accrual admission holds a live Run to is its metered spend.

    The dispatched Run stays under its cap and stays live, so another Run asking to be
    admitted is decided against what this one has spent, read off its own usage lines --
    not against a claim alone, which is what an empty stream left the governor with.
    """
    under = CAP - BASE_INPUT - 1
    canary, _answer = dispatch(tmp_path, StreamingLauncher(readings(100, 400, under)))
    records = ledger_records(canary, tmp_path / "runtime")

    usage = [
        event.payload
        for event in run_events_of(records, run_urn())
        if event.event_kind is RunEventKind.USAGE_OBSERVED
    ]
    assert len(usage) == 3
    assert all(isinstance(line, UsagePayload) and line.is_cumulative for line in usage)
    (held,) = in_flight_reservations(
        records, status_of=lambda _urn: RunStatus.RUNNING, excluding=run_urn("RUN-00000099")
    )
    assert held.accrued_tokens == BASE_INPUT + under
    assert held.accrued_tokens == CAP - 1
    assert held.tokens == CAP


def test_run_024_a_crossing_is_stated_as_budget_exhausted_with_its_payload(
    tmp_path: Path,
) -> None:
    """RUN-024: the crossing reaches the Run's stream as a typed, derivable exhaustion.

    It names the axis, the hard-limit basis and the limit band, the compiled contract,
    the notice policy and its revision, the ceiling and the reading, and the key of the
    one notice the notice ledger filed -- and it stands in the derived stream rather
    than in quarantine, because it was written before the Run's terminal edge.
    """
    over = CAP - BASE_INPUT + 50
    canary, _error = dispatch_refused(tmp_path, StreamingLauncher(readings(200, over, over + 9)))
    records = ledger_records(canary, tmp_path / "runtime")

    events = run_events_of(records, run_urn())
    (exhausted,) = [e for e in events if e.event_kind is RunEventKind.BUDGET_EXHAUSTED]
    assert exhausted.quarantine is None
    payload = exhausted.payload
    assert isinstance(payload, BudgetPayload)
    assert (payload.axis, payload.basis, payload.band) == (
        "input_tokens",
        "hard_limit",
        "limit_reached",
    )
    assert (payload.ceiling_value, payload.observed_value, payload.unit) == (
        CAP,
        CAP + 50,
        "tokens",
    )
    assert payload.fraction == pytest.approx((CAP + 50) / CAP)
    assert payload.policy_digest == canonical_digest(DEFAULT_NOTICE_POLICY.model_dump(mode="json"))
    assert payload.policy_revision == DEFAULT_NOTICE_POLICY.revision
    (binding,) = kinds(records, "run_binding")
    assert payload.contract_digest == binding["compiled_spec_digest"]
    ledger = load_notice_ledger(notices_path(canary.root / ".ea" / "state.json"))
    assert list(ledger.notices) == [payload.notice_key]
    # the reading that crossed precedes the crossing; nothing is stated after the reap
    order = [e.event_kind for e in events if e.event_kind in _BUDGET_KINDS]
    assert order == [RunEventKind.USAGE_OBSERVED] * 2 + [RunEventKind.BUDGET_EXHAUSTED]


_BUDGET_KINDS = frozenset({RunEventKind.USAGE_OBSERVED, RunEventKind.BUDGET_EXHAUSTED})
