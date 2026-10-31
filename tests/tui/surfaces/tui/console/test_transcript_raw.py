"""The transcript route draws the runtime's own transcript files in full and follows them.

The event blocks carry the summaries the hooks recorded, cut at 500 characters. The raw
view, which ``r`` swaps in, reads the files the local transcript index ties to the Run
through the run-transcript library and draws every entry whole, wrapped rather than cut.
While the route stays on screen the console's refresh tick reads each file past where it
stopped, so an appended line appears; a line still being written waits for its newline.
A Run with no indexed transcript says so instead of drawing an empty body.

The console is driven under the Textual pilot over a document daemon, with the tree the
transcript index lives in as its launch directory. The refresh tick is driven by the
console's fake clock, so no test waits on the wall clock.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.runtime.runtimes.run_transcript import TranscriptEntry, TranscriptSource
from eawf.runtime.session.transcript_index import record_transcript
from eawf.surfaces.tui.console.app import LIVE_REFRESH_SECONDS, ConsoleApp
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.harness import settle
from eawf.surfaces.tui.console.live_reads import (
    RAW_TRANSCRIPT_READ,
    HeldRawTranscript,
    read_raw_transcript,
)
from eawf.surfaces.tui.console.renderers.transcript import (
    NO_RAW_READ,
    raw_lines,
)
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SessionSetup
from tests.contract.surfaces.cli._reflect_tree import RUN_PREFIX, native_tree, run_row
from tests.tui.surfaces.tui.console import journey_support as js
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies

#: The Run whose session wrote a transcript, and a Run nothing indexed.
INDEXED: Final = "RUN-00000001"
UNINDEXED: Final = "RUN-00000002"

#: When the index rows are stamped.
NOW: Final = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)

#: An assistant message longer than the 500 characters a summary keeps.
LONG_WORDS: Final = [f"w{n:03d}" for n in range(120)]
LONG_TEXT: Final = " ".join(LONG_WORDS) + " END-OF-ENTRY"

#: The pilot's terminal: 120 by 30.
SIZE: Final = 1


def _record(kind: str, content: Any) -> str:
    """Return one Claude Code transcript line, newline included."""
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}}) + "\n"


def _tree(tmp_path: Path, *, ended: bool = False) -> tuple[Path, Path]:
    """Return a repository holding two Runs, the first indexed with a transcript file.

    Args:
        tmp_path: Where the repository and the transcript file are made.
        ended: Whether the indexed Run has stopped, so a final read takes a last line
            without its newline.

    Returns:
        The repository root and the transcript file.
    """
    repo = tmp_path / "repo"
    status = "COMPLETED" if ended else "RUNNING"
    root = native_tree(repo, [run_row(INDEXED, status=status), run_row(UNINDEXED)], [])
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        _record("user", "Explain the loader.")
        + _record("assistant", [{"type": "text", "text": LONG_TEXT}]),
        encoding="utf-8",
    )
    record_transcript(
        root,
        runtime="claude-code",
        session_id="session-1",
        transcript_path=str(transcript),
        now=NOW,
        run_ref=f"{RUN_PREFIX}/{INDEXED}",
    )
    return repo, transcript


def _app(repo: Path | None) -> ConsoleApp:
    """Return a console over the document daemon, launched in *repo* when one is given."""
    daemon = js.DocumentDaemon(bodies.DOCUMENT)
    daemon.run_events = {row["urn"]: () for row in bodies.DOCUMENT["run"].values()}
    seam = ProjectionSeam(
        route="scope.home",
        scope_id=bodies.SCOPE,
        clock=lambda: bodies.AT,
        daemon_client_factory=daemon.client,
        repo_root=repo,
    )
    return ConsoleApp(chrome=load_chrome(), seam=seam, clock=FakeClock())


def _drive(app: ConsoleApp, scenario: Callable[[ConsoleApp, Any], Any]) -> None:
    async def body() -> None:
        async with js.driven(app, size=SIZE) as harness:
            await scenario(harness.app, harness.pilot)

    asyncio.run(body())


async def _open(app: ConsoleApp, pilot: Any, run: str) -> str:
    """Open the transcript of *run* and return its settled frame."""
    app.reset(SessionSetup(route="transcript", subjId=run))
    return await _frame(app, pilot)


async def _frame(app: ConsoleApp, pilot: Any) -> str:
    app.render_frame()
    text, _cycles = await settle(pilot)
    return str(text)


async def _press(app: ConsoleApp, pilot: Any, key: str) -> str:
    await pilot.press(key)
    return await _frame(app, pilot)


async def _poll(app: ConsoleApp, pilot: Any) -> str:
    """Fire the console's refresh tick once on its fake clock and return the frame."""
    clock = app.console_clock
    assert isinstance(clock, FakeClock)
    clock.advance(LIVE_REFRESH_SECONDS)
    app.tick()
    await settle(pilot)
    return await _frame(app, pilot)


