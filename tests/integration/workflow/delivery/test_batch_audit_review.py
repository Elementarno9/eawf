"""An independent verdict outranks the report aggregate, and is bound to one head.

Three things are driven here. The first is that one required audit row
which came back false, or which its auditor could not verify, stops the
Batch -- and stops it in a world where every producing Run reported
``pass`` and every candidate sealed, because the aggregate of those
reports is the authors marking their own work and is exactly what must
not be allowed to outvote the auditor.

The second is that the audit and the review are about the same code. A
row whose auditor bound one revision and whose reviewer bound another
does not describe a review of anything in particular, and is refused at
construction rather than averaged over.

The third is reviewer independence, driven from the provider-neutral
``reviewer-independence`` fixture. Independence is not asserted by
inspecting a Run: it is two things that cannot be built. A capsule that
grants a reviewing Run the candidate seal or a workspace patch does not
seal, and an attestation carrying either grant does not validate -- so a
reviewer with a mutation grant has no representation in the system at
all. The transcript half is the brief: it has no field a producing Run's
output could be put in, and its one prose field is scanned, so a pasted
excerpt is refused rather than passed along.

Nothing sleeps, polls, reaches the network or writes outside ``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, Self

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from eawf.kernel.delivery.batch_proof import (
    CANDIDATE_MUTATION_TOOLS,
    AuditVerdict,
    BatchAudit,
    BatchVerificationStage,
    ReviewerAttestation,
)
from eawf.kernel.delivery.integration import ConflictExitKind
from eawf.kernel.delivery.receipts import RevisionBinding, canonical_digest
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery import BatchVerifyParams, verify_batch
from eawf.workflow.delivery.verification_cycle import (
    ReviewerBrief,
    VerificationRefusal,
    VerificationRefusedError,
    require_reviewer_independence,
    reviewer_attestation,
)
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

REVIEWER_RUN: Final = f"{world.CONTAINER}/run/RUN-00000020"
REPAIR_TASK: Final = f"{world.CONTAINER}/task/EAWF-0077"
OPERATOR_ACTION: Final = f"{world.CONTAINER}/pending-action/ACT-0003"

#: Where the provider-neutral fixtures live.
FIXTURE_ROOT: Final = Path(__file__).resolve().parents[3] / "fixtures" / "runtime_contract" / "v1"

#: The grants a reviewer legitimately needs: read the tree, read the
#: diff, read state, hand back a report.
READ_ONLY_GRANTS: Final = (
    SemanticToolId.REPO_READ,
    SemanticToolId.DIFF_READ,
    SemanticToolId.SUBMIT_REPORT,
)

#: The exits a blocked cycle is given somewhere to land.
EXITS: Final[dict[ConflictExitKind, str]] = {
    ConflictExitKind.REPAIR_TASK: REPAIR_TASK,
    ConflictExitKind.OPERATOR_DECISION: OPERATOR_ACTION,
}


# ---- the provider-neutral fixture shape -------------------------------------


class CaseExpectation(BaseModel):
    """What the fixture says one independence case must answer with."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["attested", "refused"]
    code: VerificationRefusal | None = None

    @model_validator(mode="after")
    def _only_a_refusal_names_a_code(self) -> Self:
        """Require a code only where one is produced.

        Raises:
            ValueError: An attested case names a refusal code, which would
                assert against an outcome the guard never reaches.
        """
        if self.verdict == "attested" and self.code is not None:
            raise ValueError("an attested case has no refusal code")
        return self


class IndependenceCase(BaseModel):
    """One reviewer shape the fixture asks a named layer to judge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    summary: str
    layer: Literal["capsule", "attestation", "record"]
    tool_grants: tuple[SemanticToolId, ...]
    run_ref: str | None = None
    brief_batch_ref: str | None = None
    statement: str | None = None
    expected: CaseExpectation

    @model_validator(mode="after")
    def _an_attestation_case_carries_a_brief(self) -> Self:
        """Require the statement exactly where a brief is built.

        Raises:
            ValueError: An attestation case names no statement, or a case
                that builds no brief names one.
        """
        if (self.layer == "attestation") != (self.statement is not None):
            raise ValueError(f"case {self.case_id} carries a statement it does not use")
        return self


class IndependenceFixture(BaseModel):
    """The provider-neutral reviewer-independence fixture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["reviewer-independence/v1"]
    fixture_id: str
    summary: str
    batch_ref: str
    producer_run_refs: tuple[str, ...]
    reviewer_run_ref: str
    cases: tuple[IndependenceCase, ...]


