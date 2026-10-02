"""The transcript read model: a Run's blocks in order, its holes, and what it is doing.

One route answers what an agent has been doing, and the thing that makes an answer
readable is that it never invents the parts it does not hold.

*Order comes from the stream, not from the file.* The blocks are the Run's event lines in
``run_sequence`` order, which is the order the daemon assigned; ledger order is arrival
order and the two differ whenever a line was retried. Quarantined lines are left out of
the run entirely, exactly as the reducer leaves them out of every other derivation: a
line retained as a diagnostic is not part of the history, and drawing it in sequence
would put a repudiated observation in the middle of the story.

*A hole is drawn, not skipped.* A recorded gap covers a range of sequences the daemon
never received, so the console does not hold them and never will. The range becomes a
block of its own in the position it occupies, carrying a value in the
:attr:`~eawf.kernel.projection.truth.TruthState.PURGED` state that names how many
sequences are missing. A transcript that silently renumbered around the hole would be
shorter than the episode it claims to show, and nothing on the frame would say so.

*The thinking state says whether it was observed or derived.* What exists is a
reasoning turn that opened and has not been summarized. When the provider marked the
opening itself, the turn is carried as a
:class:`~eawf.kernel.projection.truth.TruthField` whose kind is
:attr:`~eawf.kernel.projection.truth.TruthKind.OBSERVED`; when eawf inferred the opening
for a provider with no start marker, the kind is
:attr:`~eawf.kernel.projection.truth.TruthKind.DERIVED`, so a frame drawing it is drawing
an inference the read model owns rather than a fact a provider reported.

*A subagent is drawn from its own stream.* A delegation line names the child Run; what
the child is doing now, what it has found and that it reports back into this Run are read
off the child's own event lines, which the caller hands in beside the parent's. A child
whose lines the caller could not read says so rather than drawing a blank.

*A block unfolds to what its reference holds.* A tool result, a diff and a trace are
stored as bounded, scrubbed content and named by reference on the event line; the
caller resolves those references through the daemon's content read and hands the
answers in, and a block whose reference the caller holds content for unfolds to that
content, stating how many lines the store did not keep. A reference with no content
handed in is named instead.

Nothing here reads a ledger or a lock. The inputs are one validated
:class:`~eawf.kernel.projection.compute.RouteProjection`, the already-validated event
records the caller holds and the content it resolved for them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final

from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.projection.compute import PROJECTION_PRODUCER, RouteProjection
from eawf.kernel.projection.route_view import (
    RouteFieldSpec,
    RouteReadModel,
    build_route_read_model,
    check_field_tables,
    known_field,
    stated,
    status_and,
    unknown_field,
)
from eawf.kernel.projection.truth import (
    Freshness,
    Precision,
    TruthField,
    TruthKind,
    TruthState,
)
from eawf.kernel.runtime.content import ResolvedContent
from eawf.kernel.runtime.events import (
    ChildRunPayload,
    CommandPayload,
    ContextBoundaryPayload,
    ErrorPayload,
    EventGapPayload,
    FileChangePayload,
    MessageSummaryPayload,
    QuestionActionPayload,
    ReasoningSummaryPayload,
    RunEventKind,
    RunEventRecord,
    ToolPayload,
)
from eawf.kernel.state.enums import MeasurementQuality
from eawf.runtime.daemon.run_events import reduce_run_events

logger = logging.getLogger(__name__)

#: The family name a refusal from this module names.
FAMILY: Final = "transcript"

#: The one console route this module states a read model for.
TRANSCRIPT_ROUTE: Final = "transcript"

#: The routes of this family, stated as a tuple so the family check reads like the others.
TRANSCRIPT_ROUTES: Final[tuple[str, ...]] = (TRANSCRIPT_ROUTE,)

#: Why a Run states no outcome: its stored status is one it still leaves. What it cost is
#: the Run's usage read, which the frame draws beside the rows rather than a column of them.
RUN_NOT_ENDED: Final = "the Run has not ended"

#: Why a Run states no failure: it recorded none.
NO_FAILURE: Final = "the Run recorded no failure"

#: The revision every derived transcript cell states. A block is folded out of one line
#: that is never revised, so the first revision is the only one it stands at.
BLOCK_REVISION: Final = 1

#: What the transcript states while a reasoning turn is open.
THINKING: Final = "thinking"

#: Why the transcript states no thinking. The absence of an open turn is the whole
#: derivation, so the reason names it rather than blaming a missing producer.
NO_OPEN_TURN_REASON: Final = "no reasoning turn is open, so nothing states a thought in flight"

#: What a block stands for when its sequences were never received.
PURGED_REASON: Final = "sequences {first}-{last} never reached the daemon, so none is held"

#: What a reasoning block says before its summary exists.
OPEN_TURN_TEXT: Final = "a reasoning turn opened and has not been summarized"

#: Why a block states no text. A supported kind whose payload carries no renderable
#: sentence is a line the console holds and cannot read aloud.
NO_TEXT_REASON: Final = "this event kind states no text the transcript can render"

#: What each transcript row renders, in column order.
TRANSCRIPT_FIELDS: Final[Mapping[str, tuple[RouteFieldSpec, ...]]] = MappingProxyType(
    {
        TRANSCRIPT_ROUTE: status_and(
            stated("outcome", absent=RUN_NOT_ENDED),
            stated("failure", absent=NO_FAILURE),
        ),
    }
)


check_field_tables(family=FAMILY, routes=TRANSCRIPT_ROUTES, fields=TRANSCRIPT_FIELDS)


@dataclass(frozen=True, slots=True, kw_only=True)
class PurgedRange:
    """A run of sequences the console does not hold.

    Attributes:
        first: The first missing sequence.
        last: The last missing sequence, which is the observed sequence less one.
        gap_ref: The recorded gap's own identity.
        replay_requested: Whether the daemon asked the provider to replay the range.
        replay_available: Whether replay was available to ask for.
    """

    first: int
    last: int
    gap_ref: str
    replay_requested: bool
    replay_available: bool

    @property
    def count(self) -> int:
        """Return how many sequences the range covers; never fewer than one."""
        return self.last - self.first + 1


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentLine:
    """What a delegation block states about the child Run doing the work elsewhere.

    Attributes:
        child_key: The child Run's key.
        reports_to: The key of the Run the child reports back into: the one whose
            stream carries the delegation.
        readable: Whether the child's own lines were read. An unreadable child states
            neither what it does now nor what it found.
        now: What the child's latest block says; ``None`` before it produced one.
        found: What the child's latest assistant message says; ``None`` before one.
        outcome: How the child ended, once its terminal line landed; ``None`` while it
            still works.
    """

    child_key: str
    reports_to: str
    readable: bool
    now: str | None = None
    found: str | None = None
    outcome: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TranscriptBlock:
    """One block of a Run's transcript, in stream order.

    Attributes:
        sequence: The block's position in the Run's stream. A purged block carries the
            first missing sequence of its range.
        kind: The event kind the block was folded out of.
        at: When the daemon recorded the line. The provider's own stamp is not used: a
            worker that has stopped responding is in no position to be believed about
            what time it is.
        text: What the block says, or the token naming why it says nothing. A purged
            block's value is in the purged state rather than the unknown one, because
            the console knows precisely what is missing.
        purged: The range this block stands for, or ``None`` for an observed event.
        lane: What sort of work the block records, as the transcript names it: one of
            :data:`LANES`, read off the event kind.
        in_flight: Whether the work the block opened is still going: a command with no
            result yet, or a reasoning turn not yet summarized.
        background: Whether the block is a command detached into the background, which
            works elsewhere rather than holding the Run.
        typical_seconds: How long this kind of work usually takes -- a command family,
            a reasoning turn, a delegation -- derived from the finished instances of it
            in the same stream; ``None`` when none finished.
        delegation: What a delegation block states about its child Run, read from the
            child's own stream; ``None`` for every other block and for a delegation
            that names no child yet.
        body: The labelled lines the block folds away beyond its text, in order: what a
            tool call returned, the diff a file change is held at, what a question waits
            on, an error's code, retry class and trace. Every line is read off the
            event's own payload, or off the content its reference resolved to.
    """

    sequence: int
    kind: RunEventKind
    at: datetime
    text: TruthField[str]
    purged: PurgedRange | None = None
    lane: str = "event"
    in_flight: bool = False
    background: bool = False
    typical_seconds: int | None = None
    delegation: SubagentLine | None = None
    body: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class TranscriptReadModel(RouteReadModel):
    """The transcript route's read model: the projection's rows and the Run's blocks.

    Attributes:
        blocks: The Run's blocks in stream order, purged ranges in their own positions.
        purged: Every range the console does not hold, in stream order.
        thinking: Whether a reasoning turn is open; derived only when eawf inferred it.
        last_contiguous_sequence: The highest sequence with nothing missing in front of
            it; zero for a Run that has produced nothing.
        derivation_stopped: Whether a hole sits between the start of the stream and its
            end, which is what makes the block run shorter than the episode.
        quarantined: The lines retained as diagnostics and drawn by nothing.
    """

    blocks: tuple[TranscriptBlock, ...] = ()
    purged: tuple[PurgedRange, ...] = ()
    thinking: TruthField[str]
    last_contiguous_sequence: int = 0
    derivation_stopped: bool = False
    quarantined: int = 0

    def block_at(self, position: int) -> TranscriptBlock | None:
        """Return the block at a zero-based offset, or ``None`` past either end."""
        if position < 0 or position >= len(self.blocks):
            return None
        return self.blocks[position]

    def is_thinking(self) -> bool:
        """Return whether a reasoning turn is open right now."""
        return self.thinking.value == THINKING


def _derived(value: str, *, urn: str) -> TruthField[str]:
    """Return one stated transcript cell, labelled derived."""
    return known_field(value=value, urn=urn, revision=BLOCK_REVISION)


def _purged_field(purged: PurgedRange, *, urn: str) -> TruthField[str]:
    """Return the cell a purged range renders as: no value, and exactly why.

    The purged state is used rather than the unknown one on purpose. Unknown says the
    projection never learned a value; purged says the store does not hold one and the
    console can name the range it would have covered.
    """
    return TruthField[str](
        value=None,
        state=TruthState.PURGED,
        truth_kind=TruthKind.DERIVED,
        producer=PROJECTION_PRODUCER,
        producer_revision=BLOCK_REVISION,
        precision=Precision.UNAVAILABLE,
        measurement_quality=MeasurementQuality.UNAVAILABLE,
        freshness=Freshness.LIVE,
        provenance_refs=(urn,),
        missing_reason=PURGED_REASON.format(first=purged.first, last=purged.last),
    )


def block_text(event: RunEventRecord) -> TruthField[str]:
    """Return what one observed event says, or the cell naming why it says nothing.

    The payload union is declared to grow: forty event kinds are canonical and only some
    payload shapes exist. A kind whose payload states no sentence the transcript can
    read aloud comes back unknown with :data:`NO_TEXT_REASON` rather than blank, so the
    console keeps drawing a stream that has outgrown it instead of refusing to.

    Args:
        event: One live event line.

    Returns:
        The block's text as a derived truth field.
    """
    urn = event.event_ref
    payload = event.payload
    if isinstance(payload, ReasoningSummaryPayload):
        summary = payload.summary
        return _derived(summary if summary is not None else OPEN_TURN_TEXT, urn=urn)
    if isinstance(payload, CommandPayload):
        detail = f"{payload.command_family_ref} · {payload.execution} · {payload.phase}"
        if payload.outcome is not None:
            detail += f" · {payload.outcome}"
        return _derived(detail, urn=urn)
    if isinstance(payload, MessageSummaryPayload):
        return _derived(payload.summary, urn=urn)
    if isinstance(payload, ChildRunPayload):
        return _derived(_child_text(payload), urn=urn)
    text = _gateway_text(payload)
    if text is not None:
        return _derived(text, urn=urn)
    return unknown_field(urn=urn, revision=BLOCK_REVISION, reason=NO_TEXT_REASON)


#: The words each retry class of an error is drawn with.
RETRY_WORDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "never": "never · retrying cannot succeed",
        "after_input_change": "after the input changes",
        "after_policy_change": "after the policy changes",
        "transient_same_run": "transient · this Run may retry it",
        "new_linked_run": "in a new linked Run",
    }
)


def _gateway_text(payload: object) -> str | None:
    """Return the first line of a tool, file, question or error block; ``None`` otherwise."""
    if isinstance(payload, ToolPayload):
        outcome = ""
        if payload.phase == "result":
            outcome = " · succeeded" if payload.error_code is None else " · failed"
        return f"{payload.tool_id} · {payload.phase}{outcome}"
    if isinstance(payload, FileChangePayload):
        first, *rest = payload.changed_paths
        return first + (f" and {_more_paths(len(rest))}" if rest else "")
    if isinstance(payload, QuestionActionPayload):
        chosen = f" · {payload.choice_key}" if payload.choice_key is not None else ""
        return f"{payload.subject_ref.entity_key} · {payload.phase}{chosen}"
    if isinstance(payload, ErrorPayload):
        return payload.message
    if isinstance(payload, ContextBoundaryPayload):
        return _boundary_text(payload)
    return None


def _boundary_text(payload: ContextBoundaryPayload) -> str:
    """Return what a compaction boundary says about the Run's anchors."""
    if payload.boundary == "compacting":
        return f"context compacting · {payload.anchors.receipt_count} receipts held"
    if payload.drift:
        return f"context resumed · contract mismatch: {', '.join(payload.drift)}"
    return "context resumed · anchors unchanged"


