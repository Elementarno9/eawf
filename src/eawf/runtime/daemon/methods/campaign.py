"""The native Campaign writers and the Campaign read models the console opens.

Four verbs write a Campaign, each through the native transaction so it lands
with one WAL intent, one sequence and one firehose row, and a refusal writes
nothing:

- ``runtime.campaign.plan.approve`` admits a Campaign under its Track with its
  approved plan, every step pending. Approving the same plan again under the
  same Track finds the Campaign already admitted and writes nothing.
- ``runtime.campaign.step.update`` is the scheduler's and the accountant's
  verb: it starts a step on a Run, finishes it with its outcome, returns it to
  pending when its Run ended without one, clears a resolved contradiction, and
  records spend and progress. A step may start only once nothing blocks it.
- ``runtime.campaign.artifact.record`` is the round-checkpoint artifact
  writer. It reads the file a Run wrote in the repository, records it as an
  immutable revision whose size and digest the daemon measured, keeps its text
  beside the record, and lists the revision on the Campaign and on the step
  that wrote it. Recording unchanged content again writes no new revision.
- ``runtime.campaign.finding.promote`` promotes what a Campaign learned into a
  held ``CFN-####`` finding. Promoting the same statement again finds it.

``projection.campaign.view`` and ``projection.campaign.artifact`` read the
Campaign back as :class:`~eawf.kernel.projection.campaign.CampaignView` and one
:class:`~eawf.kernel.projection.campaign.ArtifactCardView`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.delivery.receipts import canonical_digest
from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.projection.campaign import (
    ArtifactCardView,
    CampaignView,
    build_campaign_view,
    promoted_findings,
    stored_revisions,
)
from eawf.kernel.state.epoch2.artifact_revision import (
    ArtifactRevision,
    FileName,
    MediaKind,
    RevisionWriter,
    StoredArtifactRevision,
)
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import Epoch2Model, NonEmptyStr, PrincipalKey
from eawf.kernel.state.epoch2.campaign import (
    BudgetAxisKind,
    Campaign,
    CampaignPlanStep,
    NamedProgress,
    ResearchBudget,
    StepOutcome,
    StepState,
    StepTitle,
    revision_ref,
    step_blockers,
)
from eawf.kernel.state.epoch2.finding import CampaignFinding, FindingStatement
from eawf.kernel.state.epoch2.urns import CampaignUrn, ClaimUrn, EvidenceUrn, RunUrn, TrackUrn
from eawf.kernel.state.epoch2.values import EntityOrigin
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import (
    LedgerRecord,
    effective_records,
    read_ledger_records,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusalCode,
    TransactionRefusedError,
    commit_ledger_append,
    commit_row_write,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.delivery_approval import publish_commits
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.daemon.native_guard import native_mutator, native_params, require_native_call

logger = logging.getLogger(__name__)

CAMPAIGN_PLAN_APPROVE_METHOD: Final = "runtime.campaign.plan.approve"
CAMPAIGN_STEP_UPDATE_METHOD: Final = "runtime.campaign.step.update"
CAMPAIGN_ARTIFACT_RECORD_METHOD: Final = "runtime.campaign.artifact.record"
CAMPAIGN_FINDING_PROMOTE_METHOD: Final = "runtime.campaign.finding.promote"
CAMPAIGN_VIEW_METHOD: Final = "projection.campaign.view"
CAMPAIGN_ARTIFACT_METHOD: Final = "projection.campaign.artifact"

#: The largest drawable artifact whose text is kept beside its revision. A report
#: past it is refused rather than truncated, since a card may not draw part of one.
MAX_TEXT_BYTES: Final = 262_144

_NATIVE_ORIGIN: Final = EntityOrigin(kind="native", mapping_basis="native", confidence="exact")


class _Params(BaseModel):
    """A closed request; an unknown field is refused."""

    model_config = ConfigDict(extra="forbid")


class PlanApproveParams(_Params):
    """Params of :data:`CAMPAIGN_PLAN_APPROVE_METHOD`.

    Attributes:
        actor: Who approved the plan.
        track_ref: The Track that owns the Campaign; a live document row.
        title: What the Campaign researches.
        evidence_budget: The Campaign's axis pairs.
        plan_steps: The approved steps, in order, none of them started.
    """

    actor: PrincipalKey
    track_ref: TrackUrn
    title: StepTitle
    evidence_budget: ResearchBudget
    plan_steps: Annotated[tuple[CampaignPlanStep, ...], Field(min_length=1)]


class StepUpdateParams(_Params):
    """Params of :data:`CAMPAIGN_STEP_UPDATE_METHOD`.

    Attributes:
        actor: The scheduler or accountant making the change.
        urn: The Campaign.
        expected_revision: The Campaign row revision the change was decided against.
        ordinal: The step.
        to_state: The state to move the step to; ``None`` leaves it.
        run_ref: The Run that starts the step; required to start it.
        outcome: What the step showed; required to finish it.
        evidence_refs: Evidence the step produced, appended to what it produced.
        cleared_contradiction_refs: Contradictions resolved since the step was planned.
        spent: The observed spend per axis, never lower than what is recorded.
        progress: The runner's named progress.
    """

    actor: PrincipalKey
    urn: CampaignUrn
    expected_revision: Annotated[int, Field(gt=0, strict=True)]
    ordinal: Annotated[int, Field(gt=0, strict=True)]
    to_state: StepState | None = None
    run_ref: RunUrn | None = None
    outcome: StepOutcome | None = None
    evidence_refs: tuple[EvidenceUrn, ...] = ()
    cleared_contradiction_refs: tuple[ClaimUrn, ...] = ()
    spent: dict[BudgetAxisKind, Annotated[int, Field(ge=0, strict=True)]] = Field(
        default_factory=dict
    )
    progress: NamedProgress | None = None


class ArtifactRecordParams(_Params):
    """Params of :data:`CAMPAIGN_ARTIFACT_RECORD_METHOD`.

    Attributes:
        actor: Who records it.
        urn: The Campaign that keeps it.
        file_name: The repository-relative file the Run wrote.
        media_kind: How its content is drawn.
        run_ref: The Run that wrote it.
        step_ordinal: The step that wrote it, when a step did.
    """

    actor: PrincipalKey
    urn: CampaignUrn
    file_name: FileName
    media_kind: MediaKind
    run_ref: RunUrn
    step_ordinal: Annotated[int, Field(gt=0, strict=True)] | None = None


class FindingPromoteParams(_Params):
    """Params of :data:`CAMPAIGN_FINDING_PROMOTE_METHOD`.

    Attributes:
        actor: Who promotes it.
        urn: The Campaign that learned it.
        statement: What was learned, in one line.
    """

    actor: PrincipalKey
    urn: CampaignUrn
    statement: FindingStatement


class CampaignReadParams(_Params):
    """Params of :data:`CAMPAIGN_VIEW_METHOD`."""

    repo_root: str | None = None
    campaign_key: Annotated[str, Field(pattern=r"^CAM-\d{4,}$")]


class ArtifactReadParams(_Params):
    """Params of :data:`CAMPAIGN_ARTIFACT_METHOD`."""

    repo_root: str | None = None
    campaign_key: Annotated[str, Field(pattern=r"^CAM-\d{4,}$")]
    artifact_ref: NonEmptyStr


class CampaignCommit(Epoch2Model):
    """What a Campaign writer answers.

    Attributes:
        record: The record the verb wrote or found standing.
        committed: Whether this call wrote it.
    """

    record: dict[str, Any]
    committed: bool


# ---------- shared ----------


def _refused(code: TransactionRefusalCode, detail: str, *, urn: object, fix: str) -> NoReturn:
    """Refuse the mutation with nothing written."""
    raise TransactionRefusedError(code=code, detail=detail, entity_ref=str(urn), remediation=fix)


def _next_key(prefix: str, keys: Iterable[str]) -> str:
    """Return the next ``<prefix>-####`` key after every one in *keys*."""
    taken = [int(key.split("-", 1)[1]) for key in keys if key.startswith(f"{prefix}-")]
    return f"{prefix}-{max(taken, default=0) + 1:04d}"


