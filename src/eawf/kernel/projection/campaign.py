"""The Campaign read models: the plan with its steps, the artifact cards, the findings.

The Campaign route and its two sub-surfaces read one Campaign whole. Its row in the
document holds the approved plan and the artifact revisions it keeps; the revisions
themselves and the findings it promoted are ledger lines. :func:`build_campaign_view`
joins the three into :class:`CampaignView`, deriving what a step *is* at render time
-- blocked, and by what -- from what its record stores.

An artifact card carries the revision's text as lines, taken at the revision's own
digest: the stored text is checked against the record before it is split, so a card
never draws content other than the revision it names, and a binary revision carries
no lines at all.

Nothing here reads a file. The daemon reads the document and the ledgers and hands
the rows in, so a view is a pure function of what it was served.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.state.enums import CampaignStatus
from eawf.kernel.state.epoch2.artifact_revision import (
    ARTIFACT_REVISION_PAYLOAD_KIND,
    FileName,
    MediaKind,
    RevisionWriter,
    StoredArtifactRevision,
)
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    StrictNonNegativeInt,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.campaign import (
    ArtifactRevisionRef,
    Campaign,
    CampaignPlanStep,
    CampaignStop,
    ResearchBudget,
    StepState,
    revision_ref,
    step_blockers,
)
from eawf.kernel.state.epoch2.finding import CampaignFinding
from eawf.kernel.state.epoch2.measurement import VendorSessionRef
from eawf.kernel.state.epoch2.run import Run, RunRuntimeTuple
from eawf.kernel.state.epoch2.urns import CampaignUrn, RunUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.tiers import Epoch2Collection


class _View(Epoch2Model):
    """Strict and immutable, like every other projection shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class OrdinalOfTotal(_View):
    """Which of how many, from one.

    Raises:
        pydantic.ValidationError: The ordinal is past the total.
    """

    ordinal: StrictPositiveInt
    total: StrictPositiveInt

    @model_validator(mode="after")
    def _within(self) -> Self:
        """Refuse an ordinal past its total."""
        if self.ordinal > self.total:
            raise ValueError(f"{self.ordinal} of {self.total} is past the end")
        return self


class CampaignStepView(_View):
    """One plan step as the step card reads it.

    Attributes:
        step: The step's record, as stored.
        ordinal_of_total: Its place in the plan.
        state: What it is now: its stored state, or ``blocked`` for a pending step
            something keeps from starting.
        waits_on: What keeps it from starting, by name; empty unless blocked.
        runner_ref: Its current or most recent runner; ``None`` before its first.
        runner_runtime: The runtime that runner records it ran on; ``None`` when it
            records none.
        runner_session: The vendor session that runner ran in; ``None`` when it
            records none.

    Raises:
        pydantic.ValidationError: A blocked step naming no blocker, or a
            blocker named on a step that is not blocked.
    """

    step: CampaignPlanStep
    ordinal_of_total: OrdinalOfTotal
    state: Literal["pending", "running", "done", "blocked"]
    waits_on: tuple[str, ...] = ()
    runner_ref: RunUrn | None = None
    runner_runtime: RunRuntimeTuple | None = None
    runner_session: VendorSessionRef | None = None

    @model_validator(mode="after")
    def _blocked_names_its_blocker(self) -> Self:
        """Keep ``blocked`` and a named blocker together."""
        if (self.state == "blocked") != bool(self.waits_on):
            raise ValueError("a blocked step names what blocks it, and no other step names any")
        return self


class ArtifactCardView(_View):
    """The read model of the ``campaign.artifact`` sub-surface.

    Attributes:
        artifact_ref: The revision the card is about.
        file_name: The file name the card prints.
        media_kind: How the content is drawn.
        size_bytes: The content's size.
        written_at: When it was written.
        written_by: The Run, and step, that wrote it.
        digest: The ``sha256`` of the content.
        kept_with: The Campaign that keeps it.
        ordinal_of_total: Its place among the Campaign's artifacts.
        lines: The content as text at the revision's digest, one entry per line;
            empty for a binary artifact, which opens externally.

    Raises:
        pydantic.ValidationError: A binary artifact carrying lines.
    """

    artifact_ref: ArtifactRevisionRef
    file_name: FileName
    media_kind: MediaKind
    size_bytes: StrictNonNegativeInt
    written_at: UtcDatetime
    written_by: RevisionWriter
    digest: Sha256DigestStr
    kept_with: CampaignUrn
    ordinal_of_total: OrdinalOfTotal
    lines: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _binary_is_never_inline(self) -> Self:
        """Refuse lines on a binary artifact."""
        if self.media_kind is MediaKind.BINARY and self.lines:
            raise ValueError("a binary artifact opens externally and carries no lines")
        return self


