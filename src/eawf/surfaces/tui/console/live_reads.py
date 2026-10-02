"""The reads a route owes beside its projection, and keeps current while it is on screen.

A route projection is patched by the feed, so a console that holds one is current without
asking again. Some of what a frame draws is not a projection row: a Run's event lines are
ledger lines appended with no canonical move, so nothing is published when one lands and
no patch ever carries it. Such a read is declared here once, as a :class:`LiveRead`: the
route that draws it, how the seam finds what it is about, and how it is fetched. The seam
owes every declared read of the route on screen as soon as it can be addressed, and the
console reads it again while the route stays on screen, so what is appended lands without
a relaunch. A new read is one more entry in :data:`LIVE_READS`; neither the seam nor the
console names any read itself.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Protocol

from eawf.kernel.delivery.integration import IntegrationConflict, IntegrationGeneration
from eawf.kernel.delivery.receipts import ProofReceipt
from eawf.kernel.economics.spend import CostCeilingView, RunUsageView
from eawf.kernel.identity import EntityKind
from eawf.kernel.projection.activity import ACTIVITY_ROUTE
from eawf.kernel.projection.campaign import ArtifactCardView, CampaignView
from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, RouteProjection
from eawf.kernel.projection.connection import ReplayNote
from eawf.kernel.projection.integration import GIT_PR_ROUTE, MERGE_CONFLICT_ROUTE
from eawf.kernel.projection.liveness import HeldLiveness
from eawf.kernel.projection.operations import CRASH_RECOVERY_ROUTE
from eawf.kernel.projection.registers import COST_CEILING_ROUTE
from eawf.kernel.projection.run_timeline import RunTimeline
from eawf.kernel.projection.transcript import TRANSCRIPT_ROUTE, content_refs
from eawf.kernel.projection.verification import HEALTH_ROUTE, RuntimeTupleVerdict
from eawf.kernel.runtime.boot_recovery import BootRecovery
from eawf.kernel.runtime.content import ResolvedContent
from eawf.kernel.runtime.dispatch_queue import DispatchQueueView
from eawf.kernel.runtime.events import ChildRunPayload, QuestionActionPayload, RunEventRecord
from eawf.kernel.state.epoch2.transitions import AmbiguityLabel
from eawf.kernel.store.changes import ChangePage
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods.campaign import CAMPAIGN_ARTIFACT_METHOD, CAMPAIGN_VIEW_METHOD
from eawf.runtime.daemon.methods.console_records import (
    BOOT_RECOVERY_READ_METHOD,
    CONFLICT_FRAMES_READ_METHOD,
    GENERATIONS_READ_METHOD,
    HEALTH_VERDICTS_READ_METHOD,
    HISTORY_CHANGES_READ_METHOD,
    PROOF_RECEIPTS_READ_METHOD,
    REPOSITORY_READ_METHOD,
    TARGET_RESOLVE_METHOD,
    BootRecoveryAnswer,
    ConflictFramesAnswer,
    GenerationsAnswer,
    HealthVerdictsAnswer,
    ProofReceiptsAnswer,
    TargetResolution,
)
from eawf.runtime.daemon.methods.dispatch_queue import DISPATCH_QUEUE_READ_METHOD
from eawf.runtime.daemon.methods.permission import PERMISSION_READ_METHOD, PermissionsAnswer
from eawf.runtime.daemon.methods.run import RUN_EVENTS_READ_METHOD, RunEventsAnswer
from eawf.runtime.daemon.methods.run_content import (
    CONTENT_READ_REFS,
    RUN_CONTENT_READ_METHOD,
    RunContentAnswer,
)
from eawf.runtime.daemon.methods.run_liveness import RUN_STALLS_READ_METHOD, RunStallsAnswer
from eawf.runtime.daemon.methods.spend import RUN_USAGE_READ_METHOD, SPEND_CEILING_READ_METHOD
from eawf.runtime.vcs.repository_read import RepositoryAnswer
from eawf.surfaces.tui.console.decisions import ArtifactRecord, DecisionRecords, campaign_records
from eawf.surfaces.tui.console.open_records import (
    ATTENTION_ROUTE,
    OPEN_RECORDS_READ,
    HeldOpenRecords,
    attention_address,
    fetch_open_records,
)
from eawf.surfaces.tui.console.operations import Operator
from eawf.workflow.projection.acceptance import RECEIPT_ROUTE

logger = logging.getLogger(__name__)

CAMPAIGN_ROUTE: Final = "campaign"
STEP_ROUTE: Final = "campaign.step"
ARTIFACT_ROUTE: Final = "campaign.artifact"
RUN_DETAIL_ROUTE: Final = "run.detail"
CAMPAIGN_KEY: Final = re.compile(r"^CAM-\d{4,}$")


class LiveReadHost(Protocol):
    """What a live read needs of the seam: where the console is, and a way to ask."""

    @property
    def route(self) -> str:
        """Return the console route key on screen."""
        ...

    @property
    def subject(self) -> str | None:
        """Return the key of the record the route on screen is about, if any."""
        ...

    @property
    def operator(self) -> Operator | None:
        """Return who the console acts as, or ``None`` when it acts as nobody."""
        ...

    def projection_for(self, route: str) -> RouteProjection | None:
        """Return the projection held for *route*, if any."""
        ...

    def live(self, name: str, *, anywhere: bool = False) -> Any | None:
        """Return live read *name*'s answer for the route on screen, or anywhere, if held."""
        ...

    async def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Send one read to the daemon for the seam's tree and return its answer."""
        ...

    def now(self) -> datetime:
        """Return the time the seam stamps what it reads with."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveRead:
    """One read a route owes beside its projection.

    Attributes:
        routes: The console route keys that draw what the read returns.
        address: What the read is about right now -- a URN, a key -- or ``None`` while
            it cannot be addressed yet, such as before the route's rows arrive. A held
            answer is shown only while its address is still the current one.
        fetch: Reads the answer for an address.
    """

    routes: frozenset[str]
    address: Callable[[LiveReadHost], str | None]
    fetch: Callable[[LiveReadHost, str], Awaitable[Any]]


@dataclass(frozen=True, slots=True, kw_only=True)
class HeldTranscript:
    """The event lines one Run's transcript is drawn from, as last read.

    Attributes:
        events: The Run's own lines, in sequence order, of every kind the daemon holds.
        children: The lines of each child Run the Run delegated to, by child URN. A
            child whose read failed is absent, and the transcript draws it unreadable.
        contents: The stored content the Run's blocks unfold to, by the reference each
            names. A reference whose read failed is absent, and its block names it.
        deadlines: The provider's deadline of each permission the Run's approval lines
            name, by permission URN. A permission whose read failed is absent, and its
            block says its deadline went unread.
    """

    events: tuple[RunEventRecord, ...] = ()
    children: Mapping[str, tuple[RunEventRecord, ...]] = field(default_factory=dict)
    contents: Mapping[str, ResolvedContent] = field(default_factory=dict)
    deadlines: Mapping[str, datetime] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class HeldCampaign:
    """One Campaign's read model and its runners' event lines, as last read.

    Attributes:
        view: The Campaign as the daemon served it.
        events: The event lines of each step's runner Run, by Run URN. A runner whose
            read failed is absent, and its step card draws no activity.
        read_at: When it was read, which the cards state as their ``as of``. It does not
            take part in equality, so a re-read that changed nothing repaints nothing.
    """

    view: CampaignView
    events: Mapping[str, tuple[RunEventRecord, ...]] = field(default_factory=dict)
    read_at: datetime = field(compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class HeldArtifact:
    """One artifact revision's card, as last read.

    Attributes:
        card: The card as the daemon served it.
        read_at: When it was read; it does not take part in equality.
    """

    card: ArtifactCardView
    read_at: datetime = field(compare=False)


def _transcript_address(host: LiveReadHost) -> str | None:
    """Return the URN of the Run the transcript is about, once its row is held."""
    held = host.projection_for(TRANSCRIPT_ROUTE)
    subject = host.subject
    if host.route != TRANSCRIPT_ROUTE or held is None or not subject:
        return None
    return next((row.urn for row in held.rows if row.key == subject), None)


async def _run_events(host: LiveReadHost, urn: str) -> tuple[RunEventRecord, ...]:
    """Read one Run's derived stream, in sequence order."""
    answer = RunEventsAnswer.model_validate(await host.call(RUN_EVENTS_READ_METHOD, {"urn": urn}))
    return tuple(RunEventRecord.model_validate(line) for line in answer.events)


