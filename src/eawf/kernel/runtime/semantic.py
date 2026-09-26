"""Semantic call envelopes, the tool catalog, and the closed error set.

A provider process reaches the daemon through one shape only: a
:class:`SemanticCall` naming a catalog tool and carrying a typed payload
together with the digest of that payload. The envelope is the whole
surface, so a provider that invents a field, a tool, or an error code is
refused at the boundary rather than handled downstream.

The catalog is closed. Every tool declares a typed input model and a
typed output model, and :func:`tool_input_schema` renders the input model
as JSON Schema for the per-Run MCP stdio server to publish unchanged --
one model produces both the validator and the published schema, so the
two cannot drift.

:class:`SemanticToolError` carries a closed code and the retry class that
code implies. The class travels on the wire because the provider process
reads the envelope rather than this table, and it is recomputed on every
validation so a handler cannot label a policy denial as transient.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from eawf.kernel.runtime.compiled import BoundedText, JsonPointer, canonical_digest
from eawf.kernel.runtime.lease import LeaseId, WorkspaceHandle
from eawf.kernel.runtime.provider import (
    ArtifactUrn,
    CommandFamilyId,
    Digest,
    RuntimeRecord,
    SchemaUrn,
    SemVer,
    reject_repeats,
)
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.epoch2.run import SuspensionReason
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, EvidenceUrn, QuestionUrn, RunUrn, TaskUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.kinds.evidence import EvidenceSourceKind

#: The version every catalog tool's input and output schema carries. One
#: version covers the catalog because a provider binds the catalog as a
#: whole, not one tool at a time.
TOOL_SCHEMA_VERSION: Final[str] = "1.0.0"


def _grammar(pattern: str, *, max_length: int = 256) -> StringConstraints:
    """Return a strict string constraint for *pattern*."""
    return StringConstraints(strict=True, max_length=max_length, pattern=pattern)


def reject_path_escape(value: str) -> str:
    """Refuse a repository-relative path that walks out of the tree.

    Args:
        value: A path already admitted by the string grammar.

    Returns:
        *value* unchanged.

    Raises:
        ValueError: A ``..`` segment walks out of the repository.
    """
    if ".." in value.split("/"):
        raise ValueError(f"path {value!r} walks out of the repository")
    return value


# ---- identifiers -------------------------------------------------------------

CallId = Annotated[str, _grammar(r"^call-[0-9a-f]{16}$", max_length=21)]
ReceiptId = Annotated[str, _grammar(r"^receipt-[0-9a-f]{16}$", max_length=24)]
#: The key the gateway replays a call by. Same key and same payload digest
#: return the original receipt; same key and another digest is refused.
IdempotencyKey = Annotated[str, _grammar(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", max_length=128)]
#: A candidate's own identifier, mirrored from the grammar
#: ``eawf.kernel.runtime.candidate.candidate_identity`` produces --
#: duplicated rather than imported because that module imports this one.
CandidateRef = Annotated[str, _grammar(r"^CND-[0-9a-f]{32}$", max_length=36)]
OptionId = Annotated[str, _grammar(r"^[a-z][a-z0-9_-]{0,31}$", max_length=32)]
CriterionId = Annotated[str, _grammar(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", max_length=64)]
FindingCode = Annotated[str, _grammar(r"^[a-z][a-z0-9_]{0,63}$", max_length=64)]
ProjectionSelector = Annotated[
    str, _grammar(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,3}$", max_length=128)
]
FieldName = Annotated[str, _grammar(r"^[a-z][a-z0-9_]{0,63}$", max_length=64)]
RepoRelativePath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=500, pattern=r"^[^/\\][^\\]*$"),
    AfterValidator(reject_path_escape),
]
#: A bounded stream excerpt. Empty is admitted: a command that wrote
#: nothing is a fact, not a missing value.
BoundedStreamText = Annotated[str, StringConstraints(strict=True, max_length=4096)]
CommandArgument = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]

UniqueFieldNames = Annotated[tuple[FieldName, ...], AfterValidator(reject_repeats)]
UniqueCriterionIds = Annotated[tuple[CriterionId, ...], AfterValidator(reject_repeats)]


# ---- closed vocabularies -----------------------------------------------------


class SemanticToolId(StrEnum):
    """The closed catalog of tools a Run may call.

    The three repository read tools are one contract row of the runtime
    agreement and three ids here, because tool grants narrow by id: a
    profile that grants a file read must not thereby grant a diff read.
    """

    EAWF_STATE_QUERY = "eawf_state_query"
    REPO_READ = "repo_read"
    REPO_SEARCH = "repo_search"
    DIFF_READ = "diff_read"
    WORKSPACE_APPLY_PATCH = "workspace_apply_patch"
    RUN_SCOPED_COMMAND = "run_scoped_command"
    REPORT_PROGRESS = "report_progress"
    ATTACH_EVIDENCE = "attach_evidence"
    ASK_OPERATOR = "ask_operator"
    SUBMIT_PLAN = "submit_plan"
    SUBMIT_COORDINATION_PROPOSAL = "submit_coordination_proposal"
    SUBMIT_CANDIDATE = "submit_candidate"
    SUBMIT_REPORT = "submit_report"
    SUBMIT_EVIDENCE = "submit_evidence"
    BUDGET_STATUS = "budget_status"


class SemanticToolErrorCode(StrEnum):
    """Why the gateway refused or failed a call. The set is closed."""

    RUN_NOT_ACTIVE = "RUN_NOT_ACTIVE"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    SCOPE_DENIED = "SCOPE_DENIED"
    LEASE_NOT_ACTIVE = "LEASE_NOT_ACTIVE"
    STALE_WORKSPACE_GENERATION = "STALE_WORKSPACE_GENERATION"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    POLICY_REVOKED = "POLICY_REVOKED"
    CERTIFICATION_REVOKED = "CERTIFICATION_REVOKED"
    PAYLOAD_INVALID = "PAYLOAD_INVALID"
    IDEMPOTENCY_PAYLOAD_MISMATCH = "IDEMPOTENCY_PAYLOAD_MISMATCH"
    PROTECTED_ACTION_REQUIRED = "PROTECTED_ACTION_REQUIRED"
    PROVIDER_CONTINUITY_UNKNOWN = "PROVIDER_CONTINUITY_UNKNOWN"
    INTERNAL_TRANSIENT = "INTERNAL_TRANSIENT"


class RetryClass(StrEnum):
    """What, if anything, would let a refused call succeed."""

    NEVER = "never"
    AFTER_INPUT_CHANGE = "after_input_change"
    AFTER_POLICY_CHANGE = "after_policy_change"
    TRANSIENT_SAME_RUN = "transient_same_run"
    NEW_LINKED_RUN = "new_linked_run"


#: The retry class each code implies, total over the code set. One code
#: means one remedy: a caller that saw two classes for one code could not
#: write a ladder against either.
RETRY_CLASS_BY_CODE: Final[Mapping[SemanticToolErrorCode, RetryClass]] = MappingProxyType(
    {
        SemanticToolErrorCode.RUN_NOT_ACTIVE: RetryClass.NEVER,
        SemanticToolErrorCode.CONTRACT_MISMATCH: RetryClass.NEW_LINKED_RUN,
        SemanticToolErrorCode.CAPABILITY_DENIED: RetryClass.AFTER_POLICY_CHANGE,
        SemanticToolErrorCode.SCOPE_DENIED: RetryClass.NEVER,
        SemanticToolErrorCode.LEASE_NOT_ACTIVE: RetryClass.NEW_LINKED_RUN,
        SemanticToolErrorCode.STALE_WORKSPACE_GENERATION: RetryClass.AFTER_INPUT_CHANGE,
        SemanticToolErrorCode.BUDGET_EXHAUSTED: RetryClass.AFTER_POLICY_CHANGE,
        SemanticToolErrorCode.POLICY_REVOKED: RetryClass.AFTER_POLICY_CHANGE,
        SemanticToolErrorCode.CERTIFICATION_REVOKED: RetryClass.AFTER_POLICY_CHANGE,
        SemanticToolErrorCode.PAYLOAD_INVALID: RetryClass.AFTER_INPUT_CHANGE,
        SemanticToolErrorCode.IDEMPOTENCY_PAYLOAD_MISMATCH: RetryClass.AFTER_INPUT_CHANGE,
        SemanticToolErrorCode.PROTECTED_ACTION_REQUIRED: RetryClass.AFTER_POLICY_CHANGE,
        SemanticToolErrorCode.PROVIDER_CONTINUITY_UNKNOWN: RetryClass.NEW_LINKED_RUN,
        SemanticToolErrorCode.INTERNAL_TRANSIENT: RetryClass.TRANSIENT_SAME_RUN,
    }
)

#: Shapes that make a message a shell command rather than a reason. A
#: remedy the worker can paste into a shell routes around the broker,
#: which is the one thing the gateway exists to prevent.
_SHELL_SHAPES: Final[tuple[str, ...]] = ("`", "$(", "&&", "||", "sudo ")


class ProgressMilestone(StrEnum):
    """The progress points a Run may report. Free-text status is not one."""

    STARTED = "started"
    SCOPE_CONFIRMED = "scope_confirmed"
    CRITERION_MET = "criterion_met"
    BLOCKED = "blocked"
    FINISHING = "finishing"


class OperatorQuestionKind(StrEnum):
    """What an operator is being asked to settle."""

    SCOPE_CLARIFICATION = "scope_clarification"
    DESIGN_CHOICE = "design_choice"
    PERMISSION = "permission"
    ACCEPTANCE = "acceptance"


class CoordinationAction(StrEnum):
    """The coordination changes a proposal may ask the daemon to make."""

    RESEQUENCE = "resequence"
    SPLIT_BATCH = "split_batch"
    MERGE_BATCH = "merge_batch"
    DEFER = "defer"
    ESCALATE = "escalate"


#: The four outcomes of a call, as the gateway records them on the
#: receipt. ``denied`` is a policy refusal, ``blocked`` waits on an
#: operator or another Run, and ``failed`` is the handler's own error.
SemanticCallStatus = Literal["succeeded", "denied", "blocked", "failed"]
BudgetQuality = Literal["measured", "estimated", "unknown"]


# ---- error -------------------------------------------------------------------


class SemanticToolError(RuntimeRecord):
    """Why one call did not succeed, and what could change that."""

    code: SemanticToolErrorCode
    retry_class: RetryClass
    message: BoundedText
    field_path: JsonPointer | None = None
    diagnostic_ref: ArtifactUrn | None = None

    @model_validator(mode="after")
    def _retry_class_and_message_match_the_code(self) -> Self:
        """Bind the retry class to the code and keep shell out of the message.

        Raises:
            ValueError: The retry class is not the one the code implies,
                or the message hands back a shell command as the remedy.
        """
        expected = RETRY_CLASS_BY_CODE[self.code]
        if self.retry_class is not expected:
            raise ValueError(
                f"code {self.code.value!r} implies retry_class {expected.value!r}, "
                f"not {self.retry_class.value!r}"
            )
        shape = next((shape for shape in _SHELL_SHAPES if shape in self.message), None)
        if shape is not None:
            raise ValueError(f"message offers a shell remedy at {shape!r}")
        return self


def error_for(
    code: SemanticToolErrorCode,
    *,
    message: str,
    field_path: str | None = None,
    diagnostic_ref: str | None = None,
) -> SemanticToolError:
    """Return the error of *code* with the retry class that code implies.

    Args:
        code: The refusal or failure code.
        message: The bounded reason, carrying no shell command.
        field_path: The offending member of the payload, when one exists.
        diagnostic_ref: The artifact a diagnosis was written to.

    Returns:
        The validated error.

    Raises:
        pydantic.ValidationError: A field breaks the error's own rules.
    """
    return SemanticToolError(
        code=code,
        retry_class=RETRY_CLASS_BY_CODE[code],
        message=message,
        field_path=field_path,
        diagnostic_ref=diagnostic_ref,
    )


# ---- tool inputs -------------------------------------------------------------


class SemanticToolInputBase(RuntimeRecord):
    """Base of every typed tool input: frozen, closed, and tool-tagged."""


class StateQueryInput(SemanticToolInputBase):
    """Read one projection of canonical state, filtered to the Run's scope."""

    tool_id: Literal["eawf_state_query"]
    selector: ProjectionSelector
    scope_ref: AnyEntityUrn
    requested_fields: UniqueFieldNames = ()
    cursor: BoundedText | None = None


