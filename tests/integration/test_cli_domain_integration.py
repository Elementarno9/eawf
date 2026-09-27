"""CLI tests for the verbs that produce the facts the lifecycle moves are judged by.

Each command in :mod:`eawf.surfaces.cli.commands.domain_integration` is
dispatch over one daemon verb, so these tests drive the real Typer app
with a stand-in daemon client and assert one RPC under the right name
carrying the right parameters, the daemon's refusal printed unchanged
with the refusal exit, and an answer that did not pass exiting non-zero.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import orjson
import pytest
from typer.testing import CliRunner

from eawf.runtime.daemon import methods
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from eawf.surfaces.cli.commands import domain_integration as integration_cmd

pytestmark = pytest.mark.integration

runner = CliRunner()

_ROOT = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY"
_TASK = f"{_ROOT}/task/CANARY-0001"
_RUN = f"{_ROOT}/run/RUN-00000001"
_BATCH = f"{_ROOT}/batch/BAT-0001"
_MILESTONE = f"{_ROOT}/milestone/MLS-0001"
_ACTION = f"{_ROOT}/pending-action/ACT-0001"
_SHA = "a" * 40
_BASE = "b" * 40

#: The daemon verbs no operator command sends, and why each is left out:
#: ``assess_completion`` takes a hand-assembled document that
#: ``task_assessment`` now assembles, and the verification cycle and the
#: acceptance repair belong to the review skills rather than to the loop.
_UNEXPOSED: frozenset[str] = frozenset(
    {
        "runtime.delivery.assess_completion",
        "runtime.delivery.verify_batch",
        "runtime.delivery.request_acceptance_repair",
    }
)


class _FakeClient:
    """A stand-in daemon client answering by method name."""

    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []
    answers: ClassVar[dict[str, Any]] = {}

    def __init__(self, *_a: object, **_k: object) -> None:
        return None

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        type(self).calls.append((method, dict(params)))
        answer = type(self).answers[method]
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, dict)
        return answer


@pytest.fixture(autouse=True)
def _fake_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every call to the stand-in and keep the escalation gate quiet."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *_a, **_k: 0)
    monkeypatch.setattr(domain_cmd, "DaemonClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.answers = {}


def _invoke(tmp_path: Path, *argv: str) -> Any:
    """Run the CLI against *tmp_path* as the workspace."""
    return runner.invoke(app, ["--workspace", str(tmp_path), *argv])


def _json(tmp_path: Path, name: str, payload: Any) -> Path:
    """Write *payload* to a JSON file under *tmp_path*."""
    path = tmp_path / name
    path.write_bytes(orjson.dumps(payload))
    return path


# ---- the verb table ---------------------------------------------------------


def test_every_integration_verb_is_registered_on_the_daemon() -> None:
    methods.ensure_all_methods_registered()
    registered = set(methods.registered_methods())

    assert set(integration_cmd.INTEGRATION_CLI_METHODS) <= registered


def test_every_delivery_verb_is_exposed_or_named_as_unexposed() -> None:
    """CLI set plus the named exclusions is exactly the daemon's delivery set."""
    methods.ensure_all_methods_registered()
    delivery = {
        name
        for name in methods.registered_methods()
        if name.startswith(("runtime.delivery.", "runtime.candidate."))
    }

    assert set(integration_cmd.INTEGRATION_CLI_METHODS) | _UNEXPOSED == delivery
    assert not set(integration_cmd.INTEGRATION_CLI_METHODS) & _UNEXPOSED


# ---- each command forwards one RPC ------------------------------------------


def test_task_seal_forwards_the_candidate_and_exits_nonzero_when_unsealed(
    tmp_path: Path,
) -> None:
    _FakeClient.answers[integration_cmd.CANDIDATE_REPORT_BIND] = {
        "candidate_ref": "CND-1",
        "sealed": False,
        "replayed": False,
        "failed_checks": ["base_commit_agrees"],
        "bundle": None,
        "reason": "checks do not hold",
    }
    result = _invoke(
        tmp_path,
        "task", "seal", _RUN,
        "--candidate-ref", "CND-1",
        "--resulting-tree-digest", "sha256:" + "c" * 64,
        "--idempotency-key", "seal-1",
        "--actor", "OPERATOR",
    )  # fmt: skip
    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT
    method, params = _FakeClient.calls[0]
    assert method == integration_cmd.CANDIDATE_REPORT_BIND
    assert params["candidate_ref"] == "CND-1"
    assert params["verdict"] is None
    assert "base_commit_agrees" in result.output


