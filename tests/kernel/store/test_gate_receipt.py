"""GateReceipt store-kind contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import GateReceiptResult, StoreKind
from eawf.kernel.store.kinds import PAYLOAD_MODELS
from eawf.kernel.store.kinds.gate_receipt import GateReceipt

_TS = "2026-07-28T12:00:00Z"
_SHA = "a" * 40
_DIGEST = "sha256:" + "b" * 64
_FRESHNESS_KEY = "c" * 64


def _payload() -> dict[str, object]:
    return {
        "id": "GR-01",
        "scope_id": "P01-I01-W01",
        "criterion_id": "C-01",
        "gate_id": "G-01",
        "integration_id": "WI-01",
        "integrated_sha": _SHA,
        "tree_sha": _SHA,
        "contract_digest": _DIGEST,
        "criteria_digest": _DIGEST,
        "gate_manifest_digest": _DIGEST,
        "policy_digest": _DIGEST,
        "dependency_binding_digest": _DIGEST,
        "runner_environment_digest": _DIGEST,
        "runner_digest": _DIGEST,
        "environment_digest": _DIGEST,
        "freshness_key": _FRESHNESS_KEY,
        "argv": ["uv", "run", "pytest"],
        "argv_digest": _DIGEST,
        "command": "uv run pytest",
        "timeout_class": "quick",
        "resolved_timeout_seconds": 30.0,
        "started_at": _TS,
        "ended_at": _TS,
        "duration_ms": 0,
        "result": "pass",
        "details": "3 passed",
        "exit_status": 0,
        "stdout_tail": "",
        "stderr_tail": "",
        "stdout_digest": _DIGEST,
        "stderr_digest": _DIGEST,
        "full_log_ref": "urn:eawf:v1:blob:gate-log-01",
        "selected_file_digest": None,
        "collected_nodeid_digest": None,
        "residual_manifest_digest": None,
    }


def test_gate_receipt_registered_and_round_trips() -> None:
    receipt = GateReceipt.model_validate(_payload())
    assert receipt.result is GateReceiptResult.PASS
    assert PAYLOAD_MODELS[StoreKind.GATE_RECEIPT] is GateReceipt
    assert receipt.model_dump(mode="json") == _payload()


def test_gate_receipt_is_strict_and_frozen() -> None:
    receipt = GateReceipt.model_validate(_payload())
    with pytest.raises(ValidationError, match="extra"):
        GateReceipt.model_validate({**_payload(), "extra": True})
    with pytest.raises(ValidationError, match="frozen"):
        receipt.result = GateReceiptResult.FAIL


def test_gate_receipt_rejects_unbounded_tail_and_reverse_time() -> None:
    with pytest.raises(ValidationError, match="stdout_tail"):
        GateReceipt.model_validate({**_payload(), "stdout_tail": "x" * 16_385})
    with pytest.raises(ValidationError, match="ended_at"):
        GateReceipt.model_validate(
            {
                **_payload(),
                "started_at": "2026-07-28T12:00:01Z",
                "ended_at": _TS,
            }
        )


def test_gate_receipt_allows_non_command_proof_without_argv_or_timeout() -> None:
    """All deterministic kinds retain proof while command facts stay optional."""
    payload = {
        **_payload(),
        "argv": None,
        "argv_digest": None,
        "command": None,
        "timeout_class": None,
        "resolved_timeout_seconds": None,
        "exit_status": None,
    }

    receipt = GateReceipt.model_validate(payload)

    assert receipt.argv is None
    assert receipt.resolved_timeout_seconds is None
    assert receipt.full_log_ref


def test_gate_receipt_rejects_partial_argv_pair() -> None:
    with pytest.raises(ValidationError, match="provided together"):
        GateReceipt.model_validate({**_payload(), "argv_digest": None})
