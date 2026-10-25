"""What an admitted semantic call is actually answered with.

The gateway decides whether a call may be answered; this module answers
it. The two are separate files because they fail differently: a missing
guard admits a call nobody checked, while a missing handler admits a call
nobody can serve, and the second must never be papered over with a
plausible-looking output. :data:`BROKERED_TOOLS` is therefore derived
from the handler table rather than declared beside it -- a tool is
brokered exactly when a function answers it, so the set cannot grow past
what is implemented.

Eight tools are answered here today.

``repo_read`` and ``repo_search`` read the Run's leased workspace, or the
repository root for a Run that holds no lease, such as a campaign round.
Every path is resolved inside that root and nothing under ``.ea`` or
``.git`` is read, for the same reason the jail denies daemon storage
reads. The text comes back inline, bounded and scrubbed the way the
content store keeps it, and is filed there too so the reference resolves.

``budget_status`` reports only what the daemon measured itself. Wall time
is the difference between two stamps it wrote and the call count is the
receipts it holds; the token, cost and output ceilings are reported
absent rather than as zero, because a budget that reads as untouched is
how one is overrun while it looks observed.

``submit_plan`` routes a proposal into the PlanRevision validator and
hands back its findings. It records nothing: the tool's contract is that
the daemon validates the artifact before any proposal exists, and
creating the revision record is an operator-authority verb
(``planning.plan_revision.submit``) rather than something a worker earns
by having its plan validate. So acceptance here means "this plan would
submit", and the findings are the refusal the submission would have
produced -- the same function decides both, so the preview cannot drift
from the act.

``submit_candidate`` records one worker's claim that a tree is ready to
be integrated. Sealing is a second, separate act gated on an accepted
terminal report and is not performed here, so the handler reports the
standing bundle when one already exists and reports none otherwise; the
claim itself is written once and replayed rather than duplicated when the
same candidate is submitted again with the same content. Before that
first write, the submission's commit is pinned under the candidate's ref
through the same check ``runtime.candidate.submit`` pins through, because
a claim this gateway wrote and a claim the JSON-RPC method wrote must be
equally resolvable once the leased branch is gone. A submission naming an
identity another claim already holds with different content, or naming a
commit the pin check will not hold, reaches no handler answer at all: it
raises :class:`HandlerRefusalError`, which the gateway lets propagate
rather than files as a receipt, because there is no output that could
report it without inventing one.

``workspace_apply_patch`` writes whole files into the Run's leased
workspace and nowhere else. The scope check has bounded the paths by the
Run's write set before this module is reached; the handler adds the one
check only the tree can answer -- a link that would carry a write out of
the workspace -- and runs it over every path before writing any.

``submit_report`` takes the report's content -- verdict, summary,
criteria outcomes and body -- and nothing the daemon already knows: the
schema and the contract digest come from the Run's capsule, and the body
is filed in the Run's content store under a digest the daemon computes.
A worker that names the schema or the contract anyway is refused when
either differs. Acceptance records the report's schema, digest and
verdict against the Run, once, so the separate, operator-driven act that binds
an accepted report to a candidate's seal decision
(:data:`eawf.runtime.daemon.methods.candidate.CANDIDATE_REPORT_BIND_METHOD`)
can read them back rather than take them typed by hand. Recording them
is not the same as sealing anything: acceptance here answers only "this
Run may file this report", never "this candidate is sealed".

``submit_evidence`` promotes a verified spike's measured contracts onto
the v1 evidence path, the same store a plan's approval resolves
``contract_refs`` against. It reads the filed :class:`SpikeReport` the
way ``submit_plan`` reads a filed proposal, so an unverified report, a
stale digest, or a contract measured in another repository is reported
as a finding rather than raised; a contract this daemon already promoted
is the one refusal that comes back from the v1 mutator underneath it
instead. The report itself reaches this ledger through
``runtime.evidence.spike_report.file`` (:func:`file_spike_report`), a
plain native mutator rather than a sixth brokered tool -- the same split
that keeps ``runtime.candidate.submit`` apart from ``submit_candidate``,
because filing is a record of what a Run produced, not a judged call.

No handler opens a session of its own. The gateway is already inside one
with the Run's locks and the document lock held, and a nested session
would block on the lock the caller is holding. What a handler needs from
the tree it reads through the session it was handed, and the Run's
active lease -- already resolved by the gateway's own lease check -- is
handed alongside it for the same reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import subprocess
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, model_validator

from eawf.kernel.runtime.candidate import AcceptedReport, CandidateSubmission, candidate_identity
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.compiled import JsonPointer, canonical_digest
from eawf.kernel.runtime.content import (
    CONTENT_CHAR_LIMIT,
    CONTENT_LINE_CHARS,
    CONTENT_LINE_LIMIT,
    bound_content,
    scrub_line,
)
from eawf.kernel.runtime.lease import WorkLease
from eawf.kernel.runtime.provider import ArtifactUrn, Digest, RuntimeRecord
from eawf.kernel.runtime.semantic import (
    REPO_READ_TEXT_CHARS,
    BudgetStatusOutput,
    BudgetUsage,
    FindingCode,
    RepoReadInput,
    RepoReadOutput,
    RepoSearchInput,
    RepoSearchMatch,
    RepoSearchOutput,
    SemanticCall,
    SemanticToolId,
    SemanticToolOutput,
    SubmitCandidateInput,
    SubmitCandidateOutput,
    SubmitEvidenceInput,
    SubmitEvidenceOutput,
    SubmitPlanInput,
    SubmitPlanOutput,
    SubmitReportInput,
    SubmitReportOutput,
    ValidationFinding,
    WorkspaceApplyPatchInput,
    WorkspaceApplyPatchOutput,
)
from eawf.kernel.spec.measured_contract import SpikeReport
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.store.ledger import LedgerRecord, LedgerTornTailError, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.logging.state_leak import default_allowed_emails
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.candidate.seal import (
    accepted_report_of,
    accepted_report_record,
    bundle_of,
    submission_of,
    submission_record,
)
from eawf.runtime.daemon.content_store import file_content
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession, canonical_entity_urn
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusedError, commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.runtime.integration.git_workspace import CandidatePinError, pin_submission_commit
from eawf.runtime.sandbox.cwd_guard import is_path_inside
from eawf.runtime.workspace.lease import workspace_path
from eawf.runtime.worktree.tree_change import snapshot_tree
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence._io import MEASURED_CONTRACT_STATUS, generation_contract_view
from eawf.workflow.evidence.measured_contract import submit_evidence as promote_spike_evidence
from eawf.workflow.planning.apply import (
    PlanRevisionProposal,
    placed_document,
    validate_plan_proposal,
)
from eawf.workflow.planning.revision import PlanRefusal, PlanRefusalCode

if TYPE_CHECKING:
    # The gateway imports this module's handler table, so its receipt is named here only.
    from eawf.runtime.daemon.semantic_gateway import SemanticCallReceipt

logger = logging.getLogger(__name__)


#: The discriminator separating a plan proposal from the other payload
#: kinds the artifact ledger may come to hold.
PLAN_PROPOSAL_PAYLOAD_KIND: Final = "plan_proposal"

#: The discriminator separating a spike report from the other payload
#: kinds the artifact ledger may come to hold.
SPIKE_REPORT_PAYLOAD_KIND: Final = "spike_report"

#: A refusal ``UserError.kind`` that does not itself fit the
#: ``FindingCode`` grammar, mapped to one that does. ``InvalidInput`` is
#: :func:`~eawf.workflow.evidence.artifact.add_artifact`'s generic
#: duplicate-id guard, reached when a contract this daemon already
#: promoted is submitted again.
_EVIDENCE_REFUSAL_CODES: Final[Mapping[str, str]] = MappingProxyType(
    {"InvalidInput": "contract_already_promoted"}
)

#: How long a finding message may be before it is trimmed to fit the
#: bounded-text field the output carries.
_MESSAGE_LIMIT: Final = 500

#: Validates one derived finding code against the closed grammar the
#: output field pins, so a refusal code that cannot be reported is a
#: startup failure rather than a validation error under load.
_FINDING_CODES: Final = TypeAdapter[Any](FindingCode)

#: Validates a derived pointer against the grammar the finding field pins.
_POINTERS: Final = TypeAdapter[Any](JsonPointer)

#: The finding code a report is refused with when it names a schema other
#: than the one this Run's capsule pins.
_REPORT_SCHEMA_MISMATCH: Final = "report_schema_mismatch"

#: The finding code a report is refused with when it names a contract
#: digest other than the one this Run was actually sealed under.
_REPORT_CONTRACT_MISMATCH: Final = "report_contract_mismatch"

#: The finding code a report is refused with when its Run already holds an
#: accepted report naming another body or verdict.
_REPORT_ALREADY_ACCEPTED: Final = "report_already_accepted"

#: The finding code a report is refused with when its rendered content is
#: larger than the content store keeps whole.
_REPORT_BODY_TOO_LARGE: Final = "report_body_too_large"


class HandlerTableError(RuntimeError):
    """A handler table is not total over the vocabulary it covers."""


class PlanProposalArtifact(RuntimeRecord):
    """One planner proposal, filed as an artifact a call may name.

    The proposal travels by reference because a plan is larger than an
    envelope should be, so the envelope names an artifact and the daemon
    reads the body from its own ledger. The body is kept as submitted
    rather than as a parsed proposal: a proposal that does not validate
    must still be storable, or the schema finding the tool is defined to
    return could never be produced.

    Attributes:
        payload_kind: The discriminator separating this row from
            anything else the artifact ledger holds.
        schema_version: Version of this record's shape.
        artifact_ref: The reference a semantic call names it by.
        content_digest: The digest of ``proposal`` in canonical form.
        proposal: The submitted create document, unparsed.
    """

    payload_kind: Literal["plan_proposal"] = PLAN_PROPOSAL_PAYLOAD_KIND
    schema_version: Literal["plan-proposal/v1"] = "plan-proposal/v1"
    artifact_ref: ArtifactUrn
    content_digest: Digest
    proposal: Mapping[str, Any]

    @model_validator(mode="after")
    def _digest_covers_the_body(self) -> Self:
        """Bind the recorded digest to the body stored beside it.

        Raises:
            ValueError: The digest does not cover the proposal, which
                would let a caller's digest match a row whose body was
                replaced.
        """
        if canonical_digest(dict(self.proposal)) != self.content_digest:
            raise ValueError("content_digest does not cover the proposal")
        return self


class SpikeReportArtifact(RuntimeRecord):
    """One verified spike's report, filed as an artifact a call may name.

    Mirrors :class:`PlanProposalArtifact`: the report travels by reference
    because a spike may carry several contracts, and the body is kept as
    submitted rather than as a parsed :class:`SpikeReport` so a report
    that fails to validate is still storable and reportable as a finding.

    Attributes:
        payload_kind: The discriminator separating this row from
            anything else the artifact ledger holds.
        schema_version: Version of this record's shape.
        artifact_ref: The reference a semantic call names it by.
        content_digest: The digest of ``report`` in canonical form.
        report: The submitted spike report, unparsed.
    """

    payload_kind: Literal["spike_report"] = SPIKE_REPORT_PAYLOAD_KIND
    schema_version: Literal["spike-report/v1"] = "spike-report/v1"
    artifact_ref: ArtifactUrn
    content_digest: Digest
    report: Mapping[str, Any]

    @model_validator(mode="after")
    def _digest_covers_the_body(self) -> Self:
        """Bind the recorded digest to the body stored beside it.

        Raises:
            ValueError: The digest does not cover the report, which would
                let a caller's digest match a row whose body was replaced.
        """
        if canonical_digest(dict(self.report)) != self.content_digest:
            raise ValueError("content_digest does not cover the report")
        return self


@dataclass(frozen=True, slots=True)
class HandlerInputs:
    """Everything a handler may read, gathered by the gateway.

    Attributes:
        session: The locked pass the gateway opened. A handler reads
            through it and never opens one of its own.
        call: The envelope that was admitted.
        capsule: The authority it runs under.
        run: The stored Run record.
        calls_so_far: How many receipts the Run already holds.
        held: The receipts the Run already holds, in ledger order.
        now: The instant the answer is stamped at.
        launched_at: When the worker answering now was launched or
            resumed, or ``None`` for a Run never dispatched natively. An
            answer filed before it went to a worker that may be gone, so a
            Run with no launch answers no read by reference.
        lease: The Run's active workspace lease, already resolved by the
            gateway's own lease check. ``None`` for a tool the lease check
            does not gate.
    """

    session: RootSession
    call: SemanticCall
    capsule: AuthorityCapsule
    run: Run
    calls_so_far: int
    held: tuple[SemanticCallReceipt, ...]
    now: datetime
    launched_at: datetime | None
    lease: WorkLease | None = None


class HandlerRefusalError(ValueError):
    """A call reached its handler and still cannot be answered as made.

    Raised instead of returned so the caller's session exits with no
    receipt appended -- the same "nothing is written" guarantee a
    pre-handler refusal has, for the one class of refusal that can only be
    known once the handler reads the tree.

    Attributes:
        code: The stable code a client branches on.
        detail: The operator-facing explanation.
    """

    def __init__(self, *, code: str, detail: str) -> None:
        """Bind the typed code to its one-sentence detail."""
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _budget_status(inputs: HandlerInputs) -> SemanticToolOutput:
    """Return what this Run has measurably spent and what remains of it."""
    ceiling = inputs.capsule.budget.wall_seconds
    started = inputs.run.started_at
    elapsed = 0 if started is None else int((inputs.now - started).total_seconds())
    return BudgetStatusOutput(
        tool_id="budget_status",
        used=BudgetUsage(wall_seconds=elapsed, tool_calls=inputs.calls_so_far),
        remaining=BudgetUsage(wall_seconds=max(0, ceiling - elapsed)),
        quality="measured",
        includes_children=False,
    )


def _submit_plan(inputs: HandlerInputs) -> SemanticToolOutput:
    """Validate the named proposal and return the verdict with its findings."""
    payload = inputs.call.payload
    assert isinstance(payload, SubmitPlanInput), "the envelope binds the payload to its tool"
    artifact = _plan_proposal_artifact(inputs.session, ref=payload.proposal_ref)
    if artifact is None:
        return _refused_plan(
            PlanRefusalCode.IDENTITY_NOT_FOUND,
            message=f"no plan proposal is filed under {payload.proposal_ref}",
            field_path="/proposal_ref",
        )
    if artifact.content_digest != payload.proposal_digest:
        return _refused_plan(
            PlanRefusalCode.PROOF_STALE,
            message="the call names a digest the filed proposal does not have",
            field_path="/proposal_digest",
        )
    try:
        proposal = PlanRevisionProposal.model_validate(dict(artifact.proposal))
    except ValidationError as error:
        return _refused_plan(
            PlanRefusalCode.SCHEMA_VALIDATION_FAILED,
            message="the filed proposal is not a plan revision this daemon can read",
            field_path=_pointer(error.errors()[0]["loc"]),
        )
    outcome = validate_plan_proposal(
        placed_document(inputs.session, inputs.session.read_document()),
        proposal=proposal,
        at=inputs.now,
    )
    if isinstance(outcome, PlanRefusal):
        logger.info(f"_submit_plan refused guard={outcome.guard}")
        return _refused_plan(outcome.code, message=outcome.detail)
    return SubmitPlanOutput(
        tool_id="submit_plan",
        accepted=True,
        proposal_ref=proposal.body.milestone_urn,
    )


def _submit_candidate(inputs: HandlerInputs) -> SemanticToolOutput:
    """Record one worker's claim, or answer the standing one it repeats.

    Raises:
        HandlerRefusalError: The lease this call names is not the one the
            Run holds, the submission names no commit or one that is not
            the leased worktree's own HEAD descending from its base, or
            the candidate identity is already held by a claim naming
            other content.
    """
    payload = inputs.call.payload
    assert isinstance(payload, SubmitCandidateInput), "the envelope binds the payload to its tool"
    lease = inputs.lease
    assert lease is not None, "the lease check admits only a run holding an active lease"
    if (
        canonical_entity_urn(lease.task_ref) != canonical_entity_urn(payload.task_ref)
        or lease.lease_id != payload.lease_id
    ):
        raise HandlerRefusalError(
            code="candidate_lease_mismatch",
            detail=(
                f"run {inputs.run.key!r} holds lease {lease.lease_id!r} for task "
                f"{lease.task_ref.entity_key}, which is not what this call names"
            ),
        )
    candidate_ref = candidate_identity(
        task_ref=str(payload.task_ref), resulting_tree_digest=payload.resulting_tree_digest
    )
    records = read_ledger_records(inputs.session.ledger_path(Epoch2Collection.RUN))
    standing = submission_of(records, candidate_ref)
    if standing is None:
        try:
            pin_submission_commit(
                inputs.session.context,
                lease=lease,
                candidate_ref=candidate_ref,
                submission_ref=payload.submission_ref,
            )
        except CandidatePinError as error:
            raise HandlerRefusalError(code=error.code.value, detail=error.detail) from error
        standing = CandidateSubmission(
            candidate_ref=candidate_ref,
            run_ref=inputs.call.run_ref,
            task_ref=payload.task_ref,
            lease_id=lease.lease_id,
            workspace_handle=lease.workspace_handle,
            workspace_generation=lease.workspace_generation,
            base_commit=lease.base_commit,
            submission_ref=payload.submission_ref,
            changed_paths=payload.changed_paths,
            resulting_tree_digest=payload.resulting_tree_digest,
            submitted_at=inputs.now,
        )
        commit_ledger_append(inputs.session, submission_record(standing))
        logger.info(
            f"_submit_candidate recorded candidate={candidate_ref} "
            f"run={inputs.run.key!r} paths={len(standing.changed_paths)}"
        )
    elif (
        standing.submission_ref != payload.submission_ref
        or standing.changed_paths != payload.changed_paths
    ):
        raise HandlerRefusalError(
            code="candidate_payload_conflict",
            detail=(
                f"candidate {candidate_ref} is already held by a claim naming other "
                "content, so replaying it would answer for a request nobody made"
            ),
        )
    bundle = bundle_of(records, candidate_ref)
    return SubmitCandidateOutput(
        tool_id="submit_candidate",
        candidate_ref=candidate_ref,
        resulting_tree_digest=payload.resulting_tree_digest,
        sealed_at=None if bundle is None else bundle.sealed_at,
    )


def _workspace_apply_patch(inputs: HandlerInputs) -> SemanticToolOutput:
    """Write the patch's files into the Run's leased workspace and name the tree that results.

    The scope check has already compared every path against the Run's
    write set on its spelling alone. What it cannot see is the tree: a
    directory on the way that is a link out of the workspace. Every path
    is therefore resolved against the workspace before any is written,
    so a refused patch writes nothing rather than its first few files.
    What the writes changed reaches the Run's stream through the edit
    record taken when the turn ends, the same as any other worker edit.

    Raises:
        HandlerRefusalError: A path resolves outside the leased workspace,
            or the filesystem refuses a write.
    """
    payload = inputs.call.payload
    assert isinstance(payload, WorkspaceApplyPatchInput), (
        "the envelope binds the payload to its tool"
    )
    lease = inputs.lease
    assert lease is not None, "the lease check admits only a run holding an active lease"
    workspace = workspace_path(inputs.session.context, handle=lease.workspace_handle)
    targets = [(item, workspace / item.path) for item in payload.files]
    for item, target in targets:
        if not is_path_inside(target, root=workspace):
            raise HandlerRefusalError(
                code="patch_path_escapes_workspace",
                detail=f"{item.path!r} resolves outside this run's leased workspace",
            )
    try:
        for item, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.content, encoding="utf-8")
        tree = snapshot_tree(workspace)
    except (OSError, cli_errors.CliError) as error:
        raise HandlerRefusalError(
            code="patch_write_failed",
            detail=f"the leased workspace refused the write: {type(error).__name__}",
        ) from error
    logger.info(
        f"_workspace_apply_patch run={inputs.run.key!r} paths={len(payload.files)} "
        f"generation={lease.workspace_generation}"
    )
    return WorkspaceApplyPatchOutput(
        tool_id="workspace_apply_patch",
        changed_paths=tuple(item.path for item in payload.files),
        resulting_tree_digest=tree.digest,
        workspace_generation=lease.workspace_generation,
    )


#: The trees no read reaches: eawf's own storage and git's.
_REFUSED_TREES: Final = frozenset({".ea", ".git"})

#: How many leading bytes decide that a file is binary.
_BINARY_PROBE_BYTES: Final = 8192

#: The most bytes of one file a search scans.
_SEARCH_FILE_BYTES: Final = 1_048_576

#: The most bytes one search scans across every file.
_SEARCH_TOTAL_BYTES: Final = 64 * 1_048_576

#: How long listing the tree may take before the search is refused.
_LIST_TIMEOUT_SECONDS: Final = 30


def _read_root(inputs: HandlerInputs, handle: str) -> Path:
    """Return the directory this Run reads: its leased workspace, else the repository root.

    Raises:
        HandlerRefusalError: The Run holds a lease and the call names
            another workspace.
    """
    lease = inputs.lease
    context = inputs.session.context
    if lease is None:
        # The native tree is the repository's ``.ea`` directory.
        return context.identity.tree_root.parent
    if lease.workspace_handle != handle:
        raise HandlerRefusalError(
            code="read_handle_mismatch",
            detail=f"run {inputs.run.key!r} holds another workspace than the one this call names",
        )
    return workspace_path(context, handle=lease.workspace_handle)


def _refused_tree(relative: Path) -> bool:
    """Return whether *relative* names a path under a tree no read reaches."""
    return any(part in _REFUSED_TREES for part in relative.parts)


def _inside(root: Path, path: str) -> Path:
    """Return *path* joined to *root*, refusing an escape or a refused tree.

    Both the spelling and the resolved target are checked, so a link
    pointing into ``.ea`` is refused as surely as the path ``.ea`` itself.

    Raises:
        HandlerRefusalError: The path resolves outside *root*, or it or
            its target lies under ``.ea`` or ``.git``.
    """
    target = root / path
    if not is_path_inside(target, root=root):
        raise HandlerRefusalError(
            code="repo_path_escapes_root",
            detail=f"{path!r} resolves outside the tree this run reads",
        )
    resolved = target.resolve().relative_to(root.resolve())
    if _refused_tree(Path(path)) or _refused_tree(resolved):
        raise HandlerRefusalError(
            code="repo_path_refused",
            detail=f"{path!r} lies under .ea or .git, which no run reads",
        )
    return target


def _repo_read(inputs: HandlerInputs) -> SemanticToolOutput:
    """Return a window of one file of the tree this Run reads.

    The window runs to ``byte_cap`` and is cut at whole lines, so
    ``byte_count`` names exactly the bytes the text covers and the next
    window resumes where this one stopped. Each line is scrubbed as the
    content store scrubs it, but the store's line and character bounds
    apply only to the content ``content_ref`` names.

    Raises:
        HandlerRefusalError: The path escapes the tree, lies under a
            refused tree, names no file, or names a binary file.
    """
    payload = inputs.call.payload
    assert isinstance(payload, RepoReadInput), "the envelope binds the payload to its tool"
    target = _inside(_read_root(inputs, payload.resource_handle), payload.path)
    if not target.is_file():
        raise HandlerRefusalError(
            code="repo_path_not_found", detail=f"{payload.path!r} names no file of the tree"
        )
    with target.open("rb") as handle:
        handle.seek(payload.start_byte)
        window = handle.read(payload.byte_cap)
        more = bool(handle.read(1))
    if b"\0" in window[:_BINARY_PROBE_BYTES]:
        raise HandlerRefusalError(
            code="repo_path_binary", detail=f"{payload.path!r} is a binary file"
        )
    lines = window.splitlines(keepends=True)
    if more and len(lines) > 1 and not lines[-1].endswith(b"\n"):
        # A window cut mid-line would split a line, or a character, across two reads.
        lines.pop()
    allowed = default_allowed_emails()
    decoded: list[str] = []
    shown: list[str] = []
    spent = consumed = 0
    cut = False
    for raw in lines:
        line = raw.decode("utf-8", errors="replace")
        scrubbed, _ = scrub_line(line, allowed_emails=allowed)
        # A withheld line can be longer than the bytes it replaces, so the
        # inline bound is checked on the shown text, not on the bytes read.
        if spent + len(scrubbed) > REPO_READ_TEXT_CHARS:
            break
        decoded.append(line)
        shown.append(scrubbed)
        spent += len(scrubbed) + 1
        consumed += len(raw)
        cut = cut or len(line.rstrip()) > CONTENT_LINE_CHARS
    source = "".join(decoded)
    text = "\n".join(shown) if source.strip() else ""
    digest = f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
    truncated = cut or more or consumed < len(window)
    if not payload.reread:
        repeat = _earlier_read(inputs, content_digest=digest, byte_count=consumed)
        if repeat is not None:
            call_id, earlier = repeat
            logger.info(f"_repo_read run={inputs.run.key!r} same_as_call={call_id}")
            return earlier.model_copy(
                update={"text": "", "same_as_call": call_id, "truncated": truncated}
            )
    ref = file_content(inputs.session, run_ref=inputs.call.run_ref, text=source, now=inputs.now)
    logger.info(f"_repo_read run={inputs.run.key!r} bytes={consumed} lines={len(shown)}")
    return RepoReadOutput(
        tool_id="repo_read",
        content_ref=ref,
        content_digest=digest,
        byte_count=consumed,
        truncated=truncated,
        text=text,
    )


def _earlier_read(
    inputs: HandlerInputs, *, content_digest: str, byte_count: int
) -> tuple[str, RepoReadOutput] | None:
    """Return the latest read of this launch that returned this same window's text.

    The earlier call must have named the same handle, path and window,
    with or without ``reread``, which its payload digest covers, and must
    have been answered since the worker was launched, which alone may hold
    the text. Only a read that carried its text is named, so a worker
    following the reference always lands on the text, never on another
    reference; the ledger keeps such a read naming its own call.

    Args:
        inputs: The current call and the Run's receipts, in ledger order.
        content_digest: The digest of the text the current read would return.
        byte_count: How many file bytes that text covers.

    Returns:
        That read's call id and output, or ``None`` when the launch holds
        none or the Run records no launch.
    """
    if inputs.launched_at is None:
        # A Run never dispatched natively, such as a Campaign round, records
        # no launch to bound the match by, and an unbounded match could name
        # text only an earlier worker saw.
        return None
    payload = inputs.call.payload
    windows = {
        canonical_digest(payload.model_copy(update={"reread": reread}).model_dump(mode="json"))
        for reread in (False, True)
    }
    for receipt in reversed(inputs.held):
        if receipt.recorded_at < inputs.launched_at:
            break
        output = receipt.result.bounded_output
        if (
            isinstance(output, RepoReadOutput)
            and output.same_as_call in (None, receipt.call_id)
            and receipt.payload_digest in windows
            and output.content_digest == content_digest
            and output.byte_count == byte_count
        ):
            return receipt.call_id, output
    return None


def _listed_files(root: Path, prefix: str | None) -> list[str]:
    """Return the tree's tracked and unignored files under *prefix*, sorted.

    The tree's own listing is used so an ignored directory such as a
    virtual environment is never scanned.

    Raises:
        HandlerRefusalError: The tree cannot be listed.
    """
    argv = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    if prefix is not None:
        argv += ["--", prefix]
    try:
        listed = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            check=True,
            timeout=_LIST_TIMEOUT_SECONDS,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise HandlerRefusalError(
            code="repo_search_failed",
            detail=f"the tree could not be listed: {type(error).__name__}",
        ) from error
    names = {name for name in listed.stdout.decode("utf-8", errors="replace").split("\0") if name}
    return sorted(name for name in names if not _refused_tree(Path(name)))


def _repo_search(inputs: HandlerInputs) -> SemanticToolOutput:
    """Return the first matching lines of the tree this Run reads.

    Binary files, files the tree ignores, links and anything under
    ``.ea`` or ``.git`` are skipped. The scan stops at the result cap, the
    content store's bounds or the byte budget, and ``truncated`` says
    that more matches may exist past what is returned.

    Raises:
        HandlerRefusalError: The prefix escapes the tree or lies under a
            refused tree, the pattern is not a valid expression, or the
            tree cannot be listed.
    """
    payload = inputs.call.payload
    assert isinstance(payload, RepoSearchInput), "the envelope binds the payload to its tool"
    root = _read_root(inputs, payload.resource_handle)
    if payload.path_prefix is not None:
        _inside(root, payload.path_prefix)
    try:
        pattern = re.compile(payload.query if payload.regex else re.escape(payload.query))
    except re.error as error:
        raise HandlerRefusalError(
            code="search_pattern_invalid", detail=f"the pattern does not compile: {error}"
        ) from error
    cap = min(payload.result_cap, CONTENT_LINE_LIMIT)
    matches: list[RepoSearchMatch] = []
    spent = scanned = 0
    truncated = False
    for name in _listed_files(root, payload.path_prefix):
        target = root / name
        if target.is_symlink() or not target.is_file():
            continue
        with target.open("rb") as handle:
            data = handle.read(_SEARCH_FILE_BYTES)
        if b"\0" in data[:_BINARY_PROBE_BYTES]:
            continue
        scanned += len(data)
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if pattern.search(line) is None:
                continue
            text = next(iter(bound_content(line).lines), "")
            if len(matches) == cap or spent + len(name) + len(text) > CONTENT_CHAR_LIMIT:
                truncated = True
                break
            matches.append(RepoSearchMatch(path=name, line=number, text=text))
            spent += len(name) + len(text)
        if truncated or scanned >= _SEARCH_TOTAL_BYTES:
            truncated = True
            break
    rendered = "\n".join(f"{item.path}:{item.line}: {item.text}" for item in matches)
    ref = file_content(inputs.session, run_ref=inputs.call.run_ref, text=rendered, now=inputs.now)
    logger.info(
        f"_repo_search run={inputs.run.key!r} matches={len(matches)} scanned={scanned} "
        f"truncated={truncated}"
    )
    return RepoSearchOutput(
        tool_id="repo_search",
        match_ref=ref,
        match_digest=f"sha256:{bound_content(rendered).digest}",
        match_count=len(matches),
        truncated=truncated,
        matches=tuple(matches),
    )


def _submit_report(inputs: HandlerInputs) -> SemanticToolOutput:
    """Accept the Run's terminal report content, binding it to the Run's own contract.

    The worker sends content only. The schema and the contract digest are
    taken from the capsule the Run was sealed under; a worker that names
    either anyway is refused when it differs, because a report filed
    against another contract is not this Run's report. The body is
    rendered with the verdict, summary and criteria, filed in the Run's
    content store, and the digest the acceptance records is the one the
    store names that content by, so the stored body and the recorded
    digest cannot disagree. A report too large to store whole is refused
    rather than stored cut short.

    Acceptance names the Run itself, which is the one entity a terminal
    report is ever about, and records the report's schema, digest and
    verdict against it once, so a request that later binds a candidate's
    seal to this Run's report -- typically ``/integrate seal``, presented
    only the Run -- reads them back instead of repeating them.

    A retry carrying the same content is accepted again without a second
    row. A resubmission with another body or verdict is refused rather
    than accepted and dropped, matching how a Run's candidate binding
    refuses a second, different report.
    """
    payload = inputs.call.payload
    assert isinstance(payload, SubmitReportInput), "the envelope binds the payload to its tool"
    capsule = inputs.capsule
    if payload.report_schema_ref is not None and (
        payload.report_schema_ref != capsule.report_schema_ref
    ):
        return _refused_report(
            _REPORT_SCHEMA_MISMATCH,
            message=(
                f"this run is sealed to report against {capsule.report_schema_ref!r}, "
                f"not {payload.report_schema_ref!r}; omit the field and eawf fills it"
            ),
            field_path="/report_schema_ref",
        )
    if payload.contract_digest is not None and payload.contract_digest != capsule.contract_digest:
        return _refused_report(
            _REPORT_CONTRACT_MISMATCH,
            message=(
                "the call names a contract digest this run was not sealed under; "
                "omit the field and eawf fills it"
            ),
            field_path="/contract_digest",
        )
    text = _report_text(payload)
    bounded = bound_content(text)
    if len(bounded.lines) < bounded.total_lines:
        return _refused_report(
            _REPORT_BODY_TOO_LARGE,
            message=(
                f"the report renders to {bounded.total_lines} lines and only "
                f"{len(bounded.lines)} fit the store; shorten the body"
            ),
            field_path="/body",
        )
    report_digest = f"sha256:{bounded.digest}"
    run_ref = str(inputs.call.run_ref)
    records = read_ledger_records(inputs.session.ledger_path(Epoch2Collection.RUN))
    standing = accepted_report_of(records, run_ref)
    if standing is None:
        file_content(inputs.session, run_ref=inputs.call.run_ref, text=text, now=inputs.now)
        commit_ledger_append(
            inputs.session,
            accepted_report_record(
                AcceptedReport(
                    run_ref=inputs.call.run_ref,
                    report_schema_ref=capsule.report_schema_ref,
                    report_digest=report_digest,
                    verdict=payload.verdict,
                    accepted_at=inputs.now,
                    contract_digest=capsule.contract_digest,
                )
            ),
        )
        logger.info(
            f"_submit_report accepted run={inputs.run.key!r} verdict={payload.verdict.value} "
            f"lines={len(bounded.lines)} withheld={bounded.withheld_lines}"
        )
    elif standing.report_digest != report_digest or standing.verdict != payload.verdict:
        # A seal reads the one standing report back, so accepting a different
        # body here without recording it would bind a report the worker has
        # already replaced.
        return _refused_report(
            _REPORT_ALREADY_ACCEPTED,
            message=(
                f"run {inputs.call.run_ref.entity_key!r} already holds an accepted report "
                f"with verdict {standing.verdict.value!r}; a Run files one terminal report"
            ),
            field_path="/verdict" if standing.verdict != payload.verdict else "/body",
        )
    return SubmitReportOutput(
        tool_id="submit_report", accepted=True, report_ref=inputs.call.run_ref
    )


def _report_text(payload: SubmitReportInput) -> str:
    """Return the text one terminal report is stored as.

    Every line is wrapped to the store's line bound first, so a long
    paragraph is kept whole across lines rather than cut at the bound.

    Args:
        payload: The report content the worker sent.

    Returns:
        The verdict, summary, each criterion's outcome and the body, one
        fact per line.
    """
    lines = [f"verdict: {payload.verdict.value}", f"summary: {payload.summary}"]
    if payload.criteria:
        lines.append("criteria:")
        for result in payload.criteria:
            note = "" if result.note is None else f" -- {result.note}"
            lines.append(f"- {result.criterion_id}: {result.outcome.value}{note}")
    if payload.body is not None:
        lines.extend(("", *payload.body.splitlines()))
    wrapped = (
        piece
        for line in lines
        for piece in (textwrap.wrap(line, width=CONTENT_LINE_CHARS, drop_whitespace=False) or [""])
    )
    return "\n".join(wrapped)


def _refused_report(code: str, *, message: str, field_path: str) -> SemanticToolOutput:
    """Return the refusal output one report finding produces."""
    return SubmitReportOutput(
        tool_id="submit_report",
        accepted=False,
        findings=(
            ValidationFinding(code=code, message=message[:_MESSAGE_LIMIT], field_path=field_path),
        ),
    )


def _evidence_finding_code(kind: str) -> str:
    """Return *kind* as a code the ``FindingCode`` grammar admits.

    Args:
        kind: A raised :class:`~eawf.surfaces.cli.errors.UserError`'s
            ``kind``.

    Returns:
        *kind* unchanged when it already fits the grammar, or its mapped
        equivalent from :data:`_EVIDENCE_REFUSAL_CODES`.
    """
    return _EVIDENCE_REFUSAL_CODES.get(kind, kind)


def _refused_evidence(*, code: str, message: str) -> SemanticToolOutput:
    """Return the refusal output one evidence-submission finding produces."""
    return SubmitEvidenceOutput(
        tool_id="submit_evidence",
        accepted=False,
        findings=(ValidationFinding(code=code, message=message[:_MESSAGE_LIMIT]),),
    )


def _promotion_write_refusal(error: TransactionRefusedError | OSError) -> SemanticToolOutput:
    """Return the refusal a failed evidence-ledger append answers with.

    A leak refusal's text can quote the leaking value itself, so neither
    the receipt nor the daemon log carries it; only its kind is reported.

    Args:
        error: What the ledger append raised.

    Returns:
        A refused evidence output naming one finding.
    """
    if isinstance(error, TransactionRefusedError):
        logger.warning(f"_submit_evidence write_refused=transaction code={error.code}")
        return _refused_evidence(
            code="state_leak_refused",
            message="the promotion would write a leak-shaped value into the evidence ledger",
        )
    logger.warning(f"_submit_evidence write_refused=os_error errno={error.errno}")
    return _refused_evidence(
        code="state_write_failed",
        message=(
            f"the evidence ledger could not be written: {error.strerror or type(error).__name__}"
        ),
    )


def _submit_evidence(inputs: HandlerInputs) -> SemanticToolOutput:
    """Promote a verified SpikeReport's contracts into the tree's selected generation.

    Reads the filed report the same way ``_submit_plan`` reads a filed
    proposal, hands each of its contracts to
    :func:`eawf.workflow.evidence.measured_contract.submit_evidence`, and
    files the promoted rows as one line of the generation's evidence
    ledger -- the store
    :func:`eawf.workflow.planning.apply.approve_plan_revision` resolves a
    plan's ``contract_refs`` against, so a contract promoted here is
    immediately citable from a plan's approval.
    """
    payload = inputs.call.payload
    assert isinstance(payload, SubmitEvidenceInput), "the envelope binds the payload to its tool"
    artifact = _spike_report_artifact(inputs.session, ref=payload.spike_report_ref)
    if artifact is None:
        return _refused_evidence(
            code="identity_not_found",
            message=f"no spike report is filed under {payload.spike_report_ref}",
        )
    if artifact.content_digest != payload.spike_report_digest:
        return _refused_evidence(
            code="proof_stale",
            message="the call names a digest the filed report does not have",
        )
    try:
        report = SpikeReport.model_validate(dict(artifact.report))
    except ValidationError:
        return _refused_evidence(
            code="schema_validation_failed",
            message="the filed report is not a spike report this daemon can read",
        )
    session = inputs.session
    try:
        state = generation_contract_view(
            session.read_document(),
            artifact_ledger=session.ledger_path(Epoch2Collection.ARTIFACT),
            evidence_ledger=session.ledger_path(Epoch2Collection.EVIDENCE),
        )
    except LedgerTornTailError, ValueError, KeyError:
        return _refused_evidence(
            code="no_promoted_contract_store",
            message="the tree's generation holds no readable promoted-contract store",
        )
    if state.project is None:
        return _refused_evidence(
            code="no_promoted_contract_store",
            message="the tree's generation holds no project to scope a promotion under",
        )
    try:
        promotions = promote_spike_evidence(state, report=report, scope_id=state.project.code)
    except UserError as error:
        code = _evidence_finding_code(error.kind or "measured_contract_rejected")
        return _refused_evidence(code=code, message=str(error))
    if promotions:
        # One line carries the whole report, so a refused or failed append
        # leaves none of its contracts promoted rather than some of them.
        record = LedgerRecord(
            collection=Epoch2Collection.EVIDENCE,
            record_key=report.report_id,
            status=MEASURED_CONTRACT_STATUS,
            recorded_at=inputs.now,
            payload={
                "spike_report_ref": payload.spike_report_ref,
                "contracts": [
                    state.artifacts[promotion.artifact_id].model_dump(mode="json")
                    for promotion in promotions
                ],
            },
        )
        try:
            commit_ledger_append(session, record)
        except (TransactionRefusedError, OSError) as error:
            return _promotion_write_refusal(error)
    logger.info(f"_submit_evidence report_id={report.report_id!r} contracts={len(promotions)}")
    return SubmitEvidenceOutput(
        tool_id="submit_evidence",
        accepted=True,
        contract_refs=tuple(promotion.urn for promotion in promotions),
    )


#: Which function answers each brokered tool. A tool reaches a handler or
#: it reaches none: the gateway refuses an admitted call whose tool is
#: absent here rather than inventing an outcome for it.
SEMANTIC_HANDLERS: Final[Mapping[SemanticToolId, Callable[[HandlerInputs], SemanticToolOutput]]] = (
    MappingProxyType(
        {
            SemanticToolId.BUDGET_STATUS: _budget_status,
            SemanticToolId.REPO_READ: _repo_read,
            SemanticToolId.REPO_SEARCH: _repo_search,
            SemanticToolId.SUBMIT_CANDIDATE: _submit_candidate,
            SemanticToolId.SUBMIT_EVIDENCE: _submit_evidence,
            SemanticToolId.SUBMIT_PLAN: _submit_plan,
            SemanticToolId.SUBMIT_REPORT: _submit_report,
            SemanticToolId.WORKSPACE_APPLY_PATCH: _workspace_apply_patch,
        }
    )
)

#: The tools this daemon answers itself, derived from the handler table
#: so the two cannot disagree. Every other catalog tool needs a handler
#: written before it can be brokered, and an admitted call for one is
#: refused with no receipt rather than answered with a fabricated
#: outcome.
BROKERED_TOOLS: Final[frozenset[SemanticToolId]] = frozenset(SEMANTIC_HANDLERS)


def _plan_proposal_artifact(session: RootSession, *, ref: str) -> PlanProposalArtifact | None:
    """Return the plan proposal filed under *ref*, or ``None``.

    Raises:
        ValidationError: A line claims to be a plan proposal and does not
            validate as one, which means the ledger is corrupt rather
            than merely unfamiliar.
    """
    records = read_ledger_records(session.ledger_path(Epoch2Collection.ARTIFACT))
    for item in records:
        if item.payload.get("payload_kind") != PLAN_PROPOSAL_PAYLOAD_KIND:
            continue
        artifact = PlanProposalArtifact.model_validate(item.payload)
        if artifact.artifact_ref == ref:
            return artifact
    return None


def _record_plan_proposal_artifact(
    session: RootSession, artifact: PlanProposalArtifact, *, now: datetime
) -> None:
    """File one plan proposal as a line of the root's artifact ledger."""
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.ARTIFACT,
            record_key=artifact.artifact_ref,
            status=PLAN_PROPOSAL_PAYLOAD_KIND,
            recorded_at=now,
            payload=artifact.model_dump(mode="json"),
        ),
    )