def test_task_prove_sends_the_gates_file(tmp_path: Path) -> None:
    gates = [{"id": "G-01", "criterion_id": "CR-001"}]
    _FakeClient.answers[integration_cmd.DELIVERY_PROVE] = {
        "task_ref": _TASK,
        "legs": [{"gate_id": "G-01", "head_sha": _SHA, "result": "pass", "receipt_id": "RCP-0001"}],
        "passed": True,
        "reason": "every leg passes",
    }
    result = _invoke(
        tmp_path,
        "task", "prove", _TASK,
        "--idempotency-key", "prove-1",
        "--actor", "OPERATOR",
        "--gates", str(_json(tmp_path, "gates.json", {"gates": gates})),
    )  # fmt: skip
    assert result.exit_code == exit_codes.OK, result.output
    method, params = _FakeClient.calls[0]
    assert method == integration_cmd.DELIVERY_PROVE
    assert params["gates"] == gates
    assert "G-01 pass RCP-0001" in result.output


def test_task_prove_without_gates_reruns_the_filed_ones(tmp_path: Path) -> None:
    """Boundary: no gates file sends no gates key, so the daemon reuses the filed ones."""
    _FakeClient.answers[integration_cmd.DELIVERY_PROVE] = {
        "task_ref": _TASK, "legs": [], "passed": False, "reason": "a leg failed",
    }  # fmt: skip
    result = _invoke(
        tmp_path, "task", "prove", _TASK, "--idempotency-key", "prove-1", "--actor", "OPERATOR"
    )
    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT
    assert "gates" not in _FakeClient.calls[0][1]


def _assessment(*, completable: bool) -> dict[str, Any]:
    """Return a task_assessment answer."""
    return {
        "answer": {
            "task_ref": _TASK,
            "batch_ref": _BATCH,
            "head_generation": 2,
            "delivering_generation": 2,
            "affected_criterion_ids": [],
            "rerun_gate_ids": [] if completable else ["G-01"],
            "reused_gate_ids": ["G-01"] if completable else [],
            "unavailable_gate_ids": [],
            "completable": completable,
            "reason": "judged",
        },
        "integrated_commit": _SHA,
        "assessment": {"report_verdict": "pass"},
        "integrated_binding": None,
    }


def test_task_assess_writes_the_document_task_complete_takes(tmp_path: Path) -> None:
    _FakeClient.answers[integration_cmd.DELIVERY_TASK_ASSESSMENT] = _assessment(completable=True)
    out = tmp_path / "a.json"
    result = _invoke(tmp_path, "task", "assess", _TASK, "--actor", "OPERATOR", "--out", str(out))
    assert result.exit_code == exit_codes.OK, result.output
    assert orjson.loads(out.read_bytes()) == {"report_verdict": "pass"}
    assert f"integrated_commit={_SHA}" in result.output
    assert "idempotency_key" not in _FakeClient.calls[0][1]


def test_task_assess_exits_nonzero_while_a_leg_is_unproven(tmp_path: Path) -> None:
    _FakeClient.answers[integration_cmd.DELIVERY_TASK_ASSESSMENT] = _assessment(completable=False)
    result = _invoke(tmp_path, "task", "assess", _TASK, "--actor", "OPERATOR")
    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT
    assert "rerun: G-01" in result.output


