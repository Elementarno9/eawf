"""The delivery loop run end to end through the CLI, on a real repository.

Two walks carry a Task from running work to a completed Task, and on to an
accepted Milestone, through nothing but the ``eawf`` commands an operator
types, with the CLI's daemon client dispatching into the registered verbs
in process:

- the landed walk adopts work that already landed on the Batch's target
  branch, proves the Task at that head, completes it, merges and
  reconciles the Batch by reading the branch back, and accepts the
  Milestone on a sealed approval that cites recorded evidence;
- the native walk submits and seals a candidate from a leased worktree,
  integrates it, proves the Task on the delivered commit and completes it.

The verbs the walks lean on are also driven directly, for the refusals a
walk never reaches: an adoption of a head the branch does not carry, of a
change nothing recorded, onto a Batch that is not active; a proof whose
gate fails; a read-back of a branch that does not carry the pinned head.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    effective_records,
    read_ledger_records,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import MethodContext
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from tests.integration.runtime.daemon._delivery_verb_fixtures import BRANCH
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    document_path,
    method_context,
    seed,
    seed_row,
)
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

runner = CliRunner()

ACTOR = "OP-0001"
RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
AUDIT_ID = "A-LANDED-01"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


# ---- the in-process daemon the CLI talks to ---------------------------------


class InProcessClient:
    """The CLI's daemon client, answering from the registered verbs in process."""

    context: ClassVar[MethodContext | None] = None

    def __init__(self, *_a: object, **_k: object) -> None:
        return None

    def __enter__(self) -> InProcessClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        from eawf.surfaces.cli._daemon_client import DaemonRpcError

        assert type(self).context is not None
        try:
            answer = asyncio.run(methods.dispatch(method, type(self).context, params))
        except methods.DaemonValidationError as error:
            raise DaemonRpcError(-32002, str(error)) from error
        assert isinstance(answer, dict)
        return answer


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Route the CLI's daemon client into the registered verbs."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *_a, **_k: 0)
    monkeypatch.setattr(domain_cmd, "DaemonClient", InProcessClient)
    InProcessClient.context = method_context(tmp_path / "runtime")
    methods.ensure_all_methods_registered()
    yield
    InProcessClient.context = None


def eawf(workspace: Path, *argv: str) -> dict[str, Any]:
    """Run one ``eawf --json`` command, assert it succeeded, and return its answer."""
    result = runner.invoke(app, ["--workspace", str(workspace), "--json", *argv])
    assert result.exit_code == 0, result.output
    answer = orjson.loads(result.stdout)
    assert isinstance(answer, dict)
    return answer


def spec(tmp_path: Path, name: str, payload: Any) -> str:
    """Write *payload* to a JSON file and return its path."""
    path = tmp_path / name
    path.write_bytes(orjson.dumps(payload))
    return str(path)


def gates_file(tmp_path: Path, *, exit_code: int = 0) -> str:
    """Return a gates file whose two gates look for the landed content.

    Args:
        tmp_path: Where the file is written.
        exit_code: ``0`` for gates that find what the landed change wrote,
            anything else for gates that look for content nothing wrote.
    """
    wanted = "x = 2" if exit_code == 0 else "x = 3"
    gates = [
        {
            **world.gate(criterion_id).model_dump(mode="json"),
            "args": {"argv": ["git", "grep", "-q", wanted, "--", "src/module.py"]},
        }
        for criterion_id in ("CR-01", "CR-02")
    ]
    return spec(tmp_path, f"gates-{exit_code}.json", {"gates": gates})


# ---- a repository whose target branch already carries the work --------------


def landed_canary(tmp_path: Path, *, legacy_row: bool = False) -> tuple[CanaryProvision, str, str]:
    """Return a canary whose target branch carries one landed change.

    Args:
        tmp_path: Where the repository is made.
        legacy_row: Also seed a batch row imported from epoch 1, which no
            native model reads, as a cut-over tree holds.

    Returns:
        The canary, the base commit, and the landed head.
    """
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "ci@example.com")
    git(root, "config", "user.name", "ci")
    (root / "src").mkdir()
    (root / "src" / "module.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "-b", BRANCH)
    (root / "src" / "module.py").write_text("x = 2\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "landed fix")
    head = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "main")
    canary = provision_canary(repo_root=root, ref=canary_ref("LND"), provisioned_at=AT)
    milestone = seed_row("milestone", "ACTIVE")
    milestone["required_batch_refs"] = [BATCH_URN]
    seed(
        canary,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {"MLS-0030": milestone},
            "batch": {
                "BAT-0007": seed_row("batch", "ACTIVE"),
                **(
                    {"P37-I01": {"payload": {"id": "P37-I01"}, "status": "PLANNED"}}
                    if legacy_row
                    else {}
                ),
            },
            "task": {"EAWF-0042": world.task_row(status=world.TaskStatus.RUNNING)},
            "run": {"RUN-00000010": seed_row("run", "RUNNING")},
        },
    )
    append_ledger_record(
        ledger_path(document_path(canary), Epoch2Collection.AUDIT),
        LedgerRecord(
            collection=Epoch2Collection.AUDIT,
            record_key=AUDIT_ID,
            status="appended",
            recorded_at=AT,
            payload={"payload": {"id": AUDIT_ID}},
        ),
    )
    return canary, base, head