class RepoReadInput(SemanticToolInputBase):
    """Read a bounded byte range of one repository-relative file."""

    tool_id: Literal["repo_read"]
    resource_handle: WorkspaceHandle
    path: RepoRelativePath
    start_byte: Annotated[StrictInt, Field(ge=0)] = 0
    byte_cap: Annotated[StrictInt, Field(ge=1, le=1_048_576)] = 65_536


class RepoSearchInput(SemanticToolInputBase):
    """Search the read set for a bounded number of matches."""

    tool_id: Literal["repo_search"]
    resource_handle: WorkspaceHandle
    query: BoundedText
    path_prefix: RepoRelativePath | None = None
    result_cap: Annotated[StrictInt, Field(ge=1, le=1000)] = 100


class DiffReadInput(SemanticToolInputBase):
    """Read the diff of the Run's read set against its base tree."""

    tool_id: Literal["diff_read"]
    resource_handle: WorkspaceHandle
    path: RepoRelativePath | None = None
    byte_cap: Annotated[StrictInt, Field(ge=1, le=1_048_576)] = 262_144


class WorkspaceApplyPatchInput(SemanticToolInputBase):
    """Apply one patch to the lease workspace at an expected generation."""

    tool_id: Literal["workspace_apply_patch"]
    lease_id: LeaseId
    expected_workspace_generation: Annotated[StrictInt, Field(gt=0)]
    patch_ref: ArtifactUrn
    patch_digest: Digest