class CampaignView(_View):
    """The read model of the Campaign route.

    Attributes:
        campaign_ref: The Campaign.
        key: Its ``CAM-####`` key.
        title: What it researches.
        status: Where it stands.
        stop: Why it stopped dispatching, once it has.
        evidence_budget: Its axis pairs, each limit with the spend charged against it.
        revision: The Campaign row's revision when it was read.
        plan_line: The plan's counts, derived from the steps.
        steps: The plan steps, in order.
        artifacts: The artifact revisions it keeps, in the order it listed them.
        findings: The findings it promoted, held and consumed alike, by key.
        promoted_at: The canonical sequence each finding's promoting event took, by
            finding key. A finding whose event the firehose no longer retains is
            absent, since it was promoted before any replay could start from.
    """

    campaign_ref: CampaignUrn
    key: Annotated[str, Field(pattern=r"^CAM-\d{4,}$")]
    title: str
    status: CampaignStatus
    stop: CampaignStop | None = None
    evidence_budget: ResearchBudget
    revision: StrictPositiveInt
    plan_line: str
    steps: tuple[CampaignStepView, ...]
    artifacts: tuple[ArtifactCardView, ...] = ()
    findings: tuple[CampaignFinding, ...] = ()
    promoted_at: dict[str, StrictPositiveInt] = Field(default_factory=dict)

    def promoted_after(self, cursor: int) -> tuple[CampaignFinding, ...]:
        """Return the findings whose promoting event lies past *cursor*."""
        return tuple(f for f in self.findings if self.promoted_at.get(f.key, 0) > cursor)

    def artifact(self, ref: str) -> ArtifactCardView | None:
        """Return the card of revision *ref*, or ``None`` when the Campaign keeps none."""
        return next((card for card in self.artifacts if card.artifact_ref == ref), None)


def _step_view(
    step: CampaignPlanStep, steps: tuple[CampaignPlanStep, ...], runners: Mapping[str, Run]
) -> CampaignStepView:
    blockers = step_blockers(step, steps)
    runner_ref = step.run_refs[-1] if step.run_refs else None
    runner = None if runner_ref is None else runners.get(runner_ref.entity_key)
    return CampaignStepView(
        step=step,
        ordinal_of_total=OrdinalOfTotal(ordinal=step.ordinal, total=len(steps)),
        state="blocked" if blockers else step.state.value,
        waits_on=blockers,
        runner_ref=runner_ref,
        runner_runtime=None if runner is None else runner.runtime_tuple,
        runner_session=None if runner is None else runner.vendor_session,
    )


def plan_line(steps: Iterable[CampaignStepView]) -> str:
    """Return the Campaign route's plan line, counted from the steps it summarises.

    Args:
        steps: The plan's step views.

    Returns:
        ``<done> of <total> steps done · <running> running · <blocked> blocked``,
        with ``by <blockers>`` when any step is blocked.
    """
    views = tuple(steps)
    count = {state: sum(1 for v in views if v.state == state) for state in StepState}
    blocked = [v for v in views if v.state == "blocked"]
    line = (
        f"{count[StepState.DONE]} of {len(views)} steps done · "
        f"{count[StepState.RUNNING]} running · {len(blocked)} blocked"
    )
    if not blocked:
        return line
    blockers = dict.fromkeys(name for view in blocked for name in view.waits_on)
    return f"{line} by {', '.join(blockers)}"


def artifact_card(stored: StoredArtifactRevision, *, ordinal: int, total: int) -> ArtifactCardView:
    """Return the card of one stored revision, its lines taken at its digest.

    Args:
        stored: The revision and its text, already checked against each other.
        ordinal: Its place among its Campaign's artifacts.
        total: How many artifacts its Campaign keeps.

    Returns:
        The card; a binary revision's carries no lines.
    """
    revision = stored.revision
    return ArtifactCardView(
        artifact_ref=revision_ref(revision.artifact_ref, revision.revision),
        file_name=revision.file_name,
        media_kind=revision.media_kind,
        size_bytes=revision.size_bytes,
        written_at=revision.written_at,
        written_by=revision.written_by,
        digest=revision.digest,
        kept_with=revision.kept_with,
        ordinal_of_total=OrdinalOfTotal(ordinal=ordinal, total=total),
        lines=() if stored.text is None else tuple(stored.text.splitlines()),
    )


