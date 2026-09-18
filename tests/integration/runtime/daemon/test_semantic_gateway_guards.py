"""RUN-002 at the daemon door: nine checks, one answer, nothing written.

The suite drives the registered ``semantic.call`` verb through the daemon
dispatcher against a provisioned canary, so what it exercises is what a
provider process on the socket reaches rather than a library call spelled
in a friendlier way.

Every one of the nine checks is driven by a negative case that reds on a
real defect: the refusal it asserts names one check and one closed code,
and a guard wired to the wrong evaluator, left out of the order, or given
a code its table does not declare fails here rather than passing quietly.
The order itself is asserted separately, by a call that fails several
checks at once: exactly the earliest answers.

Nothing here sleeps. Budget exhaustion is driven by seeding a Run whose
start stamp is two hours behind whatever the process clock reads against
a one-hour ceiling, so the margin is an hour of elapsed time rather than
a race, and the admitted cases seed a start five seconds back against the
same ceiling.

"Zero filesystem change" is asserted as a digest over the repository tree
taken before and after each refused call, with the daemon's own ``.ea``
tree excluded: the receipt is the one thing a refusal writes, and it is
counted as a ledger line rather than assumed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Self

import pytest
from pydantic import BaseModel, ConfigDict, model_validator

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.control import ControlDisposition, ControlFact, ControlPhase
from eawf.kernel.runtime.semantic import (
    RETRY_CLASS_BY_CODE,
    RetryClass,
    SemanticCall,
    SemanticToolErrorCode,
    SemanticToolId,
)
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.run import RUN_BIND_METHOD
from eawf.runtime.daemon.methods.semantic import (
    SEMANTIC_CALL_METHOD,
    SEMANTIC_RESULT_READ_METHOD,
)
from eawf.runtime.daemon.native_guard import NativeAuthorityRefusedError
from eawf.runtime.daemon.semantic_gateway import (
    AGENT_LAUNCH_COMMANDS,
    PRE_HANDLER_CHECKS,
    REFUSAL_CODES_BY_CHECK,
    ROLE_GATED_TOOLS,
    ROLE_TOOL_CEILING,
    SCOPE_RULES,
    GatewayTableError,
    PreHandlerCheck,
    _compile_check_order,
)
from eawf.runtime.workspace.lease import issue_lease
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed, seed_row

pytestmark = pytest.mark.integration


ACTOR: Final = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"
TASK_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
OTHER_TASK_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0099"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
ROUTE_REVISION: Final = 3

#: The one-hour ceiling every capsule here is sealed with.
WALL_CEILING: Final = 3600

#: Where the provider-neutral conformance fixtures live.
FIXTURE_ROOT: Final = Path(__file__).resolve().parents[3] / "fixtures" / "runtime_contract" / "v1"


def digest(char: str) -> str:
    """Return a well-formed digest whose body is one repeated character."""
    return f"sha256:{char * 64}"


# ---- the provider-neutral fixture shape -------------------------------------


class FixtureCall(BaseModel):
    """One call a conformance fixture asks the gateway to judge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    idempotency_key: str
    tool_id: SemanticToolId
    payload: dict[str, Any]


class FixtureExpectation(BaseModel):
    """What the fixture says the gateway must answer with."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["served", "replayed", "refused"]
    check: PreHandlerCheck | None = None
    code: SemanticToolErrorCode | None = None
    retry_class: RetryClass | None = None
    field_path: str | None = None

    @model_validator(mode="after")
    def _refusal_names_its_cause(self) -> Self:
        """Require a check and a code exactly on a refusal.

        Raises:
            ValueError: A refusal names no check or no code, or a
                non-refusal names one, which would assert against a cause
                the gateway never produces.
        """
        refused = self.verdict == "refused"
        named = self.check is not None and self.code is not None
        if refused != named:
            raise ValueError(f"a {self.verdict} expectation disagrees with check and code")
        return self


class ContractFixture(BaseModel):
    """One provider-neutral fixture: a run shape, its calls, its answers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["runtime-contract/v1"]
    fixture_id: str
    summary: str
    write_set: tuple[str, ...]
    calls: tuple[FixtureCall, ...]
    expected: tuple[FixtureExpectation, ...]

    @model_validator(mode="after")
    def _every_call_has_an_expectation(self) -> Self:
        """Require one expectation per call.

        Raises:
            ValueError: The two lists differ in length, which would leave
                a call nothing asserts about.
        """
        if len(self.calls) != len(self.expected):
            raise ValueError("a fixture states one expectation per call")
        return self