class RunScopedCommandInput(SemanticToolInputBase):
    """Run one command of an approved family inside the sandbox.

    The family is named rather than the executable: an argv the worker
    spells in full would be a shell by another name.
    """

    tool_id: Literal["run_scoped_command"]
    command_family_id: CommandFamilyId
    cwd_handle: WorkspaceHandle
    argv: Annotated[tuple[CommandArgument, ...], Field(min_length=1, max_length=64)]
    timeout_seconds: Annotated[StrictInt, Field(ge=1, le=3600)]
    expected_evidence_kind: EvidenceSourceKind


class ReportProgressInput(SemanticToolInputBase):
    """Record one progress milestone against criteria and evidence."""

    tool_id: Literal["report_progress"]
    milestone: ProgressMilestone
    summary: BoundedText
    criterion_ids: UniqueCriterionIds = ()
    evidence_refs: Annotated[tuple[EvidenceUrn, ...], Field(max_length=32)] = ()


class AttachEvidenceInput(SemanticToolInputBase):
    """Attach one immutable evidence row to a subject and criterion."""

    tool_id: Literal["attach_evidence"]
    subject_ref: AnyEntityUrn
    criterion_id: CriterionId
    evidence_kind: EvidenceSourceKind
    artifact_ref: ArtifactUrn
    note: BoundedText | None = None


