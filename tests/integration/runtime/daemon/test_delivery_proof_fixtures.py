"""Native proofs bind verified fixtures and retain observations after checkout cleanup."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.delivery.receipts import ProofReceipt, canonical_digest
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import gate_execution
from eawf.runtime.daemon.gate_receipt_hygiene import (
    diagnostic_digest,
    diagnostic_log_path,
    load_gate_diagnostic,
)
from eawf.runtime.daemon.methods import DaemonValidationError, delivery_proof
from eawf.runtime.sandbox.test_fixtures import (
    TestFixtureBinding as FixtureBinding,
)
from eawf.runtime.sandbox.test_fixtures import (
    TestFixtureInput as FixtureInput,
)
from eawf.runtime.sandbox.test_fixtures import (
    resolve_test_fixtures,
)
from eawf.workflow.audit_dsl.models import CheckSpec
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import TASK_URN
from tests.integration.runtime.daemon.test_delivery_landed_loop import _dispatch
from tests.integration.runtime.daemon.test_delivery_proof_receipt_ids import _adopted, _gates

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def local_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def readonly(tmp_path: Path) -> Iterator[Path]:
    directory = tmp_path / "immutable-data"
    directory.mkdir()
    (directory / "pack").write_bytes(b"data")
    (directory / "pack").chmod(0o444)
    directory.chmod(0o555)
    yield directory
    directory.chmod(0o755)
    (directory / "pack").chmod(0o644)


def _declaration(content: bytes = b"data") -> FixtureInput:
    files = {"pack": hashlib.sha256(content).hexdigest()}
    return FixtureInput(
        id="example-data",
        environment_variable="CP_TEST_GAME_DATA",
        files=files,
        digest=canonical_digest(files),
    )


def _fixture_gates(tmp_path: Path, item: FixtureInput) -> list[dict[str, Any]]:
    gates = _gates(tmp_path)
    for gate in gates:
        gate["args"]["fixture_inputs"] = [item.model_dump(mode="json")]
    return gates


def test_real_scrubbed_child_receives_only_explicit_fixture(
    tmp_path: Path, readonly: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _declaration()
    bound = resolve_test_fixtures([item], [FixtureBinding(id=item.id, directory=readonly)])
    monkeypatch.setenv("GH_TOKEN", "synthetic-parent-secret")
    monkeypatch.setenv("CP_TEST_OTHER_DATA", "undeclared-parent-value")
    (tmp_path / "test_probe.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def test_fixture():\n"
        "    assert Path(os.environ['CP_TEST_GAME_DATA'], 'pack').read_bytes() == b'data'\n"
        "    assert 'GH_TOKEN' not in os.environ\n"
        "    assert 'CP_TEST_OTHER_DATA' not in os.environ\n"
        "    print('VERIFIED_FIXTURE_CHILD')\n"
    )
    context = gate_execution.GateExecutionContext(
        state_path=tmp_path / ".ea" / "state.json", attempt_id="fixture-child", fixtures=bound
    )
    result = gate_execution.run_gate_out_of_process(
        CheckSpec(
            kind="command_exit_zero",
            name="G-FIXTURE",
            args={
                "argv": ["pytest", "-q", "-s", "test_probe.py"],
                "scope": "all",
                "fixture_inputs": [item.model_dump(mode="json")],
            },
        ),
        cwd=tmp_path,
        context=context,
        criterion_id="CR-FIXTURE",
        gate_id="G-FIXTURE",
    )
    assert result.passed, result.details
    assert "VERIFIED_FIXTURE_CHILD" in (result.stdout_tail or "")


def test_native_fixtures_validate_before_reuse_and_observations_survive_cleanup(
    tmp_path: Path, readonly: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = hashlib.file_digest
    hashes: list[str] = []

    def counted(*args: Any, **kwargs: Any) -> Any:
        hashes.append("hash")
        return original(*args, **kwargs)

    monkeypatch.setattr(hashlib, "file_digest", counted)
    path = _adopted(tmp_path)
    item = _declaration()
    bindings = [{"id": item.id, "directory": str(readonly)}]
    gates = _fixture_gates(tmp_path, item)
    first = _dispatch(
        "runtime.delivery.prove_task",
        tmp_path,
        urn=TASK_URN,
        idempotency_key="fixture-1",
        gates=gates,
        fixture_bindings=bindings,
    )
    assert first["passed"]
    assert len(hashes) == 1  # two gate legs share one content verification
    records = read_ledger_records(ledger_path(path, Epoch2Collection.RECEIPT))
    proofs = [row.payload for row in records if row.payload.get("payload_kind") == "proof_receipt"]
    assert len(proofs) == 2
    for proof in proofs:
        receipt = ProofReceipt.model_validate(proof["receipt"])
        assert receipt.freshness.external_inputs[0].digest == item.digest
        assert receipt.diagnostic_ref is not None
        state = tmp_path / "repo" / ".ea" / "local" / "proof-evidence" / "state.json"
        diagnostic = load_gate_diagnostic(state, receipt.diagnostic_ref)
        assert diagnostic is not None and diagnostic.log_present
        assert receipt.diagnostic_digest == f"sha256:{diagnostic_digest(diagnostic)}"
        assert diagnostic_log_path(state, receipt.diagnostic_ref).is_file()
        assert str(readonly) not in orjson.dumps(proof).decode()
        legacy = receipt.model_dump(mode="json", exclude={"diagnostic_digest", "diagnostic_ref"})
        assert ProofReceipt.model_validate(legacy).diagnostic_ref is None
    assert not any((tmp_path / "repo" / ".ea" / "local" / "proof-runs").glob("*"))
    readonly.chmod(0o755)
    (readonly / "pack").chmod(0o644)
    (readonly / "pack").write_bytes(b"changed")
    (readonly / "pack").chmod(0o444)
    readonly.chmod(0o555)
    for key in ["fixture-1", "fixture-2"]:
        with pytest.raises(DaemonValidationError, match="proof_fixture_invalid"):
            _dispatch(
                "runtime.delivery.prove_task",
                tmp_path,
                urn=TASK_URN,
                idempotency_key=key,
                gates=gates,
                fixture_bindings=bindings,
            )
    changed = _declaration(b"changed")
    second = _dispatch(
        "runtime.delivery.prove_task",
        tmp_path,
        urn=TASK_URN,
        idempotency_key="fixture-3",
        gates=_fixture_gates(tmp_path, changed),
        fixture_bindings=bindings,
    )
    assert second["passed"]
    assert len(hashes) == 4  # each new invocation, including keyed reuse, verifies once
    assert {leg["result"] for leg in second["legs"]} == {"pass"}
    assert {leg["receipt_id"] for leg in second["legs"]} == {"RCP-0003", "RCP-0004"}
    assessed = _dispatch("runtime.delivery.task_assessment", tmp_path, urn=TASK_URN)
    assert assessed["answer"]["completable"]


def test_fixture_declaration_without_local_binding_cannot_false_pass(tmp_path: Path) -> None:
    _adopted(tmp_path)
    with pytest.raises(DaemonValidationError, match="proof_fixture_invalid"):
        _dispatch(
            "runtime.delivery.prove_task",
            tmp_path,
            urn=TASK_URN,
            idempotency_key="missing",
            gates=_fixture_gates(tmp_path, _declaration()),
        )


def _historical_proof(
    tmp_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], Any]:
    """Seed exactly the pre-fixture params/answer shape, without rewriting it."""
    from eawf.runtime.daemon.epoch2_recovery import (
        canonical_params_digest,
        record_idempotency_receipt,
    )
    from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context

    _adopted(tmp_path)
    context = method_context(tmp_path / "rt").native_root_context(tmp_path / "repo" / ".ea")
    params = {
        "urn": TASK_URN,
        "actor": "OP-0001",
        "idempotency_key": "historical",
        "expected_revision": 1,
        "gates": _gates(tmp_path),
    }
    answer = {
        "task_ref": TASK_URN,
        "legs": [
            {"gate_id": "G-01", "head_sha": "a" * 40, "result": "pass", "receipt_id": "RCP-0001"}
        ],
        "passed": True,
        "reason": "historical answer",
    }
    record_idempotency_receipt(
        context,
        namespaced_key=context.idempotency_key("runtime.delivery.prove_task:historical"),
        params_digest=canonical_params_digest(params),
        receipt=answer,
        recorded_at=datetime.now(UTC),
    )
    return params, answer, context


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_historical_fixture_free_direct_replay_preserves_payload_identity(
    tmp_path: Path, explicit_empty: bool
) -> None:
    params, answer, context = _historical_proof(tmp_path)
    incoming = {**params, **({"fixture_bindings": []} if explicit_empty else {})}
    args = delivery_proof.TaskProveParams.model_validate(incoming)
    assert args.model_dump(mode="json") == params
    replayed = delivery_proof.prove_task(context, args, now=datetime.now(UTC))
    assert replayed.model_dump(mode="json") == answer


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_historical_proof_replays_through_actual_cli_and_native_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_empty: bool
) -> None:
    from eawf.surfaces.cli.commands import domain as domain_cmd
    from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context
    from tests.integration.runtime.daemon.test_delivery_landed_loop import InProcessClient, eawf

    params, answer, _ = _historical_proof(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []

    class Client(InProcessClient):
        context = method_context(tmp_path / "rt")

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append((method, params))
            return super().call(method, params)

    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *_a, **_k: 0)
    monkeypatch.setattr(domain_cmd, "DaemonClient", Client)
    gates_path = tmp_path / "historical-gates.json"
    gates_path.write_bytes(orjson.dumps({"gates": params["gates"]}))
    extra: list[str] = []
    if explicit_empty:
        binding_path = tmp_path / "empty-bindings.json"
        binding_path.write_bytes(orjson.dumps({"fixture_bindings": []}))
        extra = ["--fixture-bindings", str(binding_path)]
    argv = [
        "task",
        "prove",
        TASK_URN,
        "--gates",
        str(gates_path),
        "--expected-task-revision",
        "1",
        "--idempotency-key",
        "historical",
        "--actor",
        "OP-0001",
        "--wait",
        *extra,
    ]
    result = eawf(tmp_path / "repo", *argv)
    assert result["result"] == answer
    method, submitted = calls[-1]
    assert method == "operation.submit"
    assert submitted["params"] == params

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a completed operation replay must not execute proof again")

    monkeypatch.setattr(delivery_proof, "prove_task", forbidden)
    assert eawf(tmp_path / "repo", *argv)["result"] == answer


def test_nonempty_fixture_bindings_remain_part_of_request_fingerprint(tmp_path: Path) -> None:
    from eawf.runtime.daemon.epoch2_recovery import canonical_params_digest

    params = {"urn": TASK_URN, "actor": "OP-0001", "idempotency_key": "bound"}
    plain = delivery_proof.TaskProveParams.model_validate(params).model_dump(mode="json")
    bound = delivery_proof.TaskProveParams.model_validate(
        {**params, "fixture_bindings": [{"id": "data", "directory": str(tmp_path)}]}
    ).model_dump(mode="json")
    moved = delivery_proof.TaskProveParams.model_validate(
        {**params, "fixture_bindings": [{"id": "data", "directory": str(tmp_path / "other")}]}
    ).model_dump(mode="json")
    assert len({canonical_params_digest(value) for value in (plain, bound, moved)}) == 3


def test_legacy_receipt_serialization_retains_exact_old_fields() -> None:
    from tests.unit.runtime.daemon.methods.test_filed_proof_key import _key, _receipt

    receipt = _receipt(_key())
    historical = receipt.model_dump(mode="json", exclude={"diagnostic_digest", "diagnostic_ref"})
    assert ProofReceipt.model_validate(historical).model_dump(mode="json") == historical
    assert canonical_digest(
        ProofReceipt.model_validate(historical).model_dump(mode="json")
    ) == canonical_digest(historical)


def test_nonempty_bindings_reach_actual_cli_native_operation(
    tmp_path: Path, readonly: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.surfaces.cli.commands import domain as domain_cmd
    from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context
    from tests.integration.runtime.daemon.test_delivery_landed_loop import InProcessClient, eawf

    _adopted(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []

    class Client(InProcessClient):
        context = method_context(tmp_path / "rt")

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append((method, params))
            return super().call(method, params)

    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *_a, **_k: 0)
    monkeypatch.setattr(domain_cmd, "DaemonClient", Client)
    item = _declaration()
    gates_path = tmp_path / "bound-gates.json"
    gates_path.write_bytes(orjson.dumps({"gates": _fixture_gates(tmp_path, item)}))
    bindings = [{"id": item.id, "directory": str(readonly)}]
    binding_path = tmp_path / "bindings.json"
    binding_path.write_bytes(orjson.dumps({"fixture_bindings": bindings}))
    result = eawf(
        tmp_path / "repo",
        "task",
        "prove",
        TASK_URN,
        "--gates",
        str(gates_path),
        "--fixture-bindings",
        str(binding_path),
        "--expected-task-revision",
        "1",
        "--idempotency-key",
        "bound",
        "--actor",
        "OP-0001",
        "--wait",
    )
    assert result["result"]["passed"]
    assert calls[-1][0] == "operation.submit"
    assert calls[-1][1]["params"]["fixture_bindings"] == bindings