def _words(frame: str) -> list[str]:
    """Return the words of the frame's rows, scrollbar and caret columns ignored."""
    return frame.replace("│", " ").replace("█", " ").replace("▸", " ").split()


# ---------- the console route ----------


def test_cr_129_the_raw_view_shows_an_entry_longer_than_500_characters_in_full(
    tmp_path: Path,
) -> None:
    """An assistant message of over 500 characters is drawn whole, wrapped, never cut."""
    assert len(LONG_TEXT) > 500
    repo, _transcript = _tree(tmp_path)

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        blocks = await _open(app, pilot, INDEXED)
        assert "r raw" in blocks, "the block view offers the raw key once the files are read"
        frame = await _press(app, pilot, "r")
        assert app.session.tr_raw
        assert "runtime transcript · 2 entries · 1 file · following" in frame
        assert f"claude-code transcript of {INDEXED}" in frame
        assert "Explain the loader." in frame
        words = _words(frame)
        start = words.index(LONG_WORDS[0])
        assert words[start : start + len(LONG_WORDS) + 1] == [*LONG_WORDS, "END-OF-ENTRY"]

    _drive(_app(repo), scenario)


def test_cr_129_the_raw_view_follows_a_line_appended_after_it_opened(tmp_path: Path) -> None:
    """A line appended while the view is open appears on the next tick, the cursor on it.

    A line the runtime has not finished writing waits for its newline, so it is never
    drawn as a corrupt line.
    """
    repo, transcript = _tree(tmp_path)
    appended = _record("assistant", [{"type": "text", "text": "Appended after open."}])

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open(app, pilot, INDEXED)
        await _press(app, pilot, "r")
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(appended[:20])
        half = await _poll(app, pilot)
        assert "Appended after open." not in half
        assert "not a JSON object" not in half
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(appended[20:])
        grown = await _poll(app, pilot)
        assert "Appended after open." in grown
        assert "3 entries" in grown
        caret = next(row for row in grown.split("\n") if row.lstrip().startswith("▸"))
        assert "Appended after open." in caret, "following keeps the cursor on the newest line"

    _drive(_app(repo), scenario)


def test_a_held_raw_view_counts_the_lines_that_arrive(tmp_path: Path) -> None:
    """Up holds the view where it is; an append is counted, and End follows it again."""
    repo, transcript = _tree(tmp_path)

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open(app, pilot, INDEXED)
        await _press(app, pilot, "r")
        held = await _press(app, pilot, "up")
        assert "· held · 0 new" in held
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(_record("user", "Go on."))
        grown = await _poll(app, pilot)
        assert "· held · 2 new" in grown, "a label line and its text arrived"
        newest = await _press(app, pilot, "end")
        assert "· following" in newest
        assert app.session.tr_sel == app.session.count - 1

    _drive(_app(repo), scenario)


def test_a_run_with_no_indexed_transcript_shows_an_empty_state(tmp_path: Path) -> None:
    """The raw view of a Run no transcript is indexed for says so in its body."""
    repo, _transcript = _tree(tmp_path)

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open(app, pilot, UNINDEXED)
        frame = await _press(app, pilot, "r")
        assert f"ENTRIES   ∅ no transcript is indexed for {UNINDEXED}" in frame
        assert "r raw" in frame and "y copy" not in frame, "nothing to copy or walk"

    _drive(_app(repo), scenario)


def test_r_swaps_back_to_the_event_blocks(tmp_path: Path) -> None:
    """A second r returns to the block view, following its tail."""
    repo, _transcript = _tree(tmp_path)

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open(app, pilot, INDEXED)
        await _press(app, pilot, "r")
        frame = await _press(app, pilot, "r")
        assert not app.session.tr_raw
        assert "runtime transcript" not in frame
        assert app.session.follow

    _drive(_app(repo), scenario)


def test_y_copies_the_whole_entry_under_the_cursor(tmp_path: Path) -> None:
    """Copy takes the entry the cursor's line belongs to, uncut."""
    repo, _transcript = _tree(tmp_path)
    app = _app(repo)
    copied: list[str] = []

    def copy_text(text: str) -> bool:
        copied.append(text)
        return True

    app.copy_text = copy_text  # type: ignore[method-assign]

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open(app, pilot, INDEXED)
        await _press(app, pilot, "r")
        await _press(app, pilot, "y")
        assert copied == [LONG_TEXT]

    _drive(app, scenario)


def test_a_console_launched_outside_a_tree_reads_no_file_and_says_so() -> None:
    """With no launch directory the raw key is not offered and answers why."""

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        blocks = await _open(app, pilot, INDEXED)
        assert "r raw" not in blocks
        assert app.seam is not None and app.seam.live(RAW_TRANSCRIPT_READ) is None
        frame = await _press(app, pilot, "r")
        assert not app.session.tr_raw
        assert "runtime transcript" not in frame
        assert any(NO_RAW_READ in toast.text for toast in app.session.toasts)

    _drive(_app(None), scenario)


