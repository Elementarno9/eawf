"""Live-test the rich transcript end to end: real hooks, a real daemon, the real launch path.

CON-169, CON-170, CON-171, RUN-062 and PRX-065 are held elsewhere against typed fixture
lines. This suite holds them against what actually produces the lines. A disposable
epoch-2 canary holds one live Run whose vendor session spawns a subagent. The subagent's
start and stop reach the tree through ``eawf hook run subagent_start`` and
``eawf hook run subagent_stop`` -- the commands the Claude Code plugin maps
``SubagentStart`` and ``SubagentStop`` to -- carrying a realistic subagent transcript
file, and a real daemon on a private socket files what they report. The console is then
opened by ``eawf ui``'s own launch function, whose seam reads the lines back through the
same daemon.

Nothing here reaches the operator's daemon: ``EAWF_RUNTIME_DIR`` points at a directory
this suite owns, and both the hook runner and the console's binding are handed a client
for this suite's socket, so no call can auto-spawn a daemon for any other tree.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.server import handle_connection
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.surfaces.cli.app import app as cli
from eawf.surfaces.tui import launch
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.harness import settle
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SessionSetup
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_host_subagent_adoption import repository_row
from tests.tui.surfaces.tui.console.test_console_live_smoke import _LoopbackClient, _socket_dir

#: The subagent's own transcript, as Claude Code writes it under the session's folder.
TRANSCRIPT_FIXTURE: Final = (
    Path(__file__).resolve().parents[4] / "fixtures/console/live/subagent-transcript.jsonl"
)

#: The Run the spawning session runs as, and the Run the subagent is adopted as.
PARENT: Final = "RUN-00000010"
CHILD: Final = "RUN-00000011"

#: The spawning session and the subagent, as the hook payloads name them.
HOST_SESSION: Final = "30f683b4-388a-4c04-89f4-612b7fe60362"
AGENT_ID: Final = "a0a45f67519cb14dd"

#: What the subagent says last, which its delegation reports as found.
FINAL_WORDS: Final = "Two modules read their"

#: How long a live change is waited for; a failure guard only, never a pacing sleep.
ARRIVAL_TIMEOUT_S: Final = 15.0

#: A clock and a kind in full, at the head of a block row.
BLOCK_HEAD: Final = re.compile(r"^.\d\d:\d\d:\d\d  (?P<glyph>\S) (?P<word>[a-z]+)")


class _Daemon:
    """A real daemon serving one tree on a private unix socket from its own thread.

    The hook commands are synchronous and the console runs its own event loop, so the
    daemon gets a loop of its own that both reach over the socket, as they would reach
    ``eawfd``.
    """

    def __init__(self, runtime_root: Path, sock_path: Path) -> None:
        self._ctx = method_context(runtime_root)
        self._ctx.bus = EventBus()
        self._sock_path = sock_path
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._server: asyncio.AbstractServer | None = None

    def __enter__(self) -> _Daemon:
        self._thread.start()
        started = asyncio.run_coroutine_threadsafe(
            asyncio.start_unix_server(
                lambda r, w: handle_connection(r, w, self._ctx), path=str(self._sock_path)
            ),
            self._loop,
        )
        self._server = started.result(timeout=10)
        return self

    def __exit__(self, *_exc: object) -> None:
        server = self._server
        if server is not None:
            server.close()
            asyncio.run_coroutine_threadsafe(server.wait_closed(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()

    def client(self, *_args: Any, **_kwargs: Any) -> _LoopbackClient:
        """Return a client for this daemon, whatever the caller's own factory would take."""
        return _LoopbackClient(str(self._sock_path))