def test_batch_integrate_assembles_then_integrates(tmp_path: Path) -> None:
    request = {"urn": _BATCH, "actor": "OPERATOR", "idempotency_key": "assembled-1"}
    _FakeClient.answers[integration_cmd.DELIVERY_ASSEMBLE] = request
    _FakeClient.answers[integration_cmd.DELIVERY_INTEGRATE] = {
        "batch_ref": _BATCH,
        "candidates": [],
        "manifest_ids": [],
        "generation_ids": ["ING-000002"],
        "commit_messages": [],
        "delivered": True,
        "reason": "delivered",
    }
    refs = {"base": {"head_sha": _BASE}, "exit_refs": {}, "diagnostic_ref": "EVD"}
    result = _invoke(
        tmp_path,
        "batch", "integrate", _BATCH,
        "--actor", "OPERATOR",
        "--from-spec", str(_json(tmp_path, "refs.json", refs)),
    )  # fmt: skip
    assert result.exit_code == exit_codes.OK, result.output
    assert [call[0] for call in _FakeClient.calls] == [
        integration_cmd.DELIVERY_ASSEMBLE,
        integration_cmd.DELIVERY_INTEGRATE,
    ]
    assert _FakeClient.calls[0][1]["base"] == {"head_sha": _BASE}
    assert _FakeClient.calls[1][1]["idempotency_key"] == "assembled-1"


def test_batch_integrate_stops_when_assembly_is_refused(tmp_path: Path) -> None:
    _FakeClient.answers[integration_cmd.DELIVERY_ASSEMBLE] = DaemonRpcError(
        -32002, "validation_failed: assembly_candidates_absent: no planned Task has sealed"
    )
    result = _invoke(
        tmp_path,
        "batch", "integrate", _BATCH,
        "--actor", "OPERATOR",
        "--from-spec", str(_json(tmp_path, "refs.json", {})),
    )  # fmt: skip
    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT
    assert "assembly_candidates_absent" in result.output
    assert len(_FakeClient.calls) == 1


def test_batch_adopt_landed_forwards_every_fact(tmp_path: Path) -> None:
    _FakeClient.answers[integration_cmd.DELIVERY_ADOPT_LANDED] = {
        "batch_ref": _BATCH,
        "adoption_key": "ADP-1",
        "generation_id": "ING-000002",
        "generation": 2,
        "head_sha": _SHA,
        "tree_sha": "c" * 40,
        "target_branch": "main",
        "target_head_sha": _SHA,
        "changed_paths": 3,
        "replayed": False,
        "reason": "adopted",
    }
    result = _invoke(
        tmp_path,
        "batch", "adopt-landed", _BATCH,
        "--head", _SHA, "--base", _BASE,
        "--task", _TASK, "--task", f"{_ROOT}/task/CANARY-0002",
        "--evidence", "A-P36-I01-eval-r4",
        "--idempotency-key", "adopt-1",
        "--actor", "OPERATOR",
    )  # fmt: skip
    assert result.exit_code == exit_codes.OK, result.output
    _, params = _FakeClient.calls[0]
    assert params["head_sha"] == _SHA
    assert params["base_commit"] == _BASE
    assert params["task_refs"] == [_TASK, f"{_ROOT}/task/CANARY-0002"]
    assert params["report_verdict"] == "pass"
    assert params["evidence_refs"] == ["A-P36-I01-eval-r4"]
    assert params["affected_criterion_ids"] == []


def test_batch_adopt_landed_without_evidence_is_refused_by_typer(tmp_path: Path) -> None:
    """Boundary: evidence is required, so a bare adoption never reaches the daemon."""
    result = _invoke(
        tmp_path,
        "batch", "adopt-landed", _BATCH,
        "--head", _SHA, "--base", _BASE, "--task", _TASK,
        "--idempotency-key", "adopt-1", "--actor", "OPERATOR",
    )  # fmt: skip
    assert result.exit_code == 2
    assert _FakeClient.calls == []


_RECONCILED: dict[str, Any] = {
    "batch_ref": _BATCH,
    "outcome": "landed",
    "from_status": "MERGING",
    "to_status": "COMPLETED",
    "resolved": True,
    "record_key": "BMR-000000000000-BAT-0001",
    "reason": "the branch carries the exact head",
}


def test_batch_reconcile_with_an_observation_forwards_it(tmp_path: Path) -> None:
    _FakeClient.answers[integration_cmd.DELIVERY_RECONCILE_MERGE] = _RECONCILED
    document = {"batch_ref": _BATCH, "target_branch": "main"}
    result = _invoke(
        tmp_path,
        "batch", "reconcile", _BATCH,
        "--idempotency-key", "reconcile-1", "--actor", "OPERATOR",
        "--observation", str(_json(tmp_path, "obs.json", document)),
    )  # fmt: skip
    assert result.exit_code == exit_codes.OK, result.output
    method, params = _FakeClient.calls[0]
    assert method == integration_cmd.DELIVERY_RECONCILE_MERGE
    assert params["observation"] == document


