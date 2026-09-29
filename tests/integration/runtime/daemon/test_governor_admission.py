"""The governor admits a Run inside the dispatch, before any provider starts.

Each case drives the real ``dispatch_run`` against a provisioned canary
whose repository configuration declares the governor, so the admission
decision is read from the same layered configuration an operator writes
and recorded on the same run ledger a caller on the socket would leave.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eawf.kernel.economics.governor import AdmissionDecision, AdmissionReceipt
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import LedgerRecord
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.admission import latest_receipts
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.native_dispatch import DispatchRefusal, DispatchStage
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import document_path, seed
from tests.integration.runtime.daemon.test_native_dispatch import (
    RUN_KEY,
    SUCCESSOR_KEY,
    DaemonDiedError,
    LedgerReadingLauncher,
    attempts_of,
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

#: A governor with room for exactly one live Run.
ONE_SLOT: Final = {
    "max_concurrent_runs": 1,
    "max_in_flight_tokens": 1_000_000,
    "admission": "queue",
}


def declare(canary: CanaryProvision, **economics: Any) -> None:
    """Write the repository layer's ``economics`` table."""
    path = canary.root / ".ea" / "config.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    merged = {**(document or {}), "economics": economics}
    path.write_text(yaml.safe_dump(merged), encoding="utf-8")


def two_runs(root: Path) -> CanaryProvision:
    """Provision a canary holding the Run and its successor, both queued."""
    return make_canary(root, rows={RUN_KEY: run_row(), SUCCESSOR_KEY: run_row(key=SUCCESSOR_KEY)})


def second_params(canary: CanaryProvision, **capsule: Any) -> dict[str, Any]:
    """Return the dispatch params of the successor Run."""
    return dispatch_params(
        canary,
        key="dispatch-02",
        urn=str(run_urn(SUCCESSOR_KEY)),
        capsule=capsule_request(**capsule),
    )


def receipt_of(records: tuple[LedgerRecord, ...], key: str) -> AdmissionReceipt:
    """Return the standing admission receipt of the Run keyed *key*."""
    return latest_receipts(records)[str(run_urn(key))]


def stored_status(canary: CanaryProvision, key: str) -> str:
    """Return the Run's status as the document holds it."""
    return str(read_document(document_path(canary))["run"][key]["status"])


