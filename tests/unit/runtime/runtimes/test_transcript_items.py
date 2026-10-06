"""A native turn's tool, edit and thinking blocks reach the Run's stream as typed items.

The provider-native tool payload rides on the gateway's tool kinds without loosening the
gateway's own payload; each launcher's reading maps a provider tool to its pinned class
and never records its input or output; and a thinking block is stated as one started
marker per assistant message, its text reaching no line and no store. The end-to-end
cases drive the real launchers against the fake ``claude`` and ``codex`` binaries, and
the dispatch sink into a disposable canary whose files are then searched for the text.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, get_args

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from openai_codex.generated.v2_all import ItemCompletedNotification, ItemStartedNotification
from pydantic import ValidationError

from eawf.kernel.runtime.events import (
    EVENT_CONTRACTS,
    ChildRunPayload,
    EventPayloadKind,
    MessageSummaryPayload,
    ProviderToolClass,
    ProviderToolPayload,
    ReasoningSummaryPayload,
    RunEventKind,
    RunEventRecord,
    ToolPayload,
    TranscriptItem,
    compile_event_contracts,
)
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.dispatch_stream import _event_ref, message_sink
from eawf.runtime.daemon.run_events import RunScopedParams, run_events_of
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.codex import adapter as codex_adapter
from eawf.runtime.runtimes.host_transcript import SpokenMessage
from eawf.runtime.runtimes.transcript_items import (
    CLAUDE_TOOL_CLASSES,
    CODEX_COMMAND_TOOL,
    CODEX_PATCH_TOOL,
    ClaudeTranscriptItems,
    CodexTranscriptItems,
    provider_call_ref,
    provider_tool_name,
)
from tests.fixtures.runtime.fake_codex_server import THREAD_ID, TURN_ID, turn_completed
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)
from tests.unit.runtime.runtimes.claude import test_sdk_launcher as claude_fx
from tests.unit.runtime.runtimes.codex import test_official_launcher as codex_fx

#: What a thinking block, a tool input or a tool output says; it must reach nothing.
SENTINEL: Final = "sentinel-hidden-reasoning-7f3a"

#: The grammar every stated tool name holds to.
TOOL_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

ACTOR: Final = "OP-0001"
RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
ATTEMPT: Final = "ATT-transcript-items"
CALL: Final = "call-00000000000000c1"

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the session wrapper and the fakes are POSIX scripts"
)


def _tool(**overrides: Any) -> ProviderToolPayload:
    fields: dict[str, Any] = {
        "call_ref": CALL,
        "tool_name": "edit",
        "tool_class": "edit",
        "phase": "requested",
    }
    return ProviderToolPayload.model_validate(fields | overrides)


def _record(kind: RunEventKind, payload: Any) -> RunEventRecord:
    return RunEventRecord.model_validate(
        {
            "event_ref": "EVT-00000001",
            "run_ref": RUN_URN,
            "run_sequence": 1,
            "event_kind": kind,
            "provenance": "provider_native",
            "payload": payload,
            "actor": ACTOR,
            "recorded_at": "2026-10-06T12:00:00Z",
        }
    )


def _dumped(items: tuple[TranscriptItem, ...] | list[TranscriptItem]) -> str:
    return json.dumps([item.model_dump(mode="json") for item in items])


# ---- CR-1: the provider tool payload -----------------------------------------------------


@pytest.mark.parametrize("name", ["a", "edit", "mcp__eawf__repo_read", "a" + "b" * 63])
def test_a_tool_name_in_grammar_is_admitted(name: str) -> None:
    assert _tool(tool_name=name).tool_name == name


@pytest.mark.parametrize("name", ["", "Edit", "9lives", "_edit", "a" + "b" * 64, "web-fetch"])
def test_a_tool_name_out_of_grammar_is_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        _tool(tool_name=name)


@pytest.mark.parametrize("bad", [{"tool_name": 7}, {"call_ref": None}, {"call_ref": "toolu_1"}])
def test_a_field_of_the_wrong_type_or_shape_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _tool(**bad)


def test_a_missing_field_or_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ProviderToolPayload.model_validate(
            {"call_ref": CALL, "tool_class": "edit", "phase": "requested"}
        )
    with pytest.raises(ValidationError):
        _tool(input={"path": SENTINEL})


@pytest.mark.parametrize("tool_class", ["", "write", "EDIT"])
def test_a_class_outside_the_six_is_refused(tool_class: str) -> None:
    with pytest.raises(ValidationError):
        _tool(tool_class=tool_class)


def test_the_six_classes_are_the_contract() -> None:
    assert set(get_args(ProviderToolClass)) == {
        "edit",
        "command",
        "read",
        "search",
        "delegate",
        "other",
    }


def test_the_outcome_belongs_to_the_result_alone() -> None:
    assert _tool(phase="result", outcome="ok").outcome == "ok"
    assert _tool(phase="result", outcome="error").outcome == "error"
    with pytest.raises(ValidationError, match="names its outcome"):
        _tool(phase="result")
    with pytest.raises(ValidationError, match="no outcome yet"):
        _tool(outcome="ok")
    with pytest.raises(ValidationError):
        _tool(phase="result", outcome="failed")
    with pytest.raises(ValidationError):
        _tool(phase="accepted")


def test_a_provider_tool_rides_on_tool_requested_and_tool_result() -> None:
    requested = _record(RunEventKind.TOOL_REQUESTED, _tool())
    result = _record(RunEventKind.TOOL_RESULT, _tool(phase="result", outcome="ok"))

    assert isinstance(requested.payload, ProviderToolPayload)
    assert isinstance(result.payload, ProviderToolPayload)
    assert EVENT_CONTRACTS[RunEventKind.TOOL_REQUESTED].provider_payload_kind is (
        EventPayloadKind.PROVIDER_TOOL
    )
    assert EVENT_CONTRACTS[RunEventKind.TOOL_ACCEPTED].provider_payload_kind is None


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (RunEventKind.TOOL_ACCEPTED, {"phase": "requested"}),
        (RunEventKind.MESSAGE_SUMMARIZED, {"phase": "requested"}),
        (RunEventKind.TOOL_RESULT, {"phase": "requested"}),
        (RunEventKind.TOOL_REQUESTED, {"phase": "result", "outcome": "ok"}),
    ],
)
def test_a_provider_tool_on_another_kind_or_phase_is_refused(
    kind: RunEventKind, payload: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        _record(kind, _tool(**payload))


def test_the_gateway_tool_payload_and_its_receipt_invariant_are_unchanged() -> None:
    gateway = {"call_ref": CALL, "tool_id": "repo_read", "phase": "result"}
    with pytest.raises(ValidationError, match="exactly one of its receipt and its error"):
        ToolPayload.model_validate(gateway)
    receipted = ToolPayload.model_validate(gateway | {"result_ref": "receipt-00000000000000c1"})
    assert _record(RunEventKind.TOOL_RESULT, receipted).payload == receipted
    assert EVENT_CONTRACTS[RunEventKind.TOOL_RESULT].payload_kind is EventPayloadKind.TOOL


def test_a_provider_payload_whose_phases_omit_the_pinned_one_is_refused_at_compile() -> None:
    declared: dict[RunEventKind, tuple[EventPayloadKind, str | None]] = dict.fromkeys(
        RunEventKind, (EventPayloadKind.HEARTBEAT, None)
    )
    declared[RunEventKind.TOOL_ACCEPTED] = (EventPayloadKind.TOOL, "accepted")
    phases: dict[EventPayloadKind, tuple[str, ...]] = dict.fromkeys(EventPayloadKind, ())
    phases[EventPayloadKind.TOOL] = ("accepted",)
    phases[EventPayloadKind.PROVIDER_TOOL] = ("requested", "result")

    with pytest.raises(ValueError, match="provider payload 'provider_tool' does not admit"):
        compile_event_contracts(
            declared,
            phases=phases,
            provider_payloads={RunEventKind.TOOL_ACCEPTED: EventPayloadKind.PROVIDER_TOOL},
        )


# ---- names and call references ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "tool_"),
        ("E", "e"),
        ("Edit", "edit"),
        ("mcp__my-server__Do.It", "mcp__my_server__do_it"),
        ("9lives", "tool_9lives"),
        ("_x", "tool__x"),
        ("A" * 64, "a" * 64),
        ("A" * 65, "a" * 64),
    ],
)
def test_provider_tool_name_keeps_the_grammar(raw: str, expected: str) -> None:
    name = provider_tool_name(raw)

    assert name == expected
    assert TOOL_NAME.match(name)


def test_provider_tool_name_refuses_a_non_string() -> None:
    with pytest.raises(AttributeError):
        provider_tool_name(None)  # type: ignore[arg-type]


def test_provider_call_ref_is_stable_and_in_the_call_grammar() -> None:
    assert provider_call_ref("toolu_1") == provider_call_ref("toolu_1")
    assert provider_call_ref("toolu_1") != provider_call_ref("toolu_2")
    assert re.fullmatch(r"call-[0-9a-f]{16}", provider_call_ref(""))


# ---- CR-2: the pinned class map --------------------------------------------------------

#: Every Claude name the map pins, and three it does not.
CLAUDE_CLASSES: Final[tuple[tuple[str, str], ...]] = (
    ("Edit", "edit"),
    ("MultiEdit", "edit"),
    ("Write", "edit"),
    ("NotebookEdit", "edit"),
    ("Bash", "command"),
    ("Read", "read"),
    ("Grep", "search"),
    ("Glob", "search"),
    ("WebSearch", "search"),
    ("WebFetch", "search"),
    ("Task", "delegate"),
    ("Agent", "delegate"),
    ("TodoWrite", "other"),
    ("mcp__eawf__repo_read", "other"),
    ("edit", "other"),
)


def _assistant(*blocks: Any, message_id: str | None = "m1") -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="fixture", message_id=message_id)


@pytest.mark.parametrize(("name", "tool_class"), CLAUDE_CLASSES)
def test_a_claude_tool_is_classed_by_the_pinned_map(name: str, tool_class: str) -> None:
    reader = ClaudeTranscriptItems()
    use = ToolUseBlock(id="toolu_1", name=name, input={"file_path": SENTINEL})
    done = ToolResultBlock(tool_use_id="toolu_1", content=SENTINEL, is_error=False)

    asked = reader.items(_assistant(use))
    answered = reader.items(UserMessage(content=[done]))

    tools = [item for item in (*asked, *answered) if isinstance(item, ProviderToolPayload)]
    assert [(t.tool_name, t.tool_class, t.phase, t.outcome) for t in tools] == [
        (provider_tool_name(name), tool_class, "requested", None),
        (provider_tool_name(name), tool_class, "result", "ok"),
    ]
    assert tools[0].call_ref == tools[1].call_ref == provider_call_ref("toolu_1")
    assert SENTINEL not in _dumped((*asked, *answered))


def test_the_claude_map_pins_exactly_the_named_tools() -> None:
    assert {name: cls for name, cls in CLAUDE_CLASSES if cls != "other"} == dict(
        CLAUDE_TOOL_CLASSES
    )


@pytest.mark.parametrize("name", ["Task", "Agent"])
def test_a_delegation_still_yields_the_child_run_request(name: str) -> None:
    items = ClaudeTranscriptItems().items(
        _assistant(ToolUseBlock(id="toolu_9", name=name, input={"prompt": SENTINEL}))
    )

    assert [type(item) for item in items] == [ProviderToolPayload, ChildRunPayload]
    child = items[1]
    assert isinstance(child, ChildRunPayload)
    assert child.phase == "requested"
    assert child.delegation_request_ref.startswith("delegation://claude-code/")


def test_a_failed_claude_tool_result_is_an_error_and_an_unasked_one_is_not_stated() -> None:
    reader = ClaudeTranscriptItems()
    reader.items(_assistant(ToolUseBlock(id="toolu_1", name="Bash", input={})))

    items = reader.items(
        UserMessage(
            content=[
                ToolResultBlock(tool_use_id="toolu_1", content="exit 1", is_error=True),
                ToolResultBlock(tool_use_id="toolu_never_asked", content="?", is_error=False),
                TextBlock(text="not a tool result"),
            ]
        )
    )

    assert [(i.tool_name, i.outcome) for i in items if isinstance(i, ProviderToolPayload)] == [
        ("bash", "error")
    ]
    assert len(items) == 1


def test_claude_words_are_still_messages_and_other_messages_carry_nothing() -> None:
    reader = ClaudeTranscriptItems()

    said = reader.items(_assistant(TextBlock(text="Reading the module first.")))
    asked = reader.items(UserMessage(content="fix the cache"))
    empty = reader.items(UserMessage(content=[]))
    result = reader.items(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
        )
    )

    assert said == (
        SpokenMessage(
            message_role="assistant",
            summary="Reading the module first.",
            text="Reading the module first.",
        ),
    )
    assert asked == (
        SpokenMessage(message_role="user", summary="fix the cache", text="fix the cache"),
    )
    assert empty == () and result == ()


# ---- CR-3: thinking is a started marker, never text -------------------------------------


def test_a_thinking_block_is_one_started_marker_per_assistant_message() -> None:
    reader = ClaudeTranscriptItems()
    thought = ThinkingBlock(thinking=SENTINEL, signature="sig")

    first = reader.items(_assistant(thought, message_id="m1"))
    repeat = reader.items(_assistant(thought, TextBlock(text="done"), message_id="m1"))
    second = reader.items(_assistant(thought, message_id="m2"))

    assert first == (ReasoningSummaryPayload(phase="started"),)
    assert repeat == (SpokenMessage(message_role="assistant", summary="done", text="done"),)
    assert second == (ReasoningSummaryPayload(phase="started"),)
    assert SENTINEL not in _dumped((*first, *repeat, *second))


def test_an_unidentified_thinking_message_is_marked_each_time() -> None:
    reader = ClaudeTranscriptItems()
    thought = ThinkingBlock(thinking=SENTINEL, signature="sig")

    marks = [reader.items(_assistant(thought, message_id=None)) for _ in range(2)]

    assert marks == [(ReasoningSummaryPayload(phase="started"),)] * 2


# ---- Codex items -------------------------------------------------------------------------


def _codex_item(item_type: str, item_id: str, **fields: Any) -> dict[str, Any]:
    return {"id": item_id, "type": item_type, **fields}


def _started(item: dict[str, Any]) -> ItemStartedNotification:
    return ItemStartedNotification.model_validate(
        {"item": item, "startedAtMs": 0, "threadId": THREAD_ID, "turnId": TURN_ID}
    )


def _completed(item: dict[str, Any]) -> ItemCompletedNotification:
    return ItemCompletedNotification.model_validate(
        {"item": item, "completedAtMs": 0, "threadId": THREAD_ID, "turnId": TURN_ID}
    )


def _command(status: str, exit_code: int | None = None) -> dict[str, Any]:
    return _codex_item(
        "commandExecution",
        "cmd-1",
        command=f"cat {SENTINEL}",
        commandActions=[],
        cwd="ws",
        status=status,
        exitCode=exit_code,
        aggregatedOutput=SENTINEL,
    )


def _patch(status: str) -> dict[str, Any]:
    return _codex_item("fileChange", "patch-1", changes=[], status=status)


def _mcp(status: str, error: dict[str, Any] | None = None) -> dict[str, Any]:
    return _codex_item(
        "mcpToolCall",
        "mcp-1",
        arguments={"q": SENTINEL},
        server="eawf",
        tool="repo_read",
        status=status,
        error=error,
    )


@pytest.mark.parametrize(
    ("item", "name", "tool_class", "outcome"),
    [
        (_command("completed", 0), CODEX_COMMAND_TOOL, "command", "ok"),
        (_command("completed", 2), CODEX_COMMAND_TOOL, "command", "error"),
        (_command("declined"), CODEX_COMMAND_TOOL, "command", "error"),
        (_patch("completed"), CODEX_PATCH_TOOL, "edit", "ok"),
        (_patch("failed"), CODEX_PATCH_TOOL, "edit", "error"),
        (_mcp("completed"), "mcp__eawf__repo_read", "other", "ok"),
        (_mcp("completed", {"message": "boom"}), "mcp__eawf__repo_read", "other", "error"),
        (
            _codex_item("webSearch", "web-1", query=SENTINEL),
            "web_search",
            "other",
            "ok",
        ),
        (
            _codex_item(
                "dynamicToolCall",
                "dyn-1",
                arguments={},
                namespace="Lint",
                status="completed",
                success=False,
                tool="Check",
            ),
            "lint__check",
            "other",
            "error",
        ),
        (
            _codex_item(
                "collabAgentToolCall",
                "collab-1",
                agentsStates={},
                receiverThreadIds=[],
                senderThreadId=THREAD_ID,
                status="completed",
                tool="spawnAgent",
            ),
            "spawn_agent",
            "other",
            "ok",
        ),
    ],
)
def test_a_codex_tool_item_is_requested_on_start_and_ends_on_completion(
    item: dict[str, Any], name: str, tool_class: str, outcome: str
) -> None:
    reader = CodexTranscriptItems()

    items = (*reader.items(_started(item)), *reader.items(_completed(item)))

    assert [
        (i.tool_name, i.tool_class, i.phase, i.outcome)
        for i in items
        if isinstance(i, ProviderToolPayload)
    ] == [(name, tool_class, "requested", None), (name, tool_class, "result", outcome)]
    assert SENTINEL not in _dumped(items)


def test_a_codex_reasoning_item_is_one_marker_and_an_agent_message_is_left_to_the_launcher() -> (
    None
):
    reader = CodexTranscriptItems()
    reasoning = _codex_item("reasoning", "r-1", summary=[SENTINEL], content=[SENTINEL])
    message = _codex_item("agentMessage", "msg-1", text="hello")

    items = (
        *reader.items(_started(reasoning)),
        *reader.items(_completed(reasoning)),
        *reader.items(_completed(message)),
        *reader.items(_started(_codex_item("reasoning", "r-2"))),
    )

    assert items == (ReasoningSummaryPayload(phase="started"),) * 2
    assert SENTINEL not in _dumped(items)


# ---- the dispatch sink and the end-to-end runs ------------------------------------------


@pytest.fixture
def canary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CanaryProvision:
    """A canary holding one running Run, its scratch kept under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    provisioned = provision(tmp_path / "repo", code="ITEM")
    seed(provisioned, {Epoch2Collection.RUN.value: {"RUN-00000010": seed_row("run", "RUNNING")}})
    return provisioned


