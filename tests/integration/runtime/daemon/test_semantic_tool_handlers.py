"""RUN-021: delegation is refused with a receipt, and a plan is validated.

Both halves are driven through the registered ``semantic.call`` verb over
a canary provisioned under ``tmp_path``, so what runs is the shipped
gateway and the shipped handlers rather than a library call spelled in a
friendlier way. No provider is started, no socket is opened and no
repository outside the test's own directory is read or written.

Delegation is refused with a receipt, which is the distinction that
matters. An agent asking for a second agent is not an error the daemon
drops on the floor: the call is admitted as far as the scope check, is
denied there, and the denial is filed as a receipt line an operator can
read afterwards. Each case therefore asserts the closed code, the field
that caused it, and that the receipt ledger grew by exactly one.

The plan half proves the routing rather than the validator. The
validator has its own suite; what is asserted here is that the tool
reaches it -- the same function a plan submission runs -- and hands back
its refusal as a finding with the same closed code, and that a plan that
validates leaves no plan-revision row behind. Validating is not
submitting: a worker's proposal earns a verdict, and recording the
revision stays an operator verb.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.candidate import CandidateSubmission, candidate_identity
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.compiled import canonical_digest
from eawf.kernel.runtime.lease import LeaseStatus, WorkLease
from eawf.kernel.runtime.semantic import (
    SemanticCall,
    SemanticToolErrorCode,
    SemanticToolId,
)
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2.plan_revision import (
    PlanBody,
    PlanRevision,
    PlanRevisionStatus,
    plan_content_digest,
)
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.candidate.seal import submission_record
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.run import RUN_BIND_METHOD
from eawf.runtime.daemon.methods.semantic import SEMANTIC_CALL_METHOD
from eawf.runtime.daemon.semantic_gateway import AGENT_LAUNCH_COMMANDS, PreHandlerCheck
from eawf.runtime.daemon.semantic_handlers import (
    BROKERED_TOOLS,
    REPORTABLE_PLAN_CODES,
    SEMANTIC_HANDLERS,
    PlanProposalArtifact,
    SpikeReportArtifact,
    _record_plan_proposal_artifact,
    _record_spike_report_artifact,
)
from eawf.runtime.workspace.lease import write_lease
from eawf.workflow.planning.revision import PlanRefusalCode
from eawf.workflow.skills import integrate as integrate_skill
from eawf.workflow.skills.engine import SkillContext
from eawf.workflow.skills.lifecycle_rpc import RpcCaller, RpcRefusedError, refusal_code
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    method_context,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration


RUN_KEY: Final = "RUN-00000010"
SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
RUN_URN: Final = f"{SLOT}/run/{RUN_KEY}"
REPOSITORY_URN: Final = f"{SLOT}/repository/REP-EAWF"
#: The fixture project code the seeded v1 state carries; promoted URNs
#: resolve under it.
V1_STATE_SCOPE: Final = "QR"
V1_STATE_FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)
TRACK_URN: Final = f"{SLOT}/track/TRK-RUNTIME"
FINDING_URN: Final = f"{SLOT}/campaign-finding/CFN-0001"
PLAN_TASK_URN: Final = f"{SLOT}/task/EAWF-0042"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
HEAD: Final = "a" * 40
WALL_CEILING: Final = 3600
ROUTE_REVISION: Final = 3
PROPOSAL_REF: Final = "artifact://plan/prv-0001"

CRITERION: Final[dict[str, Any]] = {
    "id": "CR-01",
    "text": "the published wheel installs into a clean environment",
    "kind": "functional_suitability",
    "acceptance_style": "binary",
    "evidence_kind": "deterministic",
    "quality_dimension": "functional_suitability",
    "measurable_signal": "uv run pytest tests/unit/kernel/state exits zero",
}

PLAN_BODY: Final[dict[str, Any]] = {
    "milestone_urn": MILESTONE_URN,
    "milestone": {
        "key": "MLS-0030",
        "primary_track_ref": TRACK_URN,
        "title": "Publish an installable wheel",
        "outcome": "An operator installs the published wheel and the CLI answers.",
        "appetite": "M",
        "exclusions": ["platform packaging for Windows"],
        "acceptance_journey": [
            {
                "step_id": "AS-01",
                "actor": "operator",
                "action": "install the published wheel into a clean environment",
                "expected_observation": "the install completes and reports the version",
                "evidence_kinds": ["artifact"],
            }
        ],
        "required_batch_refs": [BATCH_URN],
    },
    "batches": [{"urn": BATCH_URN, "repository_ref": REPOSITORY_URN}],
    "tasks": [
        {
            "urn": PLAN_TASK_URN,
            "batch_ref": BATCH_URN,
            "priority": "P1",
            "intent": "build and publish the wheel",
            "criteria": [CRITERION],
        }
    ],
    "citations": [{"finding_ref": FINDING_URN, "note": "the wheel build was measured here"}],
}

PROPOSAL: Final[dict[str, Any]] = {
    "key": "PRV-0001",
    "author": {"principal_kind": "operator", "principal_id": "OP-0001"},
    "body": PLAN_BODY,
}


def digest(char: str) -> str:
    """Return a well-formed digest whose body is one repeated character."""
    return f"sha256:{char * 64}"


def stored_revision() -> dict[str, Any]:
    """Return a DRAFT plan revision row the document can already hold.

    The row is built through the record model rather than hand-spelled,
    because the key-free guard reads a validated revision and a row that
    does not validate reads as absent -- which would make this case pass
    for the wrong reason.
    """
    return PlanRevision.model_validate(
        {
            "key": PROPOSAL["key"],
            "revision": 1,
            "status": PlanRevisionStatus.DRAFT.value,
            "author": PROPOSAL["author"],
            "created_at": AT.isoformat(),
            "updated_at": AT.isoformat(),
            "content_digest": plan_content_digest(PlanBody.model_validate(PLAN_BODY)),
            "base_state_revision": 1,
            "policy_revision": 1,
            "head_bindings": [],
            "body": PLAN_BODY,
        }
    ).model_dump(mode="json")


def task_scope() -> dict[str, Any]:
    """Return the scope of a Run executing one Task, which may write."""
    return {
        "scope_kind": "task",
        "purpose": RunPurpose.IMPLEMENT.value,
        "task_ref": TASK_URN,
        "write_set": ["src"],
    }


def repository_scope() -> dict[str, Any]:
    """Return the scope of a Run planning inside one repository."""
    return {
        "scope_kind": "repository",
        "purpose": RunPurpose.PLAN.value,
        "repository_ref": REPOSITORY_URN,
    }


def make_canary(root: Path, *, scope: dict[str, Any], planned: bool = False) -> CanaryProvision:
    """Provision a canary holding one RUNNING Run of *scope*."""
    provisioned = provision_canary(repo_root=root, ref=canary_ref("HND"), provisioned_at=AT)
    row = seed_row("run", "RUNNING")
    row["scope"] = scope
    row["started_at"] = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    rows: dict[str, dict[str, Any]] = {"run": {RUN_KEY: row}}
    if planned:
        rows["track"] = {"TRK-RUNTIME": seed_row("track", "ACTIVE")}
        rows["repository"] = {"REP-EAWF": {"key": "REP-EAWF", "head_sha": HEAD}}
    seed(provisioned, rows)
    return provisioned


def executor_capsule(**overrides: Any) -> AuthorityCapsule:
    """Return the sealed capsule of a task-scoped executor Run."""
    fields: dict[str, Any] = {
        "run_ref": RUN_URN,
        "scope_ref": TASK_URN,
        "scope_digest": digest("a"),
        "agent_role": AgentSessionRole.EXECUTOR.value,
        "purpose": RunPurpose.IMPLEMENT.value,
        "authority": {"state": "read_only", "workspace": "scoped_write"},
        "tool_grants": ("budget_status", "run_scoped_command"),
        "budget": {"wall_seconds": WALL_CEILING, "output_bytes": 1_048_576},
        "criteria_digest": digest("b"),
        "policy_digest": digest("c"),
        "compiled_spec_digest": digest("d"),
        "report_schema_ref": "schema://executor-report/v1",
        "stop_conditions": ("budget_exhausted",),
    }
    fields.update(overrides)
    return AuthorityCapsule.seal(fields)


def planner_capsule(**overrides: Any) -> AuthorityCapsule:
    """Return the sealed capsule of a repository-scoped planner Run."""
    fields: dict[str, Any] = {
        "scope_ref": REPOSITORY_URN,
        "agent_role": AgentSessionRole.PLANNER.value,
        "purpose": RunPurpose.PLAN.value,
        "authority": {"state": "read_only", "workspace": "none"},
        "tool_grants": ("budget_status", "submit_plan"),
    }
    fields.update(overrides)
    return executor_capsule(**fields)


def call_verb(method: str, ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb the way the socket listener does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


def skill_caller(ctx: MethodContext, canary: CanaryProvision) -> RpcCaller:
    """Bridge a lifecycle skill's calls to the same verb dispatch these tests use.

    Mirrors :func:`eawf.workflow.skills.lifecycle_rpc.daemon_rpc_caller`: a
    refused verb call is turned into :class:`RpcRefusedError` rather than
    left to propagate as the daemon's own validation error, which is the
    seam a skill's ``action`` is written against.
    """

    def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return call_verb(method, ctx, repo_root=str(canary.root), **params)
        except DaemonValidationError as error:
            raise RpcRefusedError(
                method=method, code=refusal_code(str(error)), detail=str(error)
            ) from error

    return call


def seal_fixture(canary: CanaryProvision, runtime_root: Path) -> tuple[str, str]:
    """Seed a standing candidate submission and its Run's active lease.

    Bypasses ``runtime.candidate.submit`` and the git worktree it pins a
    commit against: the seal checks read only the submission and the
    lease records, so this builds those two directly rather than
    materializing a real worktree for one candidate.

    Returns:
        The candidate ref the submission was filed under, and the
        resulting tree digest it names.
    """
    tree_digest = digest("9")
    ref = candidate_identity(task_ref=TASK_URN, resulting_tree_digest=tree_digest)
    context = root_context(canary, runtime_root)
    lease = WorkLease(
        lease_id=f"LSE-{'1' * 32}",
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        workspace_handle=f"wsh-{'2' * 32}",
        workspace_generation=1,
        branch=f"eawf/lease/wsh-{'2' * 32}",
        base_commit=HEAD,
        writable_roots=("src",),
        issued_at=AT,
        heartbeat_at=AT,
        # The seal check reads the lease against the wall clock, not AT,
        # so the lease must outlive whenever this suite actually runs.
        expires_at=AT + timedelta(days=3650),
        status_at=AT,
        status=LeaseStatus.ACTIVE,
    )
    submission = CandidateSubmission(
        candidate_ref=ref,
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        lease_id=lease.lease_id,
        workspace_handle=lease.workspace_handle,
        workspace_generation=lease.workspace_generation,
        base_commit=lease.base_commit,
        submission_ref=f"artifact://git/commit/{HEAD}",
        changed_paths=("src/module.py",),
        resulting_tree_digest=tree_digest,
        submitted_at=AT,
    )
    with context.session([RUN_URN]) as session:
        write_lease(context, lease)
        commit_ledger_append(session, submission_record(submission))
    return ref, tree_digest


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


def seal_call(
    *,
    capsule: AuthorityCapsule,
    tool_id: str,
    payload: dict[str, Any],
    key: str = "call-key-01",
    ordinal: int = 1,
) -> SemanticCall:
    """Return a sealed call whose payload digest is computed for it."""
    return SemanticCall.seal(
        {
            "call_id": f"call-{ordinal:016x}",
            "run_ref": RUN_URN,
            "contract_digest": capsule.contract_digest,
            "idempotency_key": key,
            "tool_id": tool_id,
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


def command_payload(*, argv: tuple[str, ...]) -> dict[str, Any]:
    """Return the payload of a scoped command."""
    return {
        "tool_id": "run_scoped_command",
        "command_family_id": "test-runner",
        "cwd_handle": "wsh-0123456789abcdef0123456789abcdef",
        "argv": list(argv),
        "timeout_seconds": 60,
        "expected_evidence_kind": "deterministic",
    }


def plan_payload(*, proposal_digest: str, proposal_ref: str = PROPOSAL_REF) -> dict[str, Any]:
    """Return the payload of a plan submission."""
    return {
        "tool_id": "submit_plan",
        "plan_scope_ref": REPOSITORY_URN,
        "proposal_ref": proposal_ref,
        "proposal_digest": proposal_digest,
    }


def file_proposal(
    canary: CanaryProvision,
    runtime_root: Path,
    *,
    proposal: dict[str, Any],
    ref: str = PROPOSAL_REF,
) -> str:
    """File one plan proposal as an artifact and return its digest."""
    content_digest = canonical_digest(proposal)
    artifact = PlanProposalArtifact(
        artifact_ref=ref, content_digest=content_digest, proposal=proposal
    )
    context = root_context(canary, runtime_root)
    with context.session([RUN_URN]) as session:
        _record_plan_proposal_artifact(session, artifact, now=AT)
    return content_digest


def spike_report_payload(
    *, verified: bool = True, report_id: str = "RPT-0001", contract_id: str = "MCT-99990001"
) -> dict[str, Any]:
    """Return one SpikeReport document carrying one synthetic contract."""
    return {
        "report_id": report_id,
        "verified": verified,
        "contracts": [
            {
                "contract_id": contract_id,
                "surface": "a synthetic surface probed for this test",
                "probe_command": "uv run python probe.py",
                "observed": {"widget_count": 3},
                "limits": [
                    {
                        "name": "widget_count",
                        "value": 3.0,
                        "unit": "count",
                        "direction": "ceiling",
                        "basis": "one synthetic probe run",
                    }
                ],
                "boundary": "measured once, on one synthetic population",
                "observed_at": AT.isoformat(),
                "observed_at_ref": "tests/fixtures/measured_contract/probe-output.json",
                "environment": {
                    "scale_band": "dev",
                    "population": "one synthetic surface probed for this test",
                    "population_size": 1,
                    "host_platform": "darwin",
                    "toolchain": "python 3.14.3",
                },
            }
        ],
    }


def file_spike_report(
    canary: CanaryProvision,
    runtime_root: Path,
    *,
    report: dict[str, Any],
    ref: str,
) -> str:
    """File one spike report as an artifact and return its digest."""
    content_digest = canonical_digest(report)
    artifact = SpikeReportArtifact(artifact_ref=ref, content_digest=content_digest, report=report)
    context = root_context(canary, runtime_root)
    with context.session([RUN_URN]) as session:
        _record_spike_report_artifact(session, artifact, now=AT)
    return content_digest


def seed_v1_state(canary: CanaryProvision) -> None:
    """Write a v1 ``state.json`` beside *canary*'s epoch-2 tree.

    This is the store ``submit_evidence``'s handler promotes a contract
    into, at ``<repo_root>/.ea/state.json`` -- the same path
    ``native_root()`` resolves this call's ``repo_root`` to.
    """
    state_path = Path(canary.root) / ".ea" / "state.json"
    state_path.write_text(V1_STATE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")


def evidence_payload(*, spike_report_ref: str, spike_report_digest: str) -> dict[str, Any]:
    """Return the payload of a submit_evidence call."""
    return {
        "tool_id": "submit_evidence",
        "spike_report_ref": spike_report_ref,
        "spike_report_digest": spike_report_digest,
    }


def receipt_lines(canary: CanaryProvision, runtime_root: Path) -> int:
    """Return how many receipt lines the canary's receipt ledger holds."""
    context = root_context(canary, runtime_root)
    with context.session([RUN_URN]) as session:
        return len(read_ledger_records(session.ledger_path(Epoch2Collection.RECEIPT)))