def _sibling(anchor: QualifiedUrn, kind: EntityKind, key: str) -> QualifiedUrn:
    """Return the address of *key* of *kind* in *anchor*'s repository."""
    return QualifiedUrn(
        workspace_key=anchor.workspace_key,
        project_key=anchor.project_key,
        repository_key=anchor.repository_key,
        kind=kind,
        entity_key=key,
    )


def _campaign(document: dict[str, Any], urn: QualifiedUrn) -> Campaign:
    """Return the Campaign *urn* names.

    Raises:
        TransactionRefusedError: The document holds no such Campaign.
    """
    row = document_rows(document, Epoch2Collection.CAMPAIGN).get(urn.entity_key)
    if row is None:
        _refused(
            TransactionRefusalCode.IDENTITY_NOT_FOUND,
            f"the document holds no campaign keyed {urn.entity_key!r}",
            urn=urn,
            fix="Approve the Campaign's plan before writing under it.",
        )
    return Campaign.model_validate(row)


def _write_campaign(
    session: RootSession,
    campaign: Campaign,
    *,
    event_name: str,
    actor: str,
    now: datetime,
) -> Envelope:
    """Commit *campaign* as its document row, patching every route that renders it."""
    return commit_row_write(
        session,
        collection=Epoch2Collection.CAMPAIGN,
        record_key=campaign.key,
        row=campaign.model_dump(mode="json"),
        event_name=event_name,
        event_fields={
            "entity_ref": str(campaign.urn),
            "to_status": campaign.status.value,
            "revision_after": campaign.revision,
            "actor": actor,
        },
        compaction=None,
        now=now,
    )


