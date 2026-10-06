"""A committed Run event append is announced, and the stream is read by cursor.

Every append that commits a line publishes one ``run_events_appended`` notice naming
the Run and its new tail, whichever writer made it: the append verb, the dispatch
sink a launcher hands each message to, and the bridge a host subagent's stop runs.
A duplicate commits nothing and announces nothing, and a fan-out that refuses the
notice never undoes the line. The events read takes the cursor a subscriber holds and
returns only what lies past it, while the holes, the next sequence and the stall stay
facts of the whole stream.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.events import MessageSummaryPayload
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds import PAYLOAD_MODELS
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.bus import EventBus, Subscriber
from eawf.runtime.daemon.dispatch_stream import message_sink
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.host_subagent import HOST_SUBAGENT_STOP_METHOD
from eawf.runtime.daemon.methods.run import RUN_EVENT_APPEND_METHOD, RUN_EVENTS_READ_METHOD
from eawf.runtime.daemon.run_events import (
    RunEventsAppendedPayload,
    RunEventsRead,
    RunScopedParams,
    run_events_of,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"
REPOSITORY_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
AGENT_ID: Final = "a0a45f67519cb14dd"


class _RefusingBus:
    """A fan-out whose every publish raises, as a broken subscriber queue would."""

    def __init__(self) -> None:
        self.attempts = 0

    def publish(self, envelope: Envelope) -> None:
        self.attempts += 1
        raise RuntimeError("fan-out unavailable")


@pytest.fixture(autouse=True)
def hermetic_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep canary runtime dirs and transcript discovery under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary admitting one repository and holding one running Run."""
    provisioned = provision(tmp_path / "repo", code="NOTE")
    seed(
        provisioned,
        {
            Epoch2Collection.REPOSITORY.value: {
                "REP-EAWF": {
                    "key": "REP-EAWF",
                    "urn": REPOSITORY_URN,
                    "revision": 1,
                    "head_sha": "a" * 40,
                    "created_at": AT.isoformat(),
                    "updated_at": AT.isoformat(),
                }
            },
            Epoch2Collection.RUN.value: {RUN_KEY: seed_row("run", "RUNNING")},
        },
    )
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """A daemon context carrying a live bus."""
    context = method_context(tmp_path / "runtime")
    context.bus = EventBus()
    return context


def notices(ctx: MethodContext) -> Subscriber:
    """Subscribe to the stream notices alone, before anything is appended."""
    subscriber: Subscriber = ctx.bus.register(
        connection_id="notice-reader", kind_filter=[StoreKind.RUN_EVENTS_APPENDED]
    )
    return subscriber


def call(method: str, ctx: MethodContext, canary: CanaryProvision, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb against the canary, the way the server does."""
    answer: dict[str, Any] = asyncio.run(
        methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params})
    )
    return answer


def append(ctx: MethodContext, canary: CanaryProvision, sequence: int, ref: str) -> dict[str, Any]:
    """Append one reasoning-start event at ``sequence`` through the live verb."""
    return call(
        RUN_EVENT_APPEND_METHOD,
        ctx,
        canary,
        urn=RUN_URN,
        actor=ACTOR,
        event_ref=ref,
        run_sequence=sequence,
        event_kind="reasoning_started",
        payload={"payload_kind": "reasoning_summary", "phase": "started"},
    )


def ref(index: int) -> str:
    """Return the id of the ``index``-th event of a test stream."""
    return f"EVT-{index:032x}"


def read(ctx: MethodContext, canary: CanaryProvision, **params: Any) -> dict[str, Any]:
    """Read the Run's stream through the live verb."""
    return call(
        RUN_EVENTS_READ_METHOD, ctx, canary, urn=RUN_URN, stall_interval_seconds=3600, **params
    )


def stream_lines(canary: CanaryProvision, urn: str) -> int:
    """Return how many event lines the run ledger holds for ``urn``."""
    records = read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))
    return len(run_events_of(records, RunScopedParams.model_validate({"urn": urn}).urn))


# ---- the notice -------------------------------------------------------------------