async def _contents(
    host: LiveReadHost, urn: str, events: tuple[RunEventRecord, ...]
) -> dict[str, ResolvedContent]:
    """Resolve the references the Run's blocks unfold to, a read's worth at a time.

    A read that fails leaves its references out rather than failing the transcript: the
    blocks then name what they would unfold to.
    """
    refs = list(dict.fromkeys(ref for line in events for ref in content_refs(line)))
    held: dict[str, ResolvedContent] = {}
    for start in range(0, len(refs), CONTENT_READ_REFS):
        chunk = refs[start : start + CONTENT_READ_REFS]
        try:
            answer = RunContentAnswer.model_validate(
                await host.call(RUN_CONTENT_READ_METHOD, {"urn": urn, "refs": chunk})
            )
        except Exception as exc:
            logger.warning(f"transcript content unreadable cause={exc!r}")
            continue
        held.update((item.ref, item) for item in answer.contents)
    return held


async def _permission_deadlines(
    host: LiveReadHost, urn: str, events: tuple[RunEventRecord, ...]
) -> dict[str, datetime]:
    """Read the provider's deadline of every permission the Run's approval lines name.

    Nothing is read for a Run whose lines name no permission, and a failed read leaves
    the deadlines out rather than failing the transcript.
    """
    named = any(
        isinstance(line.payload, QuestionActionPayload)
        and line.payload.subject_ref.kind is EntityKind.PERMISSION
        for line in events
    )
    if not named:
        return {}
    try:
        answer = PermissionsAnswer.model_validate(
            await host.call(PERMISSION_READ_METHOD, {"urn": urn})
        )
    except Exception as exc:
        logger.warning(f"transcript permission deadlines unreadable cause={exc!r}")
        return {}
    return {
        str(item.permission["urn"]): datetime.fromisoformat(item.permission["deadline_at"])
        for item in answer.permissions
    }


