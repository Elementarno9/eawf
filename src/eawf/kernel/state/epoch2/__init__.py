"""Strict entity models of authority epoch 2.

The containment tree these models describe is Workspace, Project, Track,
Milestone, DeliveryBatch, Task. A Track owns Milestones; a Milestone owns
the Batches that must complete before it can be accepted; a Batch owns
the Tasks that integrate into one repository. Release is deliberately not
in that chain: it packages accepted Milestones across Tracks and is an
orthogonal aggregate.

Two conventions hold across every model here.

Unknown keys are refused. Each model derives from
:class:`~eawf.kernel.state.epoch2.base.Epoch2Model`, which forbids
extras, so a misspelled field fails at the loader instead of being
dropped into a record that then renders plausibly and is wrong. The same
models back the create documents, the RPC parameters and the persisted
rows, so there is one place where a field name is decided.

Sequences are tuples. A validated value object that a consumer can append
to is a value object only until someone does, and the mutation would
bypass every cross-field rule the model just checked.

Relationships are typed URNs rather than strings, parsed and rendered by
:mod:`eawf.kernel.identity`, so a field that promises a Milestone cannot
hold a Batch.
"""

from __future__ import annotations

from eawf.kernel.state.epoch2.base import (
    AcceptanceStepId,
    BatchKey,
    BranchName,
    CharterStr,
    Epoch2Model,
    MetricId,
    MilestoneKey,
    NonEmptyStr,
    OutcomeStr,
    PrincipalKey,
    SlugStr,
    TitleStr,
    TokenStr,
    TrackKey,
    normalize_phrase,
    reject_normalized_duplicates,
)
from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.milestone import (
    EXCLUSIONS_NONE_MARKER,
    AcceptanceStep,
    DurationBudget,
    Milestone,
    MilestoneCreateSpec,
    MilestoneKind,
    MilestoneStatus,
    validate_acceptance_journey,
    validate_exclusions,
)
from eawf.kernel.state.epoch2.policy import (
    CampaignTemplateRef,
    MetricSpec,
    PromotionRule,
    TrackPolicy,
    TrackPresentationDefaults,
    WipPolicy,
)
from eawf.kernel.state.epoch2.task import Task, TaskPriority, TaskStatus
from eawf.kernel.state.epoch2.track import (
    RepoTrackScope,
    Track,
    TrackCreateSpec,
    TrackScope,
    TrackStatus,
    WorkspaceTrackScope,
)
from eawf.kernel.state.epoch2.urns import (
    AnyEntityUrn,
    BatchUrn,
    CampaignUrn,
    DueScopeUrn,
    EvidenceUrn,
    MilestoneUrn,
    RepositoryUrn,
    RunUrn,
    TaskUrn,
    TrackUrn,
    render_qualified_urn,
)
from eawf.kernel.state.epoch2.values import (
    EntityOrigin,
    EntityRef,
    Epoch2Record,
    ExactRevisionBinding,
    Hold,
    MappingBasis,
    OriginConfidence,
    OwnerPrincipal,
    TransitionReason,
)

__all__ = [
    "EXCLUSIONS_NONE_MARKER",
    "AcceptanceStep",
    "AcceptanceStepId",
    "AnyEntityUrn",
    "BatchKey",
    "BatchStatus",
    "BatchUrn",
    "BranchName",
    "CampaignTemplateRef",
    "CampaignUrn",
    "CharterStr",
    "DeliveryBatch",
    "DueScopeUrn",
    "DurationBudget",
    "EntityOrigin",
    "EntityRef",
    "Epoch2Model",
    "Epoch2Record",
    "EvidenceUrn",
    "ExactRevisionBinding",
    "Hold",
    "MappingBasis",
    "MetricId",
    "MetricSpec",
    "Milestone",
    "MilestoneCreateSpec",
    "MilestoneKey",
    "MilestoneKind",
    "MilestoneStatus",
    "MilestoneUrn",
    "NonEmptyStr",
    "OriginConfidence",
    "OutcomeStr",
    "OwnerPrincipal",
    "PrincipalKey",
    "PromotionRule",
    "RepoTrackScope",
    "RepositoryUrn",
    "RunUrn",
    "SlugStr",
    "Task",
    "TaskPriority",
    "TaskStatus",
    "TaskUrn",
    "TitleStr",
    "TokenStr",
    "Track",
    "TrackCreateSpec",
    "TrackKey",
    "TrackPolicy",
    "TrackPresentationDefaults",
    "TrackScope",
    "TrackStatus",
    "TrackUrn",
    "TransitionReason",
    "WipPolicy",
    "WorkspaceTrackScope",
    "normalize_phrase",
    "reject_normalized_duplicates",
    "render_qualified_urn",
    "validate_acceptance_journey",
    "validate_exclusions",
]