def test_one_committed_append_publishes_exactly_one_notice(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    subscriber = notices(ctx)

    answer = append(ctx, canary, 1, ref(1))

    assert answer["disposition"] == "appended"
    assert len(subscriber.queue) == 1
    envelope = subscriber.queue[0]
    assert envelope.kind is StoreKind.RUN_EVENTS_APPENDED
    assert envelope.scope_id == RUN_URN
    assert envelope.payload == {"run_ref": RUN_URN, "tail_sequence": 1}
    assert PAYLOAD_MODELS[StoreKind.RUN_EVENTS_APPENDED] is RunEventsAppendedPayload


def test_each_append_names_the_new_tail(ctx: MethodContext, canary: CanaryProvision) -> None:
    subscriber = notices(ctx)

    for index in range(1, 4):
        append(ctx, canary, index, ref(index))

    assert [item.payload["tail_sequence"] for item in subscriber.queue] == [1, 2, 3]
    assert len({item.id for item in subscriber.queue}) == 3


def test_a_duplicate_append_publishes_nothing(ctx: MethodContext, canary: CanaryProvision) -> None:
    append(ctx, canary, 1, ref(1))
    subscriber = notices(ctx)

    again = append(ctx, canary, 1, ref(1))

    assert again["disposition"] == "duplicate"
    assert len(subscriber.queue) == 0


def test_a_recorded_gap_announces_the_tail_that_stopped_before_it(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    subscriber = notices(ctx)

    answer = append(ctx, canary, 3, ref(3))

    assert answer["disposition"] == "gap_recorded"
    assert [item.payload["tail_sequence"] for item in subscriber.queue] == [0]


def test_a_quarantined_line_is_still_a_committed_line_and_is_announced(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    append(ctx, canary, 1, ref(1))
    subscriber = notices(ctx)

    rival = append(ctx, canary, 1, ref(2))

    assert rival["disposition"] == "quarantined"
    assert [item.payload["tail_sequence"] for item in subscriber.queue] == [1]


def test_a_refused_publish_never_fails_the_append(tmp_path: Path, canary: CanaryProvision) -> None:
    ctx = method_context(tmp_path / "runtime")
    bus = _RefusingBus()
    ctx.bus = bus

    answer = append(ctx, canary, 1, ref(1))

    assert answer["disposition"] == "appended"
    assert bus.attempts == 1
    assert stream_lines(canary, RUN_URN) == 1


def test_a_daemon_without_a_bus_still_appends(tmp_path: Path, canary: CanaryProvision) -> None:
    ctx = method_context(tmp_path / "runtime")

    answer = append(ctx, canary, 1, ref(1))

    assert answer["disposition"] == "appended"
    assert stream_lines(canary, RUN_URN) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"run_ref": RUN_URN, "tail_sequence": -1},
        {"run_ref": RUN_URN, "tail_sequence": "1"},
        {"run_ref": RUN_URN},
        {"run_ref": RUN_URN, "tail_sequence": 1, "event": {}},
        {"run_ref": REPOSITORY_URN, "tail_sequence": 1},
    ],
)
def test_the_notice_payload_refuses_a_malformed_body(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RunEventsAppendedPayload.model_validate(payload)


# ---- the cursor ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("after_sequence", "expected"),
    [(0, [1, 2, 3, 4, 5]), (1, [2, 3, 4, 5]), (3, [4, 5]), (4, [5]), (5, []), (6, [])],
)
def test_the_read_returns_only_events_past_the_cursor(
    ctx: MethodContext, canary: CanaryProvision, after_sequence: int, expected: list[int]
) -> None:
    for index in range(1, 6):
        append(ctx, canary, index, ref(index))

    answer = read(ctx, canary, after_sequence=after_sequence)

    assert [event["run_sequence"] for event in answer["events"]] == expected
    assert answer["next_sequence"] == 6
    assert answer["last_contiguous_sequence"] == 5
    assert answer["gaps"] == []
    assert answer["stall"]["verdict"] == "live"


def test_the_cursor_defaults_to_the_whole_stream(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    for index in range(1, 3):
        append(ctx, canary, index, ref(index))

    assert [event["run_sequence"] for event in read(ctx, canary)["events"]] == [1, 2]
    assert RunEventsRead.model_validate({"urn": RUN_URN}).after_sequence == 0


def test_a_cursor_read_still_reports_the_whole_streams_holes(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    append(ctx, canary, 1, ref(1))
    append(ctx, canary, 4, ref(4))
    whole = read(ctx, canary)

    answer = read(ctx, canary, after_sequence=3)

    assert [event["run_sequence"] for event in answer["events"]] == [4]
    assert answer["gaps"] == whole["gaps"]
    assert len(answer["gaps"]) == 1
    assert answer["next_sequence"] == whole["next_sequence"] == 5
    assert answer["derivation_stopped"] is True


@pytest.mark.parametrize("after_sequence", [-1, "3", 2.5, None])
def test_the_read_refuses_a_cursor_that_is_not_a_non_negative_integer(
    ctx: MethodContext, canary: CanaryProvision, after_sequence: Any
) -> None:
    with pytest.raises(DaemonValidationError):
        read(ctx, canary, after_sequence=after_sequence)


# ---- both writers ---------------------------------------------------------------------


def test_the_dispatch_message_sink_announces_each_message(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    subscriber = notices(ctx)
    context = ctx.native_root_context(tree_root(canary))
    sink = message_sink(
        context,
        RunScopedParams.model_validate({"urn": RUN_URN}).urn,
        actor=ACTOR,
        attempt_ref="ATT-notice",
    )

    async def say() -> None:
        for words in ("Reading the cache module.", "The expiry check runs first."):
            await sink(MessageSummaryPayload(message_role="assistant", summary=words))

    asyncio.run(say())

    assert [(item.scope_id, item.payload["tail_sequence"]) for item in subscriber.queue] == [
        (RUN_URN, 1),
        (RUN_URN, 2),
    ]


def test_the_host_subagent_bridge_announces_each_bridged_line(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    transcript = tmp_path / "agent.jsonl"
    transcript.write_text(
        "".join(
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [block]}})
            + "\n"
            for block in (
                {"type": "text", "text": "I will list the package first."},
                {"type": "text", "text": "There are forty-two modules."},
            )
        ),
        encoding="utf-8",
    )
    subscriber = notices(ctx)

    answer = call(
        HOST_SUBAGENT_STOP_METHOD,
        ctx,
        canary,
        harness="claude-code",
        agent_id=AGENT_ID,
        transcript_path=str(transcript),
    )

    bridged = [item for item in subscriber.queue if item.scope_id == answer["run_ref"]]
    assert answer["bridged_events"] == 2
    assert [item.payload["tail_sequence"] for item in bridged] == [1, 2]
    assert all(item.payload["run_ref"] == answer["run_ref"] for item in bridged)