class OperatorOption(RuntimeRecord):
    """One answer an operator may pick, with the effect picking it has."""

    option_id: OptionId
    summary: BoundedText
    effect: BoundedText


class AskOperatorInput(SemanticToolInputBase):
    """Ask the operator one bounded question with two or three answers."""

    tool_id: Literal["ask_operator"]
    question_kind: OperatorQuestionKind
    subject_ref: AnyEntityUrn
    options: Annotated[tuple[OperatorOption, ...], Field(min_length=2, max_length=3)]
    recommended_option_id: OptionId
    reversible: StrictBool
    protected: StrictBool

    @model_validator(mode="after")
    def _options_are_distinct_and_carry_the_recommendation(self) -> Self:
        """Require distinct options and a recommendation among them.

        Raises:
            ValueError: Two options share an id, or the recommendation
                names no offered option.
        """
        reject_repeats(tuple(option.option_id for option in self.options))
        if self.recommended_option_id not in {option.option_id for option in self.options}:
            raise ValueError(f"recommended option {self.recommended_option_id!r} is not offered")
        return self


class SubmitPlanInput(SemanticToolInputBase):
    """Propose one plan revision, carried as a digest-bound artifact.

    The revision travels by reference because a plan is larger than an
    envelope should be; the daemon validates the artifact against the
    plan schema before any proposal exists.
    """

    tool_id: Literal["submit_plan"]
    plan_scope_ref: AnyEntityUrn
    proposal_ref: ArtifactUrn
    proposal_digest: Digest
    supersedes_revision: Annotated[StrictInt, Field(gt=0)] | None = None


class SubmitCoordinationProposalInput(SemanticToolInputBase):
    """Propose one coordination change over named targets."""

    tool_id: Literal["submit_coordination_proposal"]
    action: CoordinationAction
    target_refs: Annotated[tuple[AnyEntityUrn, ...], Field(min_length=1, max_length=32)]
    basis: BoundedText
    estimated_cost_microusd: Annotated[StrictInt, Field(ge=0)] | None = None
    precondition_refs: Annotated[tuple[AnyEntityUrn, ...], Field(max_length=32)] = ()


class SubmitCandidateInput(SemanticToolInputBase):
    """Submit the work of one lease as a candidate for integration."""

    tool_id: Literal["submit_candidate"]
    task_ref: TaskUrn
    lease_id: LeaseId
    submission_ref: ArtifactUrn
    changed_paths: Annotated[
        tuple[RepoRelativePath, ...],
        Field(min_length=1, max_length=512),
        AfterValidator(reject_repeats),
    ]
    resulting_tree_digest: Digest


class SubmitReportInput(SemanticToolInputBase):
    """Submit the terminal report body the role's schema pins."""

    tool_id: Literal["submit_report"]
    report_schema_ref: SchemaUrn
    contract_digest: Digest
    body_ref: ArtifactUrn
    body_digest: Digest
    verdict: AgentReportVerdict


class SubmitEvidenceInput(SemanticToolInputBase):
    """Submit one verified SpikeReport's measured contracts, carried by reference.

    The report travels by reference for the same reason a plan proposal
    does: it is larger than an envelope should be, and the daemon reads
    the body from its own ledger rather than trusting the call to repeat
    it faithfully.
    """

    tool_id: Literal["submit_evidence"]
    spike_report_ref: ArtifactUrn
    spike_report_digest: Digest


