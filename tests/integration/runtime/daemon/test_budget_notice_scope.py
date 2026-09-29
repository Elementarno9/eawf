"""A budget crossing notices one Run under one contract and stops only that Run.

The cases drive the real dispatch with a launcher that relays a scripted
usage stream from a live child, so the notice is the one the in-flight
meter writes and the termination is the one its kill ladder performs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.store.compaction import read_document
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.budget.notices import load_notice_ledger, notice_key_for, notices_path
from eawf.runtime.daemon import native_dispatch
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.native_dispatch import (
    DispatchParams,
    DispatchRefusal,
    DispatchStage,
    compile_launchers,
    run_binding_of,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import document_path
from tests.integration.runtime.daemon.test_native_dispatch import (
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
from tests.integration.runtime.test_run_meter_producer import (
    BASE_INPUT,
    CAP,
    StreamingLauncher,
    kinds,
    readings,
    run_status,
)

pytestmark = pytest.mark.integration


def two_runs(root: Path) -> CanaryProvision:
    """Provision a canary holding the Run and its successor, both queued."""
    return make_canary(root, rows={RUN_KEY: run_row(), SUCCESSOR_KEY: run_row(key=SUCCESSOR_KEY)})


def over_cap(canary: CanaryProvision, tmp_path: Path) -> DaemonValidationError:
    """Dispatch the first Run with a stream that crosses its sealed cap."""
    launcher = StreamingLauncher(readings(CAP - BASE_INPUT + 50))
    supplied = dispatch_params(canary, capsule=capsule_request(token_budget=CAP))
    args = DispatchParams.model_validate(
        {key: value for key, value in supplied.items() if key != "repo_root"}
    )
    context = method_ctx(tmp_path / "runtime").native_root_context(canary.root / ".ea")
    with pytest.raises(DaemonValidationError) as caught:
        asyncio.run(
            native_dispatch.dispatch_run(
                context,
                args,
                now=datetime.now(UTC),
                launchers=dict(compile_launchers((launcher,))),
            )
        )
    return caught.value


def notice_rows(canary: CanaryProvision) -> list[dict[str, Any]]:
    """Return every row of the root's notice ledger."""
    path = notices_path(canary.root / ".ea" / "state.json")
    return [row.model_dump(mode="json") for row in load_notice_ledger(path).notices.values()]


# ---- RUN-025: one notice per root, Run, compiled contract and axis ----------


def test_run_025_the_notice_is_keyed_by_the_run_its_contract_and_the_axis(
    tmp_path: Path,
) -> None:
    canary = make_canary(tmp_path / "repo")

    over_cap(canary, tmp_path)

    binding = run_binding_of(ledger_records(canary, tmp_path / "runtime"), run_urn())
    assert binding is not None
    rows = notice_rows(canary)
    assert len(rows) == 1
    assert rows[0]["contract_digest"] == binding.compiled_spec_digest
    assert rows[0]["notice_key"] == notice_key_for(
        scope_id=RUN_KEY,
        axis="tokens",
        basis="hard_limit",
        contract_digest=binding.compiled_spec_digest,
    )


def test_run_025_a_retried_termination_leaves_the_one_notice_at_its_revision(
    tmp_path: Path,
) -> None:
    canary = make_canary(tmp_path / "repo")
    over_cap(canary, tmp_path)
    first = notice_rows(canary)

    again = over_cap(canary, tmp_path)

    assert DispatchRefusal.RUN_NOT_DISPATCHABLE.value in str(again)
    assert notice_rows(canary) == first
    assert first[0]["revision"] == 1


# ---- RUN-026: exhaustion stops the subject Run and nothing else -------------


def test_run_026_hard_exhaustion_stops_only_the_subject_run(tmp_path: Path) -> None:
    canary = two_runs(tmp_path / "repo")

    error = over_cap(canary, tmp_path)

    assert DispatchRefusal.BUDGET_EXHAUSTED.value in str(error)
    assert run_status(canary, tmp_path) == "CANCELLED"
    successor = read_document(document_path(canary))["run"][SUCCESSOR_KEY]
    assert successor["status"] == "QUEUED"
    controls = kinds(ledger_records(canary, tmp_path / "runtime"), "control")
    assert {fact["run_ref"] for fact in controls} == {str(RUN_URN)}


def test_run_026_an_open_notice_does_not_guard_the_next_dispatch(tmp_path: Path) -> None:
    canary = two_runs(tmp_path / "repo")
    over_cap(canary, tmp_path)
    assert notice_rows(canary)[0]["status"] == "OPEN"
    runtime = tmp_path / "runtime"

    answer = dispatch(
        method_ctx(runtime),
        canary,
        LedgerReadingLauncher(canary, runtime),
        params=dispatch_params(canary, key="dispatch-02", urn=str(run_urn(SUCCESSOR_KEY))),
        now=datetime.now(UTC),
    )

    assert answer["stage"] == DispatchStage.ANNOUNCED.value


def test_run_026_a_notice_that_cannot_be_stored_does_not_stop_enforcement(
    tmp_path: Path,
) -> None:
    canary = make_canary(tmp_path / "repo")
    blocked = canary.root / ".ea" / "local"
    blocked.mkdir(parents=True, exist_ok=True)
    (blocked / "budget_notices.json").mkdir()

    error = over_cap(canary, tmp_path)

    assert DispatchRefusal.BUDGET_EXHAUSTED.value in str(error)
    assert run_status(canary, tmp_path) == "CANCELLED"
    records = ledger_records(canary, tmp_path / "runtime")
    assert len(kinds(records, "budget_notice")) == 1