def load_fixture(name: str) -> ContractFixture:
    """Return the conformance fixture filed under *name*."""
    return ContractFixture.model_validate(
        json.loads((FIXTURE_ROOT / f"{name}.json").read_text(encoding="utf-8"))
    )


# ---- the canary, the capsule and the call -----------------------------------


def make_repo(root: Path) -> None:
    """Initialise a one-commit git repository at *root*."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "ci"], cwd=root, check=True)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "module.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)


def make_canary(
    root: Path,
    *,
    status: str = "RUNNING",
    started_delta: timedelta = timedelta(seconds=5),
    write_set: tuple[str, ...] = ("src",),
) -> CanaryProvision:
    """Provision a canary holding one seeded Run of *status*."""
    provisioned = provision_canary(repo_root=root, ref=canary_ref("SEM"), provisioned_at=AT)
    row = seed_row("run", status)
    row["scope"]["write_set"] = list(write_set)
    if row.get("started_at") is not None:
        row["started_at"] = (datetime.now(UTC) - started_delta).isoformat()
    seed(provisioned, {"run": {RUN_KEY: row}})
    return provisioned


def method_ctx(runtime_root: Path) -> MethodContext:
    """Return a daemon context with a WAL directory of its own."""
    return MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=runtime_root / "wal",
    )


def root_ctx(provisioned: CanaryProvision, runtime_root: Path) -> Epoch2RootContext:
    """Return the native context of a provisioned canary."""
    return method_ctx(runtime_root).native_root_context(provisioned.root / ".ea")


def seal_capsule(**overrides: Any) -> AuthorityCapsule:
    """Return a sealed capsule, overriding whichever field is under test."""
    fields: dict[str, Any] = {
        "run_ref": RUN_URN,
        "scope_ref": TASK_URN,
        "scope_digest": digest("a"),
        "agent_role": AgentSessionRole.EXECUTOR.value,
        "purpose": RunPurpose.IMPLEMENT.value,
        "authority": {"state": "read_only", "workspace": "scoped_write"},
        "tool_grants": (
            "budget_status",
            "attach_evidence",
            "submit_candidate",
            "workspace_apply_patch",
            "run_scoped_command",
            "submit_coordination_proposal",
        ),
        "budget": {"wall_seconds": WALL_CEILING, "output_bytes": 1_048_576},
        "criteria_digest": digest("b"),
        "policy_digest": digest("c"),
        "compiled_spec_digest": digest("d"),
        "report_schema_ref": "schema://executor-report/v1",
        "stop_conditions": ("budget_exhausted",),
    }
    fields.update(overrides)
    return AuthorityCapsule.seal(fields)


def seal_call(
    *,
    capsule: AuthorityCapsule,
    tool_id: str,
    payload: dict[str, Any],
    key: str = "call-key-01",
    ordinal: int = 1,
    contract_digest: str | None = None,
) -> SemanticCall:
    """Return a sealed call whose payload digest is computed for it."""
    return SemanticCall.seal(
        {
            "call_id": f"call-{ordinal:016x}",
            "run_ref": RUN_URN,
            "contract_digest": contract_digest or capsule.contract_digest,
            "idempotency_key": key,
            "tool_id": tool_id,
            "tool_schema_version": "1.0.0",
            "payload": payload,
            "requested_at": AT,
        }
    )


def budget_payload(*, include_children: bool = False) -> dict[str, Any]:
    """Return the payload of a budget read."""
    return {"tool_id": "budget_status", "include_children": include_children}


def candidate_payload(
    *, changed_paths: tuple[str, ...], task_ref: str = TASK_URN
) -> dict[str, Any]:
    """Return the payload of a candidate submission."""
    return {
        "tool_id": "submit_candidate",
        "task_ref": task_ref,
        "lease_id": "lease-0123456789abcdef",
        "submission_ref": "artifact://candidate/one",
        "changed_paths": list(changed_paths),
        "resulting_tree_digest": digest("1"),
    }


def patch_payload(*, generation: int) -> dict[str, Any]:
    """Return the payload of a workspace patch at an expected generation."""
    return {
        "tool_id": "workspace_apply_patch",
        "lease_id": "lease-0123456789abcdef",
        "expected_workspace_generation": generation,
        "patch_ref": "artifact://patch/one",
        "patch_digest": digest("2"),
    }


def command_payload(*, argv: tuple[str, ...]) -> dict[str, Any]:
    """Return the payload of a scoped command."""
    return {
        "tool_id": "run_scoped_command",
        "command_family_id": "test-runner",
        "cwd_handle": "handle-0123456789abcdef",
        "argv": list(argv),
        "timeout_seconds": 60,
        "expected_evidence_kind": "deterministic",
    }


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


def tree_digest(root: Path) -> str:
    """Return a digest over every repository file outside the daemon's tree.

    The ``.ea`` tree is excluded because the receipt a refusal writes
    lives there, and ``.git`` because its own bookkeeping moves whenever
    git is asked anything at all.
    """
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in {".ea", ".git"}:
            continue
        if path.is_file():
            body = hashlib.sha256(path.read_bytes()).hexdigest()
            parts.append(f"{relative.as_posix()}:{body}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def receipt_lines(canary: CanaryProvision, runtime_root: Path) -> int:
    """Return how many receipt lines the canary's receipt ledger holds."""
    context = root_ctx(canary, runtime_root)
    with context.session([RUN_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RECEIPT))
    return len(records)


