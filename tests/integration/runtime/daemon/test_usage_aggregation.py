"""Mixed-mode usage reaches the governor without a running total summed twice.

A Run's usage events arrive through the live append verb, some as running
session totals and some as deltas, and the governor reads them back as the
Run's accrued spend when it admits the next Run. The fixture is built so
the naive sum and the correct fold land on opposite sides of the ceiling.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eawf.kernel.economics.governor import AdmissionAxis, AdmissionDecision
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.admission import latest_receipts
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.run import RUN_EVENT_APPEND_METHOD
from tests.integration.runtime.daemon.test_native_dispatch import (
    ACTOR,
    RUN_KEY,
    RUN_URN,
    SUCCESSOR_KEY,
    LedgerReadingLauncher,
    capsule_request,
    dispatch,
    dispatch_params,
    ledger_records,
    make_canary,
    method_ctx,
    run_row,
    run_urn,
)

pytestmark = pytest.mark.integration

CEILING: Final = 1_000_000

#: Two running totals and one delta. Folded: max(300k, 600k) + 100k = 700k.
#: Summed naively: 1,000,000, which would already fill the ceiling.
MIXED: Final = (
    ({"input_tokens": 300_000}, True),
    ({"input_tokens": 600_000}, True),
    ({"input_tokens": 100_000}, False),
)


def canary_with_first_run_live(tmp_path: Path) -> CanaryProvision:
    """Dispatch the first Run under a token ceiling and relay its mixed usage."""
    canary = make_canary(
        tmp_path / "repo", rows={RUN_KEY: run_row(), SUCCESSOR_KEY: run_row(key=SUCCESSOR_KEY)}
    )
    governor = {"max_concurrent_runs": 8, "max_in_flight_tokens": CEILING, "admission": "queue"}
    (canary.root / ".ea" / "config.yaml").write_text(
        yaml.safe_dump({"economics": {"governor": governor}}), encoding="utf-8"
    )
    runtime = tmp_path / "runtime"
    dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))
    for sequence, (counters, cumulative) in enumerate(MIXED, start=1):
        payload: dict[str, Any] = {
            "payload_kind": "usage",
            **counters,
            "usage_source": "provider_transcript",
            "is_cumulative": cumulative,
            "measurement_quality": "measured",
        }
        asyncio.run(
            methods.dispatch(
                RUN_EVENT_APPEND_METHOD,
                method_ctx(runtime),
                {
                    "repo_root": str(canary.root),
                    "urn": str(RUN_URN),
                    "actor": ACTOR,
                    "event_ref": f"EVT-{sequence:08x}",
                    "run_sequence": sequence,
                    "event_kind": "usage_observed",
                    "payload": payload,
                },
            )
        )
    return canary


def second(canary: CanaryProvision, tmp_path: Path, token_budget: int) -> None:
    """Dispatch the successor Run with a sealed *token_budget*."""
    runtime = tmp_path / "runtime"
    dispatch(
        method_ctx(runtime),
        canary,
        LedgerReadingLauncher(canary, runtime),
        params=dispatch_params(
            canary,
            key="dispatch-02",
            urn=str(run_urn(SUCCESSOR_KEY)),
            capsule=capsule_request(token_budget=token_budget),
        ),
    )


def test_run_050_mixed_usage_folds_running_totals_by_maximum(tmp_path: Path) -> None:
    canary = canary_with_first_run_live(tmp_path)

    second(canary, tmp_path, token_budget=CEILING - 700_000)

    receipt = latest_receipts(ledger_records(canary, tmp_path / "runtime"))[
        str(run_urn(SUCCESSOR_KEY))
    ]
    tokens = next(row for row in receipt.axes if row.axis is AdmissionAxis.TOKENS)
    assert receipt.decision is AdmissionDecision.ADMITTED
    assert tokens.in_flight == 700_000
    assert tokens.in_flight != sum(counters["input_tokens"] for counters, _ in MIXED)


def test_run_050_accrued_spend_past_the_reservation_binds_admission(tmp_path: Path) -> None:
    """The first Run reserved 200k and accrued 700k: the accrual is what counts."""
    canary = canary_with_first_run_live(tmp_path)

    with pytest.raises(DaemonValidationError, match="700000 in flight plus 300001 requested"):
        second(canary, tmp_path, token_budget=CEILING - 700_000 + 1)


def test_run_050_a_usage_event_without_is_cumulative_is_refused_at_append(
    tmp_path: Path,
) -> None:
    canary = make_canary(tmp_path / "repo")
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        asyncio.run(
            methods.dispatch(
                RUN_EVENT_APPEND_METHOD,
                method_ctx(tmp_path / "runtime"),
                {
                    "repo_root": str(canary.root),
                    "urn": str(RUN_URN),
                    "actor": ACTOR,
                    "event_ref": "EVT-0000000a",
                    "run_sequence": 1,
                    "event_kind": "usage_observed",
                    "payload": {
                        "payload_kind": "usage",
                        "input_tokens": 5,
                        "usage_source": "counter_sidecar",
                        "measurement_quality": "measured",
                    },
                },
            )
        )
