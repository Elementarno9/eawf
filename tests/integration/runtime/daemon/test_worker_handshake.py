"""No tool opens before an accepted hello, and silence is a typed fact.

The grant is a refusal by default. A Run that has never been announced
grants nothing, a Run whose announcement did not match its recorded
contract grants nothing, and neither answer depends on a live process
remembering anything: the handshake decision is a ledger line, so the
question survives the daemon that wrote it.

Stall detection is timing, and timing is where a test usually starts
sleeping. Nothing here sleeps. The arithmetic is pinned directly on
:func:`assess_stall`, which takes the reference instant as an argument,
so the exact boundary -- elapsed equal to the interval, and one
microsecond under it -- is asserted at a clock the test owns. The wired
verb is then crossed in both directions with the real daemon clock by
moving the *interval* instead: at an interval of zero any measurable
silence is a stall, and at the six-hundred-second default a Run that
just recorded an event is live. Both hold for every possible value of
the real clock, so neither assertion can flake.

A stall moves nothing. The Run's stored status and its rebuilt contract
are asserted unchanged across the flag, because a Run that ends silently
on a stall is the defect the observation exists to replace.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.events import QuarantineReason, RunEventKind
from eawf.kernel.runtime.handshake import (
    RUNTIME_HANDSHAKE_MISMATCH,
    RUNTIME_HANDSHAKE_REQUIRED,
)
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.run import (
    RUN_BIND_METHOD,
    RUN_CONTRACT_READ_METHOD,
    RUN_EVENT_APPEND_METHOD,
    RUN_EVENTS_READ_METHOD,
    RUN_TOOLS_GRANT_METHOD,
    RUN_WORKER_HELLO_METHOD,
)
from eawf.runtime.daemon.run_events import (
    DEFAULT_STALL_INTERVAL_SECONDS,
    RunEventState,
    RunLiveness,
    assess_stall,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"
SPEC_DIGEST: Final = f"sha256:{'d' * 64}"
CAPSULE_DIGEST: Final = f"sha256:{'e' * 64}"
OTHER_DIGEST: Final = f"sha256:{'9' * 64}"
ROUTE_REVISION: Final = 3
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

#: The tools a capsule of this Run would grant.
TOOLS: Final = ("repo_read", "submit_report")


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one running, bound Run."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(runtime_root)


def call(method: str, ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb the way the server does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


def hello_fields(**overrides: Any) -> dict[str, Any]:
    """Return one worker announcement, overriding whichever field is under test."""
    fields: dict[str, Any] = {
        "run_ref": RUN_URN,
        "provider_session_ref": "session-17",
        "driver_manifest_digest": f"sha256:{'1' * 64}",
        "provider_id": "claude",
        "sdk_version": "1.2.3",
        "auth_kind": "subscription",
        "model_id": "claude-opus-5",
        "os_class": "macos",
        "worker_protocol_version": "1.0.0",
        "event_codec_version": "1.0.0",
        "compiled_spec_digest": SPEC_DIGEST,
        "authority_capsule_digest": CAPSULE_DIGEST,
        "capabilities_digest": f"sha256:{'2' * 64}",
        "hello_sequence": 1,
    }
    fields.update(overrides)
    return fields


def bind(ctx: MethodContext, canary: CanaryProvision) -> dict[str, Any]:
    """Record the contract this Run's worker must echo."""
    return call(
        RUN_BIND_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        compiled_spec_digest=SPEC_DIGEST,
        authority_capsule_digest=CAPSULE_DIGEST,
        route_policy_revision=ROUTE_REVISION,
    )


def announce(ctx: MethodContext, canary: CanaryProvision, **overrides: Any) -> dict[str, Any]:
    """Announce one worker through the live verb."""
    return call(
        RUN_WORKER_HELLO_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        actor=ACTOR,
        hello=hello_fields(**overrides),
    )


def grant(ctx: MethodContext, canary: CanaryProvision) -> dict[str, Any]:
    """Ask which of this Run's tools may open."""
    return call(
        RUN_TOOLS_GRANT_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        tools=list(TOOLS),
    )


def append_event(
    ctx: MethodContext, canary: CanaryProvision, *, sequence: int, ref: str
) -> dict[str, Any]:
    """Record one reasoning-start event, which is one unit of activity."""
    return call(
        RUN_EVENT_APPEND_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        actor=ACTOR,
        event_ref=ref,
        run_sequence=sequence,
        event_kind="reasoning_started",
        payload={"payload_kind": "reasoning_summary", "phase": "started"},
    )