def _more_paths(count: int) -> str:
    """Return ``1 more path`` or ``N more paths``."""
    return f"{count} more path" + ("" if count == 1 else "s")


#: What an unfolded block says for content whose source returned nothing.
EMPTY_CONTENT: Final = "nothing was returned"


def content_refs(event: RunEventRecord) -> tuple[str, ...]:
    """Return the references one event line's block unfolds to.

    A tool result is resolved by its call, which names its output when it ran and its
    trace when it failed; a file change by its diff; an error by its trace.

    Args:
        event: One live event line.

    Returns:
        The references, empty for a line that holds no stored content.
    """
    payload = event.payload
    if isinstance(payload, ToolPayload) and payload.phase == "result":
        return (payload.call_ref,)
    if isinstance(payload, FileChangePayload):
        return (payload.diff_ref,)
    if isinstance(payload, ErrorPayload) and payload.diagnostic_ref is not None:
        return (payload.diagnostic_ref,)
    return ()


def _content_rows(label: str, held: ResolvedContent) -> tuple[tuple[str, str], ...]:
    """Return resolved content as body rows: the label on its first line, then the rest."""
    first, *rest = held.lines or (EMPTY_CONTENT,)
    rows = [(label, first), *(("", line) for line in rest)]
    if held.unkept:
        rows.append(
            ("", f"{held.unkept} more {'line was' if held.unkept == 1 else 'lines were'} not kept")
        )
    return tuple(rows)


