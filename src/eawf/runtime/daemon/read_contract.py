"""The verbs a surface reads the daemon through, and the answers it parses.

A console names a read by its verb and validates the reply against its answer model. Both
live here, apart from the handlers that serve them, because a handler module carries the
server side with it: ledgers, the delivery spine, the evaluation stack. A surface that
imported the handler for a verb name paid for all of that before its first frame. The
handler modules import what they serve from here, so each verb and answer is spelled once.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from eawf.kernel.delivery.integration import IntegrationConflict, IntegrationGeneration
from eawf.kernel.delivery.receipts import ProofReceipt
from eawf.kernel.projection.run_timeline import RunTimeline
from eawf.kernel.runtime.boot_recovery import BootRecovery
from eawf.kernel.runtime.certification import CertificationFailureCode
from eawf.kernel.runtime.content import ResolvedContent
from eawf.kernel.runtime.events import RunEventKind
from eawf.kernel.runtime.permission import PrincipalClass
from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.runtime.stall import RunStallFact
from eawf.kernel.state.epoch2.base import StrictNonNegativeInt, StrictPositiveInt
from eawf.kernel.state.epoch2.pause import OpenPause, PauseSituation
from eawf.kernel.state.epoch2.question import OpenQuestion, QuestionSituation
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.transitions import AmbiguityLabel
from eawf.kernel.state.types import UtcDatetime
from eawf.observability.doctor.models import CheckResult
from eawf.runtime.daemon.run_events import RunLiveness

#: The RPC name a console subscribes to keyed projection patches by.
PROJECTION_SUBSCRIBE_METHOD: Final = "projection.subscribe"

#: The notification a projection subscriber receives one of per keyed patch,
#: beside the ``event.push`` the other two verbs push raw envelopes as.
PROJECTION_PUSH_METHOD: Final = "projection.patch"

#: JSON-RPC error code raised when catch-up exceeds the bound.
CATCH_UP_TOO_LARGE = -32008

CAMPAIGN_VIEW_METHOD: Final = "projection.campaign.view"
CAMPAIGN_ARTIFACT_METHOD: Final = "projection.campaign.artifact"

#: Read the newest conformance verdict of every runtime tuple the tree has certified.
HEALTH_VERDICTS_READ_METHOD: Final = "projection.health.verdicts"

#: Read one Batch's integration generations, oldest first.
GENERATIONS_READ_METHOD: Final = "projection.git.pr.generations"

#: Read the branch the tree has checked out and the pull request open for it.
REPOSITORY_READ_METHOD: Final = "projection.git.pr.repository"

#: Read the conflict frames one Batch's blocked integrations left, in record order.
CONFLICT_FRAMES_READ_METHOD: Final = "projection.merge.conflict.frames"

#: Read what the daemon's last start repaired and what that cost.
BOOT_RECOVERY_READ_METHOD: Final = "projection.crash.recovery.boot"

#: Read the proof receipt filed under one receipt key.
PROOF_RECEIPTS_READ_METHOD: Final = "projection.receipt.proofs"

#: Read whether anything was ever written under one key.
TARGET_RESOLVE_METHOD: Final = "projection.target.resolve"

#: Read one page of the change feed, newest first.
HISTORY_CHANGES_READ_METHOD: Final = "projection.history.changes"

#: The read that states the dispatch queue.
DISPATCH_QUEUE_READ_METHOD: Final = "runtime.dispatch.queue.read"

#: The verb that reports one Run's permissions.
PERMISSION_READ_METHOD: Final = "runtime.permission.read"

#: The read verb of a Run's ordered event stream.
RUN_EVENTS_READ_METHOD: Final = "runtime.run.events.read"

#: Read the content a Run's transcript references resolve to.
RUN_CONTENT_READ_METHOD: Final = "runtime.run.content.read"

#: The most references one read resolves.
CONTENT_READ_REFS: Final = 256

#: The read that lists every stall still standing in a tree.
RUN_STALLS_READ_METHOD: Final = "runtime.run.stalls.read"

#: The governor ceiling beside what the live Runs spend and hold against it.
SPEND_CEILING_READ_METHOD: Final = "runtime.spend.ceiling.read"

#: One Run's spend beside the caps its binding sealed.
RUN_USAGE_READ_METHOD: Final = "runtime.run.usage.read"

#: The verb every pause of a tree is read through.
PAUSE_READ_METHOD: Final = "runtime.pause.read"

#: The verb every question of a tree is read through.
QUESTION_READ_METHOD: Final = "runtime.question.read"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TupleVerdict(_Closed):
    """One runtime tuple's newest verdict on the wire.

    Attributes:
        check: The doctor check repeating the newest stage record.
        reason_code: The failure code that record was filed under; ``None`` for a pass.
        checked_at: When that record's stage completed, which is when the check last ran.
    """

    check: CheckResult
    reason_code: CertificationFailureCode | None = None
    checked_at: UtcDatetime


class HealthVerdictsAnswer(_Closed):
    """The verdicts, ordered by tuple digest; empty for a tree that never certified."""

    verdicts: tuple[TupleVerdict, ...] = ()


class GenerationsAnswer(_Closed):
    """One Batch's generations, oldest first."""

    generations: tuple[IntegrationGeneration, ...] = ()