def append_control(
    canary: CanaryProvision,
    runtime_root: Path,
    *,
    phase: ControlPhase,
    disposition: ControlDisposition,
    sequence: int,
    effect_ref: str | None = None,
) -> None:
    """Append one control fact without committing the transition it implies.

    The three lines are written straight to the ledger rather than through
    the control verbs, because the verbs would also move the Run record
    and the window this reproduces is exactly the one where they have not
    yet: a confirmed effect standing over a record that still reads
    running.
    """
    context = root_ctx(canary, runtime_root)
    fact = ControlFact(
        control_request_ref="CTL-0000000a",
        run_ref=RUN_URN,
        control="cancel",
        phase=phase,
        disposition=disposition,
        effect_ref=effect_ref,
        actor=ACTOR,
        recorded_at=AT,
        sequence=sequence,
    )
    with context.session([RUN_URN]) as session:
        append_ledger_record(
            session.ledger_path(Epoch2Collection.RUN),
            LedgerRecord(
                collection=Epoch2Collection.RUN,
                record_key=fact.control_request_ref,
                status=fact.phase.value,
                recorded_at=AT,
                payload=fact.model_dump(mode="json"),
            ),
        )


def confirm_cancel(canary: CanaryProvision, runtime_root: Path) -> None:
    """Record a confirmed cancel whose transition has not reached the record."""
    append_control(
        canary,
        runtime_root,
        phase=ControlPhase.REQUESTED,
        disposition=ControlDisposition.REQUESTING,
        sequence=1,
    )
    append_control(
        canary,
        runtime_root,
        phase=ControlPhase.ACKNOWLEDGED,
        disposition=ControlDisposition.ACCEPTED,
        sequence=2,
    )
    append_control(
        canary,
        runtime_root,
        phase=ControlPhase.EFFECTED,
        disposition=ControlDisposition.CONFIRMED,
        sequence=3,
        effect_ref="EFF-0000000b",
    )


