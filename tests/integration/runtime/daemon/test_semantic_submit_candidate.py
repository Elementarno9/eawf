"""W19: ``submit_candidate`` is reachable through the semantic gateway.

Driven through the registered ``semantic.call`` verb over a canary
provisioned under ``tmp_path``, so what runs is the shipped gateway and
the shipped handler rather than a library call spelled in a friendlier
way. The workspace lease is seeded directly rather than earned through a
full native dispatch: this suite is about the handler seam, not about
compiling, leasing and spawning a worker, and the lease store is a plain
JSON file a canary holds regardless of how it was issued.

A valid call writes exactly one candidate-submission line and one
receipt. A call refused by one of the nine pre-handler checks writes no
candidate line at all -- the receipt ledger still grows, because a
refusal is filed as a receipt an operator can read, and every other tool
in this daemon is proven to do the same. Three checks are exercised as a
representative subset (grant, scope, lease): they are the three a
candidate submission actually depends on -- who may call it, whether its
paths stay inside the Run's write set, and whether a workspace exists to
have produced the claim at all -- while the remaining six are already
proven total over every tool by the gateway's own suite.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

import eawf.runtime.worktree.git as git
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.lease import WorkLease
from eawf.kernel.runtime.semantic import SemanticCall, SemanticToolErrorCode
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.run import RUN_BIND_METHOD
from eawf.runtime.daemon.methods.semantic import SEMANTIC_CALL_METHOD
from eawf.runtime.daemon.semantic_gateway import PreHandlerCheck
from eawf.runtime.integration.git_workspace import COMMIT_ARTIFACT_PREFIX, candidate_pin_ref
from eawf.runtime.workspace.lease import workspace_path, write_lease
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    TASK_URN,
    method_context,
    root_context,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_native_dispatch import make_repo

pytestmark = pytest.mark.integration


RUN_KEY: Final = "RUN-00000010"
SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
RUN_URN: Final = f"{SLOT}/run/{RUN_KEY}"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
WALL_CEILING: Final = 3600
ROUTE_REVISION: Final = 3
LEASE_ID: Final = f"LSE-{'a' * 32}"
WORKSPACE_HANDLE: Final = f"wsh-{'b' * 32}"
SUBMISSION_REF: Final = "artifact://work/candidate-0001"
RESULT_DIGEST: Final = f"sha256:{'1' * 64}"


def digest(char: str) -> str:
    """Return a well-formed digest whose body is one repeated character."""
    return f"sha256:{char * 64}"


def task_scope() -> dict[str, Any]:
    """Return the scope of a Run executing one Task, which may write under ``src``."""
    return {
        "scope_kind": "task",
        "purpose": "implement",
        "task_ref": TASK_URN,
        "write_set": ["src"],
    }


def make_canary(root: Path) -> CanaryProvision:
    """Provision a canary holding one RUNNING, task-scoped Run over a real repository.

    A real repository, because the seeded lease's workspace is now a real
    worktree of it: the handler pins the commit a submission names against
    that worktree's actual HEAD, which a bare canary tree has none of.
    """
    make_repo(root)
    provisioned = provision_canary(repo_root=root, ref=canary_ref("CND"), provisioned_at=AT)
    row = seed_row("run", "RUNNING")
    row["scope"] = task_scope()
    row["started_at"] = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    seed(provisioned, {"run": {RUN_KEY: row}})
    return provisioned


def executor_capsule(**overrides: Any) -> AuthorityCapsule:
    """Return the sealed capsule of a task-scoped executor Run."""
    fields: dict[str, Any] = {
        "run_ref": RUN_URN,
        "scope_ref": TASK_URN,
        "scope_digest": digest("a"),
        "agent_role": AgentSessionRole.EXECUTOR.value,
        "purpose": "implement",
        "authority": {"state": "read_only", "workspace": "scoped_write"},
        "tool_grants": ("budget_status", "submit_candidate"),
        "budget": {"wall_seconds": WALL_CEILING, "output_bytes": 1_048_576},
        "criteria_digest": digest("b"),
        "policy_digest": digest("c"),
        "compiled_spec_digest": digest("d"),
        "report_schema_ref": "schema://executor-report/v1",
        "stop_conditions": ("budget_exhausted",),
    }
    fields.update(overrides)
    return AuthorityCapsule.seal(fields)


def call_verb(method: str, ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb the way the socket listener does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


def bind(ctx: MethodContext, canary: CanaryProvision, capsule: AuthorityCapsule) -> None:
    """Record the contract this Run's calls must echo."""
    call_verb(
        RUN_BIND_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        compiled_spec_digest=digest("d"),
        authority_capsule_digest=capsule.contract_digest,
        route_policy_revision=ROUTE_REVISION,
    )