def load_fixture() -> IndependenceFixture:
    """Return the reviewer-independence fixture."""
    return IndependenceFixture.model_validate(
        json.loads((FIXTURE_ROOT / "reviewer-independence.json").read_text(encoding="utf-8"))
    )


FIXTURE: Final = load_fixture()


# ---- the world under review --------------------------------------------------


def head_binding() -> RevisionBinding:
    """Return the exact revision the Batch currently delivers."""
    return world.delivering_generation().integrated_revision


def capsule(
    *,
    tool_grants: tuple[SemanticToolId, ...] = READ_ONLY_GRANTS,
    run_ref: str = REVIEWER_RUN,
    scope_ref: str = world.BATCH,
    purpose: RunPurpose = RunPurpose.REVIEW,
) -> AuthorityCapsule:
    """Return the sealed authority one reviewing Run is dispatched under."""
    return AuthorityCapsule.seal(
        {
            "run_ref": run_ref,
            "scope_ref": scope_ref,
            "scope_digest": canonical_digest("scope"),
            "agent_role": AgentSessionRole.REVIEWER.value,
            "purpose": purpose.value,
            "authority": {"state": "read_only", "workspace": "none"},
            "tool_grants": tuple(item.value for item in tool_grants),
            "budget": {"wall_seconds": 3600, "output_bytes": 1_048_576},
            "criteria_digest": canonical_digest("criteria"),
            "policy_digest": canonical_digest("policy"),
            "compiled_spec_digest": canonical_digest("spec"),
            "report_schema_ref": "schema://reviewer-report/v1",
            "stop_conditions": ("budget_exhausted",),
        }
    )


def brief(
    *,
    criterion_id: str = "CR-01",
    statement: str = "the published wheel installs from the index into a clean environment",
    batch_ref: str = world.BATCH,
    revision: RevisionBinding | None = None,
) -> ReviewerBrief:
    """Return everything one reviewer Run is given."""
    return ReviewerBrief(
        batch_ref=batch_ref,
        criterion_id=criterion_id,
        reviewed_revision=head_binding() if revision is None else revision,
        statement=statement,
    )


def attestation(
    *, tool_grants: tuple[SemanticToolId, ...] = READ_ONLY_GRANTS, run_ref: str = REVIEWER_RUN
) -> ReviewerAttestation:
    """Return an attestation built directly, bypassing the capsule."""
    return ReviewerAttestation(
        run_ref=run_ref,
        purpose=RunPurpose.REVIEW,
        reviewed_revision=head_binding(),
        capsule_digest=canonical_digest("capsule"),
        tool_grants=tool_grants,
    )


def audit(
    criterion_id: str,
    *,
    verdict: AuditVerdict = AuditVerdict.VERIFIED_TRUE,
    required: bool = True,
    review: ReviewerAttestation | None = None,
    audited: RevisionBinding | None = None,
    audit_id: str = "BAU-000001",
) -> BatchAudit:
    """Return one criterion's independent verdict on the Batch head."""
    revision = head_binding() if audited is None else audited
    return BatchAudit(
        id=audit_id,
        batch_ref=world.BATCH,
        criterion_id=criterion_id,
        required=required,
        verdict=verdict,
        audited_revision=revision,
        review=attestation() if review is None else review,
        finding=(
            f"criterion {criterion_id} does not hold on the reviewed tree"
            if verdict is not AuditVerdict.VERIFIED_TRUE
            else None
        ),
        recorded_at=world.AT,
    )


