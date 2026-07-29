"""Durable at-most-once deterministic gate execution claims."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.store.kinds.gate_receipt import GateReceipt
from eawf.runtime.daemon import gate_execution
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec, GateFreshnessInput
from eawf.workflow.audit_dsl.registry import CHECK_REGISTRY
from eawf.workflow.audit_dsl.runner import run_checks


def _spec() -> CheckSpec:
    return CheckSpec(
        kind="command_exit_zero",
        name="G-01",
        args={"argv": ["gate"], "scope": "all"},
    )


def _receipt(freshness_key: str) -> GateReceipt:
    observed_at = datetime(2026, 7, 28, tzinfo=UTC)
    return GateReceipt(
        id=gate_execution.gate_receipt_id(freshness_key),
        scope_id="P01-I01-W01",
        criterion_id="CR-01",
        gate_id="G-01",
        integration_id="INT-01",
        integrated_sha="1" * 40,
        tree_sha="2" * 40,
        contract_digest="3" * 64,
        criteria_digest="8" * 64,
        gate_manifest_digest="9" * 64,
        policy_digest="4" * 64,
        dependency_binding_digest="a" * 64,
        runner_environment_digest="b" * 64,
        runner_digest="5" * 64,
        environment_digest="6" * 64,
        freshness_key=freshness_key,
        argv_digest="7" * 64,
        timeout_class="quick",
        resolved_timeout_seconds=60,
        started_at=observed_at,
        ended_at=observed_at,
        duration_ms=0,
        result=GateReceiptResult.PASS,
        exit_status=0,
    )


def _file_receipt(freshness_key: str) -> GateReceipt:
    """Terminal non-command proof with optional command fields absent."""
    observed_at = datetime(2026, 7, 28, tzinfo=UTC)
    return GateReceipt(
        id=gate_execution.gate_receipt_id(freshness_key),
        scope_id="P01-I01-W01",
        criterion_id="CR-01",
        gate_id="G-FILE",
        integration_id="INT-01",
        integrated_sha="1" * 40,
        tree_sha="2" * 40,
        contract_digest="3" * 64,
        criteria_digest="8" * 64,
        gate_manifest_digest="9" * 64,
        policy_digest="4" * 64,
        dependency_binding_digest="a" * 64,
        runner_environment_digest="b" * 64,
        runner_digest="5" * 64,
        environment_digest="6" * 64,
        freshness_key=freshness_key,
        started_at=observed_at,
        ended_at=observed_at,
        duration_ms=0,
        result=GateReceiptResult.PASS,
    )


def test_claim_gate_execution_orphan_is_global_and_fail_closed(
    tmp_path: Path,
) -> None:
    """A crash-orphan claim blocks another attempt without permitting rerun."""
    state_path = tmp_path / ".ea" / "state.json"
    freshness_key = "a" * 64

    first = gate_execution.claim_gate_execution(
        state_path,
        attempt_id="CA-01",
        criterion_id="CR-01",
        gate_id="G-01",
        spec=_spec(),
        freshness_key=freshness_key,
    )
    second = gate_execution.claim_gate_execution(
        state_path,
        attempt_id="CA-02",
        criterion_id="CR-01",
        gate_id="G-01",
        spec=_spec(),
        freshness_key=freshness_key,
    )

    assert first is None
    assert gate_execution.claim_path(
        state_path,
        attempt_id="CA-01",
        freshness_key=freshness_key,
    ).is_file()
    assert second is not None
    assert second.status == "blocked"
    assert second.started_at is None
    assert "without terminal receipt" in (second.details or "")


def test_completed_claim_reconstructs_exact_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed claim returns its exact typed result, including output digests."""
    state_path = tmp_path / ".ea" / "state.json"
    freshness_key = "b" * 64
    receipt = _receipt(freshness_key)
    assert (
        gate_execution.claim_gate_execution(
            state_path,
            attempt_id="CA-01",
            criterion_id="CR-01",
            gate_id="G-01",
            spec=_spec(),
            freshness_key=freshness_key,
        )
        is None
    )
    monkeypatch.setattr(gate_execution, "_load_receipt", lambda state_path, receipt_id: receipt)
    exact = CheckResult(
        name="G-01",
        kind="command_exit_zero",
        passed=True,
        status="pass",
        details="returncode=0 stdout_sha256=abc stderr_sha256=def",
        started_at=receipt.started_at,
        ended_at=receipt.ended_at,
        duration_ms=0,
        timeout_class="quick",
        resolved_timeout_seconds=60,
        exit_status=0,
        argv=["gate"],
        command="gate",
        stdout_tail="full stdout",
        stderr_tail="full stderr",
        stdout_digest="8" * 64,
        stderr_digest="9" * 64,
        selected_file_digest="a" * 64,
        collected_nodeid_digest="b" * 64,
        residual_manifest_digest="c" * 64,
        runner_fingerprint=receipt.runner_digest,
        environment_fingerprint=receipt.environment_digest,
        full_log_ref=".ea/local/gate-diagnostics/output.log",
        freshness_key=freshness_key,
    )

    gate_execution.complete_gate_execution(
        state_path,
        attempt_id="CA-01",
        freshness_key=freshness_key,
        receipt_id=receipt.id,
        result=exact,
    )
    reused = gate_execution.claim_gate_execution(
        state_path,
        attempt_id="CA-02",
        criterion_id="CR-01",
        gate_id="G-01",
        spec=_spec(),
        freshness_key=freshness_key,
    )

    assert reused is not None
    assert reused.model_dump(mode="json") == exact.model_dump(mode="json")