def stored(canary: CanaryProvision, collection: Epoch2Collection, key: str) -> dict[str, Any]:
    """Return one record from whichever tier holds it now."""
    path = document_path(canary)
    row = read_document(path).get(collection.value, {}).get(key)
    if isinstance(row, dict):
        return row
    lines = effective_records(read_ledger_records(ledger_path(path, collection)))
    payload = next(item.payload for item in lines if item.record_key == key)
    assert isinstance(payload, dict)
    return payload


def _adopt(canary: CanaryProvision, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """Dispatch one adoption of the landed change and return the raw answer."""
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": BATCH_URN,
        "actor": ACTOR,
        "idempotency_key": "adopt-1",
        "task_refs": [TASK_URN],
        "report_verdict": "pass",
        "evidence_refs": [AUDIT_ID],
        **overrides,
    }
    return asyncio.run(
        methods.dispatch("runtime.delivery.adopt_landed", method_context(tmp_path / "rt"), params)
    )


# ---- the landed walk, through the CLI ---------------------------------------


def test_landed_work_walks_to_an_accepted_milestone_through_the_cli(
    tmp_path: Path, cli: None
) -> None:
    canary, base, head = landed_canary(tmp_path, legacy_row=True)
    root = canary.root
    ready = spec(
        tmp_path,
        "ready.json",
        {"observations": ["run_report_bound"], "binding_refs": [AUDIT_ID]},
    )
    eawf(root, "task", "ready", TASK_URN, "--expected-task-revision", "1",
         "--idempotency-key", "ready-1", "--actor", ACTOR, "--from-spec", ready)  # fmt: skip
    adopted = eawf(root, "batch", "adopt-landed", BATCH_URN, "--head", head, "--base", base,
                   "--task", TASK_URN, "--evidence", AUDIT_ID, "--expected-batch-revision", "1",
                   "--idempotency-key", "adopt-1", "--actor", ACTOR)["result"]  # fmt: skip
    assert adopted["generation"] == 2
    proved = eawf(root, "task", "prove", TASK_URN, "--gates", gates_file(tmp_path),
                  "--expected-task-revision", "2",
                  "--idempotency-key", "prove-1", "--actor", ACTOR)["result"]  # fmt: skip
    assert proved["passed"] is True
    out = tmp_path / "assessment.json"
    assessed = eawf(root, "task", "assess", TASK_URN, "--actor", ACTOR, "--out", str(out))["result"]
    assert assessed["integrated_commit"] == head
    eawf(root, "task", "complete", TASK_URN, "--expected-task-revision", "2",
         "--idempotency-key", "complete-1", "--actor", ACTOR,
         "--integrated-commit", head, "--assessment", str(out))  # fmt: skip
    binding = stored(canary, Epoch2Collection.TASK, "EAWF-0042")["integrated_binding"]
    assert binding == assessed["integrated_binding"]
    assert binding["head_sha"] == head
    finished = {"observations": ["run_report_bound"], "updates": {"ended_at": AT.isoformat()}}
    finish = spec(tmp_path, "finish.json", finished)
    eawf(root, "run", "finish", RUN_URN, "--expected-run-revision", "1",
         "--idempotency-key", "finish-1", "--actor", ACTOR, "--from-spec", finish)  # fmt: skip
    batch_ready = spec(tmp_path, "batch-ready.json", {"updates": {"current_head_binding": binding}})
    eawf(root, "batch", "ready", BATCH_URN, "--expected-batch-revision", "1",
         "--idempotency-key", "bready-1", "--actor", ACTOR, "--from-spec", batch_ready)  # fmt: skip
    eawf(root, "batch", "merge", BATCH_URN, "--expected-batch-revision", "2",
         "--idempotency-key", "merge-1", "--actor", ACTOR)  # fmt: skip
    reconciled = eawf(root, "batch", "reconcile", BATCH_URN, "--expected-batch-revision", "3",
                      "--idempotency-key", "reconcile-1", "--actor", ACTOR)["result"]  # fmt: skip
    assert reconciled["outcome"] == "landed"
    merge_facts = {
        "observations": ["host_merge_observed"],
        "binding_refs": [reconciled["record_key"]],
    }
    observed = spec(tmp_path, "observed.json", merge_facts)
    eawf(root, "batch", "observe-merge", BATCH_URN, "--expected-batch-revision", "3",
         "--idempotency-key", "observe-1", "--actor", ACTOR, "--from-spec", observed)  # fmt: skip
    eawf(root, "batch", "complete", BATCH_URN, "--expected-batch-revision", "4",
         "--idempotency-key", "bcomplete-1", "--actor", ACTOR)  # fmt: skip
    review = spec(tmp_path, "review.json", {"updates": {"acceptance_bundle_revision": 1}})
    eawf(root, "milestone", "open-review", MILESTONE_URN, "--expected-milestone-revision", "1",
         "--idempotency-key", "review-1", "--actor", ACTOR, "--from-spec", review)  # fmt: skip
    recorded = eawf(root, "record", "evidence", MILESTONE_URN, "--kind", "audit",
                    "--summary", f"audit {AUDIT_ID} passed on {head}", "--expected-revision", "2",
                    "--idempotency-key", "evd-1", "--actor", ACTOR)  # fmt: skip
    evidence = recorded["result"]["evidence_ref"]
    approval = spec(tmp_path, "approval.json", {
        "requested_by": {"principal_kind": "human", "principal_id": ACTOR},
        "steps": [{"step_id": "AS-01", "passed": True, "observation": "the fix holds on main",
                   "evidence_kinds": ["audit"], "evidence_refs": [evidence]}],
        "accepted_binding": binding,
    })  # fmt: skip
    bundle = tmp_path / "bundle.json"
    opened = eawf(root, "milestone", "open-approval", MILESTONE_URN, "--actor", ACTOR,
                  "--expected-milestone-revision", "2",
                  "--from-spec", approval, "--bundle-out", str(bundle))["result"]  # fmt: skip
    sealed = eawf(root, "milestone", "seal-approval", opened["action_ref"],
                  "--expected-approval-revision", str(opened["revision"]),
                  "--idempotency-key", "seal-1", "--actor", ACTOR,
                  "--option-id", "approve", "--receipt-ref", evidence)["result"]  # fmt: skip
    eawf(root, "milestone", "accept", MILESTONE_URN, "--expected-milestone-revision", "2",
         "--idempotency-key", "accept-1", "--actor", ACTOR,
         "--approval-receipt-ref", sealed["action_ref"],
         "--acceptance-bundle", str(bundle))  # fmt: skip

    assert stored(canary, Epoch2Collection.TASK, "EAWF-0042")["status"] == "COMPLETED"
    assert stored(canary, Epoch2Collection.RUN, "RUN-00000010")["status"] == "COMPLETED"
    assert stored(canary, Epoch2Collection.BATCH, "BAT-0007")["status"] == "COMPLETED"
    assert stored(canary, Epoch2Collection.MILESTONE, "MLS-0030")["status"] == "COMPLETED"