def _sink(canary: CanaryProvision, runtime: Path) -> Any:
    context = method_context(runtime).native_root_context(tree_root(canary))
    urn = RunScopedParams.model_validate({"urn": RUN_URN}).urn
    return message_sink(context, urn, actor=ACTOR, attempt_ref=ATTEMPT)


def _events(canary: CanaryProvision) -> tuple[RunEventRecord, ...]:
    records = read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))
    return run_events_of(records, RunScopedParams.model_validate({"urn": RUN_URN}).urn)


def _files(*roots: Path) -> Iterator[Path]:
    for root in roots:
        yield from (path for path in root.rglob("*") if path.is_file())


def _hits(*roots: Path) -> list[Path]:
    """Return every file under *roots* whose bytes hold the sentinel."""
    return [path for path in _files(*roots) if SENTINEL.encode() in path.read_bytes()]


def test_the_sink_states_each_item_as_its_kind(canary: CanaryProvision, tmp_path: Path) -> None:
    sink = _sink(canary, tmp_path / "runtime")
    items: list[TranscriptItem] = [
        ReasoningSummaryPayload(phase="started"),
        MessageSummaryPayload(message_role="assistant", summary="Editing the cache."),
        _tool(),
        _tool(phase="result", outcome="ok"),
        ChildRunPayload(
            child_run_ref=None,
            delegation_request_ref="delegation://claude-code/0a1b",
            phase="requested",
        ),
    ]

    async def say() -> None:
        for item in items:
            await sink(item)

    asyncio.run(say())

    events = _events(canary)
    assert [e.event_kind for e in events] == [
        RunEventKind.REASONING_STARTED,
        RunEventKind.MESSAGE_SUMMARIZED,
        RunEventKind.TOOL_REQUESTED,
        RunEventKind.TOOL_RESULT,
        RunEventKind.CHILD_RUN_REQUESTED,
    ]
    assert [e.run_sequence for e in events] == [1, 2, 3, 4, 5]
    assert [e.payload for e in events] == items
    # a message keeps the line id it had before other items were stated
    assert events[1].event_ref == _event_ref(ATTEMPT, "message:0")