def seed_lease(
    canary: CanaryProvision,
    runtime_root: Path,
    *,
    lease_id: str = LEASE_ID,
    task_ref: str = TASK_URN,
    writable_roots: tuple[str, ...] = ("src",),
) -> WorkLease:
    """Seed one active workspace lease over a real worktree, bypassing native dispatch.

    The lease store is a plain JSON file under the canary's local store,
    written the same way a real dispatch writes it, so seeding it here
    exercises the same read path the gateway's lease check uses. The
    workspace itself is a real git worktree of the canary's repository,
    added off its own base commit exactly as a real lease issuance would,
    because the handler's pin check reads that worktree's actual HEAD.
    """
    now = datetime.now(UTC)
    context = root_context(canary, runtime_root)
    base = git.commit_sha(canary.root, "main")
    target = workspace_path(context, handle=WORKSPACE_HANDLE)
    target.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(canary.root, branch=f"lease/{lease_id}", path=target, base=base)
    lease = WorkLease.model_validate(
        {
            "lease_id": lease_id,
            "run_ref": RUN_URN,
            "task_ref": task_ref,
            "purpose": "implement",
            "workspace_handle": WORKSPACE_HANDLE,
            "workspace_generation": 1,
            "branch": f"lease/{lease_id}",
            "base_commit": base,
            "writable_roots": list(writable_roots),
            "issued_at": (now - timedelta(seconds=5)).isoformat(),
            "heartbeat_at": (now - timedelta(seconds=5)).isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "status_at": (now - timedelta(seconds=5)).isoformat(),
            "status": "active",
        }
    )
    write_lease(context, lease)
    return lease


def commit_in_seeded_lease(
    canary: CanaryProvision,
    runtime_root: Path,
    *,
    paths: tuple[str, ...] = ("src/module.py",),
    content: str = "x = 2\n",
) -> str:
    """Commit *content* to *paths* in the seeded lease's worktree and return its artifact ref."""
    workspace = workspace_path(root_context(canary, runtime_root), handle=WORKSPACE_HANDLE)
    for path in paths:
        (workspace / path).parent.mkdir(parents=True, exist_ok=True)
        (workspace / path).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "--", *paths], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "candidate work"], cwd=workspace, check=True)
    return f"{COMMIT_ARTIFACT_PREFIX}{git.commit_sha(workspace, 'HEAD')}"


def seal_call(
    *,
    capsule: AuthorityCapsule,
    payload: dict[str, Any],
    key: str = "call-key-01",
    ordinal: int = 1,
) -> SemanticCall:
    """Return a sealed submit_candidate call whose payload digest is computed for it."""
    return SemanticCall.seal(
        {
            "call_id": f"call-{ordinal:016x}",
            "run_ref": RUN_URN,
            "contract_digest": capsule.contract_digest,
            "idempotency_key": key,
            "tool_id": "submit_candidate",
            "tool_schema_version": "1.0.0",
            "payload": payload,
            "requested_at": AT,
        }
    )


def invoke(
    ctx: MethodContext, canary: CanaryProvision, call: SemanticCall, capsule: AuthorityCapsule
) -> dict[str, Any]:
    """Send one semantic call through the registered verb."""
    return call_verb(
        SEMANTIC_CALL_METHOD,
        ctx,
        repo_root=str(canary.root),
        call=call.model_dump(mode="json"),
        capsule=capsule.model_dump(mode="json"),
    )


def candidate_payload(
    *,
    task_ref: str = TASK_URN,
    lease_id: str = LEASE_ID,
    submission_ref: str = SUBMISSION_REF,
    changed_paths: tuple[str, ...] = ("src/module.py",),
    resulting_tree_digest: str = RESULT_DIGEST,
) -> dict[str, Any]:
    """Return the payload of one candidate submission."""
    return {
        "tool_id": "submit_candidate",
        "task_ref": task_ref,
        "lease_id": lease_id,
        "submission_ref": submission_ref,
        "changed_paths": list(changed_paths),
        "resulting_tree_digest": resulting_tree_digest,
    }


def receipt_lines(canary: CanaryProvision, runtime_root: Path) -> int:
    """Return how many receipt lines the canary's receipt ledger holds."""
    context = root_context(canary, runtime_root)
    with context.session([RUN_URN]) as session:
        return len(read_ledger_records(session.ledger_path(Epoch2Collection.RECEIPT)))