def assert_denied(
    answer: dict[str, Any], *, check: PreHandlerCheck, code: SemanticToolErrorCode
) -> None:
    """Assert one denied receipt names the check, the code and its remedy."""
    assert answer["result"]["status"] == "denied"
    assert answer["refused_check"] == check.value
    assert answer["result"]["error"]["code"] == code.value
    assert answer["result"]["error"]["retry_class"] == RETRY_CLASS_BY_CODE[code].value
    assert answer["result"]["bounded_output"] is None
    assert answer["result"]["output_ref"] is None


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one running Run that started five seconds ago."""
    return make_canary(tmp_path / "repo")


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_ctx(runtime_root)


# ---------------------------------------------------------------------------
# The set of nine is total, single-valued, and fails to load when it drifts
# ---------------------------------------------------------------------------


def test_the_check_order_is_the_whole_enum_exactly_once() -> None:
    assert len(PRE_HANDLER_CHECKS) == 9
    assert set(PRE_HANDLER_CHECKS) == set(PreHandlerCheck)
    assert len(set(PRE_HANDLER_CHECKS)) == len(PRE_HANDLER_CHECKS)


def test_every_check_declares_a_code_bound_to_a_retry_class() -> None:
    assert set(REFUSAL_CODES_BY_CHECK) == set(PreHandlerCheck)
    for check, codes in REFUSAL_CODES_BY_CHECK.items():
        assert codes, f"{check.value} declares no refusal code"
        for code in codes:
            assert code in RETRY_CLASS_BY_CODE


def test_an_order_missing_a_check_fails_to_compile() -> None:
    """The drift guard reds on the real defect: a check nobody walks."""
    short = tuple(check for check in PRE_HANDLER_CHECKS if check is not PreHandlerCheck.LEASE)

    with pytest.raises(GatewayTableError, match="omits lease"):
        _compile_check_order(short)


def test_an_order_repeating_a_check_fails_to_compile() -> None:
    with pytest.raises(GatewayTableError, match="twice"):
        _compile_check_order((*PRE_HANDLER_CHECKS, PreHandlerCheck.SCOPE))


def test_the_scope_rules_cover_the_whole_tool_catalog() -> None:
    assert set(SCOPE_RULES) == set(SemanticToolId)


def test_the_role_ceiling_covers_every_role_and_gates_only_gated_tools() -> None:
    assert set(ROLE_TOOL_CEILING) == set(AgentSessionRole)
    for tools in ROLE_TOOL_CEILING.values():
        assert tools <= ROLE_GATED_TOOLS


# ---------------------------------------------------------------------------
# One negative case per check
# ---------------------------------------------------------------------------


def test_a_suspended_run_calls_no_tool(tmp_path: Path, ctx: MethodContext) -> None:
    provisioned = make_canary(tmp_path / "repo", status="SUSPENDED")
    capsule = seal_capsule()
    bind(ctx, provisioned, capsule)

    answer = invoke(
        ctx,
        provisioned,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload()),
        capsule,
    )

    assert_denied(
        answer, check=PreHandlerCheck.RUN_STATE, code=SemanticToolErrorCode.RUN_NOT_ACTIVE
    )


def test_a_capsule_the_run_was_not_bound_under_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """A widened capsule is indistinguishable from a forged one."""
    bound = seal_capsule()
    bind(ctx, canary, bound)
    widened = seal_capsule(tool_grants=(*bound.tool_grants, "ask_operator"))

    answer = invoke(
        ctx,
        canary,
        seal_call(capsule=widened, tool_id="budget_status", payload=budget_payload()),
        widened,
    )

    assert widened.contract_digest != bound.contract_digest
    assert_denied(
        answer, check=PreHandlerCheck.CONTRACT_DIGEST, code=SemanticToolErrorCode.CONTRACT_MISMATCH
    )


def test_a_tool_the_capsule_never_granted_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule(tool_grants=("budget_status",))
    bind(ctx, canary, capsule)
    payload = {
        "tool_id": "attach_evidence",
        "subject_ref": TASK_URN,
        "criterion_id": "RUN-002",
        "evidence_kind": "deterministic",
        "artifact_ref": "artifact://log/one",
    }

    answer = invoke(
        ctx, canary, seal_call(capsule=capsule, tool_id="attach_evidence", payload=payload), capsule
    )

    assert_denied(answer, check=PreHandlerCheck.GRANT, code=SemanticToolErrorCode.CAPABILITY_DENIED)


def test_a_denial_outranks_a_grant_of_the_same_tool(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule(tool_grants=("budget_status",), tool_denials=("budget_status",))
    bind(ctx, canary, capsule)

    answer = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload()),
        capsule,
    )

    assert_denied(
        answer, check=PreHandlerCheck.DENIAL, code=SemanticToolErrorCode.CAPABILITY_DENIED
    )
    assert "outranks" in answer["result"]["error"]["message"]


def test_a_path_outside_the_write_set_is_refused_before_any_write(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """The scope-escape fixture, driven through the wired verb."""
    fixture = load_fixture("scope-escape")
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    before = tree_digest(canary.root)
    lines = receipt_lines(canary, runtime_root)

    answer = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id=fixture.calls[0].tool_id.value,
            payload=fixture.calls[0].payload,
            key=fixture.calls[0].idempotency_key,
        ),
        capsule,
    )

    expected = fixture.expected[0]
    assert expected.check is not None and expected.code is not None
    assert_denied(answer, check=expected.check, code=expected.code)
    assert answer["result"]["error"]["field_path"] == expected.field_path
    assert tree_digest(canary.root) == before
    assert receipt_lines(canary, runtime_root) == lines + 1


def test_a_write_with_no_active_lease_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule()
    bind(ctx, canary, capsule)

    answer = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule, tool_id="workspace_apply_patch", payload=patch_payload(generation=1)
        ),
        capsule,
    )

    assert_denied(answer, check=PreHandlerCheck.LEASE, code=SemanticToolErrorCode.LEASE_NOT_ACTIVE)


def test_a_run_past_its_wall_ceiling_calls_no_tool(tmp_path: Path, ctx: MethodContext) -> None:
    provisioned = make_canary(tmp_path / "repo", started_delta=timedelta(hours=2))
    capsule = seal_capsule()
    bind(ctx, provisioned, capsule)

    answer = invoke(
        ctx,
        provisioned,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload()),
        capsule,
    )

    assert_denied(answer, check=PreHandlerCheck.BUDGET, code=SemanticToolErrorCode.BUDGET_EXHAUSTED)


def test_a_reused_key_carrying_another_payload_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload(), key="same"),
        capsule,
    )

    answer = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id="budget_status",
            payload=budget_payload(include_children=True),
            key="same",
            ordinal=2,
        ),
        capsule,
    )

    assert_denied(
        answer,
        check=PreHandlerCheck.IDEMPOTENCY,
        code=SemanticToolErrorCode.IDEMPOTENCY_PAYLOAD_MISMATCH,
    )


def test_a_confirmed_cancel_revokes_authority_before_the_record_moves(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """The window a stored status and its control ledger legitimately differ."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    confirm_cancel(canary, runtime_root)

    answer = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload()),
        capsule,
    )

    assert_denied(
        answer, check=PreHandlerCheck.REVOCATION, code=SemanticToolErrorCode.POLICY_REVOKED
    )