def _bumped(campaign: Campaign, now: datetime, **changes: Any) -> Campaign:
    """Return *campaign* with *changes*, one revision later, revalidated."""
    return Campaign.model_validate(
        {
            **campaign.model_dump(mode="json"),
            **changes,
            "revision": campaign.revision + 1,
            "updated_at": now.isoformat(),
        }
    )


def _ledger(session: RootSession, collection: Epoch2Collection) -> tuple[LedgerRecord, ...]:
    return effective_records(read_ledger_records(session.ledger_path(collection)))


# ---------- plan approval ----------


def approve_plan(
    context: Epoch2RootContext, args: PlanApproveParams, *, now: datetime
) -> tuple[CampaignCommit, tuple[Envelope, ...]]:
    """Admit a Campaign with its approved plan.

    Args:
        context: The native context of the tree.
        args: The validated request.
        now: When the plan was approved.

    Returns:
        The Campaign, and the row to publish. A Campaign already admitted under
        the Track with the same title and plan is returned with nothing written.

    Raises:
        TransactionRefusedError: The Track is not a live row, a step arrives
            already started, or the plan does not hold against the budget.
    """
    started = [step.ordinal for step in args.plan_steps if step != _fresh(step)]
    if started:
        _refused(
            TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            f"an approved plan starts every step pending with nothing run; steps {started} are not",
            urn=args.track_ref,
            fix="Approve the plan before any step runs.",
        )
    digest = canonical_digest([step.model_dump(mode="json") for step in args.plan_steps])
    with context.session([str(args.track_ref)]) as session:
        document = session.read_document()
        if args.track_ref.entity_key not in document_rows(document, Epoch2Collection.TRACK):
            _refused(
                TransactionRefusalCode.IDENTITY_NOT_FOUND,
                f"the document holds no live track keyed {args.track_ref.entity_key!r}",
                urn=args.track_ref,
                fix="Create the Track before approving a Campaign under it.",
            )
        rows = document_rows(document, Epoch2Collection.CAMPAIGN)
        for row in rows.values():
            standing = Campaign.model_validate(row)
            if (standing.track_ref, standing.title, standing.approved_plan_digest) == (
                args.track_ref,
                args.title,
                digest,
            ):
                logger.info(f"approve_plan standing campaign={standing.key}")
                return CampaignCommit(record=standing.model_dump(mode="json"), committed=False), ()
        key = _next_key("CAM", rows)
        urn = _sibling(args.track_ref, EntityKind.CAMPAIGN, key)
        try:
            campaign = Campaign.model_validate(
                {
                    "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, str(urn))),
                    "key": key,
                    "urn": str(urn),
                    "origin": _NATIVE_ORIGIN.model_dump(mode="json"),
                    "revision": 1,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "track_ref": str(args.track_ref),
                    "title": args.title,
                    "evidence_budget": args.evidence_budget.model_dump(mode="json"),
                    "approved_plan_digest": digest,
                    "plan_steps": [step.model_dump(mode="json") for step in args.plan_steps],
                }
            )
        except ValidationError as error:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
                detail=f"the plan does not hold: {error.errors()[0]['msg']}",
                entity_ref=str(urn),
                remediation="Correct the plan's ordinals, dependencies or bounds.",
            ) from error
        envelope = _write_campaign(
            session, campaign, event_name="campaign.plan.approved", actor=args.actor, now=now
        )
    logger.info(f"approve_plan campaign={key} steps={len(campaign.plan_steps)}")
    return CampaignCommit(record=campaign.model_dump(mode="json"), committed=True), (envelope,)


