"""The records a host subagent's adoption writes, and the bridge that reads its transcript.

SURF-054: the transcript bridge turns what a host subagent said into typed message
payloads and what it delegated into child-run payloads, and a Run carries the Run that
delegated it. SURF-075: the transcript route renders those payloads as the Run's own
words and a spawned subagent as work elsewhere.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.projection.transcript import block_text
from eawf.kernel.runtime.events import (
    EVENT_CONTRACTS,
    SUPPORTED_EVENT_KINDS,
    ChildRunPayload,
    EventPayloadKind,
    MessageSummaryPayload,
    RunEventKind,
    RunEventRecord,
)
from eawf.kernel.state.epoch2.run import Run, RunCreateSpec
from eawf.runtime.runtimes.host_transcript import (
    SUMMARY_LIMIT,
    WITHHELD_TEXT,
    read_host_transcript,
)
from tests import _provider_helpers as fx

AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
DELEGATION: Final = "delegation://claude-code/0123abcd"
OTHER_RUN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"
#: Leak-shaped inputs the bridge and the reference grammar must refuse.
HOST_PATH_REF: Final = "delegation:///Users/x"  # pragma: allowlist secret
LEAKY_TEXT: Final = "see /home/someone/notes"  # pragma: allowlist secret


def event(kind: RunEventKind, payload: Any) -> RunEventRecord:
    """Return one event line carrying *payload*."""
    return RunEventRecord.model_validate(
        {
            "event_ref": "EVT-0000000a",
            "run_ref": str(fx.RUN_URN),
            "run_sequence": 1,
            "event_kind": kind,
            "provenance": "provider_native",
            "payload": payload,
            "actor": "HARNESS-CLAUDE-CODE",
            "recorded_at": AT,
        }
    )


def message(**overrides: Any) -> dict[str, Any]:
    return {
        "payload_kind": "message_summary",
        "message_role": "assistant",
        "summary": "done",
        **overrides,
    }


def child() -> dict[str, Any]:
    return {
        "payload_kind": "child_run",
        "child_run_ref": None,
        "delegation_request_ref": DELEGATION,
        "phase": "requested",
    }


# ---- the payload models ---------------------------------------------------------


@pytest.mark.parametrize("summary", ["x", "x" * SUMMARY_LIMIT], ids=["single", "at-limit"])
def test_surf_054_message_summary_holds_one_to_limit_characters(summary: str) -> None:
    assert MessageSummaryPayload.model_validate(message(summary=summary)).summary == summary


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"summary": ""}, id="empty"),
        pytest.param({"summary": "   "}, id="whitespace-only"),
        pytest.param({"summary": "x" * (SUMMARY_LIMIT + 1)}, id="one-over-limit"),
        pytest.param({"summary": 7}, id="not-a-string"),
        pytest.param({"message_role": "narrator"}, id="unknown-role"),
        pytest.param({"truncated": "yes"}, id="truncated-not-a-bool"),
        pytest.param({"extra": 1}, id="unknown-key"),
    ],
)
def test_surf_054_message_summary_refuses_a_malformed_message(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MessageSummaryPayload.model_validate(message(**overrides))


def test_surf_054_message_summary_refuses_a_missing_role() -> None:
    with pytest.raises(ValidationError, match="message_role"):
        MessageSummaryPayload.model_validate({"summary": "done"})


def test_surf_054_child_run_terminal_phase_requires_the_end_status() -> None:
    with pytest.raises(ValidationError, match="names how the child ended"):
        ChildRunPayload.model_validate(child() | {"phase": "terminal", "child_run_ref": OTHER_RUN})
    ended = ChildRunPayload.model_validate(
        child() | {"phase": "terminal", "child_run_ref": OTHER_RUN, "terminal_status": "COMPLETED"}
    )
    assert ended.terminal_status is not None


@pytest.mark.parametrize("phase", ["requested", "started"])
def test_surf_054_child_run_earlier_phases_forbid_an_end_status(phase: str) -> None:
    with pytest.raises(ValidationError, match="no terminal status yet"):
        ChildRunPayload.model_validate(child() | {"phase": phase, "terminal_status": "FAILED"})


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"delegation_request_ref": "artifact://x"}, id="wrong-scheme"),
        pytest.param({"delegation_request_ref": HOST_PATH_REF}, id="host-path"),
        pytest.param({"child_run_ref": "RUN-00000011"}, id="bare-key-not-urn"),
        pytest.param({"phase": "running"}, id="unknown-phase"),
    ],
)
def test_surf_054_child_run_refuses_malformed_references(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ChildRunPayload.model_validate(child() | overrides)


def test_surf_054_message_and_child_kinds_are_appendable() -> None:
    assert RunEventKind.MESSAGE_SUMMARIZED in SUPPORTED_EVENT_KINDS
    assert {
        RunEventKind.CHILD_RUN_REQUESTED,
        RunEventKind.CHILD_RUN_STARTED,
        RunEventKind.CHILD_RUN_TERMINAL,
    } <= SUPPORTED_EVENT_KINDS
    assert EVENT_CONTRACTS[RunEventKind.MESSAGE_SUMMARIZED].payload_kind is (
        EventPayloadKind.MESSAGE_SUMMARY
    )


def test_surf_054_event_kind_and_payload_must_agree() -> None:
    with pytest.raises(ValidationError, match="carries payload"):
        event(RunEventKind.MESSAGE_SUMMARIZED, child())
    with pytest.raises(ValidationError, match="'started' phase"):
        event(RunEventKind.CHILD_RUN_STARTED, child())


# ---- SURF-075: what a block says ---------------------------------------------------


def test_surf_075_a_message_block_says_the_runs_own_words() -> None:
    line = event(RunEventKind.MESSAGE_SUMMARIZED, message(summary="There are 42 modules."))
    assert block_text(line).value == "There are 42 modules."


def test_surf_075_a_child_block_says_it_works_elsewhere() -> None:
    requested = event(RunEventKind.CHILD_RUN_REQUESTED, child())
    ended = event(
        RunEventKind.CHILD_RUN_TERMINAL,
        child() | {"phase": "terminal", "child_run_ref": OTHER_RUN, "terminal_status": "FAILED"},
    )
    assert block_text(requested).value == "a subagent · requested · working elsewhere"
    assert block_text(ended).value == "RUN-00000011 · terminal · failed · working elsewhere"


# ---- the Run's delegation lineage ------------------------------------------------


def run_row(**overrides: Any) -> dict[str, Any]:
    return {
        "uid": "5b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "key": "RUN-00000010",
        "urn": str(fx.RUN_URN),
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": AT,
        "updated_at": AT,
        "scope": {
            "scope_kind": "repository",
            "purpose": "observe",
            "repository_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF",
        },
        "status": "QUEUED",
        **overrides,
    }


def test_surf_054_a_run_names_the_run_that_delegated_it() -> None:
    run = Run.model_validate(run_row(parent_run_ref=OTHER_RUN))
    assert run.parent_run_ref is not None
    assert run.parent_run_ref.entity_key == "RUN-00000011"
    assert Run.model_validate(run_row()).parent_run_ref is None


def test_surf_054_a_run_cannot_delegate_itself() -> None:
    with pytest.raises(ValidationError, match="must name another Run"):
        Run.model_validate(run_row(parent_run_ref=str(fx.RUN_URN)))


def test_surf_054_parent_must_be_a_run_urn() -> None:
    with pytest.raises(ValidationError):
        RunCreateSpec.model_validate(
            {
                "key": "RUN-00000010",
                "scope": run_row()["scope"],
                "parent_run_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042",
            }
        )


# ---- SURF-054: the transcript bridge ----------------------------------------------


def write(path: Path, lines: list[Any]) -> Path:
    path.write_text(
        "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines),
        encoding="utf-8",
    )
    return path


def said(role: str, content: Any) -> dict[str, Any]:
    return {"type": role, "message": {"role": role, "content": content}}


def test_surf_054_bridge_of_a_missing_or_empty_transcript_is_empty(tmp_path: Path) -> None:
    assert read_host_transcript(tmp_path / "absent.jsonl", harness="claude-code") == ()
    assert read_host_transcript(write(tmp_path / "e.jsonl", []), harness="claude-code") == ()


def test_surf_054_bridge_of_a_directory_is_empty(tmp_path: Path) -> None:
    assert read_host_transcript(tmp_path, harness="codex") == ()


def test_surf_054_bridge_skips_malformed_lines_and_non_messages(tmp_path: Path) -> None:
    path = write(
        tmp_path / "t.jsonl",
        [
            "{not json",
            "[1, 2]",
            {"type": "summary", "summary": "compaction"},
            said("user", "   "),
            said("assistant", 42),
            said("assistant", [{"type": "thinking", "thinking": "hidden"}]),
            said("user", "the prompt"),
        ],
    )
    (only,) = read_host_transcript(path, harness="claude-code")
    assert isinstance(only, MessageSummaryPayload)
    assert (only.message_role, only.summary, only.truncated) == ("user", "the prompt", False)


def test_surf_054_bridge_cuts_at_the_limit_and_says_so(tmp_path: Path) -> None:
    at_limit = "a" * SUMMARY_LIMIT
    over = "b" * (SUMMARY_LIMIT + 1)
    path = write(
        tmp_path / "t.jsonl",
        [said("assistant", [{"type": "text", "text": at_limit}]), said("assistant", over)],
    )
    first, second = read_host_transcript(path, harness="claude-code")
    assert isinstance(first, MessageSummaryPayload)
    assert isinstance(second, MessageSummaryPayload)
    assert (first.summary, first.truncated) == (at_limit, False)
    assert (len(second.summary), second.truncated) == (SUMMARY_LIMIT, True)


def test_surf_054_bridge_withholds_a_message_carrying_a_leak_shape(tmp_path: Path) -> None:
    path = write(tmp_path / "t.jsonl", [said("assistant", LEAKY_TEXT)])
    (only,) = read_host_transcript(path, harness="claude-code")
    assert isinstance(only, MessageSummaryPayload)
    assert (only.summary, only.truncated) == (WITHHELD_TEXT, True)


def test_surf_054_bridge_draws_a_spawn_as_a_requested_child(tmp_path: Path) -> None:
    spawn = {"type": "tool_use", "id": "toolu_01Spawn", "name": "Task", "input": {}}
    path = write(
        tmp_path / "t.jsonl",
        [
            said("assistant", [spawn, {"type": "tool_use", "id": "x", "name": "Bash"}]),
            said("assistant", [{"type": "tool_use", "id": "", "name": "Agent"}]),
        ],
    )
    (only,) = read_host_transcript(path, harness="claude-code")
    again = read_host_transcript(path, harness="claude-code")
    assert isinstance(only, ChildRunPayload)
    assert (only.phase, only.child_run_ref) == ("requested", None)
    assert only.delegation_request_ref.startswith("delegation://claude-code/")
    assert again == (only,)


def test_surf_054_bridge_reads_codex_user_and_assistant_but_not_developer(tmp_path: Path) -> None:
    def item(role: str, kind: str, text: str) -> dict[str, Any]:
        return {
            "type": "response_item",
            "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]},
        }

    path = write(
        tmp_path / "rollout.jsonl",
        [
            item("developer", "input_text", "instructions"),
            item("user", "input_text", "do it"),
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "dup"}},
            item("assistant", "output_text", "did it"),
        ],
    )
    bridged = read_host_transcript(path, harness="codex")
    assert [
        (p.message_role, p.summary) for p in bridged if isinstance(p, MessageSummaryPayload)
    ] == [
        ("user", "do it"),
        ("assistant", "did it"),
    ]