def plan_revision_rows(canary: CanaryProvision, runtime_root: Path) -> dict[str, Any]:
    """Return the plan-revision rows the canary's document holds."""
    context = root_context(canary, runtime_root)
    with context.session([RUN_URN]) as session:
        return dict(document_rows(session.read_document(), Epoch2Collection.PLAN_REVISION))


def findings_of(answer: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the findings of a succeeded plan-validation receipt."""
    assert answer["result"]["status"] == "succeeded", answer["result"]
    output = answer["result"]["bounded_output"]
    assert output["tool_id"] == "submit_plan"
    findings: list[dict[str, Any]] = output["findings"]
    return findings


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(runtime_root)


@pytest.fixture
def task_canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one running, task-scoped Run."""
    return make_canary(tmp_path / "repo", scope=task_scope())


@pytest.fixture
def plan_canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding a planning Run, an active Track and a head."""
    return make_canary(tmp_path / "repo", scope=repository_scope(), planned=True)


# ---------------------------------------------------------------------------
# The brokered set is exactly what has a handler
# ---------------------------------------------------------------------------


def test_every_brokered_tool_has_a_handler_and_no_other_tool_does() -> None:
    """The set is derived from the table, so the two cannot disagree."""
    assert frozenset(SEMANTIC_HANDLERS) == BROKERED_TOOLS
    assert {
        SemanticToolId.BUDGET_STATUS,
        SemanticToolId.SUBMIT_CANDIDATE,
        SemanticToolId.SUBMIT_EVIDENCE,
        SemanticToolId.SUBMIT_PLAN,
        SemanticToolId.SUBMIT_REPORT,
    } == BROKERED_TOOLS
    assert set(SemanticToolId) > BROKERED_TOOLS


def test_every_plan_refusal_code_can_be_reported_as_a_finding() -> None:
    assert frozenset(PlanRefusalCode) == REPORTABLE_PLAN_CODES


# ---------------------------------------------------------------------------
# RUN-021: delegation is denied, with a receipt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("claude", "-p", "finish the task"),
        ("/opt/homebrew/bin/claude", "-p", "finish the task"),
        ("uv", "run", "eawf", "dispatch", "wave"),
        ("npx", "opencode", "run"),
        ("env", "CODEX_HOME=/tmp", "codex", "exec"),
    ],
    ids=["bare", "absolute", "wrapped", "package-runner", "env-prefixed"],
)
def test_a_task_scoped_run_is_denied_delegation_with_a_receipt(
    task_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path, argv: tuple[str, ...]
) -> None:
    capsule = executor_capsule()
    bind(ctx, task_canary, capsule)
    before = receipt_lines(task_canary, runtime_root)

    answer = invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule, tool_id="run_scoped_command", payload=command_payload(argv=argv)
        ),
        capsule,
    )

    assert answer["result"]["status"] == "denied"
    assert answer["refused_check"] == PreHandlerCheck.SCOPE.value
    assert answer["result"]["error"]["code"] == SemanticToolErrorCode.SCOPE_DENIED.value
    assert answer["result"]["error"]["field_path"] == "/argv"
    assert receipt_lines(task_canary, runtime_root) == before + 1


def test_the_delegation_refusal_is_total_over_the_declared_agent_set(
    task_canary: CanaryProvision, ctx: MethodContext
) -> None:
    """Every executable the daemon calls an agent is refused, not one example."""
    capsule = executor_capsule()
    bind(ctx, task_canary, capsule)

    for ordinal, name in enumerate(sorted(AGENT_LAUNCH_COMMANDS), start=1):
        answer = invoke(
            ctx,
            task_canary,
            seal_call(
                capsule=capsule,
                tool_id="run_scoped_command",
                payload=command_payload(argv=(name, "--version")),
                key=f"launch-{name}",
                ordinal=ordinal,
            ),
            capsule,
        )
        assert answer["result"]["error"]["code"] == SemanticToolErrorCode.SCOPE_DENIED.value


def test_a_command_that_starts_no_agent_is_not_a_delegation(
    task_canary: CanaryProvision, ctx: MethodContext
) -> None:
    """The refusal is about the program, so an ordinary one passes the check."""
    capsule = executor_capsule()
    bind(ctx, task_canary, capsule)

    with pytest.raises(DaemonValidationError, match="handler_not_brokered"):
        invoke(
            ctx,
            task_canary,
            seal_call(
                capsule=capsule,
                tool_id="run_scoped_command",
                payload=command_payload(argv=("pytest", "tests/unit/eawf")),
            ),
            capsule,
        )


def test_a_task_scoped_run_may_not_ask_the_daemon_to_fan_out(
    task_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """The other shape of delegation: asking for work to run in another Run."""
    capsule = planner_capsule(scope_ref=TASK_URN, tool_grants=("submit_coordination_proposal",))
    bind(ctx, task_canary, capsule)
    before = receipt_lines(task_canary, runtime_root)

    answer = invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_coordination_proposal",
            payload={
                "tool_id": "submit_coordination_proposal",
                "action": "split_batch",
                "target_refs": [TASK_URN],
                "basis": "the task is larger than one lease",
            },
        ),
        capsule,
    )

    assert capsule.budget.child_runs == 0
    assert answer["result"]["status"] == "denied"
    assert answer["refused_check"] == PreHandlerCheck.SCOPE.value
    assert answer["result"]["error"]["field_path"] == "/action"
    assert receipt_lines(task_canary, runtime_root) == before + 1


# ---------------------------------------------------------------------------
# submit_plan routes into the PlanRevision validator
# ---------------------------------------------------------------------------


def test_a_valid_proposal_is_accepted_and_names_the_milestone_it_plans(
    plan_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    capsule = planner_capsule()
    bind(ctx, plan_canary, capsule)
    content_digest = file_proposal(plan_canary, runtime_root, proposal=PROPOSAL)

    answer = invoke(
        ctx,
        plan_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_plan",
            payload=plan_payload(proposal_digest=content_digest),
        ),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert answer["result"]["status"] == "succeeded"
    assert output["accepted"] is True
    assert output["proposal_ref"] == MILESTONE_URN
    assert output["findings"] == []


def test_validating_a_plan_records_no_plan_revision(
    plan_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """Validating is not submitting: recording the revision is an operator verb."""
    capsule = planner_capsule()
    bind(ctx, plan_canary, capsule)
    content_digest = file_proposal(plan_canary, runtime_root, proposal=PROPOSAL)

    invoke(
        ctx,
        plan_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_plan",
            payload=plan_payload(proposal_digest=content_digest),
        ),
        capsule,
    )

    assert plan_revision_rows(plan_canary, runtime_root) == {}


def test_a_proposal_nobody_filed_is_refused_with_a_finding(
    plan_canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = planner_capsule()
    bind(ctx, plan_canary, capsule)

    answer = invoke(
        ctx,
        plan_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_plan",
            payload=plan_payload(proposal_digest=digest("e")),
        ),
        capsule,
    )

    findings = findings_of(answer)
    assert answer["result"]["bounded_output"]["accepted"] is False
    assert findings[0]["code"] == PlanRefusalCode.IDENTITY_NOT_FOUND.value
    assert findings[0]["field_path"] == "/proposal_ref"


def test_a_digest_the_filed_proposal_does_not_have_is_refused(
    plan_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """The envelope's claim about the artifact is checked against the artifact."""
    capsule = planner_capsule()
    bind(ctx, plan_canary, capsule)
    file_proposal(plan_canary, runtime_root, proposal=PROPOSAL)

    answer = invoke(
        ctx,
        plan_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_plan",
            payload=plan_payload(proposal_digest=digest("f")),
        ),
        capsule,
    )

    findings = findings_of(answer)
    assert findings[0]["code"] == PlanRefusalCode.PROOF_STALE.value
    assert findings[0]["field_path"] == "/proposal_digest"