@posix_only
def test_claude_thinking_text_reaches_no_ledger_line_and_no_store(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", claude_fx._bypass_jail)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime = tmp_path / "runtime"
    thought = {"type": "thinking", "thinking": SENTINEL, "signature": "sig"}
    edit = {"type": "tool_use", "id": "toolu_1", "name": "Edit", "input": {"old": SENTINEL}}
    script = {
        "lines": [
            {"type": "assistant", "message": {"id": "m1", "content": [thought]}},
            {"type": "assistant", "message": {"id": "m1", "content": [thought]}},
            {"type": "assistant", "message": {"id": "m1", "content": [edit]}},
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": SENTINEL}
                    ]
                },
            },
            {"type": "result", "result": "done"},
        ]
    }
    request = claude_fx._request(workspace).model_copy(
        update={"message_sink": _sink(canary, runtime)}
    )

    _, turn = asyncio.run(
        claude_fx._launch_and_turn(claude_fx._launcher(workspace, script), request)
    )

    assert turn.exit_kind == "completed"
    events = _events(canary)
    assert [e.event_kind for e in events] == [
        RunEventKind.REASONING_STARTED,
        RunEventKind.TOOL_REQUESTED,
        RunEventKind.TOOL_RESULT,
    ]
    assert events[1].payload == _tool(call_ref=provider_call_ref("toolu_1"))
    assert _hits(workspace), "the provider did say the sentinel"
    assert _hits(canary.root, runtime, tmp_path / "scratch") == []