# ---------------------------------------------------------------------------
# One answer: the earliest check decides
# ---------------------------------------------------------------------------


def test_a_call_failing_several_checks_is_refused_for_the_earliest(
    tmp_path: Path, ctx: MethodContext
) -> None:
    """Run state, grant and scope all fail; the receipt names run state."""
    provisioned = make_canary(tmp_path / "repo", status="SUSPENDED")
    capsule = seal_capsule(tool_grants=("budget_status",))
    bind(ctx, provisioned, capsule)

    answer = invoke(
        ctx,
        provisioned,
        seal_call(
            capsule=capsule,
            tool_id="submit_candidate",
            payload=candidate_payload(changed_paths=("tests/escape.py",)),
        ),
        capsule,
    )

    assert_denied(
        answer, check=PreHandlerCheck.RUN_STATE, code=SemanticToolErrorCode.RUN_NOT_ACTIVE
    )


def test_a_grant_failure_precedes_a_scope_failure(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule(tool_grants=("budget_status",))
    bind(ctx, canary, capsule)

    answer = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_candidate",
            payload=candidate_payload(changed_paths=("tests/escape.py",)),
        ),
        capsule,
    )

    assert_denied(answer, check=PreHandlerCheck.GRANT, code=SemanticToolErrorCode.CAPABILITY_DENIED)