# ---------- the read ----------


def test_a_read_grows_past_what_is_held_and_keeps_its_readers(tmp_path: Path) -> None:
    """A second read reuses each file's reader and adds only what was appended."""
    repo, transcript = _tree(tmp_path)
    tree = repo.resolve() / ".ea"
    first = read_raw_transcript(tree, INDEXED, None)
    assert [entry.kind for _source, entry in first.entries] == ["user", "assistant"]
    same = read_raw_transcript(tree, INDEXED, first)
    assert same == first, "nothing appended reads as an equal answer, so nothing repaints"
    assert same.readers == first.readers
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(_record("user", "Next."))
    grown = read_raw_transcript(tree, INDEXED, same)
    assert [entry.text for _source, entry in grown.entries][2:] == ["Next."]


def test_a_read_of_an_ended_run_takes_its_unfinished_last_line(tmp_path: Path) -> None:
    """Once the Run has stopped, a last line without its newline is read."""
    repo, transcript = _tree(tmp_path, ended=True)
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(_record("user", "Last words.").rstrip("\n"))
    held = read_raw_transcript(repo.resolve() / ".ea", INDEXED, None)
    assert held.entries[-1][1].text == "Last words."


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        (UNINDEXED, f"no transcript is indexed for {UNINDEXED}"),
        ("RUN-99999999", "the tree holds no Run keyed 'RUN-99999999'"),
    ],
    ids=["unindexed", "unknown"],
)
def test_a_read_that_selects_no_file_says_why(tmp_path: Path, key: str, reason: str) -> None:
    """An unindexed or unknown Run reads as no entry and the reason, never as an error."""
    repo, _transcript = _tree(tmp_path)
    held = read_raw_transcript(repo.resolve() / ".ea", key, None)
    assert held.entries == ()
    assert held.unindexed is not None and held.unindexed.startswith(reason)


def test_an_empty_transcript_file_reads_as_no_entry(tmp_path: Path) -> None:
    repo, transcript = _tree(tmp_path)
    transcript.write_text("", encoding="utf-8")
    held = read_raw_transcript(repo.resolve() / ".ea", INDEXED, None)
    assert held == HeldRawTranscript()


# ---------- the lines ----------

_MAIN = TranscriptSource(
    runtime="claude-code", run_key=INDEXED, path=Path("main.jsonl"), subagent=False
)
_SUB = TranscriptSource(runtime="claude-code", run_key=None, path=Path("sub.jsonl"), subagent=True)


def _held(*entries: tuple[TranscriptSource, TranscriptEntry]) -> HeldRawTranscript:
    return HeldRawTranscript(entries=entries)


def test_raw_lines_of_no_entry_are_none() -> None:
    assert raw_lines(HeldRawTranscript(), 40) == []


def test_raw_lines_head_each_file_and_label_each_entry() -> None:
    """A heading opens each run of one file's entries; a tool entry names its tool."""
    lines = raw_lines(
        _held(
            (_MAIN, TranscriptEntry(kind="tool_call", text="{}", tool_name="Bash")),
            (_SUB, TranscriptEntry(kind="user", text="Check beta.")),
            (_MAIN, TranscriptEntry(kind="tool_result", text="no", is_error=True)),
        ),
        40,
    )
    assert [text for _index, text in lines] == [
        f" ── claude-code transcript of {INDEXED}",
        " tool call Bash",
        "     {}",
        " ── claude-code subagent transcript of no Run",
        " user",
        "     Check beta.",
        f" ── claude-code transcript of {INDEXED}",
        " tool result error of an unnamed call",
        "     no",
    ]
    assert [index for index, _text in lines] == [0, 0, 0, 1, 1, 1, 2, 2, 2]


def test_raw_lines_keep_line_breaks_and_neutralise_control_characters() -> None:
    """Each line of the text stays a line; escapes and tabs never reach the terminal."""
    text = "one\n\x1b[31mred\x1b[0m\tcell\r\n\nlast"
    lines = raw_lines(_held((_MAIN, TranscriptEntry(kind="tool_result", text=text))), 40)
    body = [line for _index, line in lines[2:]]
    assert body[0] == "     one"
    assert "\x1b" not in body[1] and "\t" not in body[1] and "red" in body[1]
    assert body[1].endswith("cell"), "a carriage return ends its line"
    assert body[2:] == ["     ", "     last"]


@pytest.mark.parametrize("room", [1, 4, 5])
def test_raw_lines_cut_a_word_wider_than_the_room_into_pieces(room: int) -> None:
    """A word with no space to wrap at is split, never clipped, at any room."""
    word = "abcdefghij"
    lines = raw_lines(_held((_MAIN, TranscriptEntry(kind="assistant", text=word))), room)
    pieces = [line.strip() for _index, line in lines[2:]]
    assert "".join(pieces) == word
    assert all(len(piece) <= room for piece in pieces)