def _deadline_words(subject: QualifiedUrn, deadline: datetime | None) -> str:
    """Return when a wait ends: its deadline in UTC, ``open``, or that it went unread.

    A provider permission always has a deadline, the provider's; a question has none,
    since nothing in the machine expires one, so it is ``open``. A permission whose
    deadline the caller could not read says so rather than claiming it is open.
    """
    if deadline is not None:
        return f"deadline {deadline.astimezone(UTC):%H:%M:%S} UTC"
    if subject.kind is EntityKind.PERMISSION:
        return "deadline not read"
    return "open"


def block_body(
    event: RunEventRecord,
    contents: Mapping[str, ResolvedContent] = MappingProxyType({}),
    *,
    deadline: datetime | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return the labelled lines a block folds away beyond its first line.

    A tool call states what it returned or the gateway's error and the trace it failed
    with; a file change its paths, its diff and the trees on either side; a question
    what it waits on and until when, or how it was answered; an error its code, what a
    retry would take and its trace. Stored content is inlined when the caller resolved
    it and named otherwise: this module reads no store.

    Args:
        event: One live event line.
        contents: The content the caller resolved, by the reference
            :func:`content_refs` names.
        deadline: When the wait a question or approval block opens ends, as its record
            states it; ``None`` when the record sets none or was not read.

    Returns:
        The lines in order, each a label and its value; empty for every other kind.
    """
    payload = event.payload
    held = next((contents[ref] for ref in content_refs(event) if ref in contents), None)
    if isinstance(payload, ToolPayload):
        error = () if payload.error_code is None else (("ERROR", payload.error_code.value),)
        if held is not None:
            return (*error, *_content_rows("OUTPUT", held))
        if payload.result_ref is not None:
            return (("OUTPUT", f"held under receipt {payload.result_ref}"),)
        return error
    if isinstance(payload, FileChangePayload):
        summary = " · a summary, not the full hunks" if payload.summary_only else ""
        diff = (
            _content_rows("DIFF", held)
            if held is not None
            else (("DIFF", f"held at {payload.diff_ref}{summary}"),)
        )
        return (
            ("PATHS", ", ".join(payload.changed_paths)),
            *diff,
            ("BEFORE", payload.before_tree_digest),
            ("AFTER", payload.after_tree_digest),
        )
    if isinstance(payload, QuestionActionPayload):
        subject = payload.subject_ref
        if payload.receipt_ref is None:
            ends = _deadline_words(subject, deadline)
            return (("WAITS ON", f"an answer to {subject.entity_key} · {ends}"),)
        chosen = f"{payload.choice_key} · " if payload.choice_key is not None else ""
        return (("ANSWERED", f"{chosen}under receipt {payload.receipt_ref}"),)
    if isinstance(payload, ErrorPayload):
        trace = payload.diagnostic_ref
        traced = (
            _content_rows("TRACE", held)
            if held is not None
            else (("TRACE", f"held at {trace}" if trace is not None else "no trace was kept"),)
        )
        return (("CODE", payload.code), ("RETRY", RETRY_WORDS[payload.retry_class]), *traced)
    return ()


def _child_text(payload: ChildRunPayload) -> str:
    """Return what a delegation block says: which child, and that it works elsewhere.

    The child's own words are never folded in here; they live on the child's stream,
    which is the whole point of drawing the delegation as one block.
    """
    child = "a subagent" if payload.child_run_ref is None else payload.child_run_ref.entity_key
    detail = f"{child} · {payload.phase}"
    if payload.terminal_status is not None:
        detail += f" · {payload.terminal_status.value.lower()}"
    return f"{detail} · working elsewhere"


def _purged_range(payload: EventGapPayload) -> PurgedRange:
    """Return the range one recorded gap covers.

    The gap states the sequence it expected and the one that arrived instead, so the
    missing run ends one before the observed sequence.
    """
    return PurgedRange(
        first=payload.expected_sequence,
        last=payload.observed_sequence - 1,
        gap_ref=payload.gap_ref,
        replay_requested=payload.replay_requested,
        replay_available=payload.replay_capability_state == "verified",
    )


def open_reasoning_turn(events: Sequence[RunEventRecord]) -> RunEventRecord | None:
    """Return the event that opened the reasoning turn still open, or ``None``.

    A turn opens on ``reasoning_started`` and closes on ``reasoning_summarized``. The
    walk is over the stream in sequence order rather than in arrival order, and it skips
    quarantined lines, so a repudiated line neither opens a turn nor closes one.

    Args:
        events: The Run's event lines, in any order.

    Returns:
        The opening event of the turn that has not been summarized, or ``None`` when
        every turn was closed and when none was ever opened.
    """
    opened: RunEventRecord | None = None
    for event in _ordered(events):
        if event.event_kind is RunEventKind.REASONING_STARTED:
            opened = event
        elif event.event_kind is RunEventKind.REASONING_SUMMARIZED:
            opened = None
    return opened


def _ordered(events: Sequence[RunEventRecord]) -> tuple[RunEventRecord, ...]:
    """Return the stream's live lines in sequence order, quarantined lines dropped."""
    return tuple(
        sorted(
            (event for event in events if event.quarantine is None),
            key=lambda event: event.run_sequence,
        )
    )


def _thinking_field(events: Sequence[RunEventRecord], *, scope_id: str) -> TruthField[str]:
    """Return whether a reasoning turn is open, observed or derived as its opener says.

    A provider that marks the start of its reasoning states the turn in a typed event, so
    the open turn is observed. A provider with no start marker leaves eawf to infer the
    opening line, and only then is the state derived.
    """
    opened = open_reasoning_turn(events)
    if opened is None:
        return unknown_field(urn=scope_id, revision=BLOCK_REVISION, reason=NO_OPEN_TURN_REASON)
    field = _derived(THINKING, urn=opened.event_ref)
    if opened.provenance == "eawf_derived":
        return field
    return field.model_validate(field.model_dump() | {"truth_kind": TruthKind.OBSERVED})


#: What sort of work each event kind records, as the transcript's kind column names it.
#: A kind not listed is drawn as a plain event rather than refused.
LANES: Final[Mapping[RunEventKind, str]] = MappingProxyType(
    {
        RunEventKind.MESSAGE_SUMMARIZED: "message",
        RunEventKind.PLAN_UPDATED: "message",
        RunEventKind.TOOL_REQUESTED: "tool",
        RunEventKind.TOOL_ACCEPTED: "tool",
        RunEventKind.TOOL_RESULT: "tool",
        RunEventKind.COMMAND_STARTED: "tool",
        RunEventKind.COMMAND_OUTPUT: "tool",
        RunEventKind.COMMAND_RESULT: "tool",
        RunEventKind.FILE_CHANGED: "file",
        RunEventKind.DIFF_SUMMARIZED: "file",
        RunEventKind.QUESTION_RAISED: "question",
        RunEventKind.APPROVAL_REQUESTED: "question",
        RunEventKind.APPROVAL_RESOLVED: "question",
        RunEventKind.ERROR_OBSERVED: "error",
        RunEventKind.REASONING_STARTED: "thinking",
        RunEventKind.REASONING_SUMMARIZED: "thinking",
        RunEventKind.CHILD_RUN_REQUESTED: "subagent",
        RunEventKind.CHILD_RUN_STARTED: "subagent",
        RunEventKind.CHILD_RUN_TERMINAL: "subagent",
        RunEventKind.HEARTBEAT: "heartbeat",
        RunEventKind.EVENT_GAP: "purged",
    }
)

#: The lane an event kind the table does not name is drawn in.
EVENT_LANE: Final = "event"


def _median(values: Sequence[int]) -> int:
    """Return the middle of ``values``, the lower middle for an even count."""
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


#: The duration key reasoning turns are compared under; command families use their own id.
_THINKING_KEY: Final = "reasoning"

#: The duration key delegations are compared under.
_SUBAGENT_KEY: Final = "delegation"

#: The duration key questions are compared under, from raised to answered.
_QUESTION_KEY: Final = "question"


def _elapsed(begun: RunEventRecord, ended: RunEventRecord) -> int:
    return max(0, int((ended.recorded_at - begun.recorded_at).total_seconds()))


def _span(payload: object) -> tuple[str, str, bool] | None:
    """Return how one line opens or closes a span of work, or ``None`` for neither.

    The answer is the span's own key, the key its kind of work is timed under, and
    whether the line opens it: a command by its execution, a delegation by its request,
    and the one reasoning turn a stream can hold open at a time.
    """
    if isinstance(payload, ReasoningSummaryPayload):
        return _THINKING_KEY, _THINKING_KEY, payload.phase == "started"
    if isinstance(payload, ChildRunPayload) and payload.phase != "requested":
        return payload.delegation_request_ref, _SUBAGENT_KEY, payload.phase == "started"
    if isinstance(payload, CommandPayload) and payload.phase != "output":
        return payload.command_ref, payload.command_family_ref, payload.phase == "started"
    if isinstance(payload, ToolPayload) and payload.phase != "requested":
        return payload.call_ref, payload.tool_id, payload.phase == "accepted"
    if isinstance(payload, QuestionActionPayload):
        return str(payload.subject_ref), _QUESTION_KEY, payload.phase != "resolved"
    return None


def _in_flight(events: tuple[RunEventRecord, ...]) -> tuple[set[str], dict[str, int]]:
    """Return the events whose work is still going, and each kind of work's typical time.

    A command is in flight from its start until a result names the same execution, a
    child Run from its start until its terminal line names the same delegation, and a
    reasoning turn until it is summarized. The typical time of a kind of work is the
    median of its finished instances in the stream: per command family, and one each for
    reasoning turns and delegations.
    """
    opened: dict[str, RunEventRecord] = {}
    durations: dict[str, list[int]] = {}
    for event in events:
        span = _span(event.payload)
        if span is None:
            continue
        key, kind, opens = span
        if opens:
            opened[key] = event
        elif key in opened:
            durations.setdefault(kind, []).append(_elapsed(opened.pop(key), event))
    going = {event.event_ref for event in opened.values()}
    typical = {kind: _median(times) for kind, times in durations.items()}
    return going, typical


def _typical_key(payload: object) -> str | None:
    """Return the key a block's kind of work is compared under, or ``None`` for none."""
    if isinstance(payload, CommandPayload):
        return payload.command_family_ref
    if isinstance(payload, ReasoningSummaryPayload):
        return _THINKING_KEY
    if isinstance(payload, ChildRunPayload):
        return _SUBAGENT_KEY
    if isinstance(payload, ToolPayload):
        return payload.tool_id
    if isinstance(payload, QuestionActionPayload):
        return _QUESTION_KEY
    return None


def _latest_found(events: tuple[RunEventRecord, ...]) -> str | None:
    """Return what the child's latest assistant message says, or ``None`` before one."""
    for event in reversed(events):
        payload = event.payload
        if isinstance(payload, MessageSummaryPayload) and payload.message_role == "assistant":
            return payload.summary
    return None


def _delegation(
    payload: ChildRunPayload,
    *,
    reports_to: str,
    outcomes: Mapping[str, str],
    children: Mapping[str, Sequence[RunEventRecord]],
) -> SubagentLine | None:
    """Return what a delegation block states about its child, read off the child's lines.

    Args:
        payload: The delegation line's payload.
        reports_to: The key of the Run whose stream carries the delegation.
        outcomes: How each ended delegation's child ended, by delegation.
        children: The child Runs' own event lines, by child URN, as the caller read them.

    Returns:
        The line, or ``None`` for a delegation that names no child yet.
    """
    if payload.child_run_ref is None:
        return None
    child = str(payload.child_run_ref)
    key = payload.child_run_ref.entity_key
    outcome = outcomes.get(payload.delegation_request_ref)
    held = children.get(child)
    if held is None:
        return SubagentLine(child_key=key, reports_to=reports_to, readable=False, outcome=outcome)
    lines = tuple(e for e in _ordered(held) if not isinstance(e.payload, EventGapPayload))
    return SubagentLine(
        child_key=key,
        reports_to=reports_to,
        readable=True,
        now=block_text(lines[-1]).value if lines else None,
        found=_latest_found(lines),
        outcome=outcome,
    )


def _subject_deadline(payload: object, deadlines: Mapping[str, datetime]) -> datetime | None:
    """Return the deadline of the record a question or approval block waits on, if held."""
    if isinstance(payload, QuestionActionPayload):
        return deadlines.get(str(payload.subject_ref))
    return None


def build_transcript_blocks(
    events: Sequence[RunEventRecord],
    *,
    children: Mapping[str, Sequence[RunEventRecord]] = MappingProxyType({}),
    contents: Mapping[str, ResolvedContent] = MappingProxyType({}),
    deadlines: Mapping[str, datetime] = MappingProxyType({}),
) -> tuple[tuple[TranscriptBlock, ...], tuple[PurgedRange, ...]]:
    """Return the Run's blocks in stream order and the ranges the console does not hold.

    Args:
        events: The Run's event lines, in any order. Quarantined lines are dropped.
        children: The event lines of the child Runs this Run delegated to, by child URN.
            A delegation whose child is absent here is drawn as unreadable.
        contents: The content the Run's references resolved to, by reference.
        deadlines: When each record a question or approval waits on stops waiting, by
            the record's URN, as the caller read it.

    Returns:
        One block per live line, a recorded gap becoming a purged block in its own
        position, and the purged ranges on their own in the same order.
    """
    blocks: list[TranscriptBlock] = []
    purged: list[PurgedRange] = []
    ordered = _ordered(events)
    going, typical = _in_flight(ordered)
    reports_to = ordered[0].run_ref.entity_key if ordered else ""
    outcomes = {
        e.payload.delegation_request_ref: e.payload.terminal_status.value.lower()
        for e in ordered
        if isinstance(e.payload, ChildRunPayload) and e.payload.terminal_status is not None
    }
    for event in ordered:
        payload = event.payload
        if isinstance(payload, EventGapPayload):
            covered = _purged_range(payload)
            purged.append(covered)
            blocks.append(
                TranscriptBlock(
                    sequence=event.run_sequence,
                    kind=event.event_kind,
                    at=event.recorded_at,
                    text=_purged_field(covered, urn=event.event_ref),
                    purged=covered,
                    lane=LANES[RunEventKind.EVENT_GAP],
                )
            )
            continue
        blocks.append(
            TranscriptBlock(
                sequence=event.run_sequence,
                kind=event.event_kind,
                at=event.recorded_at,
                text=block_text(event),
                lane=LANES.get(event.event_kind, EVENT_LANE),
                in_flight=event.event_ref in going,
                background=isinstance(payload, CommandPayload)
                and payload.execution == "background",
                typical_seconds=typical.get(_typical_key(payload) or ""),
                delegation=(
                    _delegation(
                        payload, reports_to=reports_to, outcomes=outcomes, children=children
                    )
                    if isinstance(payload, ChildRunPayload)
                    else None
                ),
                body=block_body(event, contents, deadline=_subject_deadline(payload, deadlines)),
            )
        )
    logger.debug(f"build_transcript_blocks blocks={len(blocks)} purged={len(purged)}")
    return tuple(blocks), tuple(purged)


def build_transcript_view(
    projection: RouteProjection,
    *,
    events: Sequence[RunEventRecord] = (),
    children: Mapping[str, Sequence[RunEventRecord]] = MappingProxyType({}),
    contents: Mapping[str, ResolvedContent] = MappingProxyType({}),
    deadlines: Mapping[str, datetime] = MappingProxyType({}),
) -> TranscriptReadModel:
    """Return the read model the transcript route draws from one served projection.

    Args:
        projection: The route projection the daemon answered, already validated.
        events: The Run's event lines, in any order; empty for a Run that has produced
            nothing, which draws no block rather than a block saying nothing.
        children: The event lines of the Runs this Run delegated to, by child URN.
        contents: The content the Run's references resolved to, by reference.
        deadlines: When each record a question or approval waits on stops waiting, by
            the record's URN.

    Returns:
        The route's rows, the Run's blocks, the ranges the console does not hold, and
        the derived thinking state.

    Raises:
        ValueError: The projection is for another route, or the live lines are not a
            run of sequences once every recorded gap is accounted for. An unexplained
            hole means a line was lost, and folding over the survivors would draw a
            transcript that skips it without saying so.
    """
    model = build_route_read_model(projection, family=FAMILY, fields=TRANSCRIPT_FIELDS)
    state = reduce_run_events(events)
    blocks, purged = build_transcript_blocks(
        events, children=children, contents=contents, deadlines=deadlines
    )
    return TranscriptReadModel(
        route=model.route,
        read_model=model.read_model,
        scope_id=model.scope_id,
        source_cursor=model.source_cursor,
        digest=model.digest,
        complete=model.complete,
        rows=model.rows,
        counts=model.counts,
        specs=model.specs,
        blocks=blocks,
        purged=purged,
        thinking=_thinking_field(events, scope_id=model.scope_id),
        last_contiguous_sequence=state.last_contiguous_sequence,
        derivation_stopped=state.derivation_stopped,
        quarantined=len(state.quarantined),
    )


__all__ = [
    "BLOCK_REVISION",
    "EMPTY_CONTENT",
    "EVENT_LANE",
    "FAMILY",
    "LANES",
    "NO_FAILURE",
    "NO_OPEN_TURN_REASON",
    "NO_TEXT_REASON",
    "OPEN_TURN_TEXT",
    "PURGED_REASON",
    "RETRY_WORDS",
    "RUN_NOT_ENDED",
    "THINKING",
    "TRANSCRIPT_FIELDS",
    "TRANSCRIPT_ROUTE",
    "TRANSCRIPT_ROUTES",
    "PurgedRange",
    "SubagentLine",
    "TranscriptBlock",
    "TranscriptReadModel",
    "block_body",
    "block_text",
    "build_transcript_blocks",
    "build_transcript_view",
    "content_refs",
    "open_reasoning_turn",
]