class BudgetStatusInput(SemanticToolInputBase):
    """Read the Run's measured usage and remaining ceilings."""

    tool_id: Literal["budget_status"]
    include_children: StrictBool = False


#: One call's payload, discriminated on the tool it names.
SemanticToolInput = Annotated[
    StateQueryInput
    | RepoReadInput
    | RepoSearchInput
    | DiffReadInput
    | WorkspaceApplyPatchInput
    | RunScopedCommandInput
    | ReportProgressInput
    | AttachEvidenceInput
    | AskOperatorInput
    | SubmitPlanInput
    | SubmitCoordinationProposalInput
    | SubmitCandidateInput
    | SubmitReportInput
    | SubmitEvidenceInput
    | BudgetStatusInput,
    Field(discriminator="tool_id"),
]


# ---- tool outputs ------------------------------------------------------------


class SemanticToolOutputBase(RuntimeRecord):
    """Base of every typed tool output: frozen, closed, and tool-tagged."""


class ValidationFinding(RuntimeRecord):
    """One reason a submission was not accepted."""

    code: FindingCode
    message: BoundedText
    field_path: JsonPointer | None = None


def _check_acceptance(*, accepted: bool, ref: Any, findings: tuple[ValidationFinding, ...]) -> None:
    """Refuse an acceptance with no reference or a refusal with no finding.

    Args:
        accepted: Whether the submission validated.
        ref: The reference the daemon minted on acceptance.
        findings: The reasons a refusal cites.

    Raises:
        ValueError: Acceptance carries no reference or carries findings,
            or a refusal names no finding.
    """
    if accepted and (ref is None or findings):
        raise ValueError("an accepted submission carries a reference and no findings")
    if not accepted and not findings:
        raise ValueError("a refused submission names at least one finding")


class StateQueryOutput(SemanticToolOutputBase):
    """The projection a state query resolved to, by reference and revision."""

    tool_id: Literal["eawf_state_query"]
    projection_ref: ArtifactUrn
    projection_digest: Digest
    projection_revision: Annotated[StrictInt, Field(gt=0)]
    truncated: StrictBool = False
    next_cursor: BoundedText | None = None


class RepoReadOutput(SemanticToolOutputBase):
    """The bytes a repository read returned, by reference and digest."""

    tool_id: Literal["repo_read"]
    content_ref: ArtifactUrn
    content_digest: Digest
    byte_count: Annotated[StrictInt, Field(ge=0)]
    truncated: StrictBool = False


class RepoSearchOutput(SemanticToolOutputBase):
    """The matches a repository search returned, by reference and digest."""

    tool_id: Literal["repo_search"]
    match_ref: ArtifactUrn
    match_digest: Digest
    match_count: Annotated[StrictInt, Field(ge=0)]
    truncated: StrictBool = False


class DiffReadOutput(SemanticToolOutputBase):
    """The diff a read returned, by reference and digest."""

    tool_id: Literal["diff_read"]
    diff_ref: ArtifactUrn
    diff_digest: Digest
    byte_count: Annotated[StrictInt, Field(ge=0)]
    truncated: StrictBool = False


class WorkspaceApplyPatchOutput(SemanticToolOutputBase):
    """What the patch changed and the tree that resulted."""

    tool_id: Literal["workspace_apply_patch"]
    changed_paths: Annotated[
        tuple[RepoRelativePath, ...], Field(max_length=512), AfterValidator(reject_repeats)
    ]
    resulting_tree_digest: Digest
    workspace_generation: Annotated[StrictInt, Field(gt=0)]


class RunScopedCommandOutput(SemanticToolOutputBase):
    """How a scoped command ended, with bounded streams and its digests."""

    tool_id: Literal["run_scoped_command"]
    outcome: Literal["completed", "timed_out", "denied"]
    exit_status: Annotated[StrictInt, Field(ge=0, le=255)] | None = None
    stdout_excerpt: BoundedStreamText = ""
    stderr_excerpt: BoundedStreamText = ""
    log_ref: ArtifactUrn
    command_digest: Digest
    environment_digest: Digest
    truncated: StrictBool = False

    @model_validator(mode="after")
    def _completed_command_carries_its_status(self) -> Self:
        """Require an exit status exactly when the command ran to the end.

        Raises:
            ValueError: A completed command has no exit status, or a
                command that never finished reports one.
        """
        if (self.outcome == "completed") is not (self.exit_status is not None):
            raise ValueError(f"outcome {self.outcome!r} disagrees with exit_status")
        return self


class ReportProgressOutput(SemanticToolOutputBase):
    """The progress event the daemon recorded."""

    tool_id: Literal["report_progress"]
    event_sequence: Annotated[StrictInt, Field(gt=0)]
    recorded_at: UtcDatetime


