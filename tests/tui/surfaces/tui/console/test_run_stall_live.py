"""Live-test a stalled Run and a Run's timeline end to end: real producers, a real daemon, the
real launch path.

CON-106 and CON-107 are held elsewhere against injected decision records. This suite holds
them against what produces the lost state: a disposable epoch-2 canary with one live Run on
a Claude Code session, a real ``eawf hook run`` recording its last activity, the daemon's
own stall sweep raising the stall fact (handed a later clock rather than waited on), and the
console opened by ``eawf ui``'s own launch function, whose seam fills the Run's state from
``runtime.run.stalls.read``. The same live read draws the Run under ``lost or stale`` on
Activity (UI-030) and as ``stalled`` on Unattended (UI-026). The Attention register lists
the standing stall as one ``stalled`` item, which the route's own re-read drops once the Run
answers (UI-062). CON-076 and UI-008 are held on the Run frame's timeline, drawn from the
groups the daemon's events read answers with.

Nothing reaches the operator's daemon: the harness is the live transcript suite's.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.runtime.daemon.stall_sweep import sweep_once
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SessionSetup
from tests.tui.surfaces.tui.console.test_transcript_live import (
    PARENT,
    _Daemon,
    _launch,
    _until,
    live_tree,
)
from tests.tui.surfaces.tui.console.test_transcript_questions_live import _fail, _parent_urn

__all__ = ["live_tree"]

#: A coalesced timeline row: its word, the multiplication sign and a count, then its span.
COALESCED: Final = re.compile(r"tool ×(?P<count>\d+) · \d+[smh]")  # noqa: RUF001


def _stall(daemon: _Daemon) -> None:
    """Run the daemon's own sweep with a clock past the default interval."""
    raised = sweep_once(daemon._ctx, now=datetime.now(UTC) + timedelta(hours=1))
    assert raised == (PARENT,), raised


def _outputs(repo: Path, daemon: _Daemon, count: int, first: int = 0) -> None:
    """Append ``count`` command output chunks to the Run through the append verb."""
    urn = _parent_urn(repo)
    with daemon.client() as client:
        for n in range(first, first + count):
            client.call(
                "runtime.run.event.append",
                {
                    "urn": urn,
                    "event_ref": f"EVT-{0xC0DE00 + n:08x}",
                    "run_sequence": client.call(
                        "runtime.run.events.read", {"urn": urn, "repo_root": str(repo)}
                    )["next_sequence"],
                    "event_kind": "command_output",
                    "payload": {
                        "payload_kind": "command",
                        "command_family_ref": "pytest",
                        "command_ref": "CMD-0000c0de",
                        "phase": "output",
                        "execution": "foreground",
                        "stream": "stdout",
                        "chunk_ref": f"artifact://chunks/{n}",
                    },
                    "actor": "OP-0001",
                    "repo_root": str(repo),
                },
            )


async def _route(app: ConsoleApp, pilot: Any, route: str, subject: str | None = None) -> None:
    app.reset(SessionSetup(route=route, subjId=subject))
    await pilot.pause()


def test_con_106_con_107_a_run_the_sweep_found_quiet_renders_lost_live(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CON-106, CON-107: the lost Run's two panes and its recovery, from the live stall."""
    repo, daemon = live_tree
    _fail(repo)
    _stall(daemon)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _route(app, pilot, "run.detail", PARENT)
        frame = await _until(app, pilot, lambda f: "WHAT IS TRUE" in f)
        assert "WHAT IS NOT known" in frame
        assert "This state means we do not know. It is not success and not failure," in frame
        rows = frame.split("\n")
        start = next(i for i, row in enumerate(rows) if "RECOVERY" in row)
        recovery = " ".join(row.strip(" │") for row in rows[start : start + 3])
        assert "resume waits for the same Run" in recovery
        assert "let go closes it as CANCELLED" in recovery
        assert "retry is not offered" in recovery
        assert "retry" not in rows[-1].lower()

    _launch(repo, monkeypatch, scenario, (120, 30))


def test_ui_030_ui_026_a_stalled_run_is_lost_on_activity_and_stalled_on_unattended_live(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UI-030: counted under ``lost or stale``, not ``running``; UI-026: drawn as stalled."""
    repo, daemon = live_tree
    _fail(repo)
    _stall(daemon)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _route(app, pilot, "activity")
        frame = await _until(app, pilot, lambda f: "stalled · nothing since" in f)
        row = next(line for line in frame.split("\n") if PARENT in line)
        assert "stalled · nothing since" in row
        assert re.search(r"lost or stale +1", frame), frame
        assert re.search(r"running +0", frame), frame
        await _route(app, pilot, "unattended")
        queue = await _until(app, pilot, lambda f: "stalled · nothing since" in f)
        assert "stalled · nothing since" in next(
            line for line in queue.split("\n") if PARENT in line
        )

    _launch(repo, monkeypatch, scenario, (160, 40))


def test_ui_062_a_stalled_run_is_one_stalled_attention_item_until_it_answers_live(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UI-062: the standing stall is one item under ``stalled``, gone once the Run answers."""
    repo, daemon = live_tree
    _fail(repo)
    _stall(daemon)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _route(app, pilot, "attention")
        frame = await _until(app, pilot, lambda f: " STALLED  1" in f)
        row = next(line for line in frame.split("\n") if f"{PARENT} stopped responding" in line)
        assert row.lstrip(" ▸>").startswith(f"STL-{PARENT}-")
        assert re.search(r"stalled +1", frame), frame
        assert "1 all principals" in frame, "a stalled Run is counted, not a notice"
        _outputs(repo, daemon, 1)  # the Run answers; no patch says so, the re-read does
        cleared = await _until(app, pilot, lambda f: " STALLED  " not in f)
        assert re.search(r"stalled +0", cleared), cleared
        assert f"STL-{PARENT}-" not in cleared

    _launch(repo, monkeypatch, scenario, (160, 40))


def test_con_076_ui_008_the_run_frame_draws_the_daemon_s_timeline_groups_live(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CON-076, UI-008: token and word per event, a repeated chunk run as kind, count, span."""
    repo, daemon = live_tree
    _fail(repo)
    _outputs(repo, daemon, 4)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _route(app, pilot, "run.detail", PARENT)
        frame = await _until(app, pilot, lambda f: COALESCED.search(f) is not None)
        found = COALESCED.search(frame)
        assert found is not None and found.group("count") == "4"
        assert re.search(r"! error · observed +sequence \d+ +P0", frame), frame
        assert "WHAT IS TRUE" not in frame
        # a new chunk lands while the frame is on screen and the re-read folds it in
        await asyncio.to_thread(_outputs, repo, daemon, 1, 4)
        grown = await _until(
            app, pilot, lambda f: (m := COALESCED.search(f)) is not None and m["count"] == "5"
        )
        assert grown

    _launch(repo, monkeypatch, scenario, (120, 30))