def native(tmp_path: Path, *, generations=()):
    """Return a canary tree whose Task sealed a passing candidate."""
    context = world.native_tree(tmp_path / "repo", tmp_path / "runtime")
    world.seed_task(context, world.task_row())
    world.seed_lines(context, world.TASK, [world.bundle_line(world.bundle_row())])
    if generations:
        world.seed_lines(
            context, world.BATCH, [world.generation_line(item) for item in generations]
        )
    return context


def params(**overrides: Any) -> BatchVerifyParams:
    """Return a verification request for the shared Batch."""
    fields: dict[str, Any] = {
        "urn": world.BATCH,
        "actor": "OPERATOR-LOCAL",
        "idempotency_key": "verify-1",
        "audits": (),
        "judgment_criterion_ids": ("CR-01",),
        "exit_refs": EXITS,
        "repair_budget": 1,
    }
    return BatchVerifyParams.model_validate(fields | overrides)


def run_verify(context, *, at: datetime | None = None, **overrides: Any):
    """Walk the Batch's cycle once against the seeded tree."""
    return verify_batch(context, params(**overrides), now=world.AT if at is None else at)


# ---- a required blocking row outranks the report aggregate -------------------


def test_verify_batch_clears_when_every_required_row_came_back_true(tmp_path: Path) -> None:
    """The positive control: a clear audit on the delivered head is merge-ready."""
    context = native(tmp_path, generations=(world.delivering_generation(),))
    answer = run_verify(context, audits=(audit("CR-01"),))
    assert answer.merge_ready is True
    assert answer.stage == BatchVerificationStage.READY_TO_MERGE.value
    assert answer.settled_criterion_ids == ("CR-01",)
    assert answer.blocking_criterion_ids == ()


@pytest.mark.parametrize("verdict", [AuditVerdict.VERIFIED_FALSE, AuditVerdict.UNVERIFIED])
def test_verify_batch_blocks_on_one_required_row_despite_an_all_pass_aggregate(
    tmp_path: Path, verdict: AuditVerdict
) -> None:
    """Every producing Run reported pass and sealed; one required row still blocks."""
    context = native(tmp_path, generations=(world.delivering_generation(),))
    sealed = world.bundle()
    assert sealed.verdict is AgentReportVerdict.PASS, "the aggregate under test is all-pass"
    answer = run_verify(context, audits=(audit("CR-01", verdict=verdict),))
    assert answer.merge_ready is False
    assert answer.blocking_criterion_ids == ("CR-01",)
    assert answer.stage == BatchVerificationStage.REPAIR.value


def test_verify_batch_does_not_let_an_unrequired_row_block(tmp_path: Path) -> None:
    """A row marked not required is advisory and does not stop the Batch."""
    context = native(tmp_path, generations=(world.delivering_generation(),))
    answer = run_verify(
        context,
        audits=(
            audit("CR-01", audit_id="BAU-000001"),
            audit(
                "CR-02",
                verdict=AuditVerdict.VERIFIED_FALSE,
                required=False,
                audit_id="BAU-000002",
            ),
        ),
    )
    assert answer.merge_ready is True
    assert answer.blocking_criterion_ids == ()


def test_verify_batch_refuses_a_judgment_criterion_that_carries_no_row(
    tmp_path: Path,
) -> None:
    """A criterion nothing looked at is refused, not silently cleared."""
    context = native(tmp_path, generations=(world.delivering_generation(),))
    with pytest.raises(DaemonValidationError, match="verification_audit_uncovered"):
        run_verify(context, audits=(), judgment_criterion_ids=("CR-01",))


def test_verify_batch_refuses_a_batch_with_no_selected_generation(tmp_path: Path) -> None:
    """With no integrated head there is no exact revision to verify against."""
    context = native(tmp_path)
    with pytest.raises(DaemonValidationError, match="completion_generation_unselected"):
        run_verify(context, audits=(audit("CR-01"),))


def test_verify_batch_refuses_a_reviewer_that_produced_the_work(tmp_path: Path) -> None:
    """The Run that sealed the Batch's candidate cannot review it."""
    context = native(tmp_path, generations=(world.delivering_generation(),))
    with pytest.raises(DaemonValidationError, match="verification_reviewer_is_producer"):
        run_verify(context, audits=(audit("CR-01", review=attestation(run_ref=world.RUN)),))