class AttachEvidenceOutput(SemanticToolOutputBase):
    """The immutable evidence row the daemon wrote."""

    tool_id: Literal["attach_evidence"]
    evidence_ref: EvidenceUrn
    evidence_digest: Digest
    immutable: Literal[True] = True


class AskOperatorOutput(SemanticToolOutputBase):
    """The question the daemon opened and what it did to the Run."""

    tool_id: Literal["ask_operator"]
    question_ref: QuestionUrn
    suspended: StrictBool
    suspension_reason: SuspensionReason | None = None

    @model_validator(mode="after")
    def _suspension_names_what_clears_it(self) -> Self:
        """Require a reason exactly when the Run was suspended.

        Raises:
            ValueError: A suspended Run names no clearing fact, or a Run
                that kept running names one.
        """
        if self.suspended is not (self.suspension_reason is not None):
            raise ValueError("suspended disagrees with suspension_reason")
        return self


class SubmitPlanOutput(SemanticToolOutputBase):
    """Whether the plan proposal validated, and the findings if not."""

    tool_id: Literal["submit_plan"]
    accepted: StrictBool
    proposal_ref: AnyEntityUrn | None = None
    findings: Annotated[tuple[ValidationFinding, ...], Field(max_length=64)] = ()

    @model_validator(mode="after")
    def _acceptance_carries_its_evidence(self) -> Self:
        """Require a reference on acceptance and a finding on refusal.

        Raises:
            ValueError: An accepted proposal has no reference or carries
                findings, or a refused one names no finding.
        """
        _check_acceptance(accepted=self.accepted, ref=self.proposal_ref, findings=self.findings)
        return self


class SubmitCoordinationProposalOutput(SemanticToolOutputBase):
    """The coordination proposal reference and how the daemon disposed of it."""

    tool_id: Literal["submit_coordination_proposal"]
    proposal_ref: AnyEntityUrn
    disposition: Literal["queued", "declined", "superseded"]


class SubmitCandidateOutput(SemanticToolOutputBase):
    """The claim the daemon recorded, and the bundle it seals to if one already stands.

    There is no integration result here: a worker proposes content and
    the daemon alone integrates, so a field naming an integration outcome
    would report an authority the worker does not hold. Sealing is a
    second, separate act that needs an accepted terminal report this call
    does not carry, so ``sealed_at`` names the standing bundle when one
    already exists and is absent otherwise -- a submission is never sealed
    by the act of being made.
    """

    tool_id: Literal["submit_candidate"]
    candidate_ref: CandidateRef
    resulting_tree_digest: Digest
    sealed_at: UtcDatetime | None = None


class SubmitReportOutput(SemanticToolOutputBase):
    """Whether the terminal report validated, and the findings if not."""

    tool_id: Literal["submit_report"]
    accepted: StrictBool
    report_ref: AnyEntityUrn | None = None
    findings: Annotated[tuple[ValidationFinding, ...], Field(max_length=64)] = ()

    @model_validator(mode="after")
    def _acceptance_carries_its_evidence(self) -> Self:
        """Require a reference on acceptance and a finding on refusal.

        Raises:
            ValueError: An accepted report has no reference or carries
                findings, or a refused one names no finding.
        """
        _check_acceptance(accepted=self.accepted, ref=self.report_ref, findings=self.findings)
        return self


class SubmitEvidenceOutput(SemanticToolOutputBase):
    """Whether the report validated, and the artifact revision minted per contract.

    ``contract_refs`` carries the v1 ``urn:eawf:v1:artifact:<scope>/<id>``
    form :func:`eawf.workflow.evidence.measured_contract.promote_measured_contract`
    mints, not an :data:`ArtifactUrn` -- the two catalogs are addressed
    differently, and this daemon promotes into the v1 one. Unlike the
    other submit-family outputs, acceptance does not require a non-empty
    reference either: a verified report that discriminated between
    designs without probing an external surface legitimately promotes
    nothing, and ``contract_refs`` is empty exactly then.
    """

    tool_id: Literal["submit_evidence"]
    accepted: StrictBool
    contract_refs: Annotated[tuple[BoundedText, ...], Field(max_length=64)] = ()
    findings: Annotated[tuple[ValidationFinding, ...], Field(max_length=64)] = ()

    @model_validator(mode="after")
    def _acceptance_carries_no_findings(self) -> Self:
        """Require no findings on acceptance and at least one on refusal.

        Raises:
            ValueError: An accepted submission carries findings, or a
                refused one names no finding.
        """
        if self.accepted and self.findings:
            raise ValueError("an accepted submission carries no findings")
        if not self.accepted and not self.findings:
            raise ValueError("a refused submission names at least one finding")
        return self