async def _fetch_transcript(host: LiveReadHost, urn: str) -> HeldTranscript:
    """Read a Run's lines, the content its blocks unfold to, its children's lines and
    the deadline of each permission its approval lines wait on.

    The lines are taken whatever their kind, so a producer that starts appending a new
    kind is drawn with no change here. A child whose read fails is left out rather than
    failing the parent's read.
    """
    events = await _run_events(host, urn)
    contents = await _contents(host, urn, events)
    deadlines = await _permission_deadlines(host, urn, events)
    children: dict[str, tuple[RunEventRecord, ...]] = {}
    for child in dict.fromkeys(
        str(line.payload.child_run_ref)
        for line in events
        if isinstance(line.payload, ChildRunPayload) and line.payload.child_run_ref
    ):
        try:
            children[child] = await _run_events(host, child)
        except Exception as exc:
            logger.warning(f"transcript child unreadable cause={exc!r}")
    logger.debug(
        f"fetch_transcript events={len(events)} children={len(children)} "
        f"contents={len(contents)} deadlines={len(deadlines)}"
    )
    return HeldTranscript(events=events, children=children, contents=contents, deadlines=deadlines)


#: The read the transcript route draws its blocks from.
TRANSCRIPT_READ: Final = RUN_EVENTS_READ_METHOD

#: The read the Campaign route and its step card draw the plan from.
CAMPAIGN_READ: Final = CAMPAIGN_VIEW_METHOD
#: The read the artifact card draws its file from.
ARTIFACT_READ: Final = CAMPAIGN_ARTIFACT_METHOD

#: The routes that draw one Campaign's plan: the route, and the step card opened on it.
CAMPAIGN_ROUTES: Final = frozenset({CAMPAIGN_ROUTE, STEP_ROUTE})

#: The read the Run frame draws its timeline rows from.
RUN_TIMELINE_READ: Final = f"{RUN_EVENTS_READ_METHOD}@{RUN_DETAIL_ROUTE}"

#: The read that says which running Runs went quiet, and the routes that draw it.
LIVENESS_READ: Final = RUN_STALLS_READ_METHOD
LIVENESS_ROUTES: Final = frozenset({ACTIVITY_ROUTE, RUN_DETAIL_ROUTE, "unattended"})