def _spike_report_artifact(session: RootSession, *, ref: str) -> SpikeReportArtifact | None:
    """Return the spike report filed under *ref*, or ``None``.

    Raises:
        ValidationError: A line claims to be a spike report and does not
            validate as one, which means the ledger is corrupt rather
            than merely unfamiliar.
    """
    records = read_ledger_records(session.ledger_path(Epoch2Collection.ARTIFACT))
    for item in records:
        if item.payload.get("payload_kind") != SPIKE_REPORT_PAYLOAD_KIND:
            continue
        artifact = SpikeReportArtifact.model_validate(item.payload)
        if artifact.artifact_ref == ref:
            return artifact
    return None


def _record_spike_report_artifact(
    session: RootSession, artifact: SpikeReportArtifact, *, now: datetime
) -> None:
    """File one spike report as a line of the root's artifact ledger."""
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.ARTIFACT,
            record_key=artifact.artifact_ref,
            status=SPIKE_REPORT_PAYLOAD_KIND,
            recorded_at=now,
            payload=artifact.model_dump(mode="json"),
        ),
    )


#: The verb a Run files its own verified spike report under, so a later
#: ``submit_evidence`` call has something other than a test fixture to
#: resolve. Separate from the ``semantic.call`` gateway (rather than a
#: sixth brokered tool) because filing is a record of what a Run
#: produced, not a judged tool call -- the same distinction that keeps
#: ``runtime.candidate.submit`` apart from ``submit_candidate``.
EVIDENCE_SPIKE_REPORT_FILE_METHOD: Final = "runtime.evidence.spike_report.file"