# ---------------------------------------------------------------------------
# Delegation: an agent never starts an agent
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
)
def test_a_scoped_command_may_not_start_another_agent(
    canary: CanaryProvision, ctx: MethodContext, argv: tuple[str, ...]
) -> None:
    """Naive and wrapped spellings alike: the daemon creates runs, not the Run."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)

    answer = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule, tool_id="run_scoped_command", payload=command_payload(argv=argv)
        ),
        capsule,
    )

    assert_denied(answer, check=PreHandlerCheck.SCOPE, code=SemanticToolErrorCode.SCOPE_DENIED)
    assert answer["result"]["error"]["field_path"] == "/argv"


def test_a_command_reading_a_path_that_shares_an_agent_name_is_not_a_launch(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """The check reads the program position, so a path argument is not one."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)

    with pytest.raises(DaemonValidationError, match="handler_not_brokered"):
        invoke(
            ctx,
            canary,
            seal_call(
                capsule=capsule,
                tool_id="run_scoped_command",
                payload=command_payload(argv=("pytest", "tests/unit/eawf")),
            ),
            capsule,
        )


def test_every_agent_launch_name_is_refused_in_the_program_position(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """The refusal is total over the declared set, not over one example."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)

    for ordinal, name in enumerate(sorted(AGENT_LAUNCH_COMMANDS), start=1):
        answer = invoke(
            ctx,
            canary,
            seal_call(
                capsule=capsule,
                tool_id="run_scoped_command",
                payload=command_payload(argv=(name, "--version")),
                key=f"launch-{name}",
                ordinal=ordinal,
            ),
            capsule,
        )
        assert_denied(answer, check=PreHandlerCheck.SCOPE, code=SemanticToolErrorCode.SCOPE_DENIED)


def coordinating_capsule(**overrides: Any) -> AuthorityCapsule:
    """Return a capsule of the one role whose ceiling reaches coordination."""
    fields: dict[str, Any] = {
        "agent_role": AgentSessionRole.PLANNER.value,
        "tool_grants": ("budget_status", "submit_coordination_proposal"),
    }
    fields.update(overrides)
    return seal_capsule(**fields)


def test_a_task_scoped_run_may_not_fan_out(canary: CanaryProvision, ctx: MethodContext) -> None:
    """One Task binds one lease and one candidate, so its ceiling is zero."""
    capsule = coordinating_capsule()
    bind(ctx, canary, capsule)
    payload = {
        "tool_id": "submit_coordination_proposal",
        "action": "split_batch",
        "target_refs": [TASK_URN],
        "basis": "the task is larger than one lease",
    }

    answer = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="submit_coordination_proposal", payload=payload),
        capsule,
    )

    assert capsule.budget.child_runs == 0
    assert_denied(answer, check=PreHandlerCheck.SCOPE, code=SemanticToolErrorCode.SCOPE_DENIED)
    assert answer["result"]["error"]["field_path"] == "/action"


def test_a_run_whose_ceiling_admits_children_passes_the_fan_out_check(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """The ceiling is consulted rather than assumed: a raised one admits."""
    capsule = coordinating_capsule(
        budget={"wall_seconds": WALL_CEILING, "output_bytes": 1_048_576, "child_runs": 2}
    )
    bind(ctx, canary, capsule)
    payload = {
        "tool_id": "submit_coordination_proposal",
        "action": "split_batch",
        "target_refs": [TASK_URN],
        "basis": "the batch is larger than one delivery",
    }

    with pytest.raises(DaemonValidationError, match="handler_not_brokered"):
        invoke(
            ctx,
            canary,
            seal_call(capsule=capsule, tool_id="submit_coordination_proposal", payload=payload),
            capsule,
        )


def test_a_coordination_action_that_creates_nothing_is_not_a_fan_out(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = coordinating_capsule()
    bind(ctx, canary, capsule)
    payload = {
        "tool_id": "submit_coordination_proposal",
        "action": "defer",
        "target_refs": [TASK_URN],
        "basis": "the dependency has not landed",
    }

    with pytest.raises(DaemonValidationError, match="handler_not_brokered"):
        invoke(
            ctx,
            canary,
            seal_call(capsule=capsule, tool_id="submit_coordination_proposal", payload=payload),
            capsule,
        )


# ---------------------------------------------------------------------------
# Role ceilings and typed scope
# ---------------------------------------------------------------------------


def test_a_planner_may_not_submit_a_candidate(canary: CanaryProvision, ctx: MethodContext) -> None:
    """The ceiling is daemon-side, so a capsule granting it does not open it."""
    capsule = seal_capsule(agent_role=AgentSessionRole.PLANNER.value)
    bind(ctx, canary, capsule)

    answer = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_candidate",
            payload=candidate_payload(changed_paths=("src/module.py",)),
        ),
        capsule,
    )

    assert "submit_candidate" in capsule.tool_grants
    assert_denied(answer, check=PreHandlerCheck.GRANT, code=SemanticToolErrorCode.CAPABILITY_DENIED)
    assert "planner" in answer["result"]["error"]["message"]


def test_a_call_attributing_its_output_to_another_entity_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """A verb aimed at another entity's URN would credit that entity."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    payload = {
        "tool_id": "attach_evidence",
        "subject_ref": OTHER_TASK_URN,
        "criterion_id": "RUN-002",
        "evidence_kind": "deterministic",
        "artifact_ref": "artifact://log/one",
    }

    answer = invoke(
        ctx, canary, seal_call(capsule=capsule, tool_id="attach_evidence", payload=payload), capsule
    )

    assert_denied(answer, check=PreHandlerCheck.SCOPE, code=SemanticToolErrorCode.SCOPE_DENIED)
    assert answer["result"]["error"]["field_path"] == "/subject_ref"