class ConflictFramesAnswer(_Closed):
    """The conflict frames one Batch's blocked integrations left, in record order."""

    conflicts: tuple[IntegrationConflict, ...] = ()


class BootRecoveryAnswer(_Closed):
    """The daemon's last start, or ``None`` when no start on this machine recorded one."""

    last: BootRecovery | None = None


class ProofReceiptsAnswer(_Closed):
    """The receipts filed under the key, in record order; empty when none was."""

    receipts: tuple[ProofReceipt, ...] = ()


class TargetResolution(_Closed):
    """Whether a key still names something.

    Attributes:
        key: The key asked about.
        ending: ``missing`` when no document row and no ledger line holds the key; ``None``
            when something does, so the route's own frame states where it stands.
    """

    key: str
    ending: Literal["missing"] | None = None


class PermissionAnswer(BaseModel):
    """One permission as a surface reads it.

    Attributes:
        permission: The latest revision of the record.
        repository_may_approve: Whether the repository principal class may
            approve. False renders the affordance disabled, never absent.
        approve_authority: The classes that may approve, which a disabled
            repository affordance names.
        hold_supported: Always false: the deadline is the provider's.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    permission: dict[str, Any]
    repository_may_approve: bool
    approve_authority: tuple[PrincipalClass, ...]
    hold_supported: bool


class PermissionsAnswer(BaseModel):
    """What the read and expire verbs answer with.

    Attributes:
        permissions: The Run's permissions in key order.
        expired: The keys this call recorded an expiry for.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    permissions: tuple[PermissionAnswer, ...]
    expired: tuple[str, ...] = ()


class RunStallAnswer(BaseModel):
    """How long a Run has been quiet, and what clears it.

    Attributes:
        verdict: Live, stalled, or unknown for a Run with no activity.
        last_activity_at: The activity the silence is measured from.
        last_activity_kind: What that activity was.
        elapsed_seconds: How long the Run has produced nothing.
        interval_seconds: The interval it was measured against.
        resume_method: The verb a principal resumes it through. A stall
            never terminates the Run, so this is an offer and not a
            record of something the daemon did.
        resume_control: The control that resume request asks for.
        ambiguity: ``lost`` for a stalled Run still recorded as running:
            its outcome is unknown, which is neither a success nor a
            failure, and only a principal's control ends it.
        raised: The stall fact the daemon's sweep raised for this silence, or ``None``
            while none stands: the Run is live, or the sweep has not reached it yet.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: RunLiveness
    last_activity_at: str | None = None
    last_activity_kind: RunEventKind | None = None
    elapsed_seconds: float
    interval_seconds: StrictNonNegativeInt
    resume_method: str
    resume_control: ControlKind
    ambiguity: AmbiguityLabel | None = None
    raised: RunStallFact | None = None


class RunEventsAnswer(BaseModel):
    """What a read of one Run's stream answers with.

    Attributes:
        events: The derived stream, in sequence order.
        quarantined: The lines retained as diagnostics and derived from
            by nothing.
        gaps: Every recorded hole, in stream order.
        last_contiguous_sequence: Where derivation is sound up to.
        next_sequence: The sequence the next event is expected to carry.
        derivation_stopped: Whether a hole sits inside the stream.
        run_status: The status the Run's confirmed effects support.
        stall: The Run's liveness at the moment of the read.
        timeline: The stream reduced to timeline rows, repeated low-priority events
            coalesced, so a surface draws the groups rather than recreating them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    events: tuple[dict[str, Any], ...]
    quarantined: tuple[dict[str, Any], ...]
    gaps: tuple[dict[str, Any], ...]
    last_contiguous_sequence: StrictNonNegativeInt
    next_sequence: StrictPositiveInt
    derivation_stopped: bool
    run_status: RunStatus
    stall: RunStallAnswer
    timeline: RunTimeline