class SpikeReportFileParams(BaseModel):
    """Params of :data:`EVIDENCE_SPIKE_REPORT_FILE_METHOD`.

    Attributes:
        urn: The Run filing its own report.
        actor: Who asked.
        artifact_ref: The reference a later ``submit_evidence`` call names
            this report by.
        report: The spike report body, unparsed -- kept as submitted so an
            invalid report is still storable and reportable as a finding
            when ``submit_evidence`` reads it back, the same reason
            :class:`SpikeReportArtifact` keeps it unparsed.
    """

    model_config = ConfigDict(extra="forbid")

    urn: RunUrn
    actor: PrincipalKey
    artifact_ref: ArtifactUrn
    report: Mapping[str, Any]


def file_spike_report(
    context: Epoch2RootContext, args: SpikeReportFileParams, *, now: datetime
) -> SpikeReportArtifact:
    """File one Run's spike report so a later ``submit_evidence`` call can resolve it.

    Written once and replayed rather than duplicated when the same ref is
    filed again with the same content, mirroring how a candidate
    submission's identity replay works -- a retry after a dropped
    response is a no-op rather than a conflict.

    Args:
        context: The native context of the canary the Run lives in.
        args: The validated filing request.
        now: The stamp the artifact is recorded at.

    Returns:
        The standing artifact, whether this request filed it or an
        earlier one did.

    Raises:
        DaemonValidationError: *artifact_ref* is already filed under
            different content, which would let a caller quietly repoint a
            reference ``submit_evidence`` has already resolved.
    """
    content_digest = canonical_digest(dict(args.report))
    with context.session([args.urn]) as session:
        standing = _spike_report_artifact(session, ref=args.artifact_ref)
        if standing is not None:
            if standing.content_digest != content_digest:
                raise DaemonValidationError(
                    f"validation_failed: spike_report_payload_conflict: {args.artifact_ref} "
                    "is already filed with different content"
                )
            return standing
        artifact = SpikeReportArtifact(
            artifact_ref=args.artifact_ref, content_digest=content_digest, report=args.report
        )
        _record_spike_report_artifact(session, artifact, now=now)
    logger.info(
        f"file_spike_report recorded artifact_ref={args.artifact_ref!r} run={args.urn.entity_key!r}"
    )
    return artifact