def _fresh(step: CampaignPlanStep) -> CampaignPlanStep:
    """Return *step* as a plan approval admits it: pending, nothing run or produced."""
    return CampaignPlanStep.model_validate(
        {
            **step.model_dump(
                mode="json",
                exclude={"state", "run_refs", "started_at", "ended_at", "outcome", "produced"},
            ),
            "progress": None,
            "revision": 1,
        }
    )


# ---------- step updates ----------


def _moved_step(
    step: CampaignPlanStep,
    args: StepUpdateParams,
    plan: tuple[CampaignPlanStep, ...],
    now: datetime,
) -> dict[str, Any]:
    """Return *step*'s fields after *args*, before validation.

    Raises:
        TransactionRefusedError: The move is not one a step makes, the step is
            blocked, or a spend would go down.
    """
    fields = step.model_dump(mode="json")
    cleared = {str(ref) for ref in args.cleared_contradiction_refs}
    fields["blocking_contradiction_refs"] = [
        ref for ref in fields["blocking_contradiction_refs"] if ref not in cleared
    ]
    target, current = args.to_state, step.state
    if target is not None:
        legal = {
            (StepState.PENDING, StepState.RUNNING),
            (StepState.RUNNING, StepState.DONE),
            (StepState.RUNNING, StepState.PENDING),
        }
        if (current, target) not in legal:
            _refused(
                TransactionRefusalCode.ILLEGAL_TRANSITION,
                f"step {step.ordinal} cannot move {current.value} -> {target.value}",
                urn=args.urn,
                fix="Start a pending step, then finish it or return it to pending.",
            )
        if target is StepState.RUNNING:
            probe = CampaignPlanStep.model_validate(fields)
            blockers = step_blockers(probe, plan)
            if blockers or args.run_ref is None:
                _refused(
                    TransactionRefusalCode.TRANSITION_GUARD_FAILED,
                    f"step {step.ordinal} cannot start: "
                    + (f"it waits on {', '.join(blockers)}" if blockers else "no Run is named"),
                    urn=args.urn,
                    fix="Start it on a Run once nothing blocks it.",
                )
            fields["run_refs"] = [*fields["run_refs"], str(args.run_ref)]
            fields["started_at"] = fields["started_at"] or now.isoformat()
        if target is StepState.DONE:
            fields["outcome"] = args.outcome
            fields["ended_at"] = now.isoformat()
        fields["state"] = target.value
    fields["produced"] = [*fields["produced"], *(str(ref) for ref in args.evidence_refs)]
    for axis in fields["bound"]["axes"]:
        observed = args.spent.get(axis["axis_kind"])
        if observed is None:
            continue
        if observed < axis["spent"]:
            _refused(
                TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
                f"step {step.ordinal} spent {axis['spent']} {axis['unit']} of "
                f"{axis['axis_kind']}; a spend never goes down to {observed}",
                urn=args.urn,
                fix="Report the spend observed so far, never less.",
            )
        axis["spent"] = observed
    if args.progress is not None:
        fields["progress"] = args.progress.model_dump(mode="json")
    fields["revision"] = step.revision + 1
    return fields


