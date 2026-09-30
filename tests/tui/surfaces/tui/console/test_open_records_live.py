"""Live-test a host's question and the pause over its Run in Attention and their details.

CON-131 binds the question detail to one ``QST-####`` question and draws the projection
PLAN-045 computes; CON-132 binds the pause detail to one pause and draws what PLAN-047
computes; CON-130 says each detail's ``STATE`` and ``ENDS WHEN`` are those projections'
facts; PLAN-046 says the operator's answer is recorded by the daemon's answer path; D-PAUSE
says the Run a host question holds waits under an epoch-2 pause. This suite holds them on
what produces the records: the real ``eawf hook run`` for ``pre_tool_use`` on an
``AskUserQuestion`` call, a real daemon on a private socket filing what it reports, and the
console opened by ``eawf ui``'s own launch function acting as an operator.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.surfaces.tui import launch
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.operations import Operator
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SessionSetup
from tests.tui.surfaces.tui.console.test_transcript_live import (
    PARENT,
    Scenario,
    _Daemon,
    _frame,
    _until,
    live_tree,
)
from tests.tui.surfaces.tui.console.test_transcript_questions_live import ASKED, _ask

__all__ = ["live_tree"]

#: Who the console acts as, and the records the host's question files.
OPERATOR: Final = "OP-0001"
QUESTION: Final = "QST-0001"
PAUSE: Final = "PAU-0001"


def _launch_as_operator(
    repo: Path, monkeypatch: pytest.MonkeyPatch, scenario: Scenario, size: tuple[int, int]
) -> None:
    """Open ``eawf ui``'s console on *repo* acting as :data:`OPERATOR`, and drive *scenario*."""
    driven: list[bool] = []

    def run_headless(app: ConsoleApp, seam: ProjectionSeam | None) -> int:
        assert seam is not None, "an attached epoch-2 tree opens over a live seam"

        async def drive() -> None:
            await seam.connect()
            try:
                async with app.run_test(size=size) as pilot:
                    await scenario(app, seam, pilot)
            finally:
                await seam.disconnect()

        asyncio.run(drive())
        driven.append(True)
        return 0

    monkeypatch.setattr(launch, "_run_console", run_headless)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    operator = Operator(principal=OPERATOR)
    assert launch.launch_tui(workspace=repo, no_input=False, plain=False, operator=operator) == 0
    assert driven == [True], "the launch landed in an entry state instead of the live console"


def _row(frame: str, key: str) -> str:
    return next(row for row in frame.split("\n") if key in row)


def _body(frame: str, label: str) -> str:
    found = re.search(rf"{label} +(.+?) *(?:│|\n|$)", frame)
    assert found is not None, frame
    return found.group(1)


async def _attention(app: ConsoleApp, pilot: Any) -> str:
    app.reset(SessionSetup(route="attention"))
    await pilot.pause()
    return await _until(app, pilot, lambda f: QUESTION in f and PAUSE in f)


async def _select(app: ConsoleApp, pilot: Any, key: str) -> str:
    """Walk the caret down the Attention list until it sits on *key*'s row."""
    for _ in range(6):
        frame = await _frame(app, pilot)
        caret = next((r for r in frame.split("\n") if r.lstrip(" │|").startswith("▸")), "")
        if key in caret:
            return frame
        await pilot.press("down")
    raise AssertionError(f"the caret never reached {key}")


@pytest.mark.parametrize("size", [(80, 24), (120, 30)], ids=["80x24", "120x30"])
def test_con_131_con_132_plan_045_plan_047_d_pause_a_host_question_and_its_pause_live(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    """The host's question is an Attention row and its pause a row beside it, each opening
    the detail bound to its own record, with the state the daemon's projection computed."""
    repo, _daemon = live_tree
    _ask(repo)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        frame = await _attention(app, pilot)
        assert ASKED[:20] in _row(frame, QUESTION)
        assert PARENT in _row(frame, PAUSE) and "waiting on a person" in _row(frame, PAUSE)
        await _select(app, pilot, QUESTION)
        await pilot.press("enter")
        question = await _until(app, pilot, lambda f: app.session.overlay == "question")
        assert app.session.ov_subject == QUESTION
        assert ASKED[:20] in question
        assert "SQLite" in question and "Redis" in question
        assert _body(question, "STATE").startswith("open · blocking")
        assert _body(question, "ENDS WHEN").startswith("an operator or evidence answers it")
        await pilot.press("escape")
        await _select(app, pilot, PAUSE)
        await pilot.press("enter")
        pause = await _until(app, pilot, lambda f: app.session.overlay == "pause")
        assert app.session.ov_subject == PAUSE
        assert _body(pause, "STATE").startswith("waiting on a person")
        assert QUESTION in _body(pause, "ENDS WHEN")

    _launch_as_operator(repo, monkeypatch, scenario, size)


def test_con_131_plan_046_a_digit_answers_the_host_question_and_ends_its_pause_live(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's digit is recorded by the daemon; the question reads answered by
    them, and the pause over the Run is resolved and leaves Attention."""
    repo, _daemon = live_tree
    _ask(repo)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _attention(app, pilot)
        await _select(app, pilot, QUESTION)
        await pilot.press("enter")
        await _until(app, pilot, lambda f: app.session.overlay == "question")
        await pilot.press("1")
        await app.workers.wait_for_complete()
        answered = await _until(
            app, pilot, lambda f: _body(f, "STATE").startswith("answered by you")
        )
        assert "SQLite" in answered
        await pilot.press("escape")
        await _until(app, pilot, lambda f: PAUSE not in f and QUESTION not in f)

    _launch_as_operator(repo, monkeypatch, scenario, (120, 30))