#: The read the unattended route draws its queue's progress, plan and control from.
DISPATCH_QUEUE_READ: Final = DISPATCH_QUEUE_READ_METHOD
UNATTENDED_ROUTE: Final = "unattended"

#: The address of a read that is about the whole tree rather than one record.
_TREE: Final = "tree"


def _campaign_address(host: LiveReadHost) -> str | None:
    """Return the key of the Campaign on screen: the subject, else the first row held."""
    subject = host.subject
    if subject and CAMPAIGN_KEY.match(subject):
        return subject
    if host.route != CAMPAIGN_ROUTE:
        return None
    held = host.projection_for(CAMPAIGN_ROUTE)
    rows = [row.key for row in held.rows if CAMPAIGN_KEY.match(row.key)] if held else []
    return rows[0] if rows else None


async def _fetch_campaign(host: LiveReadHost, key: str) -> HeldCampaign:
    """Read a Campaign and the lines of every runner its steps name.

    A runner whose read fails is left out rather than failing the Campaign's read.
    """
    view = CampaignView.model_validate(await host.call(CAMPAIGN_READ, {"campaign_key": key}))
    events: dict[str, tuple[RunEventRecord, ...]] = {}
    for runner in dict.fromkeys(str(s.runner_ref) for s in view.steps if s.runner_ref):
        try:
            events[runner] = await _run_events(host, runner)
        except Exception as exc:
            logger.warning(f"campaign runner unreadable cause={exc!r}")
    logger.debug(f"fetch_campaign key={key} steps={len(view.steps)} runners={len(events)}")
    return HeldCampaign(view=view, events=events, read_at=host.now())


def _artifact_address(host: LiveReadHost) -> str | None:
    """Return ``<campaign key> <revision>`` for the card on screen, once its Campaign is read.

    The card is opened from a Campaign that was read, so that read names which Campaign
    keeps the revision the subject names.
    """
    subject, held = host.subject, host.live(CAMPAIGN_READ, anywhere=True)
    if not subject or not isinstance(held, HeldCampaign) or held.view.artifact(subject) is None:
        return None
    return f"{held.view.key} {subject}"


async def _fetch_artifact(host: LiveReadHost, address: str) -> HeldArtifact:
    """Read one artifact revision's card."""
    key, ref = address.split(" ", 1)
    answer = await host.call(ARTIFACT_READ, {"campaign_key": key, "artifact_ref": ref})
    return HeldArtifact(card=ArtifactCardView.model_validate(answer), read_at=host.now())


def _run_detail_address(host: LiveReadHost) -> str | None:
    """Return the URN of the Run the Run frame is about: its subject, else its first Run."""
    held = host.projection_for(RUN_DETAIL_ROUTE)
    if host.route != RUN_DETAIL_ROUTE or held is None:
        return None
    runs = [row for row in held.rows if row.collection is Epoch2Collection.RUN]
    subject = host.subject
    if subject:
        return next((row.urn for row in runs if row.key == subject), None)
    return runs[0].urn if runs else None


async def _fetch_timeline(host: LiveReadHost, urn: str) -> RunTimeline:
    """Read one Run's timeline rows as the daemon grouped them."""
    answer = RunEventsAnswer.model_validate(await host.call(RUN_EVENTS_READ_METHOD, {"urn": urn}))
    return answer.timeline


def _tree_address(_host: LiveReadHost) -> str | None:
    """Return the one address a tree-wide read has: the tree the seam is attached to."""
    return _TREE


async def _fetch_liveness(host: LiveReadHost, _address: str) -> HeldLiveness:
    """Read every stall standing in the tree, with the instant the daemon answered."""
    answer = RunStallsAnswer.model_validate(await host.call(RUN_STALLS_READ_METHOD, {}))
    return HeldLiveness(stalls=answer.stalls, read_at=answer.read_at)


async def _fetch_dispatch_queue(host: LiveReadHost, _address: str) -> DispatchQueueView:
    """Read the dispatch queue: its Runs, running legs, plan and control."""
    return DispatchQueueView.model_validate(await host.call(DISPATCH_QUEUE_READ, {}))