def submission_lines(canary: CanaryProvision, runtime_root: Path) -> list[dict[str, Any]]:
    """Return every candidate-submission line the canary's run ledger holds."""
    context = root_context(canary, runtime_root)
    with context.session([RUN_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    return [
        item.payload
        for item in records
        if item.payload.get("payload_kind") == "candidate_submission"
    ]


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(runtime_root)


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one running, task-scoped Run, with its capsule bound."""
    return make_canary(tmp_path / "repo")


# ---------------------------------------------------------------------------
# CR-01: a valid call writes a candidate record and a receipt
# ---------------------------------------------------------------------------


def test_a_valid_submission_writes_a_candidate_record_and_a_receipt(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    capsule = executor_capsule()
    bind(ctx, canary, capsule)
    seed_lease(canary, runtime_root)
    ref = commit_in_seeded_lease(canary, runtime_root)
    before_receipts = receipt_lines(canary, runtime_root)

    answer = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, payload=candidate_payload(submission_ref=ref)),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert answer["result"]["status"] == "succeeded"
    assert output["tool_id"] == "submit_candidate"
    assert output["candidate_ref"].startswith("CND-")
    assert output["sealed_at"] is None
    submitted = submission_lines(canary, runtime_root)
    assert len(submitted) == 1
    assert submitted[0]["candidate_ref"] == output["candidate_ref"]
    assert submitted[0]["changed_paths"] == ["src/module.py"]
    assert receipt_lines(canary, runtime_root) == before_receipts + 1
    pinned = git.commit_sha(canary.root, candidate_pin_ref(output["candidate_ref"]))
    assert f"{COMMIT_ARTIFACT_PREFIX}{pinned}" == ref


# ---------------------------------------------------------------------------
# A representative subset of the nine pre-handler checks: zero mutation
# ---------------------------------------------------------------------------


def test_a_call_with_no_grant_is_denied_and_records_no_candidate(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    capsule = executor_capsule(tool_grants=("budget_status",))
    bind(ctx, canary, capsule)
    seed_lease(canary, runtime_root)
    before_receipts = receipt_lines(canary, runtime_root)

    answer = invoke(ctx, canary, seal_call(capsule=capsule, payload=candidate_payload()), capsule)

    assert answer["result"]["status"] == "denied"
    assert answer["refused_check"] == PreHandlerCheck.GRANT.value
    assert answer["result"]["error"]["code"] == SemanticToolErrorCode.CAPABILITY_DENIED.value
    assert submission_lines(canary, runtime_root) == []
    assert receipt_lines(canary, runtime_root) == before_receipts + 1


def test_a_call_whose_paths_escape_the_write_set_is_denied_and_records_no_candidate(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    capsule = executor_capsule()
    bind(ctx, canary, capsule)
    seed_lease(canary, runtime_root)
    before_receipts = receipt_lines(canary, runtime_root)
    payload = candidate_payload(changed_paths=("outside/file.py",))

    answer = invoke(ctx, canary, seal_call(capsule=capsule, payload=payload), capsule)

    assert answer["result"]["status"] == "denied"
    assert answer["refused_check"] == PreHandlerCheck.SCOPE.value
    assert answer["result"]["error"]["field_path"] == "/changed_paths"
    assert submission_lines(canary, runtime_root) == []
    assert receipt_lines(canary, runtime_root) == before_receipts + 1


def test_a_call_from_a_run_holding_no_lease_is_denied_and_records_no_candidate(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """No lease is seeded at all: the workspace this claim would be about never existed."""
    capsule = executor_capsule()
    bind(ctx, canary, capsule)
    before_receipts = receipt_lines(canary, runtime_root)

    answer = invoke(ctx, canary, seal_call(capsule=capsule, payload=candidate_payload()), capsule)

    assert answer["result"]["status"] == "denied"
    assert answer["refused_check"] == PreHandlerCheck.LEASE.value
    assert answer["result"]["error"]["code"] == SemanticToolErrorCode.LEASE_NOT_ACTIVE.value
    assert submission_lines(canary, runtime_root) == []
    assert receipt_lines(canary, runtime_root) == before_receipts + 1


# ---------------------------------------------------------------------------
# Idempotent replay, and the one conflict a replay is not
# ---------------------------------------------------------------------------


def test_a_second_submission_of_the_same_work_replays_and_writes_no_second_record(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """Two calls under different idempotency keys still name one candidate."""
    capsule = executor_capsule()
    bind(ctx, canary, capsule)
    seed_lease(canary, runtime_root)
    ref = commit_in_seeded_lease(canary, runtime_root)
    payload = candidate_payload(submission_ref=ref)

    first = invoke(
        ctx, canary, seal_call(capsule=capsule, payload=payload, key="candidate-01"), capsule
    )
    second = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, payload=payload, key="candidate-02", ordinal=2),
        capsule,
    )

    first_ref = first["result"]["bounded_output"]["candidate_ref"]
    second_ref = second["result"]["bounded_output"]["candidate_ref"]
    assert first["result"]["status"] == second["result"]["status"] == "succeeded"
    assert first_ref == second_ref
    assert len(submission_lines(canary, runtime_root)) == 1


def test_the_same_identity_with_other_content_is_refused_and_writes_no_second_record(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """Two different claims under one identity is not a replay."""
    capsule = executor_capsule()
    bind(ctx, canary, capsule)
    seed_lease(canary, runtime_root)
    ref = commit_in_seeded_lease(canary, runtime_root)
    first_payload = candidate_payload(submission_ref=ref)
    invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, payload=first_payload, key="candidate-01"),
        capsule,
    )
    conflicting = candidate_payload(submission_ref=ref, changed_paths=("src/other.py",))

    with pytest.raises(DaemonValidationError, match="candidate_payload_conflict"):
        invoke(
            ctx,
            canary,
            seal_call(capsule=capsule, payload=conflicting, key="candidate-02", ordinal=2),
            capsule,
        )

    assert len(submission_lines(canary, runtime_root)) == 1