class BudgetUsage(RuntimeRecord):
    """One side of the budget: what was used, or what remains."""

    tokens: Annotated[StrictInt, Field(ge=0)] | None = None
    cost_microusd: Annotated[StrictInt, Field(ge=0)] | None = None
    wall_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    output_bytes: Annotated[StrictInt, Field(ge=0)] | None = None
    tool_calls: Annotated[StrictInt, Field(ge=0)] | None = None


class BudgetStatusOutput(SemanticToolOutputBase):
    """Measured usage against the remaining ceilings, with its quality.

    ``quality`` travels with the numbers because an estimate read as a
    measurement is how a budget is overrun while it looks observed.
    """

    tool_id: Literal["budget_status"]
    used: BudgetUsage
    remaining: BudgetUsage
    quality: BudgetQuality
    includes_children: StrictBool = False


#: One result's payload, discriminated on the tool it answers.
SemanticToolOutput = Annotated[
    StateQueryOutput
    | RepoReadOutput
    | RepoSearchOutput
    | DiffReadOutput
    | WorkspaceApplyPatchOutput
    | RunScopedCommandOutput
    | ReportProgressOutput
    | AttachEvidenceOutput
    | AskOperatorOutput
    | SubmitPlanOutput
    | SubmitCoordinationProposalOutput
    | SubmitCandidateOutput
    | SubmitReportOutput
    | SubmitEvidenceOutput
    | BudgetStatusOutput,
    Field(discriminator="tool_id"),
]


# ---- catalog -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SemanticToolContract:
    """What one catalog tool takes, returns, and requires of the Run."""

    tool_id: SemanticToolId
    input_model: type[SemanticToolInputBase]
    output_model: type[SemanticToolOutputBase]
    schema_version: str = TOOL_SCHEMA_VERSION
    requires_mutating_task: bool = False


_CONTRACTS: Final[tuple[SemanticToolContract, ...]] = (
    SemanticToolContract(SemanticToolId.EAWF_STATE_QUERY, StateQueryInput, StateQueryOutput),
    SemanticToolContract(SemanticToolId.REPO_READ, RepoReadInput, RepoReadOutput),
    SemanticToolContract(SemanticToolId.REPO_SEARCH, RepoSearchInput, RepoSearchOutput),
    SemanticToolContract(SemanticToolId.DIFF_READ, DiffReadInput, DiffReadOutput),
    SemanticToolContract(
        SemanticToolId.WORKSPACE_APPLY_PATCH,
        WorkspaceApplyPatchInput,
        WorkspaceApplyPatchOutput,
        requires_mutating_task=True,
    ),
    SemanticToolContract(
        SemanticToolId.RUN_SCOPED_COMMAND, RunScopedCommandInput, RunScopedCommandOutput
    ),
    SemanticToolContract(SemanticToolId.REPORT_PROGRESS, ReportProgressInput, ReportProgressOutput),
    SemanticToolContract(SemanticToolId.ATTACH_EVIDENCE, AttachEvidenceInput, AttachEvidenceOutput),
    SemanticToolContract(SemanticToolId.ASK_OPERATOR, AskOperatorInput, AskOperatorOutput),
    SemanticToolContract(SemanticToolId.SUBMIT_PLAN, SubmitPlanInput, SubmitPlanOutput),
    SemanticToolContract(
        SemanticToolId.SUBMIT_COORDINATION_PROPOSAL,
        SubmitCoordinationProposalInput,
        SubmitCoordinationProposalOutput,
    ),
    SemanticToolContract(
        SemanticToolId.SUBMIT_CANDIDATE,
        SubmitCandidateInput,
        SubmitCandidateOutput,
        requires_mutating_task=True,
    ),
    SemanticToolContract(SemanticToolId.SUBMIT_REPORT, SubmitReportInput, SubmitReportOutput),
    SemanticToolContract(SemanticToolId.SUBMIT_EVIDENCE, SubmitEvidenceInput, SubmitEvidenceOutput),
    SemanticToolContract(SemanticToolId.BUDGET_STATUS, BudgetStatusInput, BudgetStatusOutput),
)

#: Every tool a Run may call, keyed by id. Read-only: a caller that could
#: add a row could add a tool nobody certified.
SEMANTIC_TOOL_CATALOG: Final[Mapping[SemanticToolId, SemanticToolContract]] = MappingProxyType(
    {contract.tool_id: contract for contract in _CONTRACTS}
)


def tool_contract(tool_id: SemanticToolId | str) -> SemanticToolContract:
    """Return the catalog contract of *tool_id*.

    Args:
        tool_id: A catalog tool id.

    Returns:
        The tool's contract.

    Raises:
        KeyError: *tool_id* names no catalog tool.
    """
    try:
        return SEMANTIC_TOOL_CATALOG[SemanticToolId(tool_id)]
    except ValueError:
        raise KeyError(f"{tool_id!r} names no catalog tool") from None


