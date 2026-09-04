"""GateReceipt and local GateDiagnostic contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import GateReceiptResult, StoreKind
from eawf.kernel.store.kinds import PAYLOAD_MODELS
from eawf.kernel.store.kinds.gate_receipt import GateDiagnostic, GateReceipt

_TS = "2026-07-28T12:00:00Z"
_SHA = "a" * 40
_DIGEST = "sha256:" + "b" * 64
_FRESHNESS_KEY = "c" * 64


def _payload() -> dict[str, object]:
    return {
        "schema_version": "2.0",
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
        "argv_digest": _DIGEST,
        "timeout_class": "quick",
        "resolved_timeout_seconds": 30.0,
        "started_at": _TS,
        "ended_at": _TS,
        "duration_ms": 0,
        "result": "pass",
        "exit_status": 0,
        "stdout_digest": _DIGEST,
        "stderr_digest": _DIGEST,
        "selected_file_digest": None,
        "collected_nodeid_digest": None,
        "residual_manifest_digest": None,
    }


def test_gate_receipt_registered_and_round_trips_safe_shape() -> None:
    receipt = GateReceipt.model_validate(_payload())
    assert receipt.result is GateReceiptResult.PASS
    assert PAYLOAD_MODELS[StoreKind.GATE_RECEIPT] is GateReceipt
    assert receipt.model_dump(mode="json") == _payload()


@pytest.mark.parametrize(
    "field,value",
    [
        ("argv", ["uv", "run", "pytest"]),
        ("command", "uv run pytest"),
        ("details", "3 passed"),
        ("stdout_tail", "stdout"),
        ("stderr_tail", "stderr"),
        ("full_log_ref", ".ea/local/gate.log"),
        ("diagnostic_id", "GD-GR-01"),
        ("diagnostic_digest", _DIGEST),
    ],
)
def test_gate_receipt_rejects_raw_diagnostic_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        GateReceipt.model_validate({**_payload(), field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract_digest", "https:" + "//internal.invalid/proof"),
        ("runner_digest", "/" + "Users/example/private"),
        ("environment_digest", "file:" + "///tmp/private"),
    ],
)
def test_gate_receipt_rejects_non_digest_values(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        GateReceipt.model_validate({**_payload(), field: value})


def test_gate_receipt_is_strict_frozen_and_time_ordered() -> None:
    receipt = GateReceipt.model_validate(_payload())
    with pytest.raises(ValidationError, match="extra"):
        GateReceipt.model_validate({**_payload(), "extra": True})
    with pytest.raises(ValidationError, match="frozen"):
        receipt.result = GateReceiptResult.FAIL
    with pytest.raises(ValidationError, match="ended_at"):
        GateReceipt.model_validate(
            {
                **_payload(),
                "started_at": "2026-07-28T12:00:01Z",
                "ended_at": _TS,
            }
        )


def test_gate_receipt_allows_non_command_digest_absence() -> None:
    receipt = GateReceipt.model_validate(
        {
            **_payload(),
            "argv_digest": None,
            "timeout_class": None,
            "resolved_timeout_seconds": None,
            "exit_status": None,
        }
    )
    assert receipt.argv_digest is None
    assert receipt.resolved_timeout_seconds is None


def test_gate_diagnostic_is_strict_and_bounded() -> None:
    payload = {
        "id": "GD-GR-01",
        "receipt_id": "GR-01",
        "attempt_id": "CA-01",
        "scope_id": "P01-I01-W01",
        "criterion_id": "C-01",
        "gate_id": "G-01",
        "captured_at": _TS,
        "argv": ["uv", "run", "pytest"],
        "command": "uv run pytest",
        "details": "3 passed",
        "stdout_tail": "stdout",
        "stderr_tail": "stderr",
        "source_log_ref": ".ea/local/incoming.log",
        "log_digest": "d" * 64,
        "log_present": True,
    }
    diagnostic = GateDiagnostic.model_validate(payload)
    assert diagnostic.receipt_id == "GR-01"
    with pytest.raises(ValidationError, match="stdout_tail"):
        GateDiagnostic.model_validate({**payload, "stdout_tail": "x" * 16_385})
    with pytest.raises(ValidationError, match="provided together"):
        GateDiagnostic.model_validate({**payload, "log_digest": None})
