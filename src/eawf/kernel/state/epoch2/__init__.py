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

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
        RunKey,
        SlugStr,
        TitleStr,
        TokenStr,
        TrackKey,
        normalize_phrase,
        reject_normalized_duplicates,
    )
    from eawf.kernel.state.epoch2.batch import BatchCreateSpec, BatchStatus, DeliveryBatch
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
    from eawf.kernel.state.epoch2.run import (
        MUTATING_PURPOSES,
        SUSPENSION_ACTIVITY_BUCKETS,
        ActivityBucket,
        BatchScope,
        CampaignScope,
        ClaimScope,
        EvidenceScope,
        MilestoneScope,
        QuestionScope,
        ReleaseScope,
        RepositoryScope,
        Run,
        RunCreateSpec,
        RunPurpose,
        RunScope,
        RunStatus,
        SuspensionReason,
        TaskScope,
        WorkspaceScope,
        WriteSetPath,
    )
    from eawf.kernel.state.epoch2.task import (
        TERMINAL_TASK_STATUSES,
        Task,
        TaskCreateSpec,
        TaskPriority,
        TaskStatus,
    )
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
        ClaimUrn,
        DueScopeUrn,
        EvidenceUrn,
        MilestoneUrn,
        QuestionUrn,
        ReleaseUrn,
        RepositoryUrn,
        RunUrn,
        TaskUrn,
        TrackUrn,
        WorkspaceUrn,
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

# Re-exports resolve on first access: importing one entity module must not
# build every epoch-2 model, which a statusline redraw pays for.
_EXPORTS: dict[str, str] = {
    "AcceptanceStepId": "eawf.kernel.state.epoch2.base",
    "BatchKey": "eawf.kernel.state.epoch2.base",
    "BranchName": "eawf.kernel.state.epoch2.base",
    "CharterStr": "eawf.kernel.state.epoch2.base",
    "Epoch2Model": "eawf.kernel.state.epoch2.base",
    "MetricId": "eawf.kernel.state.epoch2.base",
    "MilestoneKey": "eawf.kernel.state.epoch2.base",
    "NonEmptyStr": "eawf.kernel.state.epoch2.base",
    "OutcomeStr": "eawf.kernel.state.epoch2.base",
    "PrincipalKey": "eawf.kernel.state.epoch2.base",
    "RunKey": "eawf.kernel.state.epoch2.base",
    "SlugStr": "eawf.kernel.state.epoch2.base",
    "TitleStr": "eawf.kernel.state.epoch2.base",
    "TokenStr": "eawf.kernel.state.epoch2.base",
    "TrackKey": "eawf.kernel.state.epoch2.base",
    "normalize_phrase": "eawf.kernel.state.epoch2.base",
    "reject_normalized_duplicates": "eawf.kernel.state.epoch2.base",
    "BatchCreateSpec": "eawf.kernel.state.epoch2.batch",
    "BatchStatus": "eawf.kernel.state.epoch2.batch",
    "DeliveryBatch": "eawf.kernel.state.epoch2.batch",
    "EXCLUSIONS_NONE_MARKER": "eawf.kernel.state.epoch2.milestone",
    "AcceptanceStep": "eawf.kernel.state.epoch2.milestone",
    "DurationBudget": "eawf.kernel.state.epoch2.milestone",
    "Milestone": "eawf.kernel.state.epoch2.milestone",
    "MilestoneCreateSpec": "eawf.kernel.state.epoch2.milestone",
    "MilestoneKind": "eawf.kernel.state.epoch2.milestone",
    "MilestoneStatus": "eawf.kernel.state.epoch2.milestone",
    "validate_acceptance_journey": "eawf.kernel.state.epoch2.milestone",
    "validate_exclusions": "eawf.kernel.state.epoch2.milestone",
    "CampaignTemplateRef": "eawf.kernel.state.epoch2.policy",
    "MetricSpec": "eawf.kernel.state.epoch2.policy",
    "PromotionRule": "eawf.kernel.state.epoch2.policy",
    "TrackPolicy": "eawf.kernel.state.epoch2.policy",
    "TrackPresentationDefaults": "eawf.kernel.state.epoch2.policy",
    "WipPolicy": "eawf.kernel.state.epoch2.policy",
    "MUTATING_PURPOSES": "eawf.kernel.state.epoch2.run",
    "SUSPENSION_ACTIVITY_BUCKETS": "eawf.kernel.state.epoch2.run",
    "ActivityBucket": "eawf.kernel.state.epoch2.run",
    "BatchScope": "eawf.kernel.state.epoch2.run",
    "CampaignScope": "eawf.kernel.state.epoch2.run",
    "ClaimScope": "eawf.kernel.state.epoch2.run",
    "EvidenceScope": "eawf.kernel.state.epoch2.run",
    "MilestoneScope": "eawf.kernel.state.epoch2.run",
    "QuestionScope": "eawf.kernel.state.epoch2.run",
    "ReleaseScope": "eawf.kernel.state.epoch2.run",
    "RepositoryScope": "eawf.kernel.state.epoch2.run",
    "Run": "eawf.kernel.state.epoch2.run",
    "RunCreateSpec": "eawf.kernel.state.epoch2.run",
    "RunPurpose": "eawf.kernel.state.epoch2.run",
    "RunScope": "eawf.kernel.state.epoch2.run",
    "RunStatus": "eawf.kernel.state.epoch2.run",
    "SuspensionReason": "eawf.kernel.state.epoch2.run",
    "TaskScope": "eawf.kernel.state.epoch2.run",
    "WorkspaceScope": "eawf.kernel.state.epoch2.run",
    "WriteSetPath": "eawf.kernel.state.epoch2.run",
    "TERMINAL_TASK_STATUSES": "eawf.kernel.state.epoch2.task",
    "Task": "eawf.kernel.state.epoch2.task",
    "TaskCreateSpec": "eawf.kernel.state.epoch2.task",
    "TaskPriority": "eawf.kernel.state.epoch2.task",
    "TaskStatus": "eawf.kernel.state.epoch2.task",
    "RepoTrackScope": "eawf.kernel.state.epoch2.track",
    "Track": "eawf.kernel.state.epoch2.track",
    "TrackCreateSpec": "eawf.kernel.state.epoch2.track",
    "TrackScope": "eawf.kernel.state.epoch2.track",
    "TrackStatus": "eawf.kernel.state.epoch2.track",
    "WorkspaceTrackScope": "eawf.kernel.state.epoch2.track",
    "AnyEntityUrn": "eawf.kernel.state.epoch2.urns",
    "BatchUrn": "eawf.kernel.state.epoch2.urns",
    "CampaignUrn": "eawf.kernel.state.epoch2.urns",
    "ClaimUrn": "eawf.kernel.state.epoch2.urns",
    "DueScopeUrn": "eawf.kernel.state.epoch2.urns",
    "EvidenceUrn": "eawf.kernel.state.epoch2.urns",
    "MilestoneUrn": "eawf.kernel.state.epoch2.urns",
    "QuestionUrn": "eawf.kernel.state.epoch2.urns",
    "ReleaseUrn": "eawf.kernel.state.epoch2.urns",
    "RepositoryUrn": "eawf.kernel.state.epoch2.urns",
    "RunUrn": "eawf.kernel.state.epoch2.urns",
    "TaskUrn": "eawf.kernel.state.epoch2.urns",
    "TrackUrn": "eawf.kernel.state.epoch2.urns",
    "WorkspaceUrn": "eawf.kernel.state.epoch2.urns",
    "render_qualified_urn": "eawf.kernel.state.epoch2.urns",
    "EntityOrigin": "eawf.kernel.state.epoch2.values",
    "EntityRef": "eawf.kernel.state.epoch2.values",
    "Epoch2Record": "eawf.kernel.state.epoch2.values",
    "ExactRevisionBinding": "eawf.kernel.state.epoch2.values",
    "Hold": "eawf.kernel.state.epoch2.values",
    "MappingBasis": "eawf.kernel.state.epoch2.values",
    "OriginConfidence": "eawf.kernel.state.epoch2.values",
    "OwnerPrincipal": "eawf.kernel.state.epoch2.values",
    "TransitionReason": "eawf.kernel.state.epoch2.values",
}