def update_step(
    context: Epoch2RootContext, args: StepUpdateParams, *, now: datetime
) -> tuple[CampaignCommit, tuple[Envelope, ...]]:
    """Move, charge or clear one plan step.

    Args:
        context: The native context of the tree.
        args: The validated request.
        now: When the change happened.

    Returns:
        The Campaign after the change, and the row to publish.

    Raises:
        TransactionRefusedError: No such Campaign or step, the Campaign moved
            since *expected_revision*, or the change is refused.
    """
    with context.session([str(args.urn)]) as session:
        campaign = _campaign(session.read_document(), args.urn)
        if campaign.revision != args.expected_revision:
            _refused(
                TransactionRefusalCode.REVISION_CONFLICT,
                f"{campaign.key} is at revision {campaign.revision}, not {args.expected_revision}",
                urn=args.urn,
                fix="Re-read the Campaign and decide again.",
            )
        step = campaign.step(args.ordinal)
        if step is None:
            _refused(
                TransactionRefusalCode.IDENTITY_NOT_FOUND,
                f"{campaign.key} has no step {args.ordinal}",
                urn=args.urn,
                fix="Name a step of the approved plan.",
            )
        moved = _moved_step(step, args, campaign.plan_steps, now)
        spend = {axis.axis_kind: axis.spent for axis in step.bound.axes}
        budget = campaign.evidence_budget.model_dump(mode="json")
        for axis in budget["axes"]:
            for after in moved["bound"]["axes"]:
                if after["axis_kind"] == axis["axis_kind"]:
                    axis["spent"] += after["spent"] - spend[after["axis_kind"]]
        steps = [
            moved if other.ordinal == step.ordinal else other.model_dump(mode="json")
            for other in campaign.plan_steps
        ]
        try:
            updated = _bumped(campaign, now, plan_steps=steps, evidence_budget=budget)
        except ValidationError as error:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
                detail=f"step {args.ordinal} does not hold: {error.errors()[0]['msg']}",
                entity_ref=str(args.urn),
                remediation="Finish a step with its outcome, and start it on a Run.",
            ) from error
        envelope = _write_campaign(
            session, updated, event_name="campaign.step.updated", actor=args.actor, now=now
        )
    logger.info(f"update_step campaign={campaign.key} step={args.ordinal} to={args.to_state}")
    return CampaignCommit(record=updated.model_dump(mode="json"), committed=True), (envelope,)


# ---------- artifact revisions ----------