def test_a_candidate_inside_the_write_set_passes_the_scope_check(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """The scope check can pass: it refuses a path, not the tool."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)

    answer = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_candidate",
            payload=candidate_payload(changed_paths=("src/module.py",)),
        ),
        capsule,
    )

    assert_denied(answer, check=PreHandlerCheck.LEASE, code=SemanticToolErrorCode.LEASE_NOT_ACTIVE)


# ---------------------------------------------------------------------------
# The lease the daemon resolves, and the generation it is at
# ---------------------------------------------------------------------------


def test_an_active_lease_admits_a_patch_at_its_own_generation(
    tmp_path: Path, ctx: MethodContext, runtime_root: Path
) -> None:
    make_repo(tmp_path / "repo")
    provisioned = make_canary(tmp_path / "repo")
    capsule = seal_capsule()
    bind(ctx, provisioned, capsule)
    issue_lease(
        root_ctx(provisioned, runtime_root),
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=("src",),
        now=datetime.now(UTC),
        ttl=timedelta(hours=1),
    )

    with pytest.raises(DaemonValidationError, match="handler_not_brokered"):
        invoke(
            ctx,
            provisioned,
            seal_call(
                capsule=capsule,
                tool_id="workspace_apply_patch",
                payload=patch_payload(generation=1),
            ),
            capsule,
        )


def test_a_patch_aimed_at_a_stale_generation_is_refused(
    tmp_path: Path, ctx: MethodContext, runtime_root: Path
) -> None:
    make_repo(tmp_path / "repo")
    provisioned = make_canary(tmp_path / "repo")
    capsule = seal_capsule()
    bind(ctx, provisioned, capsule)
    issue_lease(
        root_ctx(provisioned, runtime_root),
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=("src",),
        now=datetime.now(UTC),
        ttl=timedelta(hours=1),
    )

    answer = invoke(
        ctx,
        provisioned,
        seal_call(
            capsule=capsule, tool_id="workspace_apply_patch", payload=patch_payload(generation=2)
        ),
        capsule,
    )

    assert_denied(
        answer,
        check=PreHandlerCheck.LEASE,
        code=SemanticToolErrorCode.STALE_WORKSPACE_GENERATION,
    )


# ---------------------------------------------------------------------------
# The admitted path, and the refusals that carry no receipt
# ---------------------------------------------------------------------------


def test_an_admitted_call_is_served_and_measured(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule()
    bind(ctx, canary, capsule)

    answer = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload()),
        capsule,
    )

    assert answer["result"]["status"] == "succeeded"
    assert answer["refused_check"] is None
    assert answer["result"]["error"] is None
    output = answer["result"]["bounded_output"]
    assert output["quality"] == "measured"
    assert output["used"]["wall_seconds"] >= 0
    assert output["remaining"]["wall_seconds"] <= WALL_CEILING


def test_a_run_this_root_never_recorded_earns_no_receipt(
    tmp_path: Path, ctx: MethodContext, runtime_root: Path
) -> None:
    provisioned = provision_canary(
        repo_root=tmp_path / "repo", ref=canary_ref("SEM"), provisioned_at=AT
    )
    capsule = seal_capsule()
    bind(ctx, provisioned, capsule)

    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        invoke(
            ctx,
            provisioned,
            seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload()),
            capsule,
        )
    assert receipt_lines(provisioned, runtime_root) == 0


def test_a_run_with_no_contract_binding_is_refused_as_a_mismatch(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule()

    answer = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload()),
        capsule,
    )

    assert_denied(
        answer, check=PreHandlerCheck.CONTRACT_DIGEST, code=SemanticToolErrorCode.CONTRACT_MISMATCH
    )


def test_a_capsule_granting_nothing_opens_nothing(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule(tool_grants=())
    bind(ctx, canary, capsule)

    answer = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload()),
        capsule,
    )

    assert_denied(answer, check=PreHandlerCheck.GRANT, code=SemanticToolErrorCode.CAPABILITY_DENIED)


def test_an_unknown_parameter_is_refused(canary: CanaryProvision, ctx: MethodContext) -> None:
    capsule = seal_capsule()
    bind(ctx, canary, capsule)

    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call_verb(
            SEMANTIC_CALL_METHOD,
            ctx,
            repo_root=str(canary.root),
            call=seal_call(
                capsule=capsule, tool_id="budget_status", payload=budget_payload()
            ).model_dump(mode="json"),
            capsule=capsule.model_dump(mode="json"),
            elevated=True,
        )


def test_a_payload_the_catalog_does_not_admit_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """The envelope is the whole surface; an invented field never parses."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    envelope = seal_call(
        capsule=capsule, tool_id="budget_status", payload=budget_payload()
    ).model_dump(mode="json")
    envelope["payload"]["elevated"] = True

    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call_verb(
            SEMANTIC_CALL_METHOD,
            ctx,
            repo_root=str(canary.root),
            call=envelope,
            capsule=capsule.model_dump(mode="json"),
        )


def test_a_production_root_reaches_neither_verb(tmp_path: Path) -> None:
    """The gateway sits behind the epoch-2 fence, like every native verb."""
    repo = tmp_path / "production"
    make_repo(repo)
    (repo / ".ea").mkdir()
    (repo / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    ctx = method_ctx(tmp_path / "runtime")
    capsule = seal_capsule()

    for method, params in (
        (
            SEMANTIC_CALL_METHOD,
            {
                "call": seal_call(
                    capsule=capsule, tool_id="budget_status", payload=budget_payload()
                ).model_dump(mode="json"),
                "capsule": capsule.model_dump(mode="json"),
            },
        ),
        (SEMANTIC_RESULT_READ_METHOD, {"run_ref": RUN_URN, "call_id": f"call-{1:016x}"}),
    ):
        with pytest.raises(NativeAuthorityRefusedError):
            call_verb(method, ctx, repo_root=str(repo), **params)
    assert not (repo / ".ea" / "local").exists()
