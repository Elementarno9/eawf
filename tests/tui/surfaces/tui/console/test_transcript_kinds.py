"""Every transcript block kind, drawn from its packet-exact payload and read through the seam.

CON-169 and CON-170 name nine kinds -- message, tool, file, question, error, thinking,
subagent, background and running -- and RUN-062 types the payload behind each. The lines
here are typed test events: the message, subagent, thinking and background lines have
producers today, and the tool, file, question and error lines are the shapes their
producers will append. Each reaches the console the way a live one reads it, through the
seam's one live read of the Run's stream, which takes every kind the daemon holds with no
wiring per kind.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Any, Final

from eawf.kernel.projection.transcript import TRANSCRIPT_ROUTE
from eawf.kernel.runtime.events import (
    ChildRunPayload,
    ErrorPayload,
    FileChangePayload,
    MessageSummaryPayload,
    QuestionActionPayload,
    RunEventKind,
    RunEventRecord,
    ToolPayload,
)
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.renderers import transcript
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES
from tests.tui.surfaces.tui.console import journey_support as js
from tests.tui.surfaces.tui.console import test_transcript_blocks as tb

CALL_DONE: Final = "call-00000000000000a1"
CALL_GOING: Final = "call-00000000000000a2"
RECEIPT: Final = "receipt-00000000000000a1"
QUESTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/question/QST-0001"
CHILD: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"

#: A clock and a kind in full at the head of a block row.
BLOCK_HEAD: Final = re.compile(r"^[ ▸]\d\d:\d\d:\d\d  (?P<glyph>\S) (?P<word>[a-z]+)")


def _line(sequence: int, kind: RunEventKind, payload: Any, at: int) -> RunEventRecord:
    return tb._event(sequence, event_kind=kind, payload=payload).model_copy(
        update={"recorded_at": tb._at(at)}
    )


def _tool(call: str, phase: str, **outcome: Any) -> ToolPayload:
    return ToolPayload.model_validate(
        {"call_ref": call, "tool_id": "repo_read", "phase": phase, **outcome}
    )


def nine_kinds() -> tuple[RunEventRecord, ...]:
    """Return a stream holding one block of every kind, the last four still in flight."""
    file = FileChangePayload(
        repository_ref="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF",  # type: ignore[arg-type]
        changed_paths=("src/replay.py", "src/normalize.py"),
        before_tree_digest="sha256:" + "a" * 64,
        after_tree_digest="sha256:" + "b" * 64,
        diff_ref="artifact://runs/diff-0001",
    )
    child = ChildRunPayload.model_validate(
        {
            "child_run_ref": CHILD,
            "delegation_request_ref": "delegation://claude/0a1b",
            "phase": "started",
        }
    )
    lines = (
        (
            RunEventKind.MESSAGE_SUMMARIZED,
            MessageSummaryPayload(
                message_role="assistant", summary="Reading the replay window first."
            ),
            0,
        ),
        (RunEventKind.TOOL_REQUESTED, _tool(CALL_DONE, "requested"), 2),
        (RunEventKind.TOOL_ACCEPTED, _tool(CALL_DONE, "accepted"), 3),
        (RunEventKind.TOOL_RESULT, _tool(CALL_DONE, "result", result_ref=RECEIPT), 9),
        (RunEventKind.FILE_CHANGED, file, 12),
        (
            RunEventKind.ERROR_OBSERVED,
            ErrorPayload(
                code="test_failed",
                retry_class="after_input_change",
                message="test_replay_gap failed with 2 inversions",
            ),
            15,
        ),
        (
            RunEventKind.COMMAND_STARTED,
            tb._command(7, command_ref="CMD-000000b1", execution="background").payload,
            20,
        ),
        (RunEventKind.CHILD_RUN_STARTED, child, 22),
        (
            RunEventKind.QUESTION_RAISED,
            QuestionActionPayload.model_validate({"subject_ref": QUESTION, "phase": "raised"}),
            30,
        ),
        (RunEventKind.TOOL_ACCEPTED, _tool(CALL_GOING, "accepted"), 40),
        (RunEventKind.REASONING_STARTED, tb._event(1).payload, 42),
    )
    return tuple(_line(n, kind, payload, at) for n, (kind, payload, at) in enumerate(lines, 1))


def frames(events: tuple[RunEventRecord, ...], keys: Sequence[str] = ()) -> list[str]:
    """Return RUN-00000010's transcript at every width after *keys*, read through the seam."""
    daemon = js.DocumentDaemon(tb.DOCUMENT)
    daemon.run_events = {tb.DOCUMENT["run"]["RUN-00000010"]["urn"]: events, CHILD: ()}

    async def body() -> list[str]:
        seam = ProjectionSeam(
            route=TRANSCRIPT_ROUTE,
            scope_id=tb.SCOPE,
            state_path=None,
            clock=lambda: tb.AT,
            daemon_client_factory=daemon.client,
        )
        app = ConsoleApp(chrome=load_chrome(), clock=FakeClock(), seam=seam)
        async with js.driven(app) as harness:
            setup = {"route": TRANSCRIPT_ROUTE, "subjId": "RUN-00000010"}
            return [
                (await js.walk(harness, {**setup, "size": n}, list(keys)))[-1]
                for n in range(len(SIZES))
            ]

    return asyncio.run(body())