def _read_artifact(repo: Path, args: ArtifactRecordParams) -> bytes:
    """Return the bytes of the file the Run wrote.

    Raises:
        TransactionRefusedError: The file is missing, resolves outside the
            repository, or is drawable text that is not UTF-8 or too large to keep.
    """
    path = (repo / args.file_name).resolve()
    if not path.is_relative_to(repo.resolve()) or not path.is_file():
        _refused(
            TransactionRefusalCode.IDENTITY_NOT_FOUND,
            f"no file {args.file_name!r} is in the repository",
            urn=args.urn,
            fix="Record a file the Run wrote inside the repository.",
        )
    content = path.read_bytes()
    if args.media_kind is not MediaKind.BINARY:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            _refused(
                TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
                f"{args.file_name} is not UTF-8 text; record it as binary",
                urn=args.urn,
                fix="Record a non-text artifact with the binary media kind.",
            )
        if len(content) > MAX_TEXT_BYTES:
            _refused(
                TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
                f"{args.file_name} is {len(content)} bytes; a drawable artifact keeps at most "
                f"{MAX_TEXT_BYTES}",
                urn=args.urn,
                fix="Split the report, or record it as binary so it opens externally.",
            )
    return content


def _artifact_urn(campaign: Campaign, key: str) -> str:
    project = campaign.urn.project_key.removeprefix("PRJ-")
    return f"urn:eawf:v1:artifact:{project}/{key}"


def record_artifact(
    context: Epoch2RootContext, args: ArtifactRecordParams, *, now: datetime
) -> tuple[CampaignCommit, tuple[Envelope, ...]]:
    """Record the file a Run wrote as a revision the Campaign keeps.

    A file the Campaign already keeps under the same name takes the next
    revision of the same artifact; unchanged content writes no revision. The
    revision is listed on the Campaign and, when a step wrote it, on that step,
    each only once, so a retry after a partial write completes it.

    Args:
        context: The native context of the tree.
        args: The validated request.
        now: When it was recorded.

    Returns:
        The stored revision, and the rows to publish.

    Raises:
        TransactionRefusedError: No such Campaign or step, a Run the step never
            ran, or a file that cannot be kept.
    """
    content = _read_artifact(context.identity.tree_root.parent, args)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    envelopes: list[Envelope] = []
    with context.session([str(args.urn)]) as session:
        campaign = _campaign(session.read_document(), args.urn)
        step = None if args.step_ordinal is None else campaign.step(args.step_ordinal)
        if args.step_ordinal is not None and (step is None or args.run_ref not in step.run_refs):
            _refused(
                TransactionRefusalCode.TRANSITION_GUARD_FAILED,
                f"step {args.step_ordinal} of {campaign.key} was never run by "
                f"{args.run_ref.entity_key}",
                urn=args.urn,
                fix="Record an artifact against the step whose Run wrote it.",
            )
        lines = _ledger(session, Epoch2Collection.ARTIFACT)
        revisions = stored_revisions(line.payload for line in lines)
        mine = [
            stored.revision
            for stored in revisions.values()
            if stored.revision.kept_with == campaign.urn
            and stored.revision.file_name == args.file_name
        ]
        latest = max(mine, key=lambda revision: revision.revision, default=None)
        if latest is not None and latest.digest == digest:
            stored = revisions[revision_ref(latest.artifact_ref, latest.revision)]
        else:
            key = (
                latest.artifact_ref.rsplit("/", 1)[1]
                if latest is not None
                else _next_key("ART", {line.record_key.split("#")[0] for line in lines})
            )
            number = 1 if latest is None else latest.revision + 1
            revision = ArtifactRevision(
                artifact_ref=_artifact_urn(campaign, key),
                revision=number,
                content_ref=(
                    f"repo:{args.file_name}"
                    if args.media_kind is MediaKind.BINARY
                    else f"ledger:artifact/{key}/r{number}"
                ),
                file_name=args.file_name,
                media_kind=args.media_kind,
                size_bytes=len(content),
                written_at=now,
                written_by=RevisionWriter(run_ref=args.run_ref, step_ordinal=args.step_ordinal),
                digest=digest,
                kept_with=campaign.urn,
            )
            text = None if args.media_kind is MediaKind.BINARY else content.decode("utf-8")
            stored = StoredArtifactRevision(revision=revision, text=text)
            envelopes.append(
                commit_ledger_append(
                    session,
                    LedgerRecord(
                        collection=Epoch2Collection.ARTIFACT,
                        record_key=f"{key}#r{number}",
                        status="recorded",
                        recorded_at=now,
                        payload=stored.model_dump(mode="json"),
                    ),
                    patch_fields={
                        "entity_ref": str(campaign.urn),
                        "to_status": campaign.status.value,
                        "revision_after": campaign.revision,
                    },
                )
            )
        ref = revision_ref(stored.revision.artifact_ref, stored.revision.revision)
        listed = ref in campaign.artifact_revision_refs
        produced = step is None or ref in step.produced
        if not (listed and produced):
            steps = [other.model_dump(mode="json") for other in campaign.plan_steps]
            if step is not None and not produced:
                steps[step.ordinal - 1]["produced"].append(ref)
            refs = campaign.artifact_revision_refs + (() if listed else (ref,))
            updated = _bumped(campaign, now, plan_steps=steps, artifact_revision_refs=refs)
            envelopes.append(
                _write_campaign(
                    session, updated, event_name="campaign.artifact.kept", actor=args.actor, now=now
                )
            )
    logger.info(f"record_artifact campaign={campaign.key} ref={ref} written={len(envelopes)}")
    return (
        CampaignCommit(record=stored.model_dump(mode="json"), committed=bool(envelopes)),
        tuple(envelopes),
    )