def stored_revisions(payloads: Iterable[Mapping[str, Any]]) -> dict[str, StoredArtifactRevision]:
    """Return the artifact ledger's revision lines, keyed by the revision each names.

    A line filed for another reason -- a plan proposal, a spike report -- is not a
    revision and is passed over.

    Raises:
        ValidationError: A line claims to be a revision and does not validate as one,
            which means the ledger is corrupt rather than merely mixed.
    """
    held: dict[str, StoredArtifactRevision] = {}
    for payload in payloads:
        if payload.get("payload_kind") != ARTIFACT_REVISION_PAYLOAD_KIND:
            continue
        stored = StoredArtifactRevision.model_validate(payload)
        held[revision_ref(stored.revision.artifact_ref, stored.revision.revision)] = stored
    return held


def build_campaign_view(
    row: Mapping[str, Any],
    *,
    revisions: Mapping[str, StoredArtifactRevision],
    findings: Iterable[CampaignFinding],
    runners: Mapping[str, Run],
    promoted_at: Mapping[str, int] | None = None,
) -> CampaignView:
    """Return one Campaign's read model.

    Args:
        row: The Campaign's document row.
        revisions: Every stored artifact revision, keyed by its reference.
        findings: Every promoted finding; those of other Campaigns are left out.
        runners: The Runs its steps name, by Run key; a step whose runner is not
            held states no runtime for it.
        promoted_at: The sequence each finding's promoting event took, by key.

    Returns:
        The view, with every artifact revision the Campaign lists resolved to its card.

    Raises:
        ValidationError: The row is not a Campaign.
        ValueError: The Campaign or one of its steps lists an artifact revision no
            ledger line records.
    """
    campaign = Campaign.model_validate(row)
    listed = campaign.artifact_revision_refs
    produced = {
        ref for step in campaign.plan_steps for ref in step.produced if isinstance(ref, str)
    }
    unresolved = sorted((set(listed) | produced) - set(revisions))
    if unresolved:
        raise ValueError(f"{campaign.key} lists artifact revisions no record holds: {unresolved}")
    steps = tuple(_step_view(step, campaign.plan_steps, runners) for step in campaign.plan_steps)
    cards = tuple(
        artifact_card(revisions[ref], ordinal=index, total=len(listed))
        for index, ref in enumerate(listed, start=1)
    )
    own = tuple(
        sorted(
            (f for f in findings if f.campaign_ref == campaign.urn),
            key=lambda finding: finding.key,
        )
    )
    sequences = promoted_at or {}
    return CampaignView(
        campaign_ref=campaign.urn,
        key=campaign.key,
        title=campaign.title,
        status=campaign.status,
        stop=campaign.stop,
        evidence_budget=campaign.evidence_budget,
        revision=campaign.revision,
        plan_line=plan_line(steps),
        steps=steps,
        artifacts=cards,
        findings=own,
        promoted_at={f.key: sequences[f.key] for f in own if f.key in sequences},
    )


def promoted_findings(payloads: Iterable[Mapping[str, Any]]) -> tuple[CampaignFinding, ...]:
    """Return every finding the finding ledger holds.

    Raises:
        ValidationError: A line does not validate as a finding, which means the
            ledger is corrupt.
    """
    return tuple(CampaignFinding.model_validate(payload) for payload in payloads)


def promoting_sequences(payloads: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Return the sequence each finding's promoting event took, from the firehose rows.

    A finding is appended once and never re-promoted, so its first row is its promotion.

    Args:
        payloads: The payloads of the firehose rows, in the order they were written.

    Returns:
        The canonical sequence, by finding key.
    """
    held: dict[str, int] = {}
    for payload in payloads:
        if payload.get("collection") != Epoch2Collection.CAMPAIGN_FINDING.value:
            continue
        key, sequence = payload.get("record_key"), payload.get("canonical_sequence")
        if isinstance(key, str) and isinstance(sequence, int) and not isinstance(sequence, bool):
            held.setdefault(key, sequence)
    return held


__all__ = [
    "ArtifactCardView",
    "CampaignStepView",
    "CampaignView",
    "OrdinalOfTotal",
    "artifact_card",
    "build_campaign_view",
    "plan_line",
    "promoted_findings",
    "promoting_sequences",
    "stored_revisions",
]