def test_verify_batch_files_the_cycle_it_reached(tmp_path: Path) -> None:
    """The walk is durable: a second pass reads the stage the first reached."""
    context = native(tmp_path, generations=(world.delivering_generation(),))
    first = run_verify(context, audits=(audit("CR-01"),))
    assert first.merge_ready is True
    second = run_verify(context, audits=(audit("CR-01"),))
    assert second.stage == BatchVerificationStage.READY_TO_MERGE.value
    assert "which nothing since has moved" in second.reason


# ---- audit and review bind the same exact head -------------------------------


def test_audit_refuses_a_review_bound_to_another_head() -> None:
    """A verdict whose reviewer read another commit is not a reviewed verdict."""
    elsewhere = world.binding(generation=2, head_sha=world.THIRD_HEAD)
    with pytest.raises(ValidationError, match="disagree on head_sha"):
        audit("CR-01", review=attestation_at(elsewhere))


def test_audit_refuses_a_review_bound_to_another_ordinal() -> None:
    """A review taken on a later generation is not this row's review."""
    later = world.binding(generation=3, head_sha=world.SECOND_HEAD)
    with pytest.raises(ValidationError, match="integration_generation"):
        audit("CR-01", review=attestation_at(later))


def attestation_at(revision: RevisionBinding) -> ReviewerAttestation:
    """Return an attestation whose reviewer read *revision*."""
    return ReviewerAttestation(
        run_ref=REVIEWER_RUN,
        purpose=RunPurpose.REVIEW,
        reviewed_revision=revision,
        capsule_digest=canonical_digest("capsule"),
        tool_grants=READ_ONLY_GRANTS,
    )


def test_verify_batch_refuses_a_row_taken_past_the_delivered_head(tmp_path: Path) -> None:
    """A verdict about a newer tree than the Batch delivers cannot be counted."""
    context = native(tmp_path, generations=(world.delivering_generation(),))
    ahead = world.binding(generation=3, head_sha=world.THIRD_HEAD)
    with pytest.raises(DaemonValidationError, match="verification_audit_unbound"):
        run_verify(
            context,
            audits=(audit("CR-01", audited=ahead, review=attestation_at(ahead)),),
        )


# ---- reviewer independence, from the provider-neutral fixture ----------------


def run_case(case: IndependenceCase) -> None:
    """Drive one fixture case through the layer it names.

    Raises:
        ValidationError: A capsule or record case is refused by
            construction.
        VerificationRefusedError: An attestation case is refused by a
            named rule.
    """
    run_ref = case.run_ref or FIXTURE.reviewer_run_ref
    if case.layer == "capsule":
        capsule(tool_grants=case.tool_grants, run_ref=run_ref)
        return
    if case.layer == "record":
        attestation(tool_grants=case.tool_grants, run_ref=run_ref)
        return
    assert case.statement is not None, "an attestation case carries a brief"
    reviewer_attestation(
        capsule(tool_grants=case.tool_grants, run_ref=run_ref),
        brief=brief(
            statement=case.statement,
            batch_ref=case.brief_batch_ref or FIXTURE.batch_ref,
            revision=(
                world.binding(
                    generation=2,
                    head_sha=world.SECOND_HEAD,
                    batch_ref=case.brief_batch_ref,
                )
                if case.brief_batch_ref
                else None
            ),
        ),
        producer_run_refs=FIXTURE.producer_run_refs,
    )


@pytest.mark.parametrize(
    "case",
    [item for item in FIXTURE.cases if item.expected.verdict == "attested"],
    ids=lambda item: item.case_id,
)
def test_reviewer_independence_admits_an_independent_reviewer(
    case: IndependenceCase,
) -> None:
    """The fixture's admitted shapes build without complaint."""
    run_case(case)


@pytest.mark.parametrize(
    "case",
    [item for item in FIXTURE.cases if item.expected.verdict == "refused"],
    ids=lambda item: item.case_id,
)
def test_reviewer_independence_refuses_a_dependent_reviewer(
    case: IndependenceCase,
) -> None:
    """Each refused shape is refused, by construction or by a named code."""
    if case.expected.code is None:
        with pytest.raises(ValidationError):
            run_case(case)
        return
    with pytest.raises(VerificationRefusedError) as caught:
        run_case(case)
    assert caught.value.code is case.expected.code


