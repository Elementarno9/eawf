"""``eawf run transcript <KEY>`` prints every transcript indexed for a Run or a Task.

Claude Code and Codex files print their user, assistant and tool entries, with each
tool's input and output whole; a Task key gathers each of its Runs and their
subagents; ``--follow`` prints what is appended after it starts, a half-written line
only once it is finished; ``--scrub`` rewrites token shapes and machine paths; and an
unknown key, an unindexed Run, a missing file and a corrupt line each say so.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.reflect import runs as reflect_runs
from eawf.runtime.runtimes.run_transcript import (
    TranscriptReader,
    TranscriptSource,
    follow_transcripts,
    runs_ended,
    select_transcripts,
)
from eawf.runtime.session.transcript_index import record_transcript
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import run_transcript as cmd
from tests.contract.surfaces.cli._reflect_tree import CODE, native_tree, run_row

runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures" / "run_transcript"
NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
TASK_KEY = f"{CODE}-0001"
TASK_URN = f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/task/{TASK_KEY}"
OTHER_TASK_URN = f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/task/{CODE}-0002"


def _row(key: str, *, status: str = "COMPLETED", task_urn: str = TASK_URN) -> dict[str, Any]:
    row = run_row(key, status=status)
    row["scope"]["task_ref"] = task_urn
    return row


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a tree with two Runs of one Task, a Run of another Task and one unindexed Run."""
    monkeypatch.delenv("EA_STATE", raising=False)
    root = native_tree(
        tmp_path / "repo",
        [
            _row("RUN-00000001"),
            _row("RUN-00000002"),
            _row("RUN-00000003", task_urn=OTHER_TASK_URN),
            _row("RUN-00000004", task_urn=OTHER_TASK_URN),
        ],
        [],
    )
    files = tmp_path / "transcripts"
    shutil.copytree(FIXTURES, files)
    _index(root, "claude-code", "session-1", files / "claude.jsonl", "RUN-00000001")
    _index(root, "codex", "thread-2", files / "codex.jsonl", "RUN-00000002")
    record_transcript(
        root,
        runtime="claude-code",
        session_id="agent-1",
        transcript_path=str(files / "claude_subagent.jsonl"),
        now=NOW,
        parent_session_id="session-1",
    )
    return root


def _index(root: Path, runtime: str, session: str, path: Path, run_key: str) -> None:
    record_transcript(
        root,
        runtime=runtime,
        session_id=session,
        transcript_path=str(path),
        now=NOW,
        run_ref=f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/run/{run_key}",
    )


def _invoke(tree: Path, *args: str) -> Any:
    return runner.invoke(app, ["-w", str(tree.parent), "run", "transcript", *args])


def _print(tree: Path, *args: str) -> str:
    result = _invoke(tree, *args)
    assert result.exit_code == 0, result.output
    return str(result.stdout)


def test_claude_code_transcript_prints_messages_and_tools_in_full(tree: Path) -> None:
    """User, assistant, tool input and tool output print; a long output prints whole."""
    out = _print(tree, "RUN-00000001")
    lines = out.splitlines()
    assert lines[0].startswith("== claude-code transcript of RUN-00000001: ")
    assert "user: List the modules." in lines
    assert "assistant: I will look." in lines
    assert 'tool call Bash: {"command": "ls src"}' in lines
    assert "tool result Bash: alpha.py" in lines
    assert "beta.py" in lines
    long_line = next(line for line in lines if line.startswith("tool result Read: module alpha"))
    assert len(long_line) > 500
    assert long_line.endswith("END-OF-OUTPUT")
    assert "tool result error Read: no such file" in lines
    assert "assistant: Two modules." in lines
    assert "HIDDEN-REASONING" not in out
    assert "Listing modules" not in out


def test_claude_code_subagent_prints_under_its_own_heading(tree: Path) -> None:
    """A subagent the Run's session spawned prints after it, headed as a subagent."""
    lines = _print(tree, "RUN-00000001").splitlines()
    heading = next(i for i, line in enumerate(lines) if "subagent transcript" in line)
    assert lines[heading].startswith("== claude-code subagent transcript of no Run: ")
    assert lines[heading + 1 :] == ["user: Check beta.", "assistant: Beta is fine."]