def tool_input_schema(tool_id: SemanticToolId | str) -> dict[str, Any]:
    """Return the JSON Schema of *tool_id*'s input the MCP server publishes.

    Args:
        tool_id: A catalog tool id.

    Returns:
        The JSON Schema of the tool's input model.

    Raises:
        KeyError: *tool_id* names no catalog tool.
    """
    return tool_contract(tool_id).input_model.model_json_schema()


# ---- envelopes ---------------------------------------------------------------

SEMANTIC_TOOL_INPUTS: Final = TypeAdapter[Any](SemanticToolInput)


class SemanticCall(RuntimeRecord):
    """One tool call a provider process sends the daemon.

    The payload digest is carried rather than derived on read so the
    gateway can compare two calls that share an idempotency key without
    trusting either to re-canonicalise the same way.
    """

    schema_version: Literal["semantic-call/v1"] = "semantic-call/v1"
    call_id: CallId
    run_ref: RunUrn
    contract_digest: Digest
    idempotency_key: IdempotencyKey
    tool_id: SemanticToolId
    tool_schema_version: SemVer
    payload_digest: Digest
    payload: SemanticToolInput
    requested_at: UtcDatetime

    @classmethod
    def seal(cls, fields: Mapping[str, Any]) -> Self:
        """Validate *fields* into a call whose payload digest is computed here.

        Args:
            fields: Every field except ``payload_digest``.

        Returns:
            The validated call.

        Raises:
            KeyError: *fields* carries no payload.
            pydantic.ValidationError: The call breaks a record rule.
        """
        payload = SEMANTIC_TOOL_INPUTS.validate_python(fields["payload"])
        digest = canonical_digest(payload.model_dump(mode="json"))
        return cls.model_validate({**fields, "payload": payload, "payload_digest": digest})

    @model_validator(mode="after")
    def _payload_is_the_one_the_envelope_names(self) -> Self:
        """Bind the payload to the tool it names and to its own digest.

        Raises:
            ValueError: The payload answers another tool, or the digest
                does not cover the payload carried beside it.
        """
        if self.payload.tool_id != self.tool_id.value:
            raise ValueError(
                f"payload of {self.payload.tool_id!r} does not match tool {self.tool_id.value!r}"
            )
        if canonical_digest(self.payload.model_dump(mode="json")) != self.payload_digest:
            raise ValueError("payload_digest does not cover the payload")
        return self


class SemanticResult(RuntimeRecord):
    """The receipt the daemon returns for one call."""

    schema_version: Literal["semantic-result/v1"] = "semantic-result/v1"
    call_id: CallId
    run_ref: RunUrn
    receipt_id: ReceiptId
    status: SemanticCallStatus
    result_schema_version: SemVer
    output_ref: ArtifactUrn | None = None
    bounded_output: SemanticToolOutput | None = None
    error: SemanticToolError | None = None
    completed_at: UtcDatetime

    @model_validator(mode="after")
    def _outcome_carries_exactly_its_own_fields(self) -> Self:
        """Require an output on success and an error on every other status.

        Raises:
            ValueError: A success carries an error or no output at all, or
                a non-success carries an output or no error.
        """
        succeeded = self.status == "succeeded"
        has_output = self.bounded_output is not None or self.output_ref is not None
        if succeeded and (self.error is not None or not has_output):
            raise ValueError("a succeeded result carries an output and no error")
        if not succeeded and (self.error is None or has_output):
            raise ValueError(f"a {self.status} result carries an error and no output")
        return self


def encode_envelope(envelope: SemanticCall | SemanticResult) -> str:
    """Return the canonical one-line JSON frame the stdio server carries.

    Args:
        envelope: A call or a result.

    Returns:
        Compact JSON with sorted keys and no newline, so two structurally
        equal envelopes encode to the same bytes.
    """
    body = envelope.model_dump(mode="json")
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def decode_call(frame: str) -> SemanticCall:
    """Return the call one stdio frame carries.

    Args:
        frame: One JSON frame.

    Returns:
        The validated call.

    Raises:
        json.JSONDecodeError: *frame* is not JSON.
        pydantic.ValidationError: The frame is not a valid call.
    """
    return SemanticCall.model_validate(json.loads(frame))


def decode_result(frame: str) -> SemanticResult:
    """Return the result one stdio frame carries.

    Args:
        frame: One JSON frame.

    Returns:
        The validated result.

    Raises:
        json.JSONDecodeError: *frame* is not JSON.
        pydantic.ValidationError: The frame is not a valid result.
    """
    return SemanticResult.model_validate(json.loads(frame))