def test_a_filed_body_that_is_not_a_proposal_is_refused_with_a_schema_finding(
    plan_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    capsule = planner_capsule()
    bind(ctx, plan_canary, capsule)
    broken = {**PROPOSAL, "body": {**PLAN_BODY, "tasks": []}}
    content_digest = file_proposal(plan_canary, runtime_root, proposal=broken)

    answer = invoke(
        ctx,
        plan_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_plan",
            payload=plan_payload(proposal_digest=content_digest),
        ),
        capsule,
    )

    findings = findings_of(answer)
    assert findings[0]["code"] == PlanRefusalCode.SCHEMA_VALIDATION_FAILED.value


def test_a_plan_over_a_track_the_document_does_not_hold_is_refused(
    tmp_path: Path, ctx: MethodContext, runtime_root: Path
) -> None:
    """The validator reads the document, so the refusal is the document's."""
    provisioned = make_canary(tmp_path / "repo", scope=repository_scope())
    capsule = planner_capsule()
    bind(ctx, provisioned, capsule)
    content_digest = file_proposal(provisioned, runtime_root, proposal=PROPOSAL)

    answer = invoke(
        ctx,
        provisioned,
        seal_call(
            capsule=capsule,
            tool_id="submit_plan",
            payload=plan_payload(proposal_digest=content_digest),
        ),
        capsule,
    )

    findings = findings_of(answer)
    assert findings[0]["code"] == PlanRefusalCode.IDENTITY_NOT_FOUND.value
    assert "TRK-RUNTIME" in findings[0]["message"]