def __getattr__(name: str) -> Any:
    """Import the submodule that defines *name* and return the name.

    Raises:
        AttributeError: *name* is not a re-exported name.
    """
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


__all__ = [
    "EXCLUSIONS_NONE_MARKER",
    "MUTATING_PURPOSES",
    "SUSPENSION_ACTIVITY_BUCKETS",
    "TERMINAL_TASK_STATUSES",
    "AcceptanceStep",
    "AcceptanceStepId",
    "ActivityBucket",
    "AnyEntityUrn",
    "BatchCreateSpec",
    "BatchKey",
    "BatchScope",
    "BatchStatus",
    "BatchUrn",
    "BranchName",
    "CampaignScope",
    "CampaignTemplateRef",
    "CampaignUrn",
    "CharterStr",
    "ClaimScope",
    "ClaimUrn",
    "DeliveryBatch",
    "DueScopeUrn",
    "DurationBudget",
    "EntityOrigin",
    "EntityRef",
    "Epoch2Model",
    "Epoch2Record",
    "EvidenceScope",
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
    "MilestoneScope",
    "MilestoneStatus",
    "MilestoneUrn",
    "NonEmptyStr",
    "OriginConfidence",
    "OutcomeStr",
    "OwnerPrincipal",
    "PrincipalKey",
    "PromotionRule",
    "QuestionScope",
    "QuestionUrn",
    "ReleaseScope",
    "ReleaseUrn",
    "RepoTrackScope",
    "RepositoryScope",
    "RepositoryUrn",
    "Run",
    "RunCreateSpec",
    "RunKey",
    "RunPurpose",
    "RunScope",
    "RunStatus",
    "RunUrn",
    "SlugStr",
    "SuspensionReason",
    "Task",
    "TaskCreateSpec",
    "TaskPriority",
    "TaskScope",
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
    "WorkspaceScope",
    "WorkspaceTrackScope",
    "WorkspaceUrn",
    "WriteSetPath",
    "normalize_phrase",
    "reject_normalized_duplicates",
    "render_qualified_urn",
    "validate_acceptance_journey",
    "validate_exclusions",
]
