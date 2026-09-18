"""The Run event vocabulary is forty kinds and its phase map is total.

Three rules are pinned here, and each of them is one a renderer would
otherwise have to guess at.

The first is the vocabulary itself. The forty kinds are asserted
verbatim rather than derived from the module under test, because an enum
compared against itself proves only that it is self-consistent; the list
below is the contract, and ``reasoning_started`` and ``command_started``
are in it because a live transcript that cannot say "thinking" or
"running in the background" ends up inferring both from silence.

The second is that the kind fixes the payload *and its phase*. A
``reasoning_summarized`` event carrying the ``started`` phase would
render as a finished thought with no text in it, and a ``command_started``
with no ``execution`` would make a detached background command
indistinguishable from one being waited on. Both are refused at the model
boundary, before any line is appended.

The third is that the map is total and single-valued. Every kind has a
payload row, every payload kind has a phase vocabulary, and a pinned
phase the payload does not admit is a startup failure rather than a
record that appends and then lies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.events import (
    EVENT_CONTRACTS,
    EVENT_PAYLOADS,
    SUPPORTED_EVENT_KINDS,
    CommandPayload,
    EventGapPayload,
    EventPayloadKind,
    QuarantineReason,
    ReasoningSummaryPayload,
    RunEventKind,
    RunEventRecord,
    compile_event_contracts,
)

pytestmark = pytest.mark.unit

RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
EVENT = "EVT-0000000a"
GAP = "GAP-0000000b"
COMMAND = "CMD-0000000c"
AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

#: The forty canonical kinds, spelled as the wire carries them.
FORTY_KINDS = (
    "allocation_started",
    "provider_accepted",
    "session_started",
    "session_ended",
    "turn_started",
    "turn_ended",
    "message_summarized",
    "plan_updated",
    "reasoning_started",
    "reasoning_summarized",
    "child_run_requested",
    "child_run_started",
    "child_run_terminal",
    "tool_requested",
    "tool_accepted",
    "tool_result",
    "command_started",
    "command_output",
    "command_result",
    "file_changed",
    "diff_summarized",
    "question_raised",
    "approval_requested",
    "approval_resolved",
    "usage_observed",
    "budget_warning",
    "budget_exhausted",
    "checkpoint_created",
    "rerouted",
    "error_observed",
    "heartbeat",
    "context_boundary",
    "control_requested",
    "control_acknowledged",
    "control_effected",
    "reconciliation_completed",
    "provider_lost",
    "event_gap",
    "capability_revoked",
    "run_transition",
)


def record(**overrides: Any) -> RunEventRecord:
    """Build one event line, overriding whichever field is under test."""
    fields: dict[str, Any] = {
        "event_ref": EVENT,
        "run_ref": RUN_URN,
        "run_sequence": 1,
        "event_kind": RunEventKind.REASONING_STARTED,
        "provenance": "provider_native",
        "payload": ReasoningSummaryPayload(phase="started"),
        "actor": "OP-0001",
        "recorded_at": AT,
    }
    fields.update(overrides)
    return RunEventRecord.model_validate(fields)


def command(**overrides: Any) -> dict[str, Any]:
    """Return command payload fields, overriding whichever is under test."""
    fields: dict[str, Any] = {
        "command_family_ref": "shell",
        "command_ref": COMMAND,
        "phase": "started",
        "execution": "foreground",
    }
    fields.update(overrides)
    return fields


# ---------------------------------------------------------------------------
# RUN-062: the vocabulary carries the two kinds a live transcript needs
# ---------------------------------------------------------------------------


def test_run_event_kind_is_the_forty_canonical_values() -> None:
    assert tuple(kind.value for kind in RunEventKind) == FORTY_KINDS


def test_run_event_kind_carries_reasoning_started_and_command_started() -> None:
    assert RunEventKind.REASONING_STARTED.value == "reasoning_started"
    assert RunEventKind.COMMAND_STARTED.value == "command_started"
    assert EVENT_PAYLOADS[RunEventKind.REASONING_STARTED] is EventPayloadKind.REASONING_SUMMARY
    assert EVENT_PAYLOADS[RunEventKind.COMMAND_STARTED] is EventPayloadKind.COMMAND


def test_the_supported_kinds_are_the_ones_whose_payload_model_exists() -> None:
    assert sorted(kind.value for kind in SUPPORTED_EVENT_KINDS) == [
        "command_output",
        "command_result",
        "command_started",
        "event_gap",
        "reasoning_started",
        "reasoning_summarized",
    ]
    assert RunEventKind.HEARTBEAT not in SUPPORTED_EVENT_KINDS


# ---------------------------------------------------------------------------
# RUN-062: the phase map is total, single-valued, and checked at import
# ---------------------------------------------------------------------------


def test_every_event_kind_has_a_payload_and_a_phase_row() -> None:
    assert set(EVENT_CONTRACTS) == set(RunEventKind)
    assert set(EVENT_PAYLOADS) == set(RunEventKind)


def test_the_pinned_phases_are_the_contract() -> None:
    pinned = {
        kind.value: contract.phase
        for kind, contract in EVENT_CONTRACTS.items()
        if contract.phase is not None
    }
    assert pinned == {
        "allocation_started": "allocation",
        "provider_accepted": "acceptance",
        "session_started": "session",
        "session_ended": "session",
        "turn_started": "turn",
        "turn_ended": "turn",
        "reasoning_started": "started",
        "reasoning_summarized": "summarized",
        "child_run_requested": "requested",
        "child_run_started": "started",
        "child_run_terminal": "terminal",
        "tool_requested": "requested",
        "tool_accepted": "accepted",
        "tool_result": "result",
        "command_started": "started",
        "command_output": "output",
        "command_result": "result",
        "question_raised": "raised",
        "approval_requested": "requested",
        "approval_resolved": "resolved",
        "control_requested": "requested",
        "control_acknowledged": "acknowledged",
        "control_effected": "effected",
    }


def test_a_kind_with_no_payload_row_is_refused_at_compile() -> None:
    declared = {
        kind: (EventPayloadKind.HEARTBEAT, None)
        for kind in RunEventKind
        if kind is not RunEventKind.HEARTBEAT
    }
    with pytest.raises(ValueError, match="no payload declared for event kind heartbeat"):
        compile_event_contracts(declared, phases=dict.fromkeys(EventPayloadKind, ()))


def test_a_payload_kind_with_no_phase_vocabulary_is_refused_at_compile() -> None:
    phases = {kind: () for kind in EventPayloadKind if kind is not EventPayloadKind.COMMAND}
    with pytest.raises(ValueError, match="no phase vocabulary declared for payload command"):
        compile_event_contracts(
            dict.fromkeys(RunEventKind, (EventPayloadKind.HEARTBEAT, None)), phases=phases
        )


def test_a_phase_the_payload_does_not_admit_is_refused_at_compile() -> None:
    declared = dict.fromkeys(RunEventKind, (EventPayloadKind.COMMAND, "started"))
    declared[RunEventKind.COMMAND_STARTED] = (EventPayloadKind.COMMAND, "finished")
    with pytest.raises(ValueError, match="pins phase 'finished'"):
        compile_event_contracts(
            declared, phases=dict.fromkeys(EventPayloadKind, ("started", "output", "result"))
        )


def test_a_phase_bearing_payload_pinned_to_no_phase_is_refused_at_compile() -> None:
    declared = dict.fromkeys(RunEventKind, (EventPayloadKind.COMMAND, None))
    with pytest.raises(ValueError, match="pins no phase"):
        compile_event_contracts(declared, phases=dict.fromkeys(EventPayloadKind, ("started",)))


# ---------------------------------------------------------------------------
# RUN-062: the failures the requirement names, refused at the boundary
# ---------------------------------------------------------------------------


def test_a_command_started_without_execution_is_refused() -> None:
    fields = command()
    del fields["execution"]
    with pytest.raises(ValidationError, match="execution"):
        CommandPayload.model_validate(fields)


def test_a_reasoning_summarized_carrying_phase_started_is_refused() -> None:
    with pytest.raises(ValidationError, match="is the 'summarized' phase"):
        record(
            event_kind=RunEventKind.REASONING_SUMMARIZED,
            payload=ReasoningSummaryPayload(phase="started"),
        )


def test_a_started_command_payload_carrying_an_outcome_is_refused() -> None:
    with pytest.raises(ValidationError, match="has not produced"):
        CommandPayload.model_validate(command(outcome="succeeded"))


def test_a_command_result_without_an_exit_code_is_refused() -> None:
    with pytest.raises(ValidationError, match="names its exit code"):
        CommandPayload.model_validate(command(phase="result", outcome="succeeded"))


def test_a_command_output_without_a_stream_is_refused() -> None:
    with pytest.raises(ValidationError, match="names its stream"):
        CommandPayload.model_validate(command(phase="output"))


def test_a_summarized_reasoning_event_without_a_summary_is_refused() -> None:
    with pytest.raises(ValidationError, match="carries the provider's summary"):
        ReasoningSummaryPayload.model_validate({"phase": "summarized"})


def test_a_started_reasoning_event_carrying_a_summary_is_refused() -> None:
    with pytest.raises(ValidationError, match="has no summary yet"):
        ReasoningSummaryPayload.model_validate({"phase": "started", "summary": "weighed two"})


def test_no_reasoning_event_may_claim_hidden_chain_of_thought() -> None:
    with pytest.raises(ValidationError, match="hidden_chain_of_thought_present"):
        ReasoningSummaryPayload.model_validate(
            {"phase": "started", "hidden_chain_of_thought_present": True}
        )


def test_a_payload_the_kind_does_not_carry_is_refused() -> None:
    with pytest.raises(ValidationError, match="carries payload 'reasoning_summary'"):
        record(payload=CommandPayload.model_validate(command()))


def test_an_unknown_payload_field_is_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        ReasoningSummaryPayload.model_validate({"phase": "started", "thoughts": "hidden"})


# ---------------------------------------------------------------------------
# Boundary cases on the record itself
# ---------------------------------------------------------------------------


def test_the_first_sequence_a_record_may_carry_is_one() -> None:
    assert record(run_sequence=1).run_sequence == 1
    with pytest.raises(ValidationError, match="run_sequence"):
        record(run_sequence=0)


def test_a_summary_at_the_length_ceiling_is_accepted_and_one_over_is_not() -> None:
    at_ceiling = ReasoningSummaryPayload(phase="summarized", summary="a" * 500)
    assert at_ceiling.summary is not None
    assert len(at_ceiling.summary) == 500
    with pytest.raises(ValidationError, match="summary"):
        ReasoningSummaryPayload.model_validate({"phase": "summarized", "summary": "a" * 501})


def test_an_empty_summary_is_refused() -> None:
    with pytest.raises(ValidationError, match="summary"):
        ReasoningSummaryPayload.model_validate({"phase": "summarized", "summary": ""})


def test_a_gap_must_name_at_least_one_missing_sequence() -> None:
    spanning_one = EventGapPayload(
        expected_sequence=3,
        observed_sequence=4,
        replay_requested=False,
        replay_capability_state="unavailable",
        gap_ref=GAP,
    )
    assert spanning_one.observed_sequence - spanning_one.expected_sequence == 1
    with pytest.raises(ValidationError, match="a gap runs from 3 to a later sequence"):
        EventGapPayload.model_validate(
            {
                "expected_sequence": 3,
                "observed_sequence": 3,
                "replay_requested": False,
                "replay_capability_state": "unavailable",
                "gap_ref": GAP,
            }
        )


def test_a_quarantined_record_names_why_it_derives_nothing() -> None:
    quarantined = record(quarantine=QuarantineReason.LATE_AFTER_TERMINAL)
    assert quarantined.quarantine is QuarantineReason.LATE_AFTER_TERMINAL
    assert record().quarantine is None


def test_an_event_identity_outside_the_grammar_is_refused() -> None:
    with pytest.raises(ValidationError, match="event_ref"):
        record(event_ref="EVT-NOTHEX")