def test_reviewer_independence_fixture_covers_every_mutation_tool() -> None:
    """Every tool the catalog calls mutating is exercised by a refused case."""
    refused = {
        tool
        for item in FIXTURE.cases
        if item.expected.verdict == "refused"
        for tool in item.tool_grants
    }
    assert refused >= CANDIDATE_MUTATION_TOOLS


def test_reviewer_independence_fixture_carries_no_vendor_identifier() -> None:
    """The fixture is provider-neutral: no machine path, no vendor session id."""
    body = (FIXTURE_ROOT / "reviewer-independence.json").read_text(encoding="utf-8").lower()
    for root in ("users", "home", "root"):
        assert f"/{root}/" not in body, "a fixture carries no machine-specific path"
    for marker in ("session", "sk-", "token", "@"):
        assert marker not in body, f"a fixture carries no {marker!r} identifier"


# ---- the guards red on a real defect ----------------------------------------


@pytest.mark.parametrize("tool", sorted(CANDIDATE_MUTATION_TOOLS))
def test_capsule_refuses_a_reviewer_granted_a_mutation_tool(tool: SemanticToolId) -> None:
    """A reviewing Run granted a workspace-writing tool cannot be sealed at all."""
    with pytest.raises(ValidationError, match="need a mutating task-scoped Run"):
        capsule(tool_grants=(SemanticToolId.REPO_READ, tool))


@pytest.mark.parametrize("tool", sorted(CANDIDATE_MUTATION_TOOLS))
def test_attestation_refuses_a_reviewer_granted_a_mutation_tool(
    tool: SemanticToolId,
) -> None:
    """The record refuses the same grant, so a hand-built row cannot carry one."""
    with pytest.raises(ValidationError, match="writes the tree its verdict is about"):
        attestation(tool_grants=(SemanticToolId.REPO_READ, tool))


def test_brief_has_no_field_a_transcript_could_be_put_in() -> None:
    """A dispatcher cannot hand over a producer's output by filling a field."""
    assert "transcript" not in ReviewerBrief.model_fields
    assert set(ReviewerBrief.model_fields) == {
        "batch_ref",
        "criterion_id",
        "reviewed_revision",
        "statement",
    }
    with pytest.raises(ValidationError, match="transcript"):
        ReviewerBrief.model_validate(
            {
                **brief().model_dump(mode="json"),
                "transcript": "the executor wrote the module and then ran the suite",
            }
        )


def test_reviewer_attestation_refuses_a_brief_quoting_a_producer_run() -> None:
    """The prose scan reds on a real pasted excerpt rather than on a marker."""
    with pytest.raises(VerificationRefusedError) as caught:
        reviewer_attestation(
            capsule(),
            brief=brief(statement="RUN-00000010 says the wheel installs, so confirm that"),
            producer_run_refs=(world.RUN,),
        )
    assert caught.value.code is VerificationRefusal.REVIEWER_PRODUCER_TRANSCRIPT
    assert "RUN-00000010" in str(caught.value)


def test_reviewer_attestation_admits_a_statement_that_names_no_run() -> None:
    """A criterion statement about the code passes the scan unchanged."""
    built = reviewer_attestation(capsule(), brief=brief(), producer_run_refs=(world.RUN,))
    assert built.tool_grants == READ_ONLY_GRANTS
    assert built.purpose is RunPurpose.REVIEW
    assert built.reviewed_revision == head_binding()


def test_require_reviewer_independence_admits_an_unrelated_reviewer() -> None:
    """A reviewer that sealed nothing for the Batch passes the record check."""
    require_reviewer_independence((audit("CR-01"),), producer_run_refs=(world.RUN,))


def test_require_reviewer_independence_refuses_an_empty_producer_set_safely() -> None:
    """With no producers on file every reviewer is independent of them."""
    require_reviewer_independence((audit("CR-01"),), producer_run_refs=())