# ---------- promoted findings ----------


def promote_finding(
    context: Epoch2RootContext, args: FindingPromoteParams, *, now: datetime
) -> tuple[CampaignCommit, tuple[Envelope, ...]]:
    """Promote what a Campaign learned into a held ``CFN-####`` finding.

    Args:
        context: The native context of the tree.
        args: The validated request.
        now: When it was promoted.

    Returns:
        The finding, and the row to publish. The same statement promoted again
        under the same Campaign is returned with nothing written.

    Raises:
        TransactionRefusedError: No such Campaign, or the statement carries a
            leak shape.
    """
    with context.session([str(args.urn)]) as session:
        campaign = _campaign(session.read_document(), args.urn)
        lines = _ledger(session, Epoch2Collection.CAMPAIGN_FINDING)
        held = promoted_findings(line.payload for line in lines)
        standing = next(
            (f for f in held if f.campaign_ref == campaign.urn and f.statement == args.statement),
            None,
        )
        if standing is not None:
            logger.info(f"promote_finding standing finding={standing.key}")
            return CampaignCommit(record=standing.model_dump(mode="json"), committed=False), ()
        key = _next_key("CFN", {line.record_key for line in lines})
        urn = _sibling(campaign.urn, EntityKind.CAMPAIGN_FINDING, key)
        finding = CampaignFinding(
            uid=uuid.uuid5(uuid.NAMESPACE_URL, str(urn)),
            key=key,
            urn=urn,
            origin=_NATIVE_ORIGIN,
            revision=1,
            created_at=now,
            updated_at=now,
            campaign_ref=campaign.urn,
            statement=args.statement,
        )
        envelope = commit_ledger_append(
            session,
            LedgerRecord(
                collection=Epoch2Collection.CAMPAIGN_FINDING,
                record_key=key,
                status=finding.disposition.value,
                recorded_at=now,
                payload=finding.model_dump(mode="json"),
            ),
            patch_fields={
                "entity_ref": str(urn),
                "to_status": finding.disposition.value,
                "revision_after": finding.revision,
            },
        )
    logger.info(f"promote_finding campaign={campaign.key} finding={key}")
    return CampaignCommit(record=finding.model_dump(mode="json"), committed=True), (envelope,)


# ---------- reads ----------