@native_mutator(EVIDENCE_SPIKE_REPORT_FILE_METHOD)
async def _file_spike_report(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record one Run's spike report as an artifact ``submit_evidence`` may name."""
    try:
        args = SpikeReportFileParams.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error
    context = ctx.native_root_context(authority.root)
    artifact = await asyncio.to_thread(file_spike_report, context, args, now=datetime.now(UTC))
    return artifact.model_dump(mode="json")


def _refused_plan(
    code: PlanRefusalCode, *, message: str, field_path: str | None = None
) -> SemanticToolOutput:
    """Return the refusal output one plan finding produces."""
    return SubmitPlanOutput(
        tool_id="submit_plan",
        accepted=False,
        findings=(
            ValidationFinding(
                code=code.value, message=message[:_MESSAGE_LIMIT], field_path=field_path
            ),
        ),
    )


def _pointer(loc: Sequence[str | int]) -> str | None:
    """Return the JSON pointer *loc* spells, or ``None`` when it spells none.

    A pydantic location can name a union member or a model class, which
    the pointer grammar does not admit. Reporting no path is better than
    reporting one no reader can resolve, so a location that does not fit
    is dropped rather than mangled.
    """
    pointer = "".join(f"/{part}" for part in loc)
    try:
        return str(_POINTERS.validate_python(pointer))
    except ValidationError:
        return None


def _compile_finding_codes() -> frozenset[PlanRefusalCode]:
    """Return the plan refusal codes once each is reportable as a finding.

    Returns:
        Every declared refusal code.

    Raises:
        HandlerTableError: A refusal code does not satisfy the finding
            grammar, so the one path that reports it would fail at the
            moment a plan was refused.
    """
    for code in PlanRefusalCode:
        try:
            _FINDING_CODES.validate_python(code.value)
        except ValidationError as error:
            raise HandlerTableError(
                f"plan refusal code {code.value!r} is not a reportable finding code"
            ) from error
    return frozenset(PlanRefusalCode)


REPORTABLE_PLAN_CODES: Final[frozenset[PlanRefusalCode]] = _compile_finding_codes()


__all__ = [
    "BROKERED_TOOLS",
    "EVIDENCE_SPIKE_REPORT_FILE_METHOD",
    "PLAN_PROPOSAL_PAYLOAD_KIND",
    "REPORTABLE_PLAN_CODES",
    "SEMANTIC_HANDLERS",
    "SPIKE_REPORT_PAYLOAD_KIND",
    "HandlerInputs",
    "HandlerRefusalError",
    "HandlerTableError",
    "PlanProposalArtifact",
    "SpikeReportArtifact",
    "SpikeReportFileParams",
    "file_spike_report",
]
