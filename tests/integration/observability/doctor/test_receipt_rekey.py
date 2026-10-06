"""``eawf doctor --fix`` re-keys filed proof receipts by appending.

A fixture repository's receipt ledger holds a proof filed under its current
key, one filed under a key its inputs never produced, one whose inputs a
later line respelled while keeping the old key, and a line that is no
proof at all. The preview names one ``ledger.receipt-rekey`` action over
the two stale effective proofs; the apply, served by the registered daemon
method in process with only that action selected, only appends; every
effective proof then validates strictly; and a rerun previews nothing.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.delivery.receipts import (
    ProofFreshnessKey,
    ProofReceipt,
    RevisionBinding,
    RevisionRefKind,
    canonical_digest,
)
from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.spec.common import GateSpec
from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerTornTailError,
    append_ledger_record,
    effective_records,
    line_digest,
    read_ledger_records,
    render_ledger_line,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.doctor.checks import CheckResult
from eawf.observability.doctor.receipt_rekey import (
    RECEIPT_REKEY_ACTION,
    ReceiptRekey,
    plan_receipt_rekeys,
    receipt_rekey_preview,
    rekeyed_record,
    selected_receipt_ledger,
)
from eawf.observability.doctor.repair import build_repair_plan, digest_repair_actions
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.console_records import read_proof_receipts
from eawf.runtime.daemon.methods.delivery_completion import PROOF_PAYLOAD_KIND, FiledProof
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context

runner = CliRunner()

AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
TASK = "EAWF-0013"
STALE_KEY = "0" * 64
PRE_CHANGE_KEY = "1" * 64


@pytest.fixture(autouse=True)
def isolated_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep registry, canary scratch and host probes inside this test."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(tmp_path / "home" / ".eawf" / "registry.json"))
    for target, value in (
        ("global_config_path", lambda: tmp_path / "missing-global-config.yaml"),
        ("check_manifest_in_sync", lambda **_k: CheckResult(name="manifest_in_sync", status="ok")),
        ("_desired_sync_drift", lambda _workspace: False),
        ("check_launchd_agent", lambda: CheckResult(name="launchd_agent", status="ok")),
        ("find_stray_daemons", lambda: []),
    ):
        monkeypatch.setattr(f"eawf.observability.doctor.repair.{target}", value)


def _freshness(batch: str) -> ProofFreshnessKey:
    binding = RevisionBinding(
        repository_ref=parse_qualified_urn("eawf://EAWF/repository/EAWF"),
        ref_kind=RevisionRefKind.INTEGRATION,
        head_sha="1" * 40,
        tree_sha="2" * 40,
        parent_sha="4" * 40,
        batch_ref=parse_qualified_urn(f"eawf://EAWF/batch/{batch}"),
        integration_generation=2,
        manifest_digest=canonical_digest("manifest"),
        criteria_digest=canonical_digest("criteria"),
        policy_digest=canonical_digest("binding-policy"),
        environment_digest=None,
        bound_at=AT,
    )
    return ProofFreshnessKey(
        revision_binding=binding,
        criterion_digest=canonical_digest("criterion"),
        gate_digest=canonical_digest("gate"),
        selector_digest=canonical_digest("selector"),
        policy_digest=canonical_digest("policy"),
        runner_digest=canonical_digest("runner"),
        environment_digest=canonical_digest("environment"),
    )


def _proof_line(receipt_id: str, batch: str, *, stored_key: str | None = None) -> LedgerRecord:
    """Return a filed-proof line, its stored freshness key overridden when *stored_key* is given."""
    freshness = _freshness(batch)
    receipt = ProofReceipt(
        id=receipt_id,
        scope_id=TASK,
        gate_id="G-01",
        criterion_ids=("CR-001",),
        evidence_kind="deterministic",
        freshness=freshness,
        freshness_key=freshness.digest(),
        result=GateReceiptResult.PASS,
        exit_status=0,
        started_at=AT,
        ended_at=AT + timedelta(seconds=5),
    )
    gate = GateSpec(
        id="G-01",
        criterion_id="CR-001",
        kind="command_exit_zero",
        args={"argv": ["uv", "run", "pytest", "-q"]},
        policy="block",
        cadence="every-wave",
    )
    payload = FiledProof(
        task_ref=parse_qualified_urn(f"eawf://EAWF/task/{TASK}"), gate=gate, receipt=receipt
    ).model_dump(mode="json")
    key = stored_key or receipt.freshness_key
    payload["receipt"]["freshness_key"] = key
    return LedgerRecord(
        collection=Epoch2Collection.RECEIPT,
        record_key=f"PRF-{key[:16]}-{TASK}",
        status="pass",
        recorded_at=AT,
        payload=payload,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Build a managed repository whose receipt ledger holds stale filed proofs."""
    built = tmp_path / "repo"
    init = ["--no-input", "init", "--target", str(built), "--project-code", "DEMO"]
    assert runner.invoke(app, init).exit_code == exit_codes.OK
    authority = require_native_authority(built / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(document_path)
    document.setdefault("project", {})["DEMO"] = {"payload": {"code": "DEMO"}}
    write_document(document_path, document)
    ledger = _ledger(built)
    append_ledger_record(ledger, _proof_line("RCP-0001", "BAT-0099"))
    append_ledger_record(ledger, _proof_line("RCP-0002", "BAT-0100", stored_key=STALE_KEY))
    old = _proof_line("RCP-0003", "BAT-0100", stored_key=PRE_CHANGE_KEY)
    append_ledger_record(ledger, old)
    respelled = _proof_line("RCP-0003", "BAT-0101", stored_key=PRE_CHANGE_KEY)
    append_ledger_record(
        ledger,
        respelled.model_copy(update={"supersedes": line_digest(render_ledger_line(old))}),
    )
    append_ledger_record(
        ledger,
        LedgerRecord(
            collection=Epoch2Collection.RECEIPT,
            record_key="NOTE-1",
            status="recorded",
            recorded_at=AT,
            payload={"note": "not a proof"},
        ),
    )
    return built


def _ledger(root: Path) -> Path:
    ledger = selected_receipt_ledger(root)
    assert ledger is not None
    return ledger


def _rekey_actions(answer: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in answer.get("actions", []) if a["action_id"] == RECEIPT_REKEY_ACTION]


def _doctor_preview(root: Path) -> dict[str, Any]:
    result = runner.invoke(app, ["--workspace", str(root), "--json", "doctor", "--fix"])
    assert result.exit_code == exit_codes.OK, result.stdout
    payload = json.loads(result.stdout)
    return payload.get("result", payload)


def _apply_only_rekey(root: Path, runtime: Path) -> dict[str, Any]:
    """Apply only the re-key action through the registered daemon method, as an operator would."""
    plan = build_repair_plan(root)
    (action,) = [a for a in plan.actions if a.action_id == RECEIPT_REKEY_ACTION]
    methods.ensure_all_methods_registered()
    params = {
        "repo_root": str(root),
        "preview_digest": plan.preview_digest,
        "selected_preview_digest": digest_repair_actions([action]),
        "action_ids": [RECEIPT_REKEY_ACTION],
        "action_previews": {RECEIPT_REKEY_ACTION: action.preview_digest},
    }
    answer = asyncio.run(methods.dispatch("doctor.apply_repair", method_context(runtime), params))
    assert isinstance(answer, dict)
    return answer


def _effective_proofs(ledger: Path) -> dict[str, tuple[LedgerRecord, FiledProof]]:
    return {
        record.record_key: (record, FiledProof.model_validate(record.payload))
        for record in effective_records(read_ledger_records(ledger))
        if record.payload.get("payload_kind") == PROOF_PAYLOAD_KIND
    }


def test_plan_covers_each_effective_stale_proof(root: Path) -> None:
    rows = plan_receipt_rekeys(_ledger(root))

    assert [row.line for row in rows] == [2, 4]
    assert [row.freshness_key for row in rows] == [
        _freshness("BAT-0100").digest(),
        _freshness("BAT-0101").digest(),
    ]
    for row in rows:
        assert row.record_key == f"PRF-{row.freshness_key[:16]}-{TASK}"


def test_a_stale_effective_proof_is_refused_on_read_before_the_repair(root: Path) -> None:
    with pytest.raises(ValidationError, match="freshness_key does not match its inputs"):
        read_proof_receipts(_ledger(root), "RCP-0003")


def test_fix_previews_applies_by_appending_and_reruns_clean(root: Path, tmp_path: Path) -> None:
    ledger = _ledger(root)
    before = ledger.read_bytes()

    preview = _doctor_preview(root)

    (action,) = _rekey_actions(preview)
    assert (action["record_count"], action["mutation_class"]) == (2, "committed_store")
    assert ledger.read_bytes() == before

    answer = _apply_only_rekey(root, tmp_path / "runtime")

    assert [(row["action_id"], row["status"]) for row in answer["actions"]] == [
        (RECEIPT_REKEY_ACTION, "applied")
    ]
    after = ledger.read_bytes()
    assert after.startswith(before)
    assert after[len(before) :].count(b"\n") == 2
    proofs = _effective_proofs(ledger)
    assert sorted(proof.receipt.id for _record, proof in proofs.values()) == [
        "RCP-0001",
        "RCP-0002",
        "RCP-0003",
    ]
    for key, (record, proof) in proofs.items():
        assert proof.receipt.freshness_key == proof.receipt.freshness.digest()
        assert key == f"PRF-{proof.receipt.freshness_key[:16]}-{TASK}"
        assert record.status == "pass"
    (respelled,) = read_proof_receipts(ledger, "RCP-0003")
    assert respelled.freshness.revision_binding.batch_ref.entity_key == "BAT-0101"

    assert _rekey_actions(_doctor_preview(root)) == []
    assert plan_receipt_rekeys(ledger) == ()


def test_apply_refuses_when_the_ledger_moved_since_the_preview(root: Path, tmp_path: Path) -> None:
    from eawf.runtime.daemon.methods.doctor import _apply_receipt_rekey

    (action,) = [a for a in build_repair_plan(root).actions if a.action_id == RECEIPT_REKEY_ACTION]
    ledger = _ledger(root)
    append_ledger_record(ledger, _proof_line("RCP-0004", "BAT-0100", stored_key=STALE_KEY))
    before = ledger.read_bytes()

    with pytest.raises(methods.DaemonValidationError, match="preview changed"):
        _apply_receipt_rekey(method_context(tmp_path / "rt"), root, action)

    assert ledger.read_bytes() == before


def test_rekeyed_record_changes_only_the_keys(root: Path) -> None:
    ledger = _ledger(root)
    row = plan_receipt_rekeys(ledger)[0]
    original = read_ledger_records(ledger)[row.line - 1]

    record = rekeyed_record(ledger, row, recorded_at=AT)

    assert record.supersedes == row.supersedes
    assert (record.collection, record.record_key, record.status) == (
        original.collection,
        row.record_key,
        original.status,
    )
    assert record.payload["receipt"]["freshness_key"] == row.freshness_key
    restored = json.loads(json.dumps(record.payload))
    restored["receipt"]["freshness_key"] = original.payload["receipt"]["freshness_key"]
    assert restored == original.payload


def test_a_missing_ledger_plans_nothing(tmp_path: Path) -> None:
    assert plan_receipt_rekeys(tmp_path / "receipt.jsonl") == ()


def test_an_empty_ledger_plans_nothing(tmp_path: Path) -> None:
    ledger = tmp_path / "receipt.jsonl"
    ledger.write_bytes(b"")

    assert plan_receipt_rekeys(ledger) == ()


def test_a_single_stale_proof_plans_one(tmp_path: Path) -> None:
    ledger = tmp_path / "receipt.jsonl"
    append_ledger_record(ledger, _proof_line("RCP-0001", "BAT-0100", stored_key=STALE_KEY))

    (row,) = plan_receipt_rekeys(ledger)

    assert (row.line, row.freshness_key) == (1, _freshness("BAT-0100").digest())


def test_a_ledger_of_current_proofs_plans_nothing(tmp_path: Path) -> None:
    ledger = tmp_path / "receipt.jsonl"
    append_ledger_record(ledger, _proof_line("RCP-0001", "BAT-0100"))

    assert plan_receipt_rekeys(ledger) == ()


def test_a_torn_ledger_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "receipt.jsonl"
    ledger.write_bytes(render_ledger_line(_proof_line("RCP-0001", "BAT-0100")).encode())

    with pytest.raises(LedgerTornTailError):
        plan_receipt_rekeys(ledger)


def test_a_line_that_is_not_a_record_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "receipt.jsonl"
    ledger.write_bytes(b'{"not": "a record"}\n')

    with pytest.raises(ValidationError):
        plan_receipt_rekeys(ledger)


@pytest.mark.parametrize("drop", ["freshness", "receipt"])
def test_a_proof_without_freshness_inputs_is_refused(tmp_path: Path, drop: str) -> None:
    line = _proof_line("RCP-0001", "BAT-0100", stored_key=STALE_KEY)
    payload = json.loads(json.dumps(line.payload))
    if drop == "receipt":
        del payload["receipt"]
    else:
        del payload["receipt"]["freshness"]
    ledger = tmp_path / "receipt.jsonl"
    append_ledger_record(ledger, line.model_copy(update={"payload": payload}))

    with pytest.raises(ValidationError, match="ProofFreshnessKey"):
        plan_receipt_rekeys(ledger)


def test_an_epoch_one_tree_has_no_receipt_ledger(tmp_path: Path) -> None:
    assert selected_receipt_ledger(tmp_path) is None


def test_the_preview_moves_with_the_ledger(root: Path) -> None:
    ledger = _ledger(root)
    rows = plan_receipt_rekeys(ledger)
    first = receipt_rekey_preview(ledger, rows)
    append_ledger_record(ledger, _proof_line("RCP-0005", "BAT-0100"))

    assert receipt_rekey_preview(ledger, rows) != first


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("line", 0),
        ("supersedes", "sha256:short"),
        ("record_key", "RCP-0001"),
        ("record_key", "PRF-0123-EAWF-0013"),
        ("freshness_key", "A" * 64),
        ("freshness_key", "0" * 63),
        ("extra", 1),
    ],
)
def test_a_malformed_planned_row_is_refused(field: str, value: object) -> None:
    fields: dict[str, object] = {
        "line": 1,
        "supersedes": "sha256:" + "0" * 64,
        "record_key": f"PRF-{'0' * 16}-{TASK}",
        "freshness_key": "0" * 64,
    }
    fields[field] = value

    with pytest.raises(ValidationError):
        ReceiptRekey.model_validate(fields)