def test_batch_reconcile_without_an_observation_reads_the_branch_back(tmp_path: Path) -> None:
    _FakeClient.answers[integration_cmd.DELIVERY_READ_BACK_MERGE] = _RECONCILED
    result = _invoke(
        tmp_path,
        "batch", "reconcile", _BATCH, "--idempotency-key", "reconcile-1", "--actor", "OPERATOR",
    )  # fmt: skip
    assert result.exit_code == exit_codes.OK, result.output
    method, params = _FakeClient.calls[0]
    assert method == integration_cmd.DELIVERY_READ_BACK_MERGE
    assert "observation" not in params
    assert "outcome landed" in result.output


@pytest.mark.parametrize("length", [0, 129])
def test_batch_reconcile_key_bounds_are_refused_before_the_wire(
    tmp_path: Path, length: int
) -> None:
    result = _invoke(
        tmp_path,
        "batch", "reconcile", _BATCH, "--idempotency-key", "k" * length, "--actor", "OPERATOR",
    )  # fmt: skip
    assert result.exit_code == exit_codes.USER_ERROR
    assert _FakeClient.calls == []


def test_batch_reconcile_refusal_renders_the_daemons_code(tmp_path: Path) -> None:
    _FakeClient.answers[integration_cmd.DELIVERY_READ_BACK_MERGE] = DaemonRpcError(
        -32002, "validation_failed: reconcile_batch_not_merging: batch BAT-0001 is ACTIVE"
    )
    result = _invoke(
        tmp_path,
        "batch", "reconcile", _BATCH, "--idempotency-key", "reconcile-1", "--actor", "OPERATOR",
    )  # fmt: skip
    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT
    assert "reconcile_batch_not_merging" in result.output


def test_milestone_open_approval_writes_the_bundle(tmp_path: Path) -> None:
    bundle = {"milestone_ref": _MILESTONE, "revision": 1}
    _FakeClient.answers[integration_cmd.DELIVERY_OPEN_APPROVAL] = {
        "action_ref": _ACTION,
        "status": "WAITING",
        "revision": 1,
        "acceptance_bundle": bundle,
        "created": True,
        "reason": "asks",
    }
    out = tmp_path / "bundle.json"
    spec = {"requested_by": {}, "steps": [], "accepted_binding": {}}
    result = _invoke(
        tmp_path,
        "milestone", "open-approval", _MILESTONE,
        "--actor", "OPERATOR",
        "--from-spec", str(_json(tmp_path, "approval.json", spec)),
        "--bundle-out", str(out),
    )  # fmt: skip
    assert result.exit_code == exit_codes.OK, result.output
    assert orjson.loads(out.read_bytes()) == bundle
    assert _FakeClient.calls[0][1]["steps"] == []


def test_record_evidence_forwards_the_row(tmp_path: Path) -> None:
    _FakeClient.answers[integration_cmd.DELIVERY_RECORD_EVIDENCE] = {
        "evidence_ref": f"{_ROOT}/evidence/EVD-0001",
        "created": True,
    }
    result = _invoke(
        tmp_path,
        "record", "evidence", _MILESTONE,
        "--kind", "audit", "--summary", "audit A-P36-I01-eval-r4 passed",
        "--idempotency-key", "evd-1", "--actor", "OPERATOR",
    )  # fmt: skip
    assert result.exit_code == exit_codes.OK, result.output
    _, params = _FakeClient.calls[0]
    assert params["kind"] == "audit"
    assert "EVD-0001" in result.output


def test_unreachable_daemon_is_a_daemon_unreachable_error(tmp_path: Path) -> None:
    _FakeClient.answers[integration_cmd.DELIVERY_TASK_ASSESSMENT] = TimeoutError("no answer")
    result = _invoke(tmp_path, "task", "assess", _TASK, "--actor", "OPERATOR")
    assert result.exit_code == exit_codes.DAEMON_UNREACHABLE