def held_queue(live: Mapping[str, object]) -> DispatchQueueView | None:
    """Return the dispatch queue among a frame's live answers, or ``None`` before its read."""
    queue = live.get(DISPATCH_QUEUE_READ)
    return queue if isinstance(queue, DispatchQueueView) else None


def held_usage(live: Mapping[str, object], key: str) -> RunUsageView | None:
    """Return the usage read's answer for Run *key* among a frame's live answers, if held."""
    return next(
        (item for item in live.values() if isinstance(item, RunUsageView) and item.run_key == key),
        None,
    )


def held_records(host: LiveReadHost) -> DecisionRecords | None:
    """Return the records the reads on screen bind, or ``None`` while none is held.

    The Campaign read binds the Campaign itself, its step cards and its artifact cards; the
    artifact read binds the one card it was read for; the stall read holds every running
    Run a stall stands over as lost; the Attention read binds every question and
    pause of the tree.
    """
    campaign, artifact = host.live(CAMPAIGN_READ), host.live(ARTIFACT_READ)
    liveness = host.live(LIVENESS_READ)
    opened = host.live(OPEN_RECORDS_READ)
    records = None
    if isinstance(campaign, HeldCampaign):
        records = campaign_records(campaign.view, campaign.events, as_of=campaign.read_at)
    if isinstance(artifact, HeldArtifact):
        card = ArtifactRecord.of_card(artifact.card, as_of=artifact.read_at)
        records = (records or DecisionRecords()).model_copy(update={"artifacts": (card,)})
    if isinstance(liveness, HeldLiveness):
        lost = dict.fromkeys(liveness.stalled_keys(), AmbiguityLabel.LOST.name)
        records = (records or DecisionRecords()).model_copy(update={"run_states": lost})
    if isinstance(opened, HeldOpenRecords):
        records = (records or DecisionRecords()).model_copy(
            update={
                "questions": opened.questions,
                "pauses": opened.pauses,
                "run_states": {**opened.run_states, **(records.run_states if records else {})},
            }
        )
    return records


async def counted_replay(host: LiveReadHost, note: ReplayNote | None) -> ReplayNote | None:
    """Return *note* with the findings promoted past its cursor counted from the head.

    A reconnect carries its replay out in one call, so the frame drawn before it adopts
    the replay is the one frame the link shows ``REPLAYING``, with the route still as of
    the cursor. The count is read from the head the replay heads toward, never from what
    is held, so that frame says how many later findings exist without drawing any of
    them. A head that cannot be read leaves the count unknown rather than failing the
    replay.

    Args:
        host: The seam carrying the replay.
        note: The replay's start and head; ``None`` when the link replays without one.

    Returns:
        The note, with the count once the head was read.
    """
    read = LIVE_READS[CAMPAIGN_READ]
    address = read.address(host) if host.route in read.routes else None
    if note is None or address is None:
        return note
    try:
        head: HeldCampaign = await read.fetch(host, address)
    except Exception as exc:
        logger.warning(f"replay head unreadable campaign={address} cause={exc!r}")
        return note
    after = head.view.promoted_after(note.replaying_from_sequence)
    return replace(note, findings_promoted_after_cursor=len(after))


#: The read the cost ceiling route draws its ceiling, stops and spend from.
CEILING_READ: Final = SPEND_CEILING_READ_METHOD

#: The read the Run frame draws its usage pane from, and the route it draws it on.
USAGE_READ: Final = RUN_USAGE_READ_METHOD
RUN_USAGE_ROUTE: Final = "run.detail"


def _ceiling_address(_host: LiveReadHost) -> str:
    """Return what the ceiling read is about: the one governor of the attached tree."""
    return COST_CEILING_ROUTE


async def _fetch_ceiling(host: LiveReadHost, _address: str) -> CostCeilingView:
    """Read the governor ceiling, the live Runs' spend, the stops and spend by provider."""
    return CostCeilingView.model_validate(await host.call(CEILING_READ, {}))


def _usage_address(host: LiveReadHost) -> str | None:
    """Return the URN of the Run the Run frame or its transcript is about, once held."""
    held = host.projection_for(host.route)
    subject = host.subject
    if held is None or not subject:
        return None
    return next((row.urn for row in held.rows if row.key == subject), None)


