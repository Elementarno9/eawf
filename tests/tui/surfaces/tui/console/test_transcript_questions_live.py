"""Live-test host questions, approvals and tool errors on a Run's transcript, end to end.

CON-170 asks the transcript for a question with what it waits on and an error with its
trace; RUN-062 names the runtime adapters the producers of the lines it draws; PLAN-045
says a question never expires, so what it waits on is ``open``. This suite holds all
three against what produces the lines: a disposable epoch-2 canary with one live Run on
a Claude Code session, the real ``eawf hook run`` for ``pre_tool_use`` and
``post_tool_use`` on an ``AskUserQuestion`` call, ``permission_request`` for a held
call, and ``post_tool_use_failure`` for a failed one, a real daemon on a private socket
filing what they report, and the console opened by ``eawf ui``'s own launch function.

Nothing reaches the operator's daemon: the runtime directory, the home and both daemon
client factories are this suite's own, exactly as in the subagent transcript suite.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.cli.app import app as cli
from eawf.surfaces.tui.console.app import ConsoleApp
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

#: The host's id of the question call, and of the call that failed.
QUESTION_CALL: Final = "toolu_01question"
FAILED_CALL: Final = "toolu_02failed"

#: What the host asks its operator, and what the operator answers.
ASKED: Final = "Which storage backend should the cache use?"
ANSWER: Final = "SQLite"

#: The question call's input, as Claude Code hands it to PreToolUse.
QUESTION_INPUT: Final = {
    "questions": [
        {
            "question": ASKED,
            "header": "Backend",
            "multiSelect": False,
            "options": [
                {"label": "SQLite", "description": "One file beside the tree."},
                {"label": "Redis", "description": "A server the operator runs."},
            ],
        }
    ]
}

#: What the failed call said went wrong.
FAILURE: Final = "File does not exist."

#: The block heads of a question and an error, clock first.
QUESTION_HEAD: Final = re.compile(r"\d\d:\d\d:\d\d  \? question")
ERROR_HEAD: Final = re.compile(r"\d\d:\d\d:\d\d  ! error")


def _hook(repo: Path, event: str, payload: dict[str, Any]) -> None:
    """Run ``eawf hook run <event> --runtime claude`` with *payload* on stdin.

    A tool hook prints nothing, because the host reads a tool hook's stdout as its
    decision on the call, and these hooks only observe it.
    """
    result = CliRunner().invoke(
        cli,
        ["--workspace", str(repo), "hook", "run", event, "--runtime", "claude"],
        input=json.dumps({"session_id": HOST_SESSION, **payload}),
    )
    assert result.exit_code == 0, result.stdout
    if event != "permission_request":
        assert result.stdout == "", result.stdout


def _ask(repo: Path) -> None:
    _hook(
        repo,
        "pre_tool_use",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "tool_use_id": QUESTION_CALL,
            "tool_input": QUESTION_INPUT,
        },
    )


def _answer(repo: Path) -> None:
    _hook(
        repo,
        "post_tool_use",
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "AskUserQuestion",
            "tool_use_id": QUESTION_CALL,
            "tool_input": QUESTION_INPUT,
            "tool_response": {"questions": QUESTION_INPUT["questions"], "answers": {ASKED: ANSWER}},
        },
    )


def _hold(repo: Path) -> None:
    _hook(
        repo,
        "permission_request",
        {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "make clean", "description": "Clean the build outputs"},
        },
    )


def _fail(repo: Path) -> None:
    _hook(
        repo,
        "post_tool_use_failure",
        {
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Read",
            "tool_use_id": FAILED_CALL,
            "tool_input": {"file_path": "missing.txt"},
            "error": FAILURE,
            "is_interrupt": False,
        },
    )


def _approve(repo: Path, daemon: _Daemon) -> None:
    """Approve the held call as the operator, through the daemon's own decide verb."""
    with daemon.client() as client:
        stream = client.call(
            "runtime.run.events.read", {"urn": _parent_urn(repo), "repo_root": str(repo)}
        )
        urn = next(
            line["payload"]["subject_ref"]
            for line in stream["events"]
            if line["event_kind"] == "approval_requested"
        )
        client.call(
            "runtime.permission.decide",
            {
                "urn": urn,
                "verb": "approve",
                "principal_class": "operator",
                "actor": "OP-0001",
                "expected_revision": 1,
                "repo_root": str(repo),
            },
        )


