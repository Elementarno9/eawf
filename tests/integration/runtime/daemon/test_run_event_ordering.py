"""A Run's events are ordered, its holes are typed, and its end is final.

The walk is driven from ``event-gap-and-late-terminal.json`` rather than
spelled inline, so what the stream is supposed to do is readable without
reading the assertions that check it. The fixture is validated through the
production payload union, which means a step whose payload the kind does
not admit fails at load rather than at assert.

The last step is the one that matters most and the one an in-process test
is at risk of getting wrong. A Run ends through a *confirmed control
effect*, and that effect is appended to the ledger before the canonical
transition it causes. Between the two the stored record still says
RUNNING while the ledger already says CANCELLED, and an ordering check
that read the record would happily admit exactly the late events the
quarantine exists to stop. The torn-window test drives that state on
purpose -- the effect lands, the transition raises -- and then proves the
append is still quarantined, because terminality is read from the
reduced control state and never from the record.

The same window is also the reason a torn control write can never look
like an event gap: control facts and events count on two separate
sequences, so a confirmed effect over an unmoved record consumes no
``run_sequence`` at all, and the stream's cursor does not move.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.runtime.events import (
    EventGapPayload,
    QuarantineReason,
    ReasoningSummaryPayload,
    RunEventKind,
    RunEventPayload,
    RunEventRecord,
)
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.run import (
    RUN_BIND_METHOD,
    RUN_CONTRACT_READ_METHOD,
    RUN_CONTROL_ACKNOWLEDGE_METHOD,
    RUN_CONTROL_EFFECT_METHOD,
    RUN_CONTROL_REQUEST_METHOD,
    RUN_EVENT_APPEND_METHOD,
    RUN_EVENTS_READ_METHOD,
)
from eawf.runtime.daemon.run_events import reduce_run_events
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"
REQUEST_REF: Final = "CTL-0000000a"
EFFECT_REF: Final = "EFF-0000000b"
SPEC_DIGEST: Final = f"sha256:{'d' * 64}"
CAPSULE_DIGEST: Final = f"sha256:{'e' * 64}"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

#: The walk this suite drives, four levels up lands on ``tests/``.
WALK: Final = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "runtime"
    / "event-gap-and-late-terminal.json"
)


class _ExpectedGap(BaseModel):
    """The hole one step is expected to record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_sequence: int
    observed_sequence: int
    replay_requested: bool


class _Step(BaseModel):
    """One submitted event and what the stream is expected to do with it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: str
    cancel_run_first: bool = False
    event_ref: str
    run_sequence: int
    event_kind: RunEventKind
    payload: RunEventPayload
    replay_capability: str = "unavailable"
    expect_disposition: str
    expect_gap: _ExpectedGap | None = None
    expect_last_contiguous: int
    expect_next: int
    expect_derivation_stopped: bool
    expect_run_status: RunStatus


class _Walk(BaseModel):
    """The whole fixture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    steps: tuple[_Step, ...]


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one running Run."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(tmp_path / "runtime")