async def _fetch_usage(host: LiveReadHost, urn: str) -> RunUsageView:
    """Read one Run's spend beside its sealed caps and its kind's typical duration."""
    return RunUsageView.model_validate(await host.call(USAGE_READ, {"urn": urn}))


def _health_address(host: LiveReadHost) -> str | None:
    """Return the health route itself once its rows are held: the verdicts are the tree's."""
    if host.route != HEALTH_ROUTE or host.projection_for(HEALTH_ROUTE) is None:
        return None
    return HEALTH_ROUTE


async def _fetch_verdicts(host: LiveReadHost, _address: str) -> tuple[RuntimeTupleVerdict, ...]:
    """Read the newest conformance verdict of every runtime tuple."""
    answer = HealthVerdictsAnswer.model_validate(await host.call(HEALTH_VERDICTS_READ_METHOD, {}))
    return tuple(
        RuntimeTupleVerdict(
            check=item.check, reason_code=item.reason_code, checked_at=item.checked_at
        )
        for item in answer.verdicts
    )


def _recovery_address(host: LiveReadHost) -> str | None:
    """Return the Recovery route once its rows are held: the last start is the daemon's."""
    if host.route != CRASH_RECOVERY_ROUTE or host.projection_for(CRASH_RECOVERY_ROUTE) is None:
        return None
    return CRASH_RECOVERY_ROUTE


async def _fetch_boot_recovery(host: LiveReadHost, _address: str) -> BootRecovery | None:
    """Read what the daemon's last start repaired and what that cost."""
    answer = BootRecoveryAnswer.model_validate(await host.call(BOOT_RECOVERY_READ_METHOD, {}))
    return answer.last


def _batch_address(route: str) -> Callable[[LiveReadHost], str | None]:
    """Return the address of the Batch *route* is about: its subject, else its first row."""

    def address(host: LiveReadHost) -> str | None:
        held = host.projection_for(route)
        if host.route != route or held is None or not held.rows:
            return None
        subject = host.subject
        if subject is None:
            return held.rows[0].urn
        return next((row.urn for row in held.rows if row.key == subject), None)

    return address


async def _fetch_generations(host: LiveReadHost, urn: str) -> tuple[IntegrationGeneration, ...]:
    """Read one Batch's integration generations, oldest first."""
    answer = GenerationsAnswer.model_validate(
        await host.call(GENERATIONS_READ_METHOD, {"urn": urn})
    )
    return answer.generations


async def _fetch_conflicts(host: LiveReadHost, urn: str) -> tuple[IntegrationConflict, ...]:
    """Read the conflict frames one Batch's blocked integrations left."""
    answer = ConflictFramesAnswer.model_validate(
        await host.call(CONFLICT_FRAMES_READ_METHOD, {"urn": urn})
    )
    return answer.conflicts


def _repository_address(host: LiveReadHost) -> str | None:
    """Return the tree while the Git surface is on screen: the checkout is the tree's."""
    return _TREE if host.route == GIT_PR_ROUTE else None


async def _fetch_repository(host: LiveReadHost, _address: str) -> RepositoryAnswer:
    """Read the tree's branch and the pull request open for it."""
    return RepositoryAnswer.model_validate(await host.call(REPOSITORY_READ_METHOD, {}))


def _receipt_address(host: LiveReadHost) -> str | None:
    """Return the receipt key the card is opened for, once the route's rows are held."""
    if host.route != RECEIPT_ROUTE or host.projection_for(RECEIPT_ROUTE) is None:
        return None
    return host.subject or None


async def _fetch_receipts(host: LiveReadHost, key: str) -> tuple[ProofReceipt, ...]:
    """Read the proof receipt filed under one receipt key."""
    answer = ProofReceiptsAnswer.model_validate(
        await host.call(PROOF_RECEIPTS_READ_METHOD, {"key": key})
    )
    return answer.receipts


def _unheld_subject(host: LiveReadHost) -> str | None:
    """Return the key the route on screen was opened onto when its held rows lack it.

    Only a key in entity grammar is asked about: a route whose subject is a position or a
    name rather than a record owes no resolution.
    """
    held = host.projection_for(host.route)
    subject = host.subject
    if held is None or not subject or ENTITY_KEY.fullmatch(subject) is None:
        return None
    return None if any(row.key == subject for row in held.rows) else subject