ContentRef = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


class ContentRefusal(BaseModel):
    """A reference the read refuses to resolve, and why.

    Attributes:
        ref: The reference asked for.
        code: ``content_pruned`` when the text it named was kept once and the local
            store has since pruned it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: ContentRef
    code: Literal["content_pruned"]


class RunContentAnswer(BaseModel):
    """What the read answers with.

    Attributes:
        contents: The content each resolved reference names, in the order asked.
        missing: The references this Run holds nothing under.
        refused: The references whose content the read cannot give, each with its code.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contents: tuple[ResolvedContent, ...] = ()
    missing: tuple[str, ...] = ()
    refused: tuple[ContentRefusal, ...] = ()


class RunStallsAnswer(BaseModel):
    """Every stall still standing in a tree, at one instant.

    Attributes:
        stalls: One fact per running Run that has produced nothing since its stall was
            raised, in Run key order.
        read_at: When the daemon answered, which is what a reader ages the answer by.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stalls: tuple[RunStallFact, ...]
    read_at: UtcDatetime


class PauseItem(BaseModel):
    """One pause as the read reports it.

    Attributes:
        pause: The record, at its latest revision.
        situation: The situation it projects to now.
        ends_when: What would end it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pause: OpenPause
    situation: PauseSituation
    ends_when: str


class PausesAnswer(BaseModel):
    """What :data:`PAUSE_READ_METHOD` answers.

    Attributes:
        pauses: Every pause of the tree, by key.
        run_states: The state of each Run a pause holds, by Run key; a Run the document
            no longer holds is absent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pauses: tuple[PauseItem, ...] = ()
    run_states: dict[str, str] = {}


def stall_of(pause: OpenPause) -> str | None:
    """Return the stall fact key a pause over a quiet Run cites, or ``None`` for any other."""
    return next((ref for ref in pause.health_evidence_refs if isinstance(ref, str)), None)


class QuestionItem(BaseModel):
    """One question as the read reports it.

    Attributes:
        question: The record, at its latest revision.
        asked_by_run: The key of the Run that asked it; ``None`` for a question no Run asked.
        situation: The situation it projects to for the reading principal.
        ends_when: What would end it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: OpenQuestion
    asked_by_run: str | None = None
    situation: QuestionSituation
    ends_when: str


class QuestionsAnswer(BaseModel):
    """What :data:`QUESTION_READ_METHOD` answers.

    Attributes:
        questions: Every question of the tree, by key.
        run_states: The state of each asking Run, by Run key; a Run the document no
            longer holds is absent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    questions: tuple[QuestionItem, ...] = ()
    run_states: dict[str, str] = {}


__all__ = [
    "BOOT_RECOVERY_READ_METHOD",
    "CAMPAIGN_ARTIFACT_METHOD",
    "CAMPAIGN_VIEW_METHOD",
    "CATCH_UP_TOO_LARGE",
    "CONFLICT_FRAMES_READ_METHOD",
    "CONTENT_READ_REFS",
    "DISPATCH_QUEUE_READ_METHOD",
    "GENERATIONS_READ_METHOD",
    "HEALTH_VERDICTS_READ_METHOD",
    "HISTORY_CHANGES_READ_METHOD",
    "PAUSE_READ_METHOD",
    "PERMISSION_READ_METHOD",
    "PROJECTION_PUSH_METHOD",
    "PROJECTION_SUBSCRIBE_METHOD",
    "PROOF_RECEIPTS_READ_METHOD",
    "QUESTION_READ_METHOD",
    "REPOSITORY_READ_METHOD",
    "RUN_CONTENT_READ_METHOD",
    "RUN_EVENTS_READ_METHOD",
    "RUN_STALLS_READ_METHOD",
    "RUN_USAGE_READ_METHOD",
    "SPEND_CEILING_READ_METHOD",
    "TARGET_RESOLVE_METHOD",
    "BootRecoveryAnswer",
    "ConflictFramesAnswer",
    "ContentRef",
    "ContentRefusal",
    "GenerationsAnswer",
    "HealthVerdictsAnswer",
    "PauseItem",
    "PausesAnswer",
    "PermissionAnswer",
    "PermissionsAnswer",
    "ProofReceiptsAnswer",
    "QuestionItem",
    "QuestionsAnswer",
    "RunContentAnswer",
    "RunEventsAnswer",
    "RunStallAnswer",
    "RunStallsAnswer",
    "TargetResolution",
    "TupleVerdict",
    "stall_of",
]