# ---- adoption, directly ------------------------------------------------------


def test_adoption_selects_a_generation_on_the_landed_head(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)

    answer = _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    assert answer["generation_id"] == "ING-000002"
    assert answer["tree_sha"] == git(canary.root, "rev-parse", f"{head}^{{tree}}")
    assert answer["target_head_sha"] == head
    assert answer["changed_paths"] == 1
    assert answer["replayed"] is False


def test_adopting_the_same_head_again_replays(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)
    _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    again = _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    assert again["replayed"] is True
    assert again["generation_id"] == "ING-000002"


def test_adoption_refuses_a_head_the_target_branch_does_not_carry(tmp_path: Path) -> None:
    canary, base, _ = landed_canary(tmp_path)
    git(canary.root, "checkout", "-q", "-b", "elsewhere", base)
    (canary.root / "src" / "other.py").write_text("y = 1\n", encoding="utf-8")
    git(canary.root, "add", "src/other.py")
    git(canary.root, "commit", "-q", "-m", "unlanded")
    stray = git(canary.root, "rev-parse", "HEAD")

    with pytest.raises(methods.DaemonValidationError, match="adoption_head_unlanded"):
        _adopt(canary, tmp_path, base_commit=base, head_sha=stray)


def test_adoption_refuses_evidence_no_ledger_holds(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)

    with pytest.raises(methods.DaemonValidationError, match="adoption_evidence_unheld"):
        _adopt(canary, tmp_path, base_commit=base, head_sha=head, evidence_refs=["A-NOWHERE"])


def test_adoption_refuses_a_commit_the_repository_does_not_hold(tmp_path: Path) -> None:
    canary, base, _ = landed_canary(tmp_path)

    with pytest.raises(methods.DaemonValidationError, match="adoption_commit_unknown"):
        _adopt(canary, tmp_path, base_commit=base, head_sha="f" * 40)