async def _fetch_resolution(host: LiveReadHost, key: str) -> str | None:
    """Read whether anything was ever written under *key*; its ending when nothing was."""
    answer = TargetResolution.model_validate(await host.call(TARGET_RESOLVE_METHOD, {"key": key}))
    return answer.ending


#: The read the health route draws its runtime tuple rows from.
HEALTH_VERDICTS_READ: Final = HEALTH_VERDICTS_READ_METHOD

#: The read the Recovery frame draws the daemon's last start from.
BOOT_RECOVERY_READ: Final = BOOT_RECOVERY_READ_METHOD

#: The read the Git surface draws a Batch's generations from.
GENERATIONS_READ: Final = GENERATIONS_READ_METHOD

#: The read the Git surface draws the tree's branch, review and checks from.
REPOSITORY_READ: Final = REPOSITORY_READ_METHOD

#: The read the conflict card draws its frames and hunks from.
CONFLICTS_READ: Final = CONFLICT_FRAMES_READ_METHOD

#: The read the receipt card draws its receipt from.
RECEIPTS_READ: Final = PROOF_RECEIPTS_READ_METHOD

#: The read that tells a route opened onto a key it does not hold whether the key names
#: anything at all; a key that names nothing opens the resolution card.
RESOLUTION_READ: Final = TARGET_RESOLVE_METHOD

#: The grammar of a record key, ``PREFIX-body``.
ENTITY_KEY: Final = re.compile(r"[A-Z][A-Z0-9]*-[A-Za-z0-9.-]+")

HISTORY_ROUTE: Final = "history"
HISTORY_DIFF_ROUTE: Final = "history.diff"

#: The read History lists the tree's newest changes from.
HISTORY_READ: Final = HISTORY_CHANGES_READ_METHOD

#: The read the diff draws one record's changes from.
HISTORY_DIFF_READ: Final = f"{HISTORY_CHANGES_READ_METHOD}@{HISTORY_DIFF_ROUTE}"


def _history_address(host: LiveReadHost) -> str | None:
    """Return the tree once History's rows are held: its feed is the whole tree's."""
    if host.route != HISTORY_ROUTE or host.projection_for(HISTORY_ROUTE) is None:
        return None
    return _TREE


async def _fetch_history(host: LiveReadHost, _address: str) -> ChangePage:
    """Read the newest page of the tree's change feed."""
    return ChangePage.model_validate(await host.call(HISTORY_CHANGES_READ_METHOD, {}))


def diff_subject(subject: str | None, rows_keys: list[str]) -> str | None:
    """Return the key the diff is about: its subject, else the first record held."""
    if subject and ENTITY_KEY.fullmatch(subject):
        return subject
    return rows_keys[0] if rows_keys else None


def _history_diff_address(host: LiveReadHost) -> str | None:
    """Return the key of the record the diff is about, once the route's rows are held."""
    held = host.projection_for(HISTORY_DIFF_ROUTE)
    if host.route != HISTORY_DIFF_ROUTE or held is None:
        return None
    return diff_subject(host.subject, [row.key for row in held.rows])


async def _fetch_record_history(host: LiveReadHost, key: str) -> ChangePage:
    """Read the newest page of one record's changes."""
    return ChangePage.model_validate(await host.call(HISTORY_CHANGES_READ_METHOD, {"key": key}))


def held_changes(live: Mapping[str, object], name: str) -> ChangePage | None:
    """Return the change page live read *name* holds, or ``None`` before its read."""
    page = live.get(name)
    return page if isinstance(page, ChangePage) else None