def test_codex_transcript_prints_messages_and_tools_in_full(tree: Path) -> None:
    """Codex messages, function and custom tool calls with their outputs all print."""
    out = _print(tree, "RUN-00000002")
    assert out.splitlines()[0].startswith("== codex transcript of RUN-00000002: ")
    assert "user: Count the tests." in out
    assert 'tool call shell: {"command":["ls","tests"]}' in out
    assert "tool result shell: test_a.py\ntest_b.py" in out
    assert "tool call apply_patch: *** Begin Patch\n*** Add File: notes.txt" in out
    assert "tool result apply_patch: Success. Updated the following files:" in out
    assert "assistant: Two tests." in out
    for hidden in ("DEVELOPER-PROMPT", "HIDDEN-REASONING", "EVENT-DUPLICATE"):
        assert hidden not in out


def test_a_task_key_gathers_every_linked_run(tree: Path) -> None:
    """The Task's two Runs and the subagent print; another Task's Run does not."""
    headings = [line for line in _print(tree, TASK_KEY).splitlines() if line.startswith("== ")]
    assert [line.split(":")[0] for line in headings] == [
        "== claude-code transcript of RUN-00000001",
        "== codex transcript of RUN-00000002",
        "== claude-code subagent transcript of no Run",
    ]


def test_a_run_urn_resolves_like_its_key(tree: Path) -> None:
    """Boundary: the qualified URN names the same Run as the bare key."""
    urn = f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/run/RUN-00000002"
    assert _print(tree, urn) == _print(tree, "RUN-00000002")


def test_json_lists_each_entry(tree: Path) -> None:
    """The machine form carries each entry with its kind, tool and whole text."""
    result = runner.invoke(
        app, ["--json", "-w", str(tree.parent), "run", "transcript", "RUN-00000002"]
    )
    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout)["result"]
    assert answer["run_keys"] == ["RUN-00000002"]
    kinds = [entry["kind"] for entry in answer["entries"]]
    assert kinds == ["user", "tool_call", "tool_result", "tool_call", "tool_result", "assistant"]
    assert answer["entries"][2]["tool_name"] == "shell"


def test_selection_over_many_runs_folds_no_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Max-length: among 2000 Runs a Run key and a Task key select theirs, and no Run is folded.

    A full reading folds each Run's subtree over every Run, which grows with the
    square of the Run count; selecting a transcript needs only each Run's key,
    Task and end stamp.
    """
    monkeypatch.delenv("EA_STATE", raising=False)
    rows = [
        _row(f"RUN-{index:08d}", task_urn=TASK_URN if index % 500 == 0 else OTHER_TASK_URN)
        for index in range(1, 2001)
    ]
    rows[-1] = _row("RUN-00002000", status="RUNNING")
    root = native_tree(tmp_path / "repo", rows, [])
    for key in ("RUN-00000500", "RUN-00002000"):
        _index(root, "claude-code", f"session-{key}", tmp_path / f"{key}.jsonl", key)
    folded: list[str] = []

    def count_fold(run: Any, runs: Any) -> None:
        folded.append(run.key)

    monkeypatch.setattr(reflect_runs, "fold_subtree", count_fold)

    by_run = select_transcripts(root, "RUN-00000500")
    by_task = select_transcripts(root, TASK_KEY)

    assert by_run.run_keys == ("RUN-00000500",)
    assert by_task.run_keys == ("RUN-00000500", "RUN-00001000", "RUN-00001500", "RUN-00002000")
    assert [source.run_key for source in by_task.sources] == ["RUN-00000500", "RUN-00002000"]
    assert runs_ended(root, by_run.run_keys) is True
    assert runs_ended(root, by_task.run_keys) is False
    assert folded == []


def _set_status(root: Path, key: str, status: str) -> None:
    authority = resolve_authority(root)
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(path)
    document[Epoch2Collection.RUN.value][key] = _row(key, status=status)
    write_document(path, document)


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def test_follow_prints_lines_appended_after_start(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line appended while following prints; a torn line waits until it is finished."""
    _set_status(tree, "RUN-00000002", "RUNNING")
    path = tree.parent.parent / "transcripts" / "codex.jsonl"
    late = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Appended later."}],
            },
        }
    )
    torn = late.replace("Appended later.", "Finished after a pause.")

    def first() -> None:
        _append(path, late + "\n" + torn[:20])

    def second() -> None:
        _append(path, torn[20:] + "\n")
        _set_status(tree, "RUN-00000002", "COMPLETED")

    steps = iter([first, second])
    waits: list[int] = []

    def step() -> None:
        waits.append(1)
        next(steps)()

    monkeypatch.setattr(cmd, "wait_between_reads", step)
    out = _print(tree, "RUN-00000002", "--follow")
    lines = out.splitlines()
    assert lines[-2:] == ["assistant: Appended later.", "assistant: Finished after a pause."]
    assert "notice" not in out
    assert len(waits) == 2