@posix_only
def test_codex_reasoning_and_tool_items_reach_the_sink_without_their_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(codex_adapter, "_maybe_jail_argv", codex_fx._bypass_jail)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    reasoning = _codex_item("reasoning", "r-1", summary=[SENTINEL], content=[SENTINEL])

    def note(method: str, item: dict[str, Any]) -> dict[str, Any]:
        stamp = "startedAtMs" if method == "item/started" else "completedAtMs"
        return {
            "method": method,
            "params": {"item": item, stamp: 0, "threadId": THREAD_ID, "turnId": TURN_ID},
        }

    script = {
        "lines": [
            note("item/started", reasoning),
            note("item/completed", reasoning),
            note("item/started", _patch("inProgress")),
            note("item/completed", _patch("completed")),
            note("item/started", _command("inProgress")),
            note("item/completed", _command("failed", 1)),
            turn_completed(),
        ]
    }
    relayed: list[TranscriptItem] = []

    async def sink(item: TranscriptItem) -> None:
        relayed.append(item)

    request = codex_fx._request(workspace).model_copy(update={"message_sink": sink})

    _, turn = asyncio.run(codex_fx._launch_and_turn(codex_fx._launcher(workspace, script), request))

    assert turn.exit_kind == "completed"
    assert relayed[0] == ReasoningSummaryPayload(phase="started")
    assert [
        (i.tool_class, i.phase, i.outcome) for i in relayed if isinstance(i, ProviderToolPayload)
    ] == [
        ("edit", "requested", None),
        ("edit", "result", "ok"),
        ("command", "requested", None),
        ("command", "result", "error"),
    ]
    assert len(relayed) == 5
    assert SENTINEL not in _dumped(relayed)