def call(method: str, ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb the way the server does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


def append(
    ctx: MethodContext, canary: CanaryProvision, *, payload: Any, **params: Any
) -> dict[str, Any]:
    """Append one event through the live verb."""
    return call(
        RUN_EVENT_APPEND_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        actor=ACTOR,
        payload=payload,
        **params,
    )


def cancel(ctx: MethodContext, canary: CanaryProvision) -> None:
    """Walk one control to a confirmed effect, ending the Run."""
    for method, extra in (
        (RUN_CONTROL_REQUEST_METHOD, {"control": "cancel"}),
        (RUN_CONTROL_ACKNOWLEDGE_METHOD, {"decision": "accepted"}),
        (
            RUN_CONTROL_EFFECT_METHOD,
            {
                "disposition": "confirmed",
                "effect_ref": EFFECT_REF,
                "idempotency_key": "idem-order",
            },
        ),
    ):
        call(
            method,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            control_request_ref=REQUEST_REF,
            actor=ACTOR,
            **extra,
        )


def stored_status(canary: CanaryProvision) -> str:
    """Return the canonical Run status, from whichever tier holds it.

    A Run that reaches a terminal state is compacted out of the document
    and into the run ledger, beside the control and event lines the same
    ledger carries. The Run's own line is the one with no payload
    discriminator, which is how the daemon's own reader tells it apart.
    """
    rows = json.loads(document_path(canary).read_text(encoding="utf-8")).get("run", {})
    if RUN_KEY in rows:
        return str(rows[RUN_KEY]["status"])
    ledger = ledger_path(document_path(canary), Epoch2Collection.RUN)
    for item in effective_records(read_ledger_records(ledger)):
        if item.record_key == RUN_KEY and "payload_kind" not in item.payload:
            return str(item.status)
    raise AssertionError(f"neither tier holds a run record keyed {RUN_KEY!r}")


def reasoning(sequence: int, ref: str) -> dict[str, Any]:
    """Return the params of one reasoning-start event."""
    return {
        "event_ref": ref,
        "run_sequence": sequence,
        "event_kind": "reasoning_started",
        "payload": {"payload_kind": "reasoning_summary", "phase": "started"},
    }


def record(sequence: int, ref: str, **overrides: Any) -> RunEventRecord:
    """Build one event line directly, for the reducer's own contract."""
    fields: dict[str, Any] = {
        "event_ref": ref,
        "run_ref": RUN_URN,
        "run_sequence": sequence,
        "event_kind": RunEventKind.REASONING_STARTED,
        "provenance": "provider_native",
        "payload": ReasoningSummaryPayload(phase="started"),
        "actor": ACTOR,
        "recorded_at": AT,
    }
    fields.update(overrides)
    return RunEventRecord.model_validate(fields)


# ---------------------------------------------------------------------------
# RUN-014: the recorded walk
# ---------------------------------------------------------------------------


def test_the_recorded_walk_orders_gaps_and_the_late_terminal(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    walk = _Walk.model_validate(json.loads(WALK.read_text(encoding="utf-8")))

    for step in walk.steps:
        if step.cancel_run_first:
            cancel(ctx, canary)
        answer = append(
            ctx,
            canary,
            payload=step.payload.model_dump(mode="json"),
            event_ref=step.event_ref,
            run_sequence=step.run_sequence,
            event_kind=step.event_kind.value,
            replay_capability=step.replay_capability,
        )
        assert answer["disposition"] == step.expect_disposition, step.step
        assert answer["last_contiguous_sequence"] == step.expect_last_contiguous, step.step
        assert answer["next_sequence"] == step.expect_next, step.step
        assert answer["run_status"] == step.expect_run_status.value, step.step
        if step.expect_gap is None:
            assert answer["gap"] is None, step.step
        else:
            assert answer["gap"]["expected_sequence"] == step.expect_gap.expected_sequence
            assert answer["gap"]["observed_sequence"] == step.expect_gap.observed_sequence
            assert answer["gap"]["replay_requested"] is step.expect_gap.replay_requested

    read = call(RUN_EVENTS_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    assert read["derivation_stopped"] is True
    assert read["last_contiguous_sequence"] == 2
    assert [event["run_sequence"] for event in read["events"]] == [1, 2, 3, 5]
    assert [gap["expected_sequence"] for gap in read["gaps"]] == [3]
    assert [event["quarantine"] for event in read["quarantined"]] == ["late_after_terminal"]
    assert read["run_status"] == "CANCELLED"


def test_a_late_event_leaves_the_terminal_run_where_it_ended(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    call(
        RUN_BIND_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        compiled_spec_digest=SPEC_DIGEST,
        authority_capsule_digest=CAPSULE_DIGEST,
        route_policy_revision=1,
    )
    cancel(ctx, canary)

    answer = append(ctx, canary, **reasoning(1, "EVT-0000000a"))

    assert answer["disposition"] == "quarantined"
    assert answer["run_status"] == "CANCELLED"
    assert answer["last_contiguous_sequence"] == 0
    assert stored_status(canary) == "CANCELLED"
    contract = call(RUN_CONTRACT_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    assert contract["status"] == "CANCELLED"


# ---------------------------------------------------------------------------
# The torn control window is not a gap, and does not admit a late event
# ---------------------------------------------------------------------------


def torn_cancel(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirm a cancelling effect and lose the transition that follows it."""
    import eawf.runtime.daemon.methods.run as run_methods

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the transition never committed")

    call(
        RUN_CONTROL_REQUEST_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        control_request_ref=REQUEST_REF,
        control="cancel",
        actor=ACTOR,
    )
    call(
        RUN_CONTROL_ACKNOWLEDGE_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        control_request_ref=REQUEST_REF,
        actor=ACTOR,
        decision="accepted",
    )
    monkeypatch.setattr(run_methods, "run_transaction", refuse)
    with pytest.raises(RuntimeError, match="never committed"):
        call(
            RUN_CONTROL_EFFECT_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            control_request_ref=REQUEST_REF,
            actor=ACTOR,
            disposition="confirmed",
            effect_ref=EFFECT_REF,
            idempotency_key="idem-torn",
        )
    monkeypatch.undo()


def test_an_event_inside_the_torn_control_window_is_still_quarantined(
    canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record says RUNNING, the effect says CANCELLED, and the effect wins."""
    append(ctx, canary, **reasoning(1, "EVT-0000000a"))
    torn_cancel(ctx, canary, monkeypatch)
    assert stored_status(canary) == "RUNNING"

    answer = append(ctx, canary, **reasoning(2, "EVT-0000000b"))

    assert answer["disposition"] == "quarantined"
    assert answer["run_status"] == "CANCELLED"
    assert answer["event"]["quarantine"] == QuarantineReason.LATE_AFTER_TERMINAL.value


def test_the_torn_control_window_leaves_the_event_cursor_alone(
    canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control facts and events count on two sequences, so neither holes the other."""
    append(ctx, canary, **reasoning(1, "EVT-0000000a"))
    before = call(RUN_EVENTS_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)

    torn_cancel(ctx, canary, monkeypatch)

    after = call(RUN_EVENTS_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    assert after["last_contiguous_sequence"] == before["last_contiguous_sequence"] == 1
    assert after["next_sequence"] == before["next_sequence"] == 2
    assert after["gaps"] == []
    assert after["derivation_stopped"] is False


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_one_event_identity_cannot_carry_two_different_events(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    append(ctx, canary, **reasoning(1, "EVT-0000000a"))

    with pytest.raises(DaemonValidationError, match="already recorded with different content"):
        append(
            ctx,
            canary,
            event_ref="EVT-0000000a",
            run_sequence=2,
            event_kind="reasoning_started",
            payload={"payload_kind": "reasoning_summary", "phase": "started"},
        )


def test_a_second_event_claiming_a_held_sequence_is_quarantined(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    append(ctx, canary, **reasoning(1, "EVT-0000000a"))

    answer = append(ctx, canary, **reasoning(1, "EVT-0000000b"))

    assert answer["disposition"] == "quarantined"
    assert answer["event"]["quarantine"] == QuarantineReason.SEQUENCE_CONFLICT.value
    assert answer["last_contiguous_sequence"] == 1


def test_a_sequence_inside_a_recorded_gap_is_not_an_append(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    append(ctx, canary, **reasoning(1, "EVT-0000000a"))
    append(ctx, canary, **reasoning(4, "EVT-0000000b"))

    with pytest.raises(DaemonValidationError, match="replay fill rather than an append"):
        append(ctx, canary, **reasoning(2, "EVT-0000000c"))


def test_a_kind_with_no_payload_model_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        append(
            ctx,
            canary,
            event_ref="EVT-0000000a",
            run_sequence=1,
            event_kind="heartbeat",
            payload={"payload_kind": "heartbeat", "worker_monotonic_ms": 10},
        )


def test_an_event_naming_an_unheld_run_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        call(
            RUN_EVENT_APPEND_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00009999",
            actor=ACTOR,
            **reasoning(1, "EVT-0000000a"),
        )


def test_an_unknown_append_parameter_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        append(ctx, canary, force=True, **reasoning(1, "EVT-0000000a"))


def test_an_event_read_of_a_run_with_no_stream_reports_an_empty_one(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    read = call(RUN_EVENTS_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)

    assert read["events"] == []
    assert read["last_contiguous_sequence"] == 0
    assert read["next_sequence"] == 1
    assert read["derivation_stopped"] is False
    assert read["stall"]["verdict"] == "unknown"


# ---------------------------------------------------------------------------
# The reducer's own contract
# ---------------------------------------------------------------------------


def test_the_reducer_refuses_an_unexplained_hole() -> None:
    with pytest.raises(ValueError, match="expected sequence 2, found 3"):
        reduce_run_events((record(1, "EVT-0000000a"), record(3, "EVT-0000000b")))


def test_the_reducer_walks_a_recorded_gap_without_complaint() -> None:
    marker = record(
        2,
        "EVT-0000000b",
        event_kind=RunEventKind.EVENT_GAP,
        provenance="daemon_observed",
        payload=EventGapPayload(
            expected_sequence=2,
            observed_sequence=4,
            replay_requested=False,
            replay_capability_state="unavailable",
            gap_ref="GAP-0000000f",
        ),
    )

    state = reduce_run_events((record(1, "EVT-0000000a"), marker, record(4, "EVT-0000000c")))

    assert state.last_contiguous_sequence == 1
    assert state.next_sequence == 5
    assert state.derivation_stopped is True
    assert len(state.gaps) == 1


def test_a_quarantined_line_derives_nothing() -> None:
    quarantined = record(
        1, "EVT-0000000b", quarantine=QuarantineReason.LATE_AFTER_TERMINAL, recorded_at=AT
    )

    state = reduce_run_events((quarantined,))

    assert state.last_contiguous_sequence == 0
    assert state.next_sequence == 1
    assert state.last_activity_at is None
    assert state.quarantined == (quarantined,)


def test_an_empty_stream_reduces_to_the_first_sequence() -> None:
    state = reduce_run_events(())

    assert state.last_contiguous_sequence == 0
    assert state.next_sequence == 1
    assert state.gaps == ()
    assert state.last_activity_kind is None


def test_the_fixture_payloads_parse_as_production_payloads() -> None:
    """A step the production union refuses must fail at load, not at assert."""
    with pytest.raises(ValidationError):
        _Step.model_validate(
            {
                "step": "a summarized reasoning event with no summary",
                "event_ref": "EVT-0000000a",
                "run_sequence": 1,
                "event_kind": "reasoning_summarized",
                "payload": {"payload_kind": "reasoning_summary", "phase": "summarized"},
                "expect_disposition": "appended",
                "expect_last_contiguous": 1,
                "expect_next": 2,
                "expect_derivation_stopped": False,
                "expect_run_status": "RUNNING",
            }
        )