def test_scrub_withholds_token_shapes_and_machine_paths(tree: Path, tmp_path: Path) -> None:
    """--scrub rewrites a token-shaped string and a home path; without it both print."""
    token = "sk-" + "Q7" * 15
    home = "/" + "home" + "/operator/notes.txt"
    leaky = tmp_path / "transcripts" / "leaky.jsonl"
    leaky.write_text(
        json.dumps({"type": "user", "message": {"content": f"use {token} and read {home}"}}) + "\n"
    )
    _index(tree, "claude-code", "session-4", leaky, "RUN-00000004")
    plain = _print(tree, "RUN-00000004")
    assert token in plain
    assert home in plain
    scrubbed = _print(tree, "RUN-00000004", "--scrub")
    assert token not in scrubbed
    assert "user: use <scrubbed> and read <scrubbed>/notes.txt" in scrubbed
    assert str(tmp_path) not in scrubbed


def test_an_unknown_key_is_not_found(tree: Path) -> None:
    """Error path: a key naming no Run and no Task's Run is a user error naming it."""
    result = _invoke(tree, "RUN-0000FFFF")
    assert result.exit_code == exit_codes.USER_ERROR
    assert "RUN-0000FFFF" in result.output


def test_a_run_with_no_indexed_transcript_is_not_found(tree: Path) -> None:
    """Error path: a Run the index names no file for says so."""
    result = _invoke(tree, "RUN-00000003")
    assert result.exit_code == exit_codes.USER_ERROR
    assert "no transcript is indexed for RUN-00000003" in result.output


def test_a_missing_file_is_noticed_and_the_rest_prints(tree: Path) -> None:
    """Error path: a deleted file prints a notice in place of its entries."""
    (tree.parent.parent / "transcripts" / "claude_subagent.jsonl").unlink()
    lines = _print(tree, TASK_KEY).splitlines()
    assert lines[-1] == "notice: the transcript file is missing"
    assert "assistant: Two tests." in lines


def test_a_corrupt_line_is_noticed_and_skipped(tree: Path) -> None:
    """Error path: a line that is not a JSON object is named and the next one prints."""
    path = tree.parent.parent / "transcripts" / "claude_subagent.jsonl"
    rows = path.read_text().splitlines()
    path.write_text("\n".join([rows[0], "{torn", "[1, 2]", rows[1]]) + "\n")
    lines = _print(tree, "RUN-00000001").splitlines()
    tail = lines[lines.index("user: Check beta.") :]
    assert tail == [
        "user: Check beta.",
        "notice: line 2 is not a JSON object and is skipped",
        "notice: line 3 is not a JSON object and is skipped",
        "assistant: Beta is fine.",
    ]


def test_follow_with_json_is_refused(tree: Path) -> None:
    """Error path: a followed transcript has no machine form."""
    result = runner.invoke(
        app, ["--json", "-w", str(tree.parent), "run", "transcript", "RUN-00000001", "--follow"]
    )
    assert result.exit_code == exit_codes.USER_ERROR


def _reader(path: Path) -> TranscriptReader:
    return TranscriptReader(
        TranscriptSource(runtime="claude-code", run_key="RUN-00000001", path=path, subagent=False)
    )


def test_an_empty_file_reads_as_no_entries(tmp_path: Path) -> None:
    """Boundary: an empty file and blank lines yield nothing."""
    path = tmp_path / "empty.jsonl"
    path.write_text("\n\n")
    assert list(follow_transcripts([_reader(path)], lambda: False)) == []


def test_no_readers_follows_nothing() -> None:
    """Boundary: with no file to read, the one pass and the final pass yield nothing."""
    asked: list[int] = []

    def stop() -> bool:
        asked.append(1)
        return False

    assert list(follow_transcripts([], stop)) == []
    assert asked == [1]


def test_a_final_line_without_newline_is_read_on_the_final_pass(tmp_path: Path) -> None:
    """Boundary: a last line missing its newline is held, then read once nothing follows."""
    path = tmp_path / "one.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": "only"}}))
    reader = _reader(path)
    assert reader.read(final=False) == ()
    (entry,) = reader.read(final=True)
    assert (entry.kind, entry.text) == ("user", "only")
    assert reader.read(final=True) == ()
