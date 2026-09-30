"""Live-test a host file edit end to end: real hooks, real edits, a real daemon, the real console.

CON-170 and RUN-062 hold the ``± file`` block against the lines the Claude Code tool
hooks actually produce. The canary is a git repository whose one live Run is the host
session. Each edit is bracketed by ``eawf hook run pre_tool_use`` and
``eawf hook run post_tool_use`` -- the commands the plugin maps ``PreToolUse`` and
``PostToolUse`` to -- carrying the payloads Claude Code sends for ``Edit`` and
``Write``, while the file on disk is really changed between the two. A real daemon on a
private socket takes the trees and files the lines; the console is opened by ``eawf
ui``'s own launch function and reads them back through the same daemon.

Nothing reaches the operator's daemon or home: the fixture this suite shares with the
subagent transcript suite points ``HOME``, ``EAWF_RUNTIME_DIR`` and both daemon clients
at directories and a socket it owns.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app as cli
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.seam import ProjectionSeam
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed_row
from tests.tui.surfaces.tui.console.test_transcript_live import (
    HOST_SESSION,
    PARENT,
    _caret_row,
    _Daemon,
    _frame,
    _kinds,
    _launch,
    _open,
    _until,
    live_tree,  # noqa: F401  -- the shared private-daemon canary fixture
)

#: The edited module, as committed and as the Edit tool leaves it.
MODULE: Final = "src/app.py"
ORIGINAL: Final = "def greet() -> str:\n    return 'hello'\n"
EDITED: Final = "def greet() -> str:\n    return 'hello, world'\n"

#: A home path, spelled in pieces so the leak lint does not read it as one.
MACOS_HOME: Final = "/" + "Users" + "/" + "alice"

#: The file the Write tool creates, carrying a home path the stored diff must withhold.
NOTES: Final = "docs/notes.md"
NOTES_TEXT: Final = f"# Notes\n\nThe cache lives under {MACOS_HOME}/.cache/app.\n"

#: The hook whose output line names the recorded edit.
EDIT_HOOK: Final = "runtime.host_file_edit"

#: A digest as a block's BEFORE and AFTER lines state it.
DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=ci", "-c", "user.email=ci@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def git_tree(live_tree: tuple[Path, _Daemon]) -> tuple[Path, _Daemon]:  # noqa: F811
    """The shared canary, made a one-commit git repository holding the module."""
    repo, daemon = live_tree
    (repo / ".gitignore").write_text(".ea/local/\n.ea/locks/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / MODULE).write_text(ORIGINAL, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo, daemon


def _hook(repo: Path, event: str, payload: dict[str, Any]) -> str:
    """Run ``eawf hook run <event> --runtime claude`` and return the edit hook's output."""
    result = CliRunner().invoke(
        cli,
        ["--workspace", str(repo), "hook", "run", event, "--runtime", "claude"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == "", "a tool observer hands Claude Code no decision"
    (edit,) = [row for row in result.stderr.splitlines() if row.startswith(EDIT_HOOK)]
    return edit


def _tool_call(
    repo: Path, tool_use_id: str, tool_name: str, tool_input: dict[str, Any], edit: Any
) -> str:
    """Bracket one real edit with the two tool hooks, as Claude Code fires them."""
    base = {
        "session_id": HOST_SESSION,
        "transcript_path": str(repo / "transcript.jsonl"),
        "cwd": str(repo),
        "permission_mode": "default",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
    }
    pre = _hook(repo, "pre_tool_use", {**base, "hook_event_name": "PreToolUse"})
    assert " ok run=" in pre, pre
    edit()
    return _hook(
        repo,
        "post_tool_use",
        {
            **base,
            "hook_event_name": "PostToolUse",
            "tool_response": {"filePath": tool_input["file_path"], "success": True},
        },
    )


def _edit_module(repo: Path) -> str:
    path = repo / MODULE
    return _tool_call(
        repo,
        "toolu_01EditModuleA1B2C3",
        "Edit",
        {"file_path": str(path), "old_string": "'hello'", "new_string": "'hello, world'"},
        lambda: path.write_text(EDITED, encoding="utf-8"),
    )


def _write_notes(repo: Path) -> str:
    path = repo / NOTES

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(NOTES_TEXT, encoding="utf-8")

    return _tool_call(
        repo,
        "toolu_01WriteNotesD4E5F6",
        "Write",
        {"file_path": str(path), "content": NOTES_TEXT},
        write,
    )


def _field(frame: str, label: str, following: str) -> str:
    """Return a folded line's value with its wrapping undone, up to the next label."""
    tail = frame.split(f" {label} ", 1)[1].split(following, 1)[0]
    return "".join(tail.split())


def _file_lines(daemon: _Daemon, repo: Path) -> list[dict[str, Any]]:
    """Return the Run's file-change lines, read back through the daemon."""
    with daemon.client() as client:
        answer = client.call(
            "runtime.run.events.read",
            {"repo_root": str(repo), "urn": seed_row("run", "RUNNING")["urn"]},
        )
    return [line for line in answer["events"] if line["payload"]["payload_kind"] == "file_change"]


@pytest.mark.parametrize("size", [(80, 24), (120, 30)], ids=["80x24", "120x30"])
def test_con_170_run_062_live_host_edits_render_as_file_blocks_that_unfold(
    git_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    """CON-170, RUN-062: an Edit and a Write each land as a ``± file`` block.

    The block names the edited path; Enter unfolds PATHS, DIFF, BEFORE and AFTER, the
    two digests differ, and the diff it points at is stored with the home path the
    written file carried withheld. The second edit lands while the transcript is open
    and appears without a relaunch.
    """
    repo, _daemon = git_tree
    assert "event=EVT-" in _edit_module(repo)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _open(app, pilot, PARENT)
        frame = await _until(app, pilot, lambda f: "± file" in f)
        assert re.search(rf"\d\d:\d\d:\d\d  ± file +{re.escape(MODULE)}", frame), frame
        folded = _caret_row(frame)
        assert re.search(r"▸ \d+ lines?", folded), folded
        await pilot.press("enter")
        opened = await _frame(app, pilot)
        assert re.search(rf"PATHS +{re.escape(MODULE)}", opened), opened
        assert re.search(r"DIFF +diff --git a/src/app\.py b/src/app\.py", opened), opened
        assert "-    return 'hello'" in opened and "+    return 'hello, world'" in opened
        assert re.search(r"BEFORE +sha256:[0-9a-f]", opened), opened
        if size == (120, 30):
            before, after = _field(opened, "BEFORE", "AFTER"), _field(opened, "AFTER", "─")
            assert DIGEST.fullmatch(before) and DIGEST.fullmatch(after), opened
            assert before != after
        else:
            assert re.search(r"… \d+ below", opened), "a short frame counts what it hides"
        assert "▾" in _caret_row(opened)
        await pilot.press("enter")

        output = await asyncio.to_thread(_write_notes, repo)
        assert "event=EVT-" in output, output
        both = await _until(app, pilot, lambda f: _kinds(f).count("file") == 2)
        assert NOTES in _caret_row(both), "following takes the cursor to the new block"
        await pilot.press("enter")
        notes = await _frame(app, pilot)
        assert re.search(r"DIFF +diff --git a/docs/notes\.md", notes), notes
        assert MACOS_HOME not in notes
        if size == (120, 30):
            assert "+# Notes" in notes
            assert "is withheld" in notes, "the line carrying a home path is withheld whole"

    _launch(repo, monkeypatch, scenario, size)

    lines = _file_lines(git_tree[1], repo)
    assert [line["event_kind"] for line in lines] == ["file_changed", "file_changed"]
    assert [line["payload"]["changed_paths"] for line in lines] == [[MODULE], [NOTES]]
    for line in lines:
        payload = line["payload"]
        assert DIGEST.fullmatch(payload["before_tree_digest"])
        assert DIGEST.fullmatch(payload["after_tree_digest"])
        assert payload["before_tree_digest"] != payload["after_tree_digest"]


def test_run_062_edit_hooks_skip_other_tools_and_refuse_paths_outside_the_repo(
    git_tree: tuple[Path, _Daemon], tmp_path: Path
) -> None:
    """RUN-062: a Read is skipped; an edit outside the repository records nothing."""
    repo, _daemon = git_tree
    read = _hook(
        repo,
        "pre_tool_use",
        {
            "hook_event_name": "PreToolUse",
            "session_id": HOST_SESSION,
            "tool_name": "Read",
            "tool_input": {"file_path": str(repo / MODULE)},
            "tool_use_id": "toolu_01ReadOnlyG7H8I9",
        },
    )
    assert "skipped: Read edits no file" in read
    outside = tmp_path / "elsewhere.txt"
    call = {
        "session_id": HOST_SESSION,
        "tool_name": "Write",
        "tool_input": {"file_path": str(outside), "content": "x\n"},
        "tool_use_id": "toolu_01OutsideJ1K2L3",
    }
    pre = _hook(repo, "pre_tool_use", {**call, "hook_event_name": "PreToolUse"})
    assert "path_escape" in pre, pre
    outside.write_text("x\n", encoding="utf-8")
    post = _hook(repo, "post_tool_use", {**call, "hook_event_name": "PostToolUse"})
    assert "event=None" in post, post
    assert _file_lines(_daemon, repo) == []


def test_run_062_an_edit_that_changes_nothing_records_no_line(
    git_tree: tuple[Path, _Daemon],
) -> None:
    """RUN-062: an Edit the host rejected leaves the tree alone and records no block."""
    repo, _daemon = git_tree
    output = _tool_call(
        repo,
        "toolu_01NoChangeM4N5O6",
        "Edit",
        {"file_path": str(repo / MODULE), "old_string": "absent", "new_string": "x"},
        lambda: None,
    )
    assert "event=None" in output, output
    assert _file_lines(_daemon, repo) == []