def campaign_view(authority: RootAuthority, key: str) -> CampaignView:
    """Return one Campaign's read model, its artifacts and findings resolved.

    Raises:
        DaemonValidationError: The tree holds no such Campaign.
        ValueError: The Campaign lists an artifact revision no record holds.
    """
    document_file = document_path(authority)
    row = document_rows(read_document(document_file), Epoch2Collection.CAMPAIGN).get(key)
    if row is None:
        raise DaemonValidationError(
            f"validation_failed: identity_not_found: the tree holds no campaign keyed {key!r}"
        )

    def payloads(collection: Epoch2Collection) -> tuple[Mapping[str, Any], ...]:
        path = ledger_path(document_file, collection)
        return tuple(line.payload for line in effective_records(read_ledger_records(path)))

    return build_campaign_view(
        row,
        revisions=stored_revisions(payloads(Epoch2Collection.ARTIFACT)),
        findings=promoted_findings(payloads(Epoch2Collection.CAMPAIGN_FINDING)),
    )


def _read_params[T: BaseModel](model: type[T], params: dict[str, Any], method: str) -> T:
    try:
        return model.model_validate(params)
    except ValidationError as error:
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: {error.error_count()} bad "
            f"parameter(s) for {method}"
        ) from error


@register(CAMPAIGN_VIEW_METHOD)
async def read_campaign(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return one Campaign's :class:`CampaignView`. Nothing is written."""
    args = _read_params(CampaignReadParams, params, CAMPAIGN_VIEW_METHOD)
    authority = require_native_call(ctx, params)
    view = await asyncio.to_thread(campaign_view, authority, args.campaign_key)
    return view.model_dump(mode="json")


@register(CAMPAIGN_ARTIFACT_METHOD)
async def read_artifact(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return one artifact revision's :class:`ArtifactCardView`. Nothing is written.

    Raises:
        DaemonValidationError: The Campaign keeps no such revision.
    """
    args = _read_params(ArtifactReadParams, params, CAMPAIGN_ARTIFACT_METHOD)
    authority = require_native_call(ctx, params)
    view = await asyncio.to_thread(campaign_view, authority, args.campaign_key)
    card: ArtifactCardView | None = view.artifact(args.artifact_ref)
    if card is None:
        raise DaemonValidationError(
            f"validation_failed: identity_not_found: {args.campaign_key} keeps no revision "
            f"{args.artifact_ref!r}"
        )
    return card.model_dump(mode="json")


# ---------- writers on the wire ----------


#: A Campaign writer: the tree's context, validated params and the stamp in; the
#: answer and the rows to publish out.
_Write = Callable[..., tuple[CampaignCommit, tuple[Envelope, ...]]]


def _writer(method: str, model: type[BaseModel], write: _Write) -> None:
    """Register *write* as the native mutator *method*, its params validated by *model*."""

    async def handler(
        ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
    ) -> dict[str, Any]:
        args = native_params(model, params)
        context = ctx.native_root_context(authority.root)
        commit, envelopes = await asyncio.to_thread(write, context, args, now=datetime.now(UTC))
        publish_commits(ctx, envelopes)
        return commit.model_dump(mode="json")

    handler.__name__ = write.__name__
    native_mutator(method)(handler)


_writer(CAMPAIGN_PLAN_APPROVE_METHOD, PlanApproveParams, approve_plan)
_writer(CAMPAIGN_STEP_UPDATE_METHOD, StepUpdateParams, update_step)
_writer(CAMPAIGN_ARTIFACT_RECORD_METHOD, ArtifactRecordParams, record_artifact)
_writer(CAMPAIGN_FINDING_PROMOTE_METHOD, FindingPromoteParams, promote_finding)


__all__ = [
    "CAMPAIGN_ARTIFACT_METHOD",
    "CAMPAIGN_ARTIFACT_RECORD_METHOD",
    "CAMPAIGN_FINDING_PROMOTE_METHOD",
    "CAMPAIGN_PLAN_APPROVE_METHOD",
    "CAMPAIGN_STEP_UPDATE_METHOD",
    "CAMPAIGN_VIEW_METHOD",
    "approve_plan",
    "campaign_view",
    "promote_finding",
    "record_artifact",
    "update_step",
]