@pytest.fixture
def live_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, _Daemon]]:
    """A canary whose one live Run is the spawning session, served by a private daemon."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    canary = provision(tmp_path / "repo", code="LIVE")
    parent = seed_row("run", "RUNNING")
    parent["vendor_session"] = {
        "harness": "claude-code",
        "session_digest": hash_vendor_session_id(HOST_SESSION),
    }
    seed(
        canary,
        {
            Epoch2Collection.REPOSITORY.value: {"REP-EAWF": repository_row()},
            Epoch2Collection.RUN.value: {PARENT: parent},
        },
    )
    socket_dir = _socket_dir()
    socket_dir.mkdir(mode=0o700)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(socket_dir))
    with _Daemon(tmp_path / "runtime", socket_dir / "eawfd.sock") as daemon:
        monkeypatch.setattr(
            "eawf.runtime.hooks.runner._default_daemon_client_factory", daemon.client
        )
        monkeypatch.setattr("eawf.surfaces.tui.chassis.state_binding.DaemonClient", daemon.client)
        yield canary.root, daemon
    for leftover in socket_dir.iterdir():
        leftover.unlink()
    socket_dir.rmdir()


def _hook(repo: Path, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run ``eawf hook run <event> --runtime claude`` with *payload* on stdin."""
    result = CliRunner().invoke(
        cli,
        ["--workspace", str(repo), "hook", "run", event, "--runtime", "claude"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)["body"]["results"]
    (adopted,) = [row for row in rows if row["name"] == "runtime.host_subagent"]
    assert " ok run=" in adopted["output"], adopted["output"]
    return dict(adopted)


def _start(repo: Path) -> None:
    _hook(
        repo,
        "subagent_start",
        {"hook_event_name": "SubagentStart", "agent_id": AGENT_ID, "session_id": HOST_SESSION},
    )


def _stop(repo: Path) -> None:
    _hook(
        repo,
        "subagent_stop",
        {
            "hook_event_name": "SubagentStop",
            "agent_id": AGENT_ID,
            "session_id": HOST_SESSION,
            "agent_transcript_path": str(TRANSCRIPT_FIXTURE),
        },
    )


Scenario = Callable[[ConsoleApp, ProjectionSeam, Any], Any]


def _launch(
    repo: Path, monkeypatch: pytest.MonkeyPatch, scenario: Scenario, size: tuple[int, int]
) -> None:
    """Open ``eawf ui``'s console on *repo* and drive *scenario* in it at *size*.

    The launch function resolves the tree, attaches and builds the console and its seam
    exactly as ``eawf ui`` does; only the last step, which would hand the terminal to the
    console, is replaced by a headless pilot.
    """
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
    assert launch.launch_tui(workspace=repo, no_input=False, plain=False) == 0
    assert driven == [True], "the launch landed in an entry state instead of the live console"


async def _frame(app: ConsoleApp, pilot: Any) -> str:
    app.render_frame()
    text, _cycles = await settle(pilot)
    return str(text)


async def _until(app: ConsoleApp, pilot: Any, holds: Callable[[str], bool]) -> str:
    """Return the first frame for which *holds* is true, repainting as the app works."""
    deadline = asyncio.get_running_loop().time() + ARRIVAL_TIMEOUT_S
    while True:
        frame = await _frame(app, pilot)
        if holds(frame):
            return frame
        assert asyncio.get_running_loop().time() < deadline, frame
        await pilot.pause(0.1)


async def _open(app: ConsoleApp, pilot: Any, run: str) -> None:
    app.reset(SessionSetup(route="transcript", subjId=run))
    await pilot.pause()


def _kinds(frame: str) -> list[str]:
    rows = (row.removeprefix(" ") for row in frame.split("\n"))
    return [found.group("word") for row in rows if (found := BLOCK_HEAD.match(row))]


def _caret_row(frame: str) -> str:
    return next(row for row in frame.split("\n") if row.lstrip(" │|").startswith(("▸", ">")))


@pytest.mark.parametrize("size", [(80, 24), (120, 30)], ids=["80x24", "120x30"])
def test_con_169_con_170_prx_065_a_live_subagent_transcript_renders_and_follows(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    """CON-169, CON-170, CON-171, RUN-062, PRX-065 on lines the real hooks produced.

    The parent's transcript draws the delegation the start hook stated as a typed
    ``child_run_started``, in flight and counted as background work; its fold opens the
    child's now, found and reports-back lines. The stop lands while the transcript is
    open and appears without a relaunch. The child's own transcript draws the bridged
    messages, the delegation it made in turn, and a long message folded and unfolded.
    """
    repo, _daemon = live_tree
    _start(repo)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _open(app, pilot, PARENT)
        frame = await _until(app, pilot, lambda f: "» subagent" in f)
        assert f"{CHILD} · started · working elsewhere" in frame
        assert "1 running in the background" in frame.split("\n")[1]
        assert re.search(r"\d\d:\d\d:\d\d  » subagent", frame)
        await pilot.press("enter")
        opened = await _frame(app, pilot)
        assert re.search(r"NOW +nothing yet", opened)
        assert re.search(rf"REPORTS +reports back into {PARENT}", opened)
        await pilot.press("enter")
        # the stop lands while the transcript is open, and the re-read draws it
        await asyncio.to_thread(_stop, repo)
        ended = await _until(app, pilot, lambda f: _kinds(f).count("subagent") == 2)
        assert "completed" in _caret_row(ended), "following takes the cursor to the new block"
        assert "running in the background" not in ended.split("\n")[1]
        await pilot.press("up", "enter")
        found = await _frame(app, pilot)
        assert re.search(r"ENDED +completed", found)
        assert re.search(rf"FOUND +{FINAL_WORDS}", found), found
        await pilot.press("f")
        assert "· following" in (await _frame(app, pilot)).split("\n")[1]

        await _open(app, pilot, CHILD)
        child = await _until(app, pilot, lambda f: "¶ message" in f)
        kinds = _kinds(child)
        assert set(kinds) == {"message", "subagent"}, child
        assert "a subagent · requested" in child
        assert "hidden reasoning" not in child
        assert "…" not in "".join(r for r in child.split("\n") if BLOCK_HEAD.match(r.lstrip(" ")))
        # the long message sits one above the final one; it folds beyond its preview
        await pilot.press("up")
        folded = await _frame(app, pilot)
        hidden = re.search(r"▸ (\d+ lines?)", _caret_row(folded))
        assert hidden is not None, _caret_row(folded)
        await pilot.press("enter")
        unfolded = await _frame(app, pilot)
        assert f"▾ {hidden.group(1)}" in _caret_row(unfolded)
        # the unfolded block shows what the preview hid; a short frame counts the rest below
        assert "Moving" in unfolded and "Moving" not in folded, unfolded

    _launch(repo, monkeypatch, scenario, size)


def test_con_067_a_live_transcript_in_ascii_mode_draws_the_twins(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CON-067, CON-169: under ``ui.glyphs: ascii`` every kind carries its one-cell twin."""
    repo, _daemon = live_tree
    monkeypatch.setenv("EAWF_UI__GLYPHS", "ascii")
    _start(repo)
    _stop(repo)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        assert app.glyphs == "ascii"
        await _open(app, pilot, CHILD)
        frame = await _until(app, pilot, lambda f: '" message' in f)
        assert frame.isascii(), [ch for ch in frame if not ch.isascii()]
        assert "} subagent" in frame

    _launch(repo, monkeypatch, scenario, (80, 24))