def _parent_urn(repo: Path) -> str:
    """Return the URN of the live Run the host session is on, as the tree stores it."""
    authority = require_native_authority(repo / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    generation = authority.target.generation_path(authority.generation_id)
    rows = document_rows(read_document(generation / GENERATION_DOCUMENT), Epoch2Collection.RUN)
    return str(rows[PARENT]["urn"])


def _quick_permission_hook(repo: Path) -> None:
    """Let the permission hook return at once rather than wait for a decision."""
    path = repo / ".ea" / "config.yaml"
    held = yaml.safe_load(path.read_text("utf-8")) if path.exists() else None
    document = held if isinstance(held, dict) else {}
    document.setdefault("runtime", {}).setdefault("claude", {})["permission_wait_s"] = 0
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def _body(frame: str, label: str) -> str:
    """Return the text a block's labelled body line carries."""
    found = re.search(rf"{label} +(.+?) *(?:│|\n|$)", frame)
    assert found is not None, frame
    return found.group(1)


@pytest.mark.parametrize("size", [(80, 24), (120, 30)], ids=["80x24", "120x30"])
def test_con_170_run_062_plan_045_a_host_question_waits_then_is_answered_live(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    """CON-170, RUN-062, PLAN-045 on lines the real question hooks produced.

    The question the host asks lands as a ``? question`` block in flight: the header
    says the Run is waiting, and its fold says what it waits on and that nothing
    expires it. The answer lands while the transcript is open, as a second question
    block whose fold names the chosen option and the ledger line it was recorded as.
    """
    repo, _daemon = live_tree
    _ask(repo)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _open(app, pilot, PARENT)
        frame = await _until(app, pilot, lambda f: QUESTION_HEAD.search(f) is not None)
        assert "QST-0001 · raised" in frame
        assert "waiting" in _caret_row(frame)
        assert "WAITING for" in frame.split("\n")[1]
        await pilot.press("enter")
        waits = _body(await _frame(app, pilot), "WAITS ON")
        assert waits.startswith("an answer to QST-0001 · open"), waits
        await pilot.press("enter")
        # the answer lands while the transcript is open, and the re-read draws it
        await asyncio.to_thread(_answer, repo)
        answered = await _until(app, pilot, lambda f: _kinds(f).count("question") == 2)
        assert "QST-0001 · resolved · option_1" in _caret_row(answered)
        assert "WAITING for" not in answered.split("\n")[1]
        await pilot.press("enter")
        opened = await _frame(app, pilot)
        assert _body(opened, "ANSWERED").startswith("option_1 · under receipt"), opened
        # the receipt wraps as prose at the narrow width rather than being cut
        assert re.search(r"receipt-[0-9a-f]{16}", opened), opened

    _launch(repo, monkeypatch, scenario, size)


def test_con_170_run_062_a_held_call_shows_its_deadline_then_its_approval_live(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CON-170, RUN-062, RUN-051: a permission prompt waits on the provider's deadline.

    The host's permission prompt is the provider permission the hook records; its
    ``approval_requested`` block folds out the deadline the provider owns, and the
    operator's approval, given through the daemon's decide verb, lands as its answer.
    """
    repo, daemon = live_tree
    _quick_permission_hook(repo)
    _hold(repo)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _open(app, pilot, PARENT)
        frame = await _until(app, pilot, lambda f: "PERM-0001 · requested" in f)
        assert QUESTION_HEAD.search(frame) is not None
        await pilot.press("enter")
        waits = _body(await _frame(app, pilot), "WAITS ON")
        assert re.fullmatch(
            r"an answer to PERM-0001 · deadline \d\d:\d\d:\d\d UTC", waits.strip()
        ), waits
        await pilot.press("enter")
        await asyncio.to_thread(_approve, repo, daemon)
        await _until(app, pilot, lambda f: "PERM-0001 · resolved · approved" in f)
        await pilot.press("enter")
        body = _body(await _frame(app, pilot), "ANSWERED")
        assert body.startswith("approved · under receipt receipt-"), body

    _launch(repo, monkeypatch, scenario, (120, 30))


def test_con_170_run_062_a_failed_host_call_is_an_error_block_live(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CON-170, RUN-062: a failed host call lands as a ``! error`` block with its trace.

    The first line is what the host said went wrong; the fold carries the code naming
    the tool, what a retry would take, and the trace the host's error text was filed as.
    """
    repo, _daemon = live_tree
    _fail(repo)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _open(app, pilot, PARENT)
        frame = await _until(app, pilot, lambda f: ERROR_HEAD.search(f) is not None)
        assert f"Read failed: {FAILURE}" in frame
        folded = re.search(r"▸ (\d+ lines?)", _caret_row(frame))
        assert folded is not None, _caret_row(frame)
        await pilot.press("enter")
        opened = await _frame(app, pilot)
        assert f"▾ {folded.group(1)}" in _caret_row(opened)
        assert _body(opened, "CODE").startswith("read.failed")
        assert _body(opened, "RETRY").startswith("after the input changes")
        # the trace unfolds to the host's full error text, read from the content store
        assert _body(opened, "TRACE").startswith(FAILURE)

    _launch(repo, monkeypatch, scenario, (80, 24))
