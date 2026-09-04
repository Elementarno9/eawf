"""Committed GateReceipt hygiene and daemon scrub migration."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.enums import GateReceiptResult, StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.gate_receipt import (
    GateDiagnostic,
    GateReceipt,
    LegacyGateReceipt,
)
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.gate_receipt_hygiene import (
    append_gate_receipt,
    diagnostic_digest,
    diagnostic_log_path,
    diagnostic_path,
    scrub_gate_receipt_store,
    write_gate_diagnostic,
)

_NOW = datetime(2026, 7, 29, tzinfo=UTC)
_PRIVATE_ROOT = Path("/").joinpath("Users", "example")
_PRIVATE_TEST = _PRIVATE_ROOT / "private-test.py"
_PRIVATE_LOG_URI = (_PRIVATE_ROOT / "private.log").as_uri()


def _legacy_receipt() -> LegacyGateReceipt:
    return LegacyGateReceipt(
        id="GR-legacy",
        scope_id="P01-I01-W01",
        criterion_id="C-01",
        gate_id="G-01",
        integration_id="WI-01",
        integrated_sha="1" * 40,
        tree_sha="2" * 40,
        contract_digest="3" * 64,
        criteria_digest="4" * 64,
        gate_manifest_digest="5" * 64,
        policy_digest="6" * 64,
        dependency_binding_digest="7" * 64,
        runner_environment_digest="8" * 64,
        runner_digest="9" * 64,
        environment_digest="a" * 64,
        freshness_key="b" * 64,
        argv=["uv", "run", "pytest", _PRIVATE_TEST.as_posix()],
        argv_digest="c" * 64,
        command=f"uv run pytest {_PRIVATE_TEST.as_posix()}",
        timeout_class="quick",
        resolved_timeout_seconds=30,
        started_at=_NOW,
        ended_at=_NOW,
        duration_ms=0,
        result=GateReceiptResult.PASS,
        details="see https://example.invalid/private",
        exit_status=0,
        stdout_tail="private stdout",
        stderr_tail="private stderr",
        stdout_digest="d" * 64,
        stderr_digest="e" * 64,
        full_log_ref=".ea/local/close-logs/legacy.log",
    )


def _safe_receipt(receipt_id: str = "GR-safe") -> GateReceipt:
    return GateReceipt(
        id=receipt_id,
        scope_id="P01-I01-W01",
        criterion_id="C-01",
        gate_id="G-01",
        integration_id="WI-01",
        integrated_sha="1" * 40,
        tree_sha="2" * 40,
        contract_digest="3" * 64,
        criteria_digest="4" * 64,
        gate_manifest_digest="5" * 64,
        policy_digest="6" * 64,
        dependency_binding_digest="7" * 64,
        runner_environment_digest="8" * 64,
        runner_digest="9" * 64,
        environment_digest="a" * 64,
        freshness_key="b" * 64,
        started_at=_NOW,
        ended_at=_NOW,
        duration_ms=0,
        result=GateReceiptResult.PASS,
    )


def test_gate_diagnostic_and_receipt_writers_are_idempotent_and_collision_safe(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    log_bytes = b"private diagnostic\n"
    diagnostic = GateDiagnostic(
        id="GD-GR-safe",
        receipt_id="GR-safe",
        attempt_id="CA-01",
        scope_id="P01-I01-W01",
        criterion_id="C-01",
        gate_id="G-01",
        captured_at=_NOW,
        argv=["uv", "run", "pytest"],
        details="private detail",
        log_digest=hashlib.sha256(log_bytes).hexdigest(),
        log_present=True,
    )
    assert write_gate_diagnostic(state_path, diagnostic, log_bytes=log_bytes) == (
        diagnostic_digest(diagnostic)
    )
    assert write_gate_diagnostic(state_path, diagnostic, log_bytes=log_bytes) == (
        diagnostic_digest(diagnostic)
    )
    assert diagnostic_log_path(state_path, "GR-safe").read_bytes() == log_bytes
    assert diagnostic_path(state_path, "GR-safe").is_file()

    receipt = _safe_receipt()
    assert append_gate_receipt(state_path, receipt) is True
    assert append_gate_receipt(state_path, receipt) is False
    with pytest.raises(ValueError, match="id collision"):
        append_gate_receipt(
            state_path,
            receipt.model_copy(update={"runner_digest": "changed"}),
        )
    assert len(store_path(state_path, StoreKind.GATE_RECEIPT).read_bytes().splitlines()) == 1


def test_scrub_gate_receipt_store_moves_raw_fields_local_and_preserves_ids(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    original_state = b'{"gate_receipt_ids":["GR-legacy"]}\n'
    state_path.write_bytes(original_state)
    legacy = _legacy_receipt()
    source_log = tmp_path / legacy.full_log_ref
    source_log.parent.mkdir(parents=True)
    source_log.write_bytes(b"full private log\n")
    envelope = Envelope(
        id=legacy.id,
        kind=StoreKind.GATE_RECEIPT,
        scope_id=legacy.scope_id,
        created_at=_NOW,
        summary="legacy receipt https://example.invalid/private",
        payload=legacy.model_dump(mode="json"),
        blob_refs=[_PRIVATE_LOG_URI],
    )
    receipt_path = store_path(state_path, StoreKind.GATE_RECEIPT)
    receipt_path.parent.mkdir(parents=True)
    original_store = envelope.model_dump_json().encode() + b"\n"
    receipt_path.write_bytes(original_store)

    report = scrub_gate_receipt_store(state_path)

    assert report.changed is True
    assert report.migrated_count == 1
    assert report.quarantined_count == 0
    assert report.quarantine_path is not None
    assert report.quarantine_path.read_bytes() == original_store
    assert state_path.read_bytes() == original_state
    rewritten = receipt_path.read_bytes()
    assert _PRIVATE_ROOT.as_posix().encode() not in rewritten
    assert b"https://" not in rewritten
    assert b"private stdout" not in rewritten
    row = Envelope.model_validate(orjson.loads(rewritten))
    receipt = GateReceipt.model_validate(row.payload)
    assert row.id == receipt.id == "GR-legacy"
    assert diagnostic_path(state_path, receipt.id).name == "GR-legacy.json"
    local = orjson.loads(diagnostic_path(state_path, receipt.id).read_bytes())
    assert local["receipt_id"] == "GR-legacy"
    assert local["argv"][-1] == _PRIVATE_TEST.as_posix()
    assert local["details"] == "see https://example.invalid/private"
    assert diagnostic_log_path(state_path, receipt.id).read_bytes() == b"full private log\n"

    second = scrub_gate_receipt_store(state_path)
    assert second.changed is False
    assert receipt_path.read_bytes() == rewritten


def test_scrub_gate_receipt_store_quarantines_corrupt_rows_without_rewriting(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    receipt = _safe_receipt()
    envelope = Envelope(
        id=receipt.id,
        kind=StoreKind.GATE_RECEIPT,
        scope_id=receipt.scope_id,
        created_at=_NOW,
        summary=f"gate {receipt.gate_id} pass for {receipt.scope_id}",
        payload=receipt.model_dump(mode="json"),
    )
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    path.parent.mkdir(parents=True)
    original = envelope.model_dump_json().encode() + b"\n{broken-json\n"
    path.write_bytes(original)

    with pytest.raises(ValueError, match="blocked by malformed rows"):
        scrub_gate_receipt_store(state_path)

    assert path.read_bytes() == original
    quarantines = list(
        (state_path.parent / "local" / "gate-diagnostics" / "quarantine").glob(
            "gate_receipt-*.jsonl"
        )
    )
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == original


def test_scrub_gate_receipt_store_collision_leaves_committed_store_unchanged(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    legacy = _legacy_receipt()
    envelope = Envelope(
        id=legacy.id,
        kind=StoreKind.GATE_RECEIPT,
        scope_id=legacy.scope_id,
        created_at=_NOW,
        summary="legacy",
        payload=legacy.model_dump(mode="json"),
    )
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    path.parent.mkdir(parents=True)
    original = envelope.model_dump_json().encode() + b"\n"
    path.write_bytes(original)
    conflicting = GateDiagnostic(
        id="GD-GR-legacy",
        receipt_id="GR-legacy",
        scope_id=legacy.scope_id,
        criterion_id=legacy.criterion_id,
        gate_id=legacy.gate_id,
        captured_at=_NOW,
        details="different local observation",
    )
    write_gate_diagnostic(state_path, conflicting, log_bytes=None)

    with pytest.raises(ValueError, match="diagnostic collision"):
        scrub_gate_receipt_store(state_path)

    assert path.read_bytes() == original
    assert not diagnostic_log_path(state_path, legacy.id).exists()


def test_scrub_gate_receipt_store_resumes_after_replacement_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eawf.runtime.daemon import gate_receipt_hygiene

    state_path = tmp_path / ".ea" / "state.json"
    legacy = _legacy_receipt()
    envelope = Envelope(
        id=legacy.id,
        kind=StoreKind.GATE_RECEIPT,
        scope_id=legacy.scope_id,
        created_at=_NOW,
        summary="legacy",
        payload=legacy.model_dump(mode="json"),
    )
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    path.parent.mkdir(parents=True)
    original = envelope.model_dump_json().encode() + b"\n"
    path.write_bytes(original)
    atomic_write = gate_receipt_hygiene._atomic_write_bytes_locked

    def fail_replacement(target: Path, payload: bytes) -> None:
        if target == path:
            raise OSError("simulated replacement failure")
        atomic_write(target, payload)

    monkeypatch.setattr(
        gate_receipt_hygiene,
        "_atomic_write_bytes_locked",
        fail_replacement,
    )
    with pytest.raises(OSError, match="replacement failure"):
        scrub_gate_receipt_store(state_path)
    assert path.read_bytes() == original
    assert diagnostic_path(state_path, legacy.id).is_file()

    monkeypatch.setattr(
        gate_receipt_hygiene,
        "_atomic_write_bytes_locked",
        atomic_write,
    )
    report = scrub_gate_receipt_store(state_path)

    assert report.migrated_count == 1
    assert (
        GateReceipt.model_validate(Envelope.model_validate_json(path.read_bytes()).payload).id
        == legacy.id
    )