def read_events(ctx: MethodContext, canary: CanaryProvision, **overrides: Any) -> dict[str, Any]:
    """Read the Run's stream and its liveness."""
    return call(RUN_EVENTS_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN, **overrides)


def hello_lines(canary: CanaryProvision, runtime_root: Path) -> int:
    """Return how many handshake lines the run ledger holds."""
    context = root_context(canary, runtime_root)
    with context.session([RUN_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    return sum(1 for item in records if item.payload.get("payload_kind") == "worker_hello")


def state(*, last_activity_at: datetime | None) -> RunEventState:
    """Return a reduced stream whose only interesting field is its clock."""
    return RunEventState(
        last_contiguous_sequence=1 if last_activity_at else 0,
        next_sequence=2 if last_activity_at else 1,
        gaps=(),
        quarantined=(),
        derivation_stopped=False,
        last_activity_at=last_activity_at,
        last_activity_kind=RunEventKind.REASONING_STARTED if last_activity_at else None,
    )


# ---------------------------------------------------------------------------
# No tool grant precedes an accepted hello
# ---------------------------------------------------------------------------


def test_a_run_that_was_never_announced_grants_no_tool(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    bind(ctx, canary)

    answer = grant(ctx, canary)

    assert answer["granted"] == []
    assert answer["refusal_code"] == RUNTIME_HANDSHAKE_REQUIRED


def test_an_accepted_hello_opens_the_requested_tools(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    bind(ctx, canary)

    announced = announce(ctx, canary)
    answer = grant(ctx, canary)

    assert announced["disposition"] == "accepted"
    assert announced["mismatched_fields"] == []
    assert announced["refusal_code"] is None
    assert answer["granted"] == list(TOOLS)
    assert answer["refusal_code"] is None


def test_a_hello_that_does_not_match_the_binding_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    bind(ctx, canary)

    announced = announce(ctx, canary, compiled_spec_digest=OTHER_DIGEST)

    assert announced["disposition"] == "mismatched"
    assert announced["mismatched_fields"] == ["compiled_spec_digest"]
    assert announced["refusal_code"] == RUNTIME_HANDSHAKE_MISMATCH
    assert "compiled_spec_digest" in announced["reason"]
    assert OTHER_DIGEST not in announced["reason"]


def test_no_tool_opens_behind_a_mismatched_hello(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    bind(ctx, canary)
    announce(ctx, canary, authority_capsule_digest=OTHER_DIGEST)

    answer = grant(ctx, canary)

    assert answer["granted"] == []
    assert answer["refusal_code"] == RUNTIME_HANDSHAKE_MISMATCH


def test_a_later_mismatch_closes_tools_an_earlier_acceptance_opened(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """The live worker's claim decides, not the one that came before it."""
    bind(ctx, canary)
    announce(ctx, canary)
    assert grant(ctx, canary)["granted"] == list(TOOLS)

    announce(ctx, canary, hello_sequence=2, compiled_spec_digest=OTHER_DIGEST)

    assert grant(ctx, canary)["granted"] == []


def test_a_hello_naming_another_run_is_a_mismatch(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    bind(ctx, canary)

    announced = announce(ctx, canary, run_ref="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00009999")

    assert announced["mismatched_fields"] == ["run_ref"]


def test_a_run_with_no_binding_has_nothing_to_check_a_hello_against(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="has no contract binding"):
        announce(ctx, canary)


def test_repeating_an_announcement_appends_no_second_line(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    bind(ctx, canary)
    first = announce(ctx, canary)

    second = announce(ctx, canary)

    assert second["fact"] == first["fact"]
    assert hello_lines(canary, runtime_root) == 1


def test_an_announcement_below_the_recorded_sequence_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    bind(ctx, canary)
    announce(ctx, canary, hello_sequence=2)

    with pytest.raises(DaemonValidationError, match="does not reach 3"):
        announce(ctx, canary, hello_sequence=1)


def test_a_hello_with_an_unknown_field_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    bind(ctx, canary)

    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        announce(ctx, canary, elevated=True)


def test_a_grant_naming_one_tool_twice_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    bind(ctx, canary)
    announce(ctx, canary)

    with pytest.raises(DaemonValidationError, match="tools"):
        call(
            RUN_TOOLS_GRANT_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            tools=["repo_read", "repo_read"],
        )


def test_a_grant_of_no_tools_opens_nothing_and_refuses_nothing(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """The empty capsule is a real case: the Run reaches no semantic tool."""
    bind(ctx, canary)
    announce(ctx, canary)

    answer = call(RUN_TOOLS_GRANT_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)

    assert answer["granted"] == []
    assert answer["refusal_code"] is None


# ---------------------------------------------------------------------------
# A Run silent past its threshold is flagged, and nothing else happens to it
# ---------------------------------------------------------------------------


def test_a_run_with_no_activity_has_no_silence_to_measure(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    assert read_events(ctx, canary)["stall"]["verdict"] == RunLiveness.UNKNOWN.value


def test_a_run_that_just_produced_something_is_live(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    append_event(ctx, canary, sequence=1, ref="EVT-0000000a")

    stall = read_events(ctx, canary)["stall"]

    assert stall["verdict"] == RunLiveness.LIVE.value
    assert stall["interval_seconds"] == DEFAULT_STALL_INTERVAL_SECONDS
    assert stall["last_activity_kind"] == "reasoning_started"


def test_a_run_silent_past_its_threshold_is_flagged_stalled(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    append_event(ctx, canary, sequence=1, ref="EVT-0000000a")

    stall = read_events(ctx, canary, stall_interval_seconds=0)["stall"]

    assert stall["verdict"] == RunLiveness.STALLED.value
    assert stall["elapsed_seconds"] >= 0.0
    assert stall["resume_method"] == "runtime.run.control.request"
    assert stall["resume_control"] == "resume"


def test_a_stall_moves_neither_the_record_nor_the_contract(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    bind(ctx, canary)
    append_event(ctx, canary, sequence=1, ref="EVT-0000000a")

    flagged = read_events(ctx, canary, stall_interval_seconds=0)

    assert flagged["stall"]["verdict"] == RunLiveness.STALLED.value
    assert flagged["run_status"] == "RUNNING"
    contract = call(RUN_CONTRACT_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    assert contract["status"] == "RUNNING"


def test_a_quarantined_event_does_not_refresh_the_liveness_clock(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    append_event(ctx, canary, sequence=1, ref="EVT-0000000a")
    live = read_events(ctx, canary)["stall"]["last_activity_at"]

    conflicting = append_event(ctx, canary, sequence=1, ref="EVT-0000000b")

    assert conflicting["event"]["quarantine"] == QuarantineReason.SEQUENCE_CONFLICT.value
    assert read_events(ctx, canary)["stall"]["last_activity_at"] == live


def test_a_negative_stall_interval_is_refused(canary: CanaryProvision, ctx: MethodContext) -> None:
    with pytest.raises(DaemonValidationError, match="stall_interval_seconds"):
        read_events(ctx, canary, stall_interval_seconds=-1)


# ---------------------------------------------------------------------------
# The boundary itself, at a clock the test owns
# ---------------------------------------------------------------------------


def test_silence_exactly_equal_to_the_interval_is_a_stall() -> None:
    assessed = assess_stall(
        state=state(last_activity_at=AT), now=AT + timedelta(seconds=600), interval_seconds=600
    )

    assert assessed.verdict is RunLiveness.STALLED
    assert assessed.elapsed_seconds == pytest.approx(600.0)


def test_one_microsecond_under_the_interval_is_still_live() -> None:
    assessed = assess_stall(
        state=state(last_activity_at=AT),
        now=AT + timedelta(seconds=600, microseconds=-1),
        interval_seconds=600,
    )

    assert assessed.verdict is RunLiveness.LIVE


def test_a_stream_with_no_activity_is_unknown_at_any_clock() -> None:
    assessed = assess_stall(
        state=state(last_activity_at=None), now=AT + timedelta(days=7), interval_seconds=0
    )

    assert assessed.verdict is RunLiveness.UNKNOWN
    assert assessed.elapsed_seconds == pytest.approx(0.0)
    assert assessed.last_activity_kind is None


def test_the_resume_path_is_an_offer_and_not_a_transition() -> None:
    assessed = assess_stall(
        state=state(last_activity_at=AT), now=AT + timedelta(hours=1), interval_seconds=600
    )

    assert assessed.verdict is RunLiveness.STALLED
    assert assessed.resume_control.value == "resume"