def test_a_plan_whose_key_the_document_already_holds_is_refused(
    plan_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """A repair is a child revision, never a second write of one key."""
    capsule = planner_capsule()
    bind(ctx, plan_canary, capsule)
    content_digest = file_proposal(plan_canary, runtime_root, proposal=PROPOSAL)
    seed(plan_canary, {"plan_revision": {"PRV-0001": stored_revision()}})

    answer = invoke(
        ctx,
        plan_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_plan",
            payload=plan_payload(proposal_digest=content_digest),
        ),
        capsule,
    )

    findings = findings_of(answer)
    assert findings[0]["code"] == PlanRefusalCode.REVISION_CONFLICT.value


def test_a_plan_aimed_outside_the_runs_scope_never_reaches_the_validator(
    plan_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """The scope check runs before any handler, so no artifact is even read."""
    capsule = planner_capsule()
    bind(ctx, plan_canary, capsule)
    content_digest = file_proposal(plan_canary, runtime_root, proposal=PROPOSAL)
    payload = {
        **plan_payload(proposal_digest=content_digest),
        "plan_scope_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-OTHER/repository/REP-OTHER",
    }

    answer = invoke(
        ctx,
        plan_canary,
        seal_call(capsule=capsule, tool_id="submit_plan", payload=payload),
        capsule,
    )

    assert answer["result"]["status"] == "denied"
    assert answer["refused_check"] == PreHandlerCheck.SCOPE.value
    assert answer["result"]["error"]["field_path"] == "/plan_scope_ref"


def test_an_executor_may_not_submit_a_plan_however_it_is_granted(
    plan_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """The role ceiling is daemon-side, so a capsule granting it opens nothing."""
    capsule = executor_capsule(
        scope_ref=REPOSITORY_URN,
        purpose=RunPurpose.PLAN.value,
        authority={"state": "read_only", "workspace": "none"},
        tool_grants=("budget_status", "submit_plan"),
    )
    bind(ctx, plan_canary, capsule)
    content_digest = file_proposal(plan_canary, runtime_root, proposal=PROPOSAL)

    answer = invoke(
        ctx,
        plan_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_plan",
            payload=plan_payload(proposal_digest=content_digest),
        ),
        capsule,
    )

    assert "submit_plan" in capsule.tool_grants
    assert answer["refused_check"] == PreHandlerCheck.GRANT.value
    assert answer["result"]["error"]["code"] == SemanticToolErrorCode.CAPABILITY_DENIED.value


def test_a_filed_artifact_whose_digest_does_not_cover_its_body_is_refused() -> None:
    """The row binds its own digest, so a replaced body cannot keep the name."""
    with pytest.raises(ValueError, match="content_digest"):
        PlanProposalArtifact(
            artifact_ref=PROPOSAL_REF, content_digest=digest("0"), proposal=PROPOSAL
        )


def test_a_filed_artifact_names_a_typed_reference() -> None:
    with pytest.raises(ValueError, match="artifact_ref"):
        PlanProposalArtifact(
            artifact_ref="/etc/passwd",
            content_digest=canonical_digest(PROPOSAL),
            proposal=PROPOSAL,
        )


def test_the_budget_handler_still_answers_from_measured_numbers_only(
    plan_canary: CanaryProvision, ctx: MethodContext
) -> None:
    """The second handler in the table is unchanged by the first arriving."""
    capsule = planner_capsule()
    bind(ctx, plan_canary, capsule)

    answer = invoke(
        ctx,
        plan_canary,
        seal_call(
            capsule=capsule,
            tool_id="budget_status",
            payload={"tool_id": "budget_status", "include_children": False},
        ),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert output["quality"] == "measured"
    assert output["used"]["tokens"] is None
    assert output["remaining"]["wall_seconds"] <= WALL_CEILING


def test_a_receipt_survives_the_call_that_asked_for_it(
    plan_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """A served plan validation is filed, not only returned."""
    capsule = planner_capsule()
    bind(ctx, plan_canary, capsule)
    content_digest = file_proposal(plan_canary, runtime_root, proposal=PROPOSAL)
    before = receipt_lines(plan_canary, runtime_root)

    answer = invoke(
        ctx,
        plan_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_plan",
            payload=plan_payload(proposal_digest=content_digest),
        ),
        capsule,
    )

    context = root_context(plan_canary, runtime_root)
    with context.session([RUN_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RECEIPT))
    filed = json.loads(json.dumps(records[-1].payload))
    assert receipt_lines(plan_canary, runtime_root) == before + 1
    assert filed["call_id"] == answer["call_id"]
    assert filed["result"]["bounded_output"]["accepted"] is True


# ---------------------------------------------------------------------------
# submit_report resolves to a handler that checks the Run's own contract
# ---------------------------------------------------------------------------


def test_a_report_naming_its_own_contract_is_accepted(
    task_canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = executor_capsule(tool_grants=("budget_status", "submit_report"))
    bind(ctx, task_canary, capsule)

    answer = invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_report",
            payload={
                "tool_id": "submit_report",
                "report_schema_ref": capsule.report_schema_ref,
                "contract_digest": capsule.contract_digest,
                "body_ref": "artifact://report/executor/ar-0001",
                "body_digest": digest("7"),
                "verdict": "pass",
            },
        ),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert answer["result"]["status"] == "succeeded"
    assert output["accepted"] is True
    assert output["report_ref"] == RUN_URN
    assert output["findings"] == []


def test_a_report_naming_another_schema_than_its_run_is_refused(
    task_canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = executor_capsule(tool_grants=("budget_status", "submit_report"))
    bind(ctx, task_canary, capsule)

    answer = invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_report",
            payload={
                "tool_id": "submit_report",
                "report_schema_ref": "schema://other-report/v1",
                "contract_digest": capsule.contract_digest,
                "body_ref": "artifact://report/executor/ar-0002",
                "body_digest": digest("7"),
                "verdict": "pass",
            },
        ),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert output["accepted"] is False
    assert output["findings"][0]["code"] == "report_schema_mismatch"
    assert output["findings"][0]["field_path"] == "/report_schema_ref"


def test_a_report_naming_a_contract_its_run_was_not_sealed_under_is_refused(
    task_canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = executor_capsule(tool_grants=("budget_status", "submit_report"))
    bind(ctx, task_canary, capsule)

    answer = invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_report",
            payload={
                "tool_id": "submit_report",
                "report_schema_ref": capsule.report_schema_ref,
                "contract_digest": digest("0"),
                "body_ref": "artifact://report/executor/ar-0003",
                "body_digest": digest("7"),
                "verdict": "pass",
            },
        ),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert output["accepted"] is False
    assert output["findings"][0]["code"] == "report_contract_mismatch"
    assert output["findings"][0]["field_path"] == "/contract_digest"


# ---------------------------------------------------------------------------
# /integrate seal reaches the report-bind verb instead of stopping
# ---------------------------------------------------------------------------


def test_integrate_seal_stops_when_the_run_and_tree_are_not_presented(
    task_canary: CanaryProvision, ctx: MethodContext
) -> None:
    """Naming only the candidate still names neither field the verb always needs."""
    caller = skill_caller(ctx, task_canary)

    result = integrate_skill.IntegrateSkill(caller=caller).action(
        SkillContext(
            scope="scope",
            session="session",
            args={"action": "seal", "subject_ref": f"CND-{'0' * 32}"},
        )
    )

    assert isinstance(result.body, dict)
    assert result.body["outcome"] == "blocked"
    assert result.body["refusal_code"] == "candidate_report_unbound"
    assert set(result.body["unresolved_request_fields"]) == {"urn", "resulting_tree_digest"}


def test_integrate_seal_binds_the_report_and_returns_a_sealed_candidate(
    task_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """Presented in full, the seal reaches the verb and the candidate seals."""
    ref, tree_digest = seal_fixture(task_canary, runtime_root)
    caller = skill_caller(ctx, task_canary)

    result = integrate_skill.IntegrateSkill(caller=caller).action(
        SkillContext(
            scope="scope",
            session="session",
            args={
                "action": "seal",
                "subject_ref": ref,
                "run": RUN_URN,
                "report_schema_ref": "schema://executor-report/v1",
                "report_digest": digest("7"),
                "verdict": "pass",
                "resulting_tree_digest": tree_digest,
            },
        )
    )

    assert isinstance(result.body, dict)
    assert result.body["method"] == integrate_skill.CANDIDATE_REPORT_BIND_METHOD
    assert result.body["outcome"] == "sealed"
    assert result.status == "ok"


def test_integrate_seal_reads_the_report_off_the_run_when_omitted(
    task_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """Naming only the Run and the candidate is enough once a report was accepted."""
    ref, tree_digest = seal_fixture(task_canary, runtime_root)
    capsule = executor_capsule(tool_grants=("budget_status", "submit_report"))
    bind(ctx, task_canary, capsule)
    invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_report",
            payload={
                "tool_id": "submit_report",
                "report_schema_ref": capsule.report_schema_ref,
                "contract_digest": capsule.contract_digest,
                "body_ref": "artifact://report/executor/ar-0009",
                "body_digest": digest("7"),
                "verdict": "pass",
            },
        ),
        capsule,
    )
    caller = skill_caller(ctx, task_canary)

    result = integrate_skill.IntegrateSkill(caller=caller).action(
        SkillContext(
            scope="scope",
            session="session",
            args={
                "action": "seal",
                "subject_ref": ref,
                "run": RUN_URN,
                "resulting_tree_digest": tree_digest,
            },
        )
    )

    assert isinstance(result.body, dict)
    assert result.body["outcome"] == "sealed"
    assert result.status == "ok"


def test_integrate_seal_still_stops_when_the_run_holds_no_accepted_report(
    task_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """A Run that never submitted a report has nothing to resolve the omission from."""
    ref, tree_digest = seal_fixture(task_canary, runtime_root)
    caller = skill_caller(ctx, task_canary)

    result = integrate_skill.IntegrateSkill(caller=caller).action(
        SkillContext(
            scope="scope",
            session="session",
            args={
                "action": "seal",
                "subject_ref": ref,
                "run": RUN_URN,
                "resulting_tree_digest": tree_digest,
            },
        )
    )

    assert isinstance(result.body, dict)
    assert result.body["outcome"] == "blocked"
    assert result.body["refusal_code"] == "candidate_report_unresolved"


# ---------------------------------------------------------------------------
# submit_evidence promotes a verified SpikeReport's contracts
# ---------------------------------------------------------------------------


def test_submit_evidence_promotes_a_contract_through_the_real_handler(
    task_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """The registered submit_evidence handler writes an artifact revision."""
    capsule = executor_capsule(tool_grants=("budget_status", "submit_evidence"))
    bind(ctx, task_canary, capsule)
    seed_v1_state(task_canary)
    ref = "artifact://spike/run-10"
    digest = file_spike_report(task_canary, runtime_root, report=spike_report_payload(), ref=ref)

    answer = invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_evidence",
            payload=evidence_payload(spike_report_ref=ref, spike_report_digest=digest),
        ),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert answer["result"]["status"] == "succeeded"
    assert output["tool_id"] == "submit_evidence"
    assert output["accepted"] is True
    assert output["contract_refs"] == [f"urn:eawf:v1:artifact:{V1_STATE_SCOPE}/MCT-99990001"]


def test_submit_evidence_refuses_an_unverified_report_through_the_real_handler(
    task_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """An unverified report is reported as a finding, and nothing is promoted."""
    capsule = executor_capsule(tool_grants=("budget_status", "submit_evidence"))
    bind(ctx, task_canary, capsule)
    seed_v1_state(task_canary)
    ref = "artifact://spike/run-11"
    digest = file_spike_report(
        task_canary,
        runtime_root,
        report=spike_report_payload(verified=False, report_id="RPT-0002"),
        ref=ref,
    )

    answer = invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_evidence",
            payload=evidence_payload(spike_report_ref=ref, spike_report_digest=digest),
            key="unverified",
        ),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert answer["result"]["status"] == "succeeded"
    assert output["accepted"] is False
    assert output["findings"][0]["code"] == "spike_report_unverified"


def test_submit_evidence_refuses_a_digest_the_filed_report_does_not_have(
    task_canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    capsule = executor_capsule(tool_grants=("budget_status", "submit_evidence"))
    bind(ctx, task_canary, capsule)
    seed_v1_state(task_canary)
    ref = "artifact://spike/run-12"
    file_spike_report(task_canary, runtime_root, report=spike_report_payload(), ref=ref)

    answer = invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_evidence",
            payload=evidence_payload(spike_report_ref=ref, spike_report_digest=digest("e")),
            key="stale-digest",
        ),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert output["accepted"] is False
    assert output["findings"][0]["code"] == "proof_stale"


def test_submit_evidence_refuses_a_report_nobody_filed(
    task_canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = executor_capsule(tool_grants=("budget_status", "submit_evidence"))
    bind(ctx, task_canary, capsule)
    seed_v1_state(task_canary)

    answer = invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_evidence",
            payload=evidence_payload(
                spike_report_ref="artifact://spike/unfiled", spike_report_digest=digest("f")
            ),
            key="unfiled",
        ),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert output["accepted"] is False
    assert output["findings"][0]["code"] == "identity_not_found"