#: Every live read, by the name the seam owes it under.
LIVE_READS: Final[Mapping[str, LiveRead]] = MappingProxyType(
    {
        TRANSCRIPT_READ: LiveRead(
            routes=frozenset({TRANSCRIPT_ROUTE}),
            address=_transcript_address,
            fetch=_fetch_transcript,
        ),
        CAMPAIGN_READ: LiveRead(
            routes=CAMPAIGN_ROUTES, address=_campaign_address, fetch=_fetch_campaign
        ),
        ARTIFACT_READ: LiveRead(
            routes=frozenset({ARTIFACT_ROUTE}), address=_artifact_address, fetch=_fetch_artifact
        ),
        CEILING_READ: LiveRead(
            routes=frozenset({COST_CEILING_ROUTE}), address=_ceiling_address, fetch=_fetch_ceiling
        ),
        USAGE_READ: LiveRead(
            routes=frozenset({RUN_USAGE_ROUTE, TRANSCRIPT_ROUTE}),
            address=_usage_address,
            fetch=_fetch_usage,
        ),
        HEALTH_VERDICTS_READ: LiveRead(
            routes=frozenset({HEALTH_ROUTE}), address=_health_address, fetch=_fetch_verdicts
        ),
        BOOT_RECOVERY_READ: LiveRead(
            routes=frozenset({CRASH_RECOVERY_ROUTE}),
            address=_recovery_address,
            fetch=_fetch_boot_recovery,
        ),
        GENERATIONS_READ: LiveRead(
            routes=frozenset({GIT_PR_ROUTE}),
            address=_batch_address(GIT_PR_ROUTE),
            fetch=_fetch_generations,
        ),
        REPOSITORY_READ: LiveRead(
            routes=frozenset({GIT_PR_ROUTE}), address=_repository_address, fetch=_fetch_repository
        ),
        CONFLICTS_READ: LiveRead(
            routes=frozenset({MERGE_CONFLICT_ROUTE}),
            address=_batch_address(MERGE_CONFLICT_ROUTE),
            fetch=_fetch_conflicts,
        ),
        RECEIPTS_READ: LiveRead(
            routes=frozenset({RECEIPT_ROUTE}), address=_receipt_address, fetch=_fetch_receipts
        ),
        # every route served a projection may be opened onto a key it does not hold
        RESOLUTION_READ: LiveRead(
            routes=frozenset(ROUTE_COLLECTIONS), address=_unheld_subject, fetch=_fetch_resolution
        ),
        RUN_TIMELINE_READ: LiveRead(
            routes=frozenset({RUN_DETAIL_ROUTE}), address=_run_detail_address, fetch=_fetch_timeline
        ),
        LIVENESS_READ: LiveRead(
            routes=LIVENESS_ROUTES, address=_tree_address, fetch=_fetch_liveness
        ),
        DISPATCH_QUEUE_READ: LiveRead(
            routes=frozenset({UNATTENDED_ROUTE}), address=_tree_address, fetch=_fetch_dispatch_queue
        ),
        HISTORY_READ: LiveRead(
            routes=frozenset({HISTORY_ROUTE}), address=_history_address, fetch=_fetch_history
        ),
        HISTORY_DIFF_READ: LiveRead(
            routes=frozenset({HISTORY_DIFF_ROUTE}),
            address=_history_diff_address,
            fetch=_fetch_record_history,
        ),
        # the questions and pauses the Attention rows open, with what the daemon projected
        OPEN_RECORDS_READ: LiveRead(
            routes=frozenset({ATTENTION_ROUTE}),
            address=attention_address,
            fetch=fetch_open_records,
        ),
    }
)


__all__ = [
    "ARTIFACT_READ",
    "BOOT_RECOVERY_READ",
    "CAMPAIGN_READ",
    "CAMPAIGN_ROUTES",
    "CEILING_READ",
    "CONFLICTS_READ",
    "DISPATCH_QUEUE_READ",
    "GENERATIONS_READ",
    "HEALTH_VERDICTS_READ",
    "HISTORY_DIFF_READ",
    "HISTORY_READ",
    "LIVENESS_READ",
    "LIVENESS_ROUTES",
    "LIVE_READS",
    "RECEIPTS_READ",
    "REPOSITORY_READ",
    "RESOLUTION_READ",
    "RUN_TIMELINE_READ",
    "RUN_USAGE_ROUTE",
    "TRANSCRIPT_READ",
    "UNATTENDED_ROUTE",
    "USAGE_READ",
    "HeldArtifact",
    "HeldCampaign",
    "HeldTranscript",
    "LiveRead",
    "LiveReadHost",
    "counted_replay",
    "diff_subject",
    "held_changes",
    "held_queue",
    "held_records",
    "held_usage",
]