def _kinds(frame: str) -> list[tuple[str, str]]:
    return [
        (m.group("glyph"), m.group("word")) for r in frame.split("\n") if (m := BLOCK_HEAD.match(r))
    ]


def test_con_169_con_170_every_kind_renders_as_its_glyph_and_word_at_every_width() -> None:
    """CON-169, CON-170, RUN-062: all nine kinds, each glyph beside its full word."""
    expected = [
        "message",
        "tool",
        "tool",
        "tool",
        "file",
        "error",
        "background",
        "subagent",
        "question",
        "running",
        "thinking",
    ]
    for shot in frames(nine_kinds()):
        drawn = _kinds(shot)
        assert [word for _glyph, word in drawn][-len(expected) :] == expected[-len(drawn) :]
        for glyph, word in drawn:
            assert glyph == transcript.NATIVE_GLYPH[word]


def test_con_170_the_header_states_thinking_and_what_runs_in_the_background() -> None:
    for shot in frames(nine_kinds()):
        context = shot.split("\n")[1]
        assert "THINKING for 0s" in context
        # the background command and the subagent; the foreground tool call holds the Run
        assert "· 2 running in the background ·" in context


def test_con_170_work_in_flight_states_how_long_and_a_question_what_it_waits_for() -> None:
    wide = frames(nine_kinds())[1]
    question = next(r for r in wide.split("\n") if "? question" in r)
    running = next(r for r in wide.split("\n") if "⋯ running" in r)
    assert "QST-0001 · raised" in question and re.search(r"waiting 12s\s*$", question)
    # the typical time is the finished call of the same tool, derived, never a countdown
    assert "repo_read · accepted" in running and re.search(r"2s · ~6s\s*$", running)


def _opened(kind: str) -> str:
    """Return the wide frame with the block of *kind* unfolded."""
    events = nine_kinds()
    shots = frames(events, keys=[*(["ArrowUp"] * (len(events) - 1 - _index(kind))), "Enter"])
    return shots[1]


def _index(kind: str) -> int:
    return {"tool": 3, "file": 4, "error": 5, "question": 8}[kind]


def test_con_170_a_tool_opens_on_the_receipt_its_output_is_held_under() -> None:
    opened = _opened("tool")
    assert "repo_read · result · succeeded" in opened
    assert re.search(rf"OUTPUT +held under receipt {RECEIPT}", opened)


def test_con_170_a_file_opens_on_its_paths_and_the_diff_it_is_held_at() -> None:
    opened = _opened("file")
    assert "src/replay.py and 1 more path" in opened
    assert re.search(r"PATHS +src/replay\.py, src/normalize\.py", opened)
    assert re.search(r"DIFF +held at artifact://runs/diff-0001", opened)


def test_con_170_an_error_opens_on_its_code_retry_class_and_trace() -> None:
    opened = _opened("error")
    assert "! error      test_replay_gap failed with 2 inversions" in opened
    assert re.search(r"CODE +test_failed", opened)
    assert re.search(r"RETRY +after the input changes", opened)
    assert re.search(r"TRACE +no trace was kept", opened)


def test_con_170_a_question_opens_on_what_it_waits_on() -> None:
    assert re.search(r"WAITS ON +an answer to QST-0001", _opened("question"))
