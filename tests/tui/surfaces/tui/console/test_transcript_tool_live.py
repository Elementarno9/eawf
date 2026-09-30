"""Live-test host tool calls on the transcript: real tool hooks, a real daemon, the real console.

CON-170 and RUN-062 against what actually produces the lines. A disposable epoch-2 canary
holds one live Run whose vendor session is a Claude Code session. That session reads a
file, runs a shell command that succeeds and one that fails, and each call reaches the
tree through ``eawf hook run pre_tool_use`` / ``post_tool_use`` /
``post_tool_use_failure`` -- the commands the plugin maps ``PreToolUse``,
``PostToolUse`` and ``PostToolUseFailure`` to -- carrying the payloads Claude Code
sends. A real daemon on a private socket gives each call a gateway call id and, once it
has run, a receipt naming its bounded, scrubbed output. The console, opened by
``eawf ui``'s own launch function, draws every phase as a ``tool`` block under its glyph, and a
result unfolds to the output the store kept, or to the error and the trace it failed
with.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from eawf.kernel.runtime.content import WITHHELD_LINE
from eawf.surfaces.cli.app import app as cli
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.renderers.transcript import NATIVE_GLYPH
from eawf.surfaces.tui.console.seam import ProjectionSeam
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
    live_tree,
)

__all__ = ["live_tree"]

#: The three calls, as Claude Code names them.
READ_ID: Final = "toolu_01ReadNotesA1b2C3d4E5f6G7"
BUILD_ID: Final = "toolu_01BashBuildH8i9J0k1L2m3N4"
BROKEN_ID: Final = "toolu_01BashBrokenO5p6Q7r8S9t0U"

#: A home path a command printed, which the store must never keep.
LEAKED: Final = "/Users/jdoe/project/keys"  # pragma: allowlist secret


def _tool_hook(repo: Path, event: str, payload: dict[str, Any]) -> None:
    """Run ``eawf hook run <event> --runtime claude`` as the plugin wrapper does."""
    body = {
        "session_id": HOST_SESSION,
        "transcript_path": "~/.claude/projects/live/session.jsonl",
        "cwd": str(repo),
        "permission_mode": "default",
        **payload,
    }
    result = CliRunner().invoke(
        cli,
        ["--workspace", str(repo), "hook", "run", event, "--runtime", "claude"],
        input=json.dumps(body),
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == "", "a tool observer hands Claude Code no decision"
    assert "runtime.host_tool ok run=" in result.stderr, result.stderr


def _call(repo: Path, tool: str, tool_use_id: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    head = {"tool_name": tool, "tool_use_id": tool_use_id, "tool_input": tool_input}
    _tool_hook(repo, "pre_tool_use", {"hook_event_name": "PreToolUse", **head})
    return head


def _session(repo: Path) -> None:
    """Drive the three calls through the hooks, as the session makes them."""
    notes = repo / "notes.txt"
    read = _call(repo, "Read", READ_ID, {"file_path": str(notes)})
    _tool_hook(
        repo,
        "post_tool_use",
        {
            "hook_event_name": "PostToolUse",
            **read,
            "tool_response": {
                "type": "text",
                "file": {
                    "filePath": str(notes),
                    "content": "alpha is the first line\nbeta is the second\ngamma is the third\n",
                    "numLines": 3,
                    "startLine": 1,
                    "totalLines": 3,
                },
            },
        },
    )
    build = _call(repo, "Bash", BUILD_ID, {"command": "make build", "description": "Build"})
    _tool_hook(
        repo,
        "post_tool_use",
        {
            "hook_event_name": "PostToolUse",
            **build,
            "tool_response": {
                "stdout": f"built 3 targets\nwrote the bundle under {LEAKED}",
                "stderr": "",
                "interrupted": False,
                "isImage": False,
            },
        },
    )
    broken = _call(repo, "Bash", BROKEN_ID, {"command": "make nope", "description": "Break"})
    _tool_hook(
        repo,
        "post_tool_use_failure",
        {
            "hook_event_name": "PostToolUseFailure",
            **broken,
            "error": "Exit code 1\nmake: *** No rule to make target 'nope'.  Stop.",
            "is_interrupt": False,
        },
    )


def _unfold(frame: str) -> str:
    """Return the caret row's fold note, ``▸ N lines``."""
    found = re.search(r"▸ (\d+ lines?)", _caret_row(frame))
    assert found is not None, _caret_row(frame)
    return found.group(1)