def test_adoption_refuses_a_base_that_does_not_precede_the_head(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)

    with pytest.raises(methods.DaemonValidationError, match="adoption_base_diverged"):
        _adopt(canary, tmp_path, base_commit=head, head_sha=base)


def test_adoption_refuses_a_batch_that_is_not_active(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)
    seed(canary, {"batch": {"BAT-0007": seed_row("batch", "READY_TO_MERGE")}})

    with pytest.raises(methods.DaemonValidationError, match="adoption_batch_not_active"):
        _adopt(canary, tmp_path, base_commit=base, head_sha=head)


def test_adoption_refuses_a_task_that_has_not_started(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)
    seed(canary, {"task": {"EAWF-0042": world.task_row(status=world.TaskStatus.PLANNED)}})

    with pytest.raises(methods.DaemonValidationError, match="adoption_task_not_started"):
        _adopt(canary, tmp_path, base_commit=base, head_sha=head)


def test_adoption_refuses_a_non_delivering_verdict(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)

    with pytest.raises(methods.DaemonValidationError, match="report_verdict"):
        _adopt(canary, tmp_path, base_commit=base, head_sha=head, report_verdict="fail")


# ---- proof and assessment, directly -----------------------------------------


def _dispatch(method: str, tmp_path: Path, **params: Any) -> dict[str, Any]:
    """Dispatch one verb against the landed canary in *tmp_path*."""
    return asyncio.run(
        methods.dispatch(
            method,
            method_context(tmp_path / "rt"),
            {"repo_root": str(tmp_path / "repo"), "actor": ACTOR, **params},
        )
    )


def test_a_failing_gate_files_a_failing_receipt_and_does_not_pass(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)
    _adopt(canary, tmp_path, base_commit=base, head_sha=head)
    gates = orjson.loads(Path(gates_file(tmp_path, exit_code=1)).read_bytes())["gates"]

    answer = _dispatch(
        "runtime.delivery.prove_task", tmp_path, urn=TASK_URN, idempotency_key="p", gates=gates
    )

    assert answer["passed"] is False
    assert {leg["result"] for leg in answer["legs"]} == {"fail"}
    assessed = _dispatch("runtime.delivery.task_assessment", tmp_path, urn=TASK_URN)
    assert assessed["answer"]["completable"] is False
    assert assessed["integrated_binding"] is None


def test_a_second_proof_reuses_the_passing_receipts(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)
    _adopt(canary, tmp_path, base_commit=base, head_sha=head)
    gates = orjson.loads(Path(gates_file(tmp_path)).read_bytes())["gates"]
    _dispatch(
        "runtime.delivery.prove_task", tmp_path, urn=TASK_URN, idempotency_key="p", gates=gates
    )

    again = _dispatch("runtime.delivery.prove_task", tmp_path, urn=TASK_URN, idempotency_key="q")

    assert again["passed"] is True
    assert {leg["result"] for leg in again["legs"]} == {"reused"}


def test_proof_with_no_gate_named_or_filed_is_refused(tmp_path: Path) -> None:
    canary, base, head = landed_canary(tmp_path)
    _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    with pytest.raises(methods.DaemonValidationError, match="proof_gates_unnamed"):
        _dispatch("runtime.delivery.prove_task", tmp_path, urn=TASK_URN, idempotency_key="p")


def test_assessment_of_a_task_nothing_adopted_or_sealed_is_refused(tmp_path: Path) -> None:
    landed_canary(tmp_path)

    with pytest.raises(methods.DaemonValidationError):
        _dispatch("runtime.delivery.task_assessment", tmp_path, urn=TASK_URN)


# ---- the read-back, directly ------------------------------------------------


def test_read_back_of_a_branch_missing_the_pinned_head_stays_unknown(tmp_path: Path) -> None:
    canary, _, _ = landed_canary(tmp_path)
    merging = seed_row("batch", "MERGING")
    seed(canary, {"batch": {"BAT-0007": merging}})

    answer = _dispatch(
        "runtime.delivery.read_back_merge", tmp_path, urn=BATCH_URN, idempotency_key="r"
    )

    assert answer["outcome"] == "unknown"
    assert answer["resolved"] is False


def test_read_back_of_an_absent_branch_stays_unknown(tmp_path: Path) -> None:
    canary, _, _ = landed_canary(tmp_path)
    merging = seed_row("batch", "MERGING")
    merging["target_branch"] = "feature/never-created"
    seed(canary, {"batch": {"BAT-0007": merging}})

    answer = _dispatch(
        "runtime.delivery.read_back_merge", tmp_path, urn=BATCH_URN, idempotency_key="r"
    )

    assert answer["outcome"] == "unknown"