def test_corrupt_claim_fails_closed_without_becoming_a_new_claim(
    tmp_path: Path,
) -> None:
    """Unreadable durable claim state cannot reopen the execution window."""
    state_path = tmp_path / ".ea" / "state.json"
    freshness_key = "c" * 64
    path = gate_execution.claim_path(
        state_path,
        attempt_id="CA-01",
        freshness_key=freshness_key,
    )
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="claim unreadable"):
        gate_execution.claim_gate_execution(
            state_path,
            attempt_id="CA-02",
            criterion_id="CR-01",
            gate_id="G-01",
            spec=_spec(),
            freshness_key=freshness_key,
        )

    assert path.read_text(encoding="utf-8") == "{not-json"


def test_file_exists_claim_precedes_execution_and_orphan_prevents_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every deterministic kind claims first; orphaned proof never reruns."""
    state_path = tmp_path / ".ea" / "state.json"
    calls = 0
    claimed_keys: list[str] = []

    def _counted(spec: CheckSpec, cwd: Path) -> CheckResult:
        nonlocal calls
        calls += 1
        assert claimed_keys
        assert gate_execution.claim_path(
            state_path,
            attempt_id="CA-01",
            freshness_key=claimed_keys[-1],
        ).is_file()
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=True,
            details="path=sentinel exists=True",
        )

    monkeypatch.setitem(CHECK_REGISTRY, "file_exists", _counted)
    spec = CheckSpec(
        kind="file_exists",
        name="G-FILE",
        args={"path": "sentinel"},
        freshness=GateFreshnessInput(
            integration_id="INT-01",
            dependency_binding_digest="a" * 64,
            runner_environment_digest="b" * 64,
            full_log_ref=".ea/local/logs/file-proof.log",
        ),
    )

    def _claim(attempt_id: str, spec: CheckSpec, key: str) -> CheckResult | None:
        claimed_keys.append(key)
        return gate_execution.claim_gate_execution(
            state_path,
            attempt_id=attempt_id,
            criterion_id="CR-01",
            gate_id="G-FILE",
            spec=spec,
            freshness_key=key,
        )

    first = run_checks(
        [spec],
        cwd=tmp_path,
        before_execute=lambda spec, key: _claim("CA-01", spec, key),
    )[0]
    second = run_checks(
        [spec],
        cwd=tmp_path,
        before_execute=lambda spec, key: _claim("CA-02", spec, key),
    )[0]

    assert calls == 1
    assert first.status == "pass"
    assert first.started_at is not None
    assert first.ended_at is not None
    assert first.runner_fingerprint
    assert first.environment_fingerprint
    assert first.full_log_ref == ".ea/local/logs/file-proof.log"
    assert (tmp_path / first.full_log_ref).is_file()
    assert second.status == "blocked"
    assert second.started_at is None
    assert "without terminal receipt" in (second.details or "")


def test_file_exists_terminal_receipt_reuses_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receipt-only recovery reconstructs a non-command result exactly."""
    freshness_key = "d" * 64
    receipt = _file_receipt(freshness_key)
    calls = 0

    def _must_not_run(spec: CheckSpec, cwd: Path) -> CheckResult:
        nonlocal calls
        calls += 1
        raise AssertionError("file_exists reran despite terminal receipt")

    monkeypatch.setitem(CHECK_REGISTRY, "file_exists", _must_not_run)
    monkeypatch.setattr(
        gate_execution,
        "_load_receipt",
        lambda state_path, receipt_id: receipt,
    )
    spec = CheckSpec(
        kind="file_exists",
        name="G-FILE",
        args={"path": "sentinel"},
    )

    result = gate_execution.claim_gate_execution(
        tmp_path / ".ea" / "state.json",
        attempt_id="CA-02",
        criterion_id="CR-01",
        gate_id="G-FILE",
        spec=spec,
        freshness_key=freshness_key,
    )

    assert calls == 0
    assert result is not None
    assert result.status == "pass"
    assert result.details == f"reused gate receipt {receipt.id}"
    assert result.argv is None
    assert result.resolved_timeout_seconds is None
    assert result.stdout_tail is None
    assert result.stderr_tail is None
    assert result.started_at == receipt.started_at
    assert result.ended_at == receipt.ended_at
    assert result.runner_fingerprint == receipt.runner_digest
    assert result.environment_fingerprint == receipt.environment_digest