@pytest.mark.parametrize("size", [(80, 24), (120, 30)], ids=["80x24", "120x30"])
def test_con_170_run_062_live_host_tool_calls_render_with_their_output(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    """CON-170, RUN-062: each host call is three tool blocks; a result unfolds to its output.

    The failed command states the gateway's error code and the trace it failed with; the
    successful one its output, with the line carrying a home path withheld whole; the
    file read the file's lines. Nothing a withheld line carried reaches the tree.
    """
    repo, _daemon = live_tree
    _session(repo)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _open(app, pilot, PARENT)
        await _until(app, pilot, lambda f: "Bash failed: Exit code 1" in f)
        # the failure is also stated as an error block after its result; the result is one up
        await pilot.press("up")
        frame = await _frame(app, pilot)
        assert re.search(rf"\d\d:\d\d:\d\d  {re.escape(NATIVE_GLYPH['tool'])} tool", frame)
        assert "bash · result · failed" in _caret_row(frame)
        # the failed command unfolds to the gateway's code and the trace it failed with
        hidden = _unfold(frame)
        await pilot.press("enter")
        failed = await _frame(app, pilot)
        assert f"▾ {hidden}" in _caret_row(failed)
        assert re.search(r"ERROR +PAYLOAD_INVALID", failed), failed
        assert re.search(r"OUTPUT +Exit code 1", failed), failed
        assert "No rule to make target 'nope'." in failed
        await pilot.press("enter")
        # three blocks up: the build's result, whose leaked line is withheld whole
        await pilot.press("up", "up", "up")
        folded = await _frame(app, pilot)
        assert "bash · result · succeeded" in _caret_row(folded)
        _unfold(folded)
        await pilot.press("enter")
        built = await _frame(app, pilot)
        assert re.search(r"OUTPUT +built 3 targets", built), built
        assert WITHHELD_LINE.split(",")[0] in built
        assert "jdoe" not in built
        await pilot.press("enter")
        # three more up: the file read, which unfolds to the file's lines
        await pilot.press("up", "up", "up")
        folded = await _frame(app, pilot)
        assert "read · result · succeeded" in _caret_row(folded)
        await pilot.press("enter")
        read = await _frame(app, pilot)
        assert re.search(r"OUTPUT +alpha is the first line", read), read
        assert "gamma is the third" in read

    _launch(repo, monkeypatch, scenario, size)
    stored = "".join(
        path.read_text(errors="replace") for path in (repo / ".ea").rglob("*") if path.is_file()
    )
    assert "jdoe" not in stored, "a withheld value never reaches the tree"


def test_run_062_every_host_call_phase_is_one_tool_block_in_run_order(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """RUN-062: nine lines, one per phase, and a retried hook adds none."""
    repo, _daemon = live_tree
    _session(repo)
    _session(repo)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _open(app, pilot, PARENT)
        await _until(app, pilot, lambda f: "Bash failed: Exit code 1" in f)
        # the failed call's error block is the one line past the nine tool phases
        await pilot.press("up")
        frame = await _frame(app, pilot)
        assert set(_kinds(frame)) == {"tool", "error"}, frame
        assert "bash · result · failed" in _caret_row(frame)
        rows = [_caret_row(frame)]
        for _ in range(9):
            await pilot.press("up")
            rows.append(_caret_row(await _frame(app, pilot)))
        found = [re.search(r"(read|bash) · (requested|accepted|result)", row) for row in rows]
        assert [(m.group(1), m.group(2)) for m in reversed(found) if m] == [
            ("read", "requested"),
            ("read", "requested"),
            ("read", "accepted"),
            ("read", "result"),
            ("bash", "requested"),
            ("bash", "accepted"),
            ("bash", "result"),
            ("bash", "requested"),
            ("bash", "accepted"),
            ("bash", "result"),
        ], "nine blocks in run order; the cursor rests on the first past the top"

    _launch(repo, monkeypatch, scenario, (120, 30))