def test_econ_010_a_run_past_the_ceiling_is_queued_before_any_lease_or_spawn(
    tmp_path: Path,
) -> None:
    canary = two_runs(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    declare(canary, governor=ONE_SLOT)
    launcher = LedgerReadingLauncher(canary, runtime)
    dispatch(method_ctx(runtime), canary, launcher)

    with pytest.raises(DaemonValidationError) as caught:
        dispatch(method_ctx(runtime), canary, launcher, params=second_params(canary))

    assert DispatchRefusal.ADMISSION_QUEUED.value in str(caught.value)
    assert "concurrency breached" in str(caught.value)
    assert len(launcher.calls) == 1
    own = [row for row in attempts_of(canary, runtime) if row.run_ref == run_urn(SUCCESSOR_KEY)]
    assert own == []
    receipt = receipt_of(ledger_records(canary, runtime), SUCCESSOR_KEY)
    assert receipt.decision is AdmissionDecision.QUEUED


def test_econ_010_a_deny_governor_refuses_with_the_denied_code(tmp_path: Path) -> None:
    canary = two_runs(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    declare(canary, governor={**ONE_SLOT, "admission": "deny"})
    launcher = LedgerReadingLauncher(canary, runtime)
    dispatch(method_ctx(runtime), canary, launcher)

    with pytest.raises(DaemonValidationError) as caught:
        dispatch(method_ctx(runtime), canary, launcher, params=second_params(canary))

    assert DispatchRefusal.ADMISSION_DENIED.value in str(caught.value)


def test_econ_010_an_invalid_economics_table_refuses_before_anything_is_written(
    tmp_path: Path,
) -> None:
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    declare(canary, governor={"max_concurrent_runs": 1, "admission": "queue"})
    launcher = LedgerReadingLauncher(canary, runtime)

    with pytest.raises(DaemonValidationError) as caught:
        dispatch(method_ctx(runtime), canary, launcher)

    assert DispatchRefusal.ECONOMICS_INVALID.value in str(caught.value)
    assert launcher.calls == []
    assert latest_receipts(ledger_records(canary, runtime)) == {}


def test_econ_004_a_prompt_over_its_byte_ceiling_is_denied_by_name(tmp_path: Path) -> None:
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    allocations = [
        {"class_id": "authority_capsule", "max_tokens": 4000, "priority": 100, "measured": True},
        {
            "class_id": "task_packet",
            "max_tokens": 24000,
            "max_bytes": 8,
            "priority": 90,
            "measured": True,
        },
        {"class_id": "steering_zone1", "max_tokens": 12000, "priority": 80, "measured": True},
        {"class_id": "tool_catalog", "max_tokens": 8000, "priority": 60, "measured": True},
        {"class_id": "steering_zone2", "max_tokens": 24000, "priority": 40, "measured": True},
        {"class_id": "memory_injection", "max_tokens": 8000, "priority": 20, "measured": True},
    ]
    declare(
        canary,
        prompt_budget={
            "policy_id": "BUD-TIGHT",
            "revision": 1,
            "input_window_tokens": 200000,
            "reserved_output_tokens": 32000,
            "on_exhaustion": "degrade_by_priority",
            "allocations": allocations,
        },
    )
    launcher = LedgerReadingLauncher(canary, runtime)

    with pytest.raises(DaemonValidationError) as caught:
        dispatch(method_ctx(runtime), canary, launcher)

    assert DispatchRefusal.ADMISSION_DENIED.value in str(caught.value)
    assert "prompt budget exhausted on task_packet" in str(caught.value)
    assert launcher.calls == []
    receipt = receipt_of(ledger_records(canary, runtime), RUN_KEY)
    assert receipt.prompt_budget.exhausted[0].value == "task_packet"


def test_econ_013_a_queued_run_keeps_its_lifecycle_exactly(tmp_path: Path) -> None:
    canary = two_runs(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    declare(canary, governor=ONE_SLOT)
    launcher = LedgerReadingLauncher(canary, runtime)
    dispatch(method_ctx(runtime), canary, launcher)
    before = stored_status(canary, SUCCESSOR_KEY)

    with pytest.raises(DaemonValidationError):
        dispatch(method_ctx(runtime), canary, launcher, params=second_params(canary))

    assert stored_status(canary, SUCCESSOR_KEY) == before == "QUEUED"
    statuses = {
        item.status
        for item in ledger_records(canary, runtime)
        if item.record_key == SUCCESSOR_KEY and "payload_kind" not in item.payload
    }
    assert statuses == set()


@pytest.mark.parametrize("terminal", ["COMPLETED", "FAILED", "CANCELLED"])
def test_econ_012_a_terminal_run_releases_its_reservation(tmp_path: Path, terminal: str) -> None:
    """A Run ending in any terminal status frees its slot; a lost Run lands in FAILED."""
    canary = two_runs(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    declare(canary, governor=ONE_SLOT)
    launcher = LedgerReadingLauncher(canary, runtime)
    dispatch(method_ctx(runtime), canary, launcher)
    with pytest.raises(DaemonValidationError):
        dispatch(method_ctx(runtime), canary, launcher, params=second_params(canary))

    seed(canary, {"run": {RUN_KEY: run_row(terminal)}})
    answer = dispatch(method_ctx(runtime), canary, launcher, params=second_params(canary))

    assert answer["stage"] == DispatchStage.ANNOUNCED.value
    receipt = receipt_of(ledger_records(canary, runtime), SUCCESSOR_KEY)
    assert receipt.decision is AdmissionDecision.ADMITTED
    assert receipt.axes[0].in_flight == 0


def test_econ_012_a_live_run_keeps_its_reservation(tmp_path: Path) -> None:
    canary = two_runs(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    declare(canary, governor=ONE_SLOT)
    launcher = LedgerReadingLauncher(canary, runtime)
    dispatch(method_ctx(runtime), canary, launcher)
    seed(canary, {"run": {RUN_KEY: run_row("SUSPENDED")}})

    with pytest.raises(DaemonValidationError, match="dispatch_admission_queued"):
        dispatch(method_ctx(runtime), canary, launcher, params=second_params(canary))


def test_econ_014_the_admitted_reservation_is_the_sealed_cap_the_meter_enforces(
    tmp_path: Path,
) -> None:
    canary = two_runs(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    declare(canary, governor={**ONE_SLOT, "max_concurrent_runs": 8})
    launcher = LedgerReadingLauncher(canary, runtime)
    dispatch(method_ctx(runtime), canary, launcher)

    capsule = launcher.calls[0]["capsule"]
    first = receipt_of(ledger_records(canary, runtime), RUN_KEY)
    assert first.reservation.tokens == capsule.budget.tokens == 200_000

    with pytest.raises(DaemonValidationError) as caught:
        dispatch(
            method_ctx(runtime),
            canary,
            launcher,
            params=second_params(canary, token_budget=800_001),
        )
    assert "tokens breached: 200000 in flight plus 800001 requested" in str(caught.value)


def test_econ_005_a_policy_change_after_admission_invalidates_the_attempt(
    tmp_path: Path,
) -> None:
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    declare(canary, governor=ONE_SLOT)
    with pytest.raises(DaemonDiedError):
        dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime, die=True))

    declare(canary, governor={**ONE_SLOT, "max_in_flight_tokens": 2_000_000})
    with pytest.raises(DaemonValidationError) as caught:
        dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))

    assert DispatchRefusal.CONTRACT_DRIFT.value in str(caught.value)
    assert "admitted under another governor policy" in str(caught.value)


def test_econ_005_an_unchanged_policy_resumes_the_attempt(tmp_path: Path) -> None:
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    declare(canary, governor=ONE_SLOT)
    with pytest.raises(DaemonDiedError):
        dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime, die=True))

    answer = dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))

    assert answer["resumed"] is True
    assert len(latest_receipts(ledger_records(canary, runtime))) == 1
