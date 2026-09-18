"""Run events: the closed kind vocabulary and the payload each kind carries.

A Run event is one observation of an episode, and the thing that makes a
stream of them readable is that the kind and the payload cannot disagree.
:data:`EVENT_CONTRACTS` is the total map that binds them -- compiled at
import through :func:`compile_event_contracts`, so a kind nobody gave a
payload, or one whose declared phase its payload does not admit, is a
startup failure rather than a line that appends and then renders as
something it is not.

The phase is the half of that contract a reader forgets. ``command`` and
``reasoning_summary`` each carry a ``phase`` field whose value the event
kind already fixes: ``reasoning_summarized`` is the summarized phase and
nothing else, ``command_started`` is the started phase and nothing else.
Left unchecked, a provider could append a ``reasoning_summarized`` event
whose payload says ``started``, and a transcript would then show a
finished thought with no text in it. The pinning is in the record's own
validator, so the pairing is refused before the line is written.

Ordering is a separate concern from vocabulary and lives with the daemon
that assigns it. What this module contributes is the shape: a record
carries its own ``run_sequence``, an ``event_gap`` record explains a
range of missing ones, and a quarantined record explains itself with a
typed reason. Derivation stops at the first gap, so a hole is a stated
fact rather than a silently shorter history.

Not every declared kind is constructible yet. :data:`EVENT_PAYLOADS`
names a payload kind for all forty, because the vocabulary is the closed
canonical one and a partial enum would have to be edited -- in a
persisted vocabulary -- the first time an adapter emitted a kind it
lacked. :data:`SUPPORTED_EVENT_KINDS` is the subset whose payload model
exists, and an append naming any other kind is refused at the boundary
rather than parsed into an approximation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StringConstraints, model_validator

from eawf.kernel.runtime.compiled import BoundedText
from eawf.kernel.runtime.provider import ArtifactUrn, CommandFamilyId, RuntimeRecord
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime


def _identifier(prefix: str) -> StringConstraints:
    """Return the grammar of a prefixed, hex-bodied runtime identifier."""
    return StringConstraints(strict=True, pattern=rf"^{prefix}-[0-9a-f]{{8,32}}$")


#: One observed Run event. The worker mints it and repeats it on a retry,
#: so it is the key a duplicate append is recognised by.
RunEventId = Annotated[str, _identifier("EVT")]

#: One recorded hole in a Run's event stream.
EventGapId = Annotated[str, _identifier("GAP")]

#: One command execution a command event reports on.
CommandExecutionId = Annotated[str, _identifier("CMD")]


class RunEventKind(StrEnum):
    """The closed set of things that happen to a Run.

    ``reasoning_started`` and ``command_started`` are the two that make a
    live episode readable without guessing: a provider that streams a
    reasoning start marker emits the first, and a detached background
    command in flight is the second rather than an inference drawn from
    the absence of a result.
    """

    ALLOCATION_STARTED = "allocation_started"
    PROVIDER_ACCEPTED = "provider_accepted"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    TURN_STARTED = "turn_started"
    TURN_ENDED = "turn_ended"
    MESSAGE_SUMMARIZED = "message_summarized"
    PLAN_UPDATED = "plan_updated"
    REASONING_STARTED = "reasoning_started"
    REASONING_SUMMARIZED = "reasoning_summarized"
    CHILD_RUN_REQUESTED = "child_run_requested"
    CHILD_RUN_STARTED = "child_run_started"
    CHILD_RUN_TERMINAL = "child_run_terminal"
    TOOL_REQUESTED = "tool_requested"
    TOOL_ACCEPTED = "tool_accepted"
    TOOL_RESULT = "tool_result"
    COMMAND_STARTED = "command_started"
    COMMAND_OUTPUT = "command_output"
    COMMAND_RESULT = "command_result"
    FILE_CHANGED = "file_changed"
    DIFF_SUMMARIZED = "diff_summarized"
    QUESTION_RAISED = "question_raised"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    USAGE_OBSERVED = "usage_observed"
    BUDGET_WARNING = "budget_warning"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CHECKPOINT_CREATED = "checkpoint_created"
    REROUTED = "rerouted"
    ERROR_OBSERVED = "error_observed"
    HEARTBEAT = "heartbeat"
    CONTEXT_BOUNDARY = "context_boundary"
    CONTROL_REQUESTED = "control_requested"
    CONTROL_ACKNOWLEDGED = "control_acknowledged"
    CONTROL_EFFECTED = "control_effected"
    RECONCILIATION_COMPLETED = "reconciliation_completed"
    PROVIDER_LOST = "provider_lost"
    EVENT_GAP = "event_gap"
    CAPABILITY_REVOKED = "capability_revoked"
    RUN_TRANSITION = "run_transition"


class EventPayloadKind(StrEnum):
    """The closed set of payload shapes a Run event may carry."""

    RUN_TRANSITION = "run_transition"
    PROVIDER_LIFECYCLE = "provider_lifecycle"
    PROVIDER_LOSS = "provider_loss"
    MESSAGE_SUMMARY = "message_summary"
    PLAN_UPDATE = "plan_update"
    REASONING_SUMMARY = "reasoning_summary"
    CHILD_RUN = "child_run"
    TOOL = "tool"
    COMMAND = "command"
    FILE_CHANGE = "file_change"
    QUESTION_ACTION = "question_action"
    USAGE = "usage"
    CHECKPOINT = "checkpoint"
    REROUTE = "reroute"
    ERROR = "error"
    HEARTBEAT = "heartbeat"
    CONTEXT_BOUNDARY = "context_boundary"
    CONTROL = "control"
    RECONCILIATION = "reconciliation"
    EVENT_GAP = "event_gap"
    CAPABILITY = "capability"


class QuarantineReason(StrEnum):
    """Why an event was retained as a diagnostic instead of derived from.

    A quarantined event is still on the ledger. It is kept out of the
    contiguity walk, out of the liveness clock and out of every
    derivation, because the thing that made it suspect -- arriving after
    the Run ended, or claiming a sequence another event already holds --
    is exactly the thing that would corrupt those answers.
    """

    LATE_AFTER_TERMINAL = "late_after_terminal"
    SEQUENCE_CONFLICT = "sequence_conflict"


#: Where an event came from. A provider reports its own facts, the daemon
#: reports what it observed about the stream, and the two are never mixed.
EventProvenance = Literal["provider_native", "eawf_derived", "daemon_observed", "operator"]

#: The phase vocabulary of each payload kind, empty where the payload has
#: no phase field at all. Declared here so the kind-to-phase pinning is
#: checked against the payload rather than against a second opinion.
_PAYLOAD_PHASES: Final[Mapping[EventPayloadKind, tuple[str, ...]]] = {
    EventPayloadKind.RUN_TRANSITION: (),
    EventPayloadKind.PROVIDER_LIFECYCLE: ("allocation", "acceptance", "session", "turn"),
    EventPayloadKind.PROVIDER_LOSS: (),
    EventPayloadKind.MESSAGE_SUMMARY: (),
    EventPayloadKind.PLAN_UPDATE: (),
    EventPayloadKind.REASONING_SUMMARY: ("started", "summarized"),
    EventPayloadKind.CHILD_RUN: ("requested", "started", "terminal"),
    EventPayloadKind.TOOL: ("requested", "accepted", "result"),
    EventPayloadKind.COMMAND: ("started", "output", "result"),
    EventPayloadKind.FILE_CHANGE: (),
    EventPayloadKind.QUESTION_ACTION: ("raised", "requested", "resolved"),
    EventPayloadKind.USAGE: (),
    EventPayloadKind.CHECKPOINT: (),
    EventPayloadKind.REROUTE: (),
    EventPayloadKind.ERROR: (),
    EventPayloadKind.HEARTBEAT: (),
    EventPayloadKind.CONTEXT_BOUNDARY: (),
    EventPayloadKind.CONTROL: ("requested", "acknowledged", "effected"),
    EventPayloadKind.RECONCILIATION: (),
    EventPayloadKind.EVENT_GAP: (),
    EventPayloadKind.CAPABILITY: (),
}


@dataclass(frozen=True, slots=True)
class EventContract:
    """What one event kind is allowed to carry.

    Attributes:
        payload_kind: The one payload shape the kind admits.
        phase: The payload phase the kind pins, or ``None`` when the
            payload carries no phase.
    """

    payload_kind: EventPayloadKind
    phase: str | None


#: Which payload each kind carries and which phase of it, as declared.
#: Compiled through :func:`compile_event_contracts`.
_DECLARED_EVENT_CONTRACTS: Final[Mapping[RunEventKind, tuple[EventPayloadKind, str | None]]] = {
    RunEventKind.ALLOCATION_STARTED: (EventPayloadKind.PROVIDER_LIFECYCLE, "allocation"),
    RunEventKind.PROVIDER_ACCEPTED: (EventPayloadKind.PROVIDER_LIFECYCLE, "acceptance"),
    RunEventKind.SESSION_STARTED: (EventPayloadKind.PROVIDER_LIFECYCLE, "session"),
    RunEventKind.SESSION_ENDED: (EventPayloadKind.PROVIDER_LIFECYCLE, "session"),
    RunEventKind.TURN_STARTED: (EventPayloadKind.PROVIDER_LIFECYCLE, "turn"),
    RunEventKind.TURN_ENDED: (EventPayloadKind.PROVIDER_LIFECYCLE, "turn"),
    RunEventKind.MESSAGE_SUMMARIZED: (EventPayloadKind.MESSAGE_SUMMARY, None),
    RunEventKind.PLAN_UPDATED: (EventPayloadKind.PLAN_UPDATE, None),
    RunEventKind.REASONING_STARTED: (EventPayloadKind.REASONING_SUMMARY, "started"),
    RunEventKind.REASONING_SUMMARIZED: (EventPayloadKind.REASONING_SUMMARY, "summarized"),
    RunEventKind.CHILD_RUN_REQUESTED: (EventPayloadKind.CHILD_RUN, "requested"),
    RunEventKind.CHILD_RUN_STARTED: (EventPayloadKind.CHILD_RUN, "started"),
    RunEventKind.CHILD_RUN_TERMINAL: (EventPayloadKind.CHILD_RUN, "terminal"),
    RunEventKind.TOOL_REQUESTED: (EventPayloadKind.TOOL, "requested"),
    RunEventKind.TOOL_ACCEPTED: (EventPayloadKind.TOOL, "accepted"),
    RunEventKind.TOOL_RESULT: (EventPayloadKind.TOOL, "result"),
    RunEventKind.COMMAND_STARTED: (EventPayloadKind.COMMAND, "started"),
    RunEventKind.COMMAND_OUTPUT: (EventPayloadKind.COMMAND, "output"),
    RunEventKind.COMMAND_RESULT: (EventPayloadKind.COMMAND, "result"),
    RunEventKind.FILE_CHANGED: (EventPayloadKind.FILE_CHANGE, None),
    RunEventKind.DIFF_SUMMARIZED: (EventPayloadKind.FILE_CHANGE, None),
    RunEventKind.QUESTION_RAISED: (EventPayloadKind.QUESTION_ACTION, "raised"),
    RunEventKind.APPROVAL_REQUESTED: (EventPayloadKind.QUESTION_ACTION, "requested"),
    RunEventKind.APPROVAL_RESOLVED: (EventPayloadKind.QUESTION_ACTION, "resolved"),
    RunEventKind.USAGE_OBSERVED: (EventPayloadKind.USAGE, None),
    RunEventKind.BUDGET_WARNING: (EventPayloadKind.USAGE, None),
    RunEventKind.BUDGET_EXHAUSTED: (EventPayloadKind.USAGE, None),
    RunEventKind.CHECKPOINT_CREATED: (EventPayloadKind.CHECKPOINT, None),
    RunEventKind.REROUTED: (EventPayloadKind.REROUTE, None),
    RunEventKind.ERROR_OBSERVED: (EventPayloadKind.ERROR, None),
    RunEventKind.HEARTBEAT: (EventPayloadKind.HEARTBEAT, None),
    RunEventKind.CONTEXT_BOUNDARY: (EventPayloadKind.CONTEXT_BOUNDARY, None),
    RunEventKind.CONTROL_REQUESTED: (EventPayloadKind.CONTROL, "requested"),
    RunEventKind.CONTROL_ACKNOWLEDGED: (EventPayloadKind.CONTROL, "acknowledged"),
    RunEventKind.CONTROL_EFFECTED: (EventPayloadKind.CONTROL, "effected"),
    RunEventKind.RECONCILIATION_COMPLETED: (EventPayloadKind.RECONCILIATION, None),
    RunEventKind.PROVIDER_LOST: (EventPayloadKind.PROVIDER_LOSS, None),
    RunEventKind.EVENT_GAP: (EventPayloadKind.EVENT_GAP, None),
    RunEventKind.CAPABILITY_REVOKED: (EventPayloadKind.CAPABILITY, None),
    RunEventKind.RUN_TRANSITION: (EventPayloadKind.RUN_TRANSITION, None),
}


def compile_event_contracts(
    declared: Mapping[RunEventKind, tuple[EventPayloadKind, str | None]],
    *,
    phases: Mapping[EventPayloadKind, Iterable[str]],
) -> Mapping[RunEventKind, EventContract]:
    """Compile *declared* into the total kind-to-payload-and-phase map.

    Args:
        declared: The payload kind and pinned phase of each event kind.
        phases: The phase vocabulary of each payload kind, empty for a
            payload that carries no phase.

    Returns:
        A read-only mapping covering every event kind.

    Raises:
        ValueError: An event kind has no row, so whichever reducer read
            it would decide for itself what it may carry; a payload kind
            has no phase vocabulary, so the pinning is unchecked; a row
            pins a phase its payload does not admit; or a row pins no
            phase for a payload that has one, which would let a phase-
            bearing payload say something the kind contradicts.
    """
    missing_phase_rows = sorted(kind.value for kind in EventPayloadKind if kind not in phases)
    if missing_phase_rows:
        raise ValueError(
            f"no phase vocabulary declared for payload {', '.join(missing_phase_rows)}"
        )
    missing_kinds = sorted(kind.value for kind in RunEventKind if kind not in declared)
    if missing_kinds:
        raise ValueError(f"no payload declared for event kind {', '.join(missing_kinds)}")
    table: dict[RunEventKind, EventContract] = {}
    for kind, (payload_kind, phase) in declared.items():
        admitted = tuple(phases[payload_kind])
        if admitted and phase is None:
            raise ValueError(
                f"event kind {kind.value!r} pins no phase, but payload "
                f"{payload_kind.value!r} carries one of {', '.join(admitted)}"
            )
        if phase is not None and phase not in admitted:
            raise ValueError(
                f"event kind {kind.value!r} pins phase {phase!r}, which payload "
                f"{payload_kind.value!r} does not admit"
            )
        table[kind] = EventContract(payload_kind=payload_kind, phase=phase)
    return MappingProxyType(table)


#: The total, import-checked map every Run event record is validated
#: against.
EVENT_CONTRACTS: Final[Mapping[RunEventKind, EventContract]] = compile_event_contracts(
    _DECLARED_EVENT_CONTRACTS, phases=_PAYLOAD_PHASES
)

#: The payload kind of each event kind, for a reader that wants the
#: compatibility half of the contract without the phase half.
EVENT_PAYLOADS: Final[Mapping[RunEventKind, EventPayloadKind]] = MappingProxyType(
    {kind: contract.payload_kind for kind, contract in EVENT_CONTRACTS.items()}
)


class ReasoningSummaryPayload(RuntimeRecord):
    """The provider-designated reasoning marker. It carries no thinking.

    Attributes:
        payload_kind: The payload discriminator.
        phase: ``started`` for the marker that a thought began,
            ``summarized`` for the one that carries its summary.
        summary: The provider's own summary, present only at
            ``summarized``.
        provider_designated: Always true: no summary is synthesised here.
        hidden_chain_of_thought_present: Always false, and pinned as a
            literal so no record can ever say otherwise.
    """

    payload_kind: Literal["reasoning_summary"] = "reasoning_summary"
    phase: Literal["started", "summarized"]
    summary: BoundedText | None = None
    provider_designated: Literal[True] = True
    hidden_chain_of_thought_present: Literal[False] = False

    @model_validator(mode="after")
    def _summary_belongs_to_the_summarized_phase(self) -> Self:
        """Require the summary exactly where the phase makes it a fact.

        Raises:
            ValueError: A summarized reasoning event carries no summary,
                or a started one carries text it cannot have yet.
        """
        if self.phase == "summarized" and self.summary is None:
            raise ValueError("a summarized reasoning event carries the provider's summary")
        if self.phase == "started" and self.summary is not None:
            raise ValueError("a started reasoning event has no summary yet")
        return self


class CommandPayload(RuntimeRecord):
    """One command execution, at one phase of it.

    Attributes:
        payload_kind: The payload discriminator.
        command_family_ref: The declared command family it belongs to.
        command_ref: The execution this event reports on. All phases of
            one command share it.
        phase: ``started``, ``output`` or ``result``.
        execution: ``foreground`` or ``background``. It has no default:
            a detached background command in flight is a fact somebody
            recorded, and defaulting it would make every unlabelled
            command look like it was being waited on.
        stream: Which stream a chunk came from, at ``output`` only.
        chunk_ref: The stored chunk, at ``output`` only.
        exit_code: The process exit status, at ``result`` only.
        outcome: How the command ended, at ``result`` only.
    """

    payload_kind: Literal["command"] = "command"
    command_family_ref: CommandFamilyId
    command_ref: CommandExecutionId
    phase: Literal["started", "output", "result"]
    execution: Literal["foreground", "background"]
    stream: Literal["stdout", "stderr"] | None = None
    chunk_ref: ArtifactUrn | None = None
    exit_code: StrictInt | None = None
    outcome: Literal["succeeded", "failed", "timed_out", "cancelled"] | None = None

    @model_validator(mode="after")
    def _fields_match_the_phase(self) -> Self:
        """Require exactly the fields each phase of a command makes facts.

        Raises:
            ValueError: A started command carries an outcome it cannot
                know, an output event names no stream or chunk, or a
                result event names no exit code or outcome.
        """
        if self.phase == "started":
            named = tuple(
                field
                for field, value in (
                    ("stream", self.stream),
                    ("chunk_ref", self.chunk_ref),
                    ("exit_code", self.exit_code),
                    ("outcome", self.outcome),
                )
                if value is not None
            )
            if named:
                raise ValueError(f"a started command has not produced {', '.join(named)} yet")
            return self
        if self.phase == "output":
            if self.stream is None or self.chunk_ref is None:
                raise ValueError("a command output event names its stream and its chunk")
            return self
        if self.exit_code is None or self.outcome is None:
            raise ValueError("a command result event names its exit code and its outcome")
        return self


class EventGapPayload(RuntimeRecord):
    """A range of sequences the daemon never received.

    Attributes:
        payload_kind: The payload discriminator.
        expected_sequence: The first missing sequence.
        observed_sequence: The sequence that arrived instead, so the
            missing range is ``expected .. observed - 1``.
        replay_requested: Whether the daemon asked the provider to
            replay the range.
        replay_capability_state: Whether replay was available to ask for.
        gap_ref: This gap's own identity.
    """

    payload_kind: Literal["event_gap"] = "event_gap"
    expected_sequence: StrictPositiveInt
    observed_sequence: StrictPositiveInt
    replay_requested: StrictBool
    replay_capability_state: Literal["verified", "unavailable"]
    gap_ref: EventGapId

    @model_validator(mode="after")
    def _observed_exceeds_expected(self) -> Self:
        """Require a gap to describe at least one missing sequence.

        Raises:
            ValueError: The observed sequence does not exceed the
                expected one, so the record names no hole.
        """
        if self.observed_sequence <= self.expected_sequence:
            raise ValueError(
                f"a gap runs from {self.expected_sequence} to a later sequence, "
                f"not to {self.observed_sequence}"
            )
        return self


#: The payload variants that exist today, discriminated on payload kind.
RunEventPayload = Annotated[
    ReasoningSummaryPayload | CommandPayload | EventGapPayload,
    Field(discriminator="payload_kind"),
]

#: The payload kinds a record can actually be built with.
_IMPLEMENTED_PAYLOAD_KINDS: Final[frozenset[EventPayloadKind]] = frozenset(
    {
        EventPayloadKind.REASONING_SUMMARY,
        EventPayloadKind.COMMAND,
        EventPayloadKind.EVENT_GAP,
    }
)

#: The event kinds an append may name. Every other declared kind has a
#: payload shape but no model yet, and is refused at the boundary rather
#: than coerced into one that happens to parse.
SUPPORTED_EVENT_KINDS: Final[frozenset[RunEventKind]] = frozenset(
    kind
    for kind, contract in EVENT_CONTRACTS.items()
    if contract.payload_kind in _IMPLEMENTED_PAYLOAD_KINDS
)


class RunEventRecord(RuntimeRecord):
    """One append-only line of a Run's event stream.

    Attributes:
        payload_kind: The discriminator separating an event line from a
            control fact, a contract binding and a compacted Run record.
        event_ref: The event's own identity, minted by whoever observed
            it and repeated on a retry, so a duplicate is recognisable.
        run_ref: The Run the event belongs to.
        run_sequence: The event's position in this Run's stream,
            counting from one.
        event_kind: What happened.
        provenance: Who observed it.
        payload: The typed detail, whose shape the kind fixes.
        actor: The principal the observation is attributable to.
        observed_at: When the observer says it happened, which may be
            absent and may be out of receive order.
        recorded_at: When the daemon appended the line. Liveness is
            measured against this and never against a provider clock.
        quarantine: Why this line derives nothing, or ``None`` when it
            is part of the stream.
    """

    payload_kind: Literal["run_event"] = "run_event"
    event_ref: RunEventId
    run_ref: RunUrn
    run_sequence: StrictPositiveInt
    event_kind: RunEventKind
    provenance: EventProvenance
    payload: RunEventPayload
    actor: PrincipalKey
    observed_at: UtcDatetime | None = None
    recorded_at: UtcDatetime
    quarantine: QuarantineReason | None = None

    @model_validator(mode="after")
    def _payload_and_phase_match_the_kind(self) -> Self:
        """Refuse a payload, or a phase of one, the kind does not admit.

        Raises:
            ValueError: The payload is not the shape the kind carries, or
                it is that shape at a phase the kind contradicts. The
                second is the check that stops a ``reasoning_summarized``
                event from carrying the ``started`` phase, which would
                render as a finished thought with nothing in it.
        """
        contract = EVENT_CONTRACTS[self.event_kind]
        if self.payload.payload_kind != contract.payload_kind.value:
            raise ValueError(
                f"event kind {self.event_kind.value!r} carries payload "
                f"{contract.payload_kind.value!r}, not {self.payload.payload_kind!r}"
            )
        phase = getattr(self.payload, "phase", None)
        if contract.phase is not None and phase != contract.phase:
            raise ValueError(
                f"event kind {self.event_kind.value!r} is the {contract.phase!r} phase, "
                f"not {phase!r}"
            )
        return self


__all__ = [
    "EVENT_CONTRACTS",
    "EVENT_PAYLOADS",
    "SUPPORTED_EVENT_KINDS",
    "CommandExecutionId",
    "CommandPayload",
    "EventContract",
    "EventGapId",
    "EventGapPayload",
    "EventPayloadKind",
    "EventProvenance",
    "QuarantineReason",
    "ReasoningSummaryPayload",
    "RunEventId",
    "RunEventKind",
    "RunEventPayload",
    "RunEventRecord",
    "compile_event_contracts",
]
