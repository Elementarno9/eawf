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

The last sections follow a live Run by its append notices against a document daemon that
counts every run-events read: one cursor read per notice for the Run on screen, none for
another Run or another route, none on the refresh timer; then the hold and follow keys,
and the feed wiring that carries the notice from the daemon bus to the seam.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.runtime.events import CommandPayload, RunEventKind, RunEventRecord
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods.run import RUN_EVENTS_READ_METHOD
from eawf.runtime.daemon.methods.spend import RUN_USAGE_READ_METHOD
from eawf.runtime.daemon.methods.state_subscribe import (
    PROJECTION_SUBSCRIBE_METHOD,
)
from eawf.runtime.daemon.server import _projection_frames, handle_connection
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.surfaces.cli.app import app as cli
from eawf.surfaces.tui import launch
from eawf.surfaces.tui.chassis.state_binding import StateBinding, StateBindingCallbacks
from eawf.surfaces.tui.console.app import LIVE_REFRESH_SECONDS, ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.harness import settle
from eawf.surfaces.tui.console.live_reads import TRANSCRIPT_READ, HeldTranscript
from eawf.surfaces.tui.console.renderers.transcript import follow_word
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session, SessionSetup
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_host_subagent_adoption import repository_row
from tests.tui.surfaces.tui.console import journey_support as js
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies
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


async def _shut(server: asyncio.AbstractServer) -> None:
    server.close()
    await server.wait_closed()


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
            # Closed on the daemon's own loop: a close from this thread races the loop
            # detaching the last connection, and both then wake the server's waiters.
            asyncio.run_coroutine_threadsafe(_shut(server), self._loop).result(10)
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


# ---------- following a live Run by its append notices ----------

#: The Run the transcript is opened on, a Run beside it, and a Run with no line yet.
FOLLOWED: Final = "RUN-00000002"
FOLLOWED_URN: Final = f"{bodies.ROOT}/run/{FOLLOWED}"
BESIDE_URN: Final = f"{bodies.ROOT}/run/RUN-00000001"
QUIET_RUN: Final = "RUN-00000003"
QUIET_URN: Final = f"{bodies.ROOT}/run/{QUIET_RUN}"

#: How many times the refresh timer is let to fire while the reads are counted.
TIMER_FIRINGS: Final = 3


def _line(urn: str, sequence: int) -> RunEventRecord:
    """Return one started foreground command at *sequence*: one block of its own."""
    return RunEventRecord.model_validate(
        {
            "event_ref": f"EVT-{sequence:08x}",
            "run_ref": urn,
            "run_sequence": sequence,
            "event_kind": RunEventKind.COMMAND_STARTED,
            "provenance": "provider_native",
            "payload": CommandPayload(
                command_family_ref="shell",
                command_ref=f"CMD-{sequence:08x}",
                phase="started",
                execution="foreground",
            ),
            "actor": bodies.ME,
            "recorded_at": bodies.AT + timedelta(seconds=sequence),
        }
    )


class _CountingDaemon(js.DocumentDaemon):
    """A document daemon that answers run-events reads by cursor and counts each one.

    Attributes:
        reads: Every run-events read, as the Run URN and the cursor it asked past.
        usage_reads: How many usage reads were answered, which the refresh timer makes.
        refuse_events: Whether a run-events read fails, as an unreachable daemon's would.
    """

    def __init__(self) -> None:
        super().__init__(bodies.DOCUMENT)
        self.reads: list[tuple[str, int]] = []
        self.usage_reads = 0
        self.refuse_events = False
        self.run_events = {
            FOLLOWED_URN: tuple(_line(FOLLOWED_URN, n) for n in (1, 2, 3)),
            BESIDE_URN: (_line(BESIDE_URN, 1),),
            QUIET_URN: (),
        }

    def append(self, urn: str, count: int = 1) -> int:
        """Append *count* lines to Run *urn*'s stream; return its new tail."""
        held = self.run_events[urn]
        tail = held[-1].run_sequence if held else 0
        grown = tuple(_line(urn, tail + n) for n in range(1, count + 1))
        self.run_events[urn] = (*held, *grown)
        return tail + count

    def answer(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Answer one call, a run-events read past its cursor as the daemon does."""
        if method == RUN_USAGE_READ_METHOD:
            self.usage_reads += 1
        if method != RUN_EVENTS_READ_METHOD:
            return super().answer(method, params)
        after = int(params.get("after_sequence", 0))
        self.reads.append((str(params["urn"]), after))
        if self.refuse_events:
            raise ConnectionError("the daemon went away")
        answer = super().answer(method, params)
        answer["events"] = [line for line in answer["events"] if line["run_sequence"] > after]
        return answer


def _notice(urn: str, tail: int, *, kind: StoreKind = StoreKind.RUN_EVENTS_APPENDED) -> Envelope:
    """Return the envelope the daemon publishes for one committed append."""
    return Envelope(
        id=f"run-events-appended-{urn.rsplit('/', 1)[-1]}-{tail}",
        kind=kind,
        scope_id=urn,
        created_at=bodies.AT,
        summary=f"run_events_appended tail={tail}",
        payload={"run_ref": urn, "tail_sequence": tail},
    )


def _strip(app: ConsoleApp) -> str:
    """Return the status strip under the header."""
    return app.frame_rows[1]


def _sequences(app: ConsoleApp) -> list[int]:
    """Return the sequences of the lines the transcript on screen holds, in order."""
    assert app.seam is not None
    held = app.seam.live(TRANSCRIPT_READ)
    assert isinstance(held, HeldTranscript)
    return [line.run_sequence for line in held.events]


async def _announce(daemon: _CountingDaemon, app: ConsoleApp, pilot: Any, urn: str) -> None:
    """Append one line to *urn*, announce it as the daemon does, and let the console settle."""
    tail = daemon.append(urn)
    assert app.seam is not None
    await app.seam.take_notice(_notice(urn, tail))
    await settle(pilot)


async def _open_followed(app: ConsoleApp, pilot: Any, run: str = FOLLOWED) -> None:
    app.reset(SessionSetup(route="transcript", subjId=run))
    await settle(pilot)


def _drive(daemon: _CountingDaemon, scenario: Callable[[ConsoleApp, Any], Any]) -> None:
    """Drive *scenario* in a console whose seam reads *daemon*."""

    async def body() -> None:
        async with js.driven(js.held_app(daemon), size=1) as harness:
            await scenario(harness.app, harness.pilot)

    asyncio.run(body())


def test_a_notice_for_the_run_on_screen_reads_past_the_held_cursor_once() -> None:
    """One run-events read per notice, asking past the last sequence held; nothing re-read."""
    daemon = _CountingDaemon()

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open_followed(app, pilot)
        assert daemon.reads == [(FOLLOWED_URN, 0)], "the transcript opens on one whole read"
        assert _sequences(app) == [1, 2, 3]
        await _announce(daemon, app, pilot, FOLLOWED_URN)
        assert daemon.reads[1:] == [(FOLLOWED_URN, 3)]
        assert _sequences(app) == [1, 2, 3, 4], "the new line is appended to what was held"
        await _announce(daemon, app, pilot, FOLLOWED_URN)
        await _announce(daemon, app, pilot, FOLLOWED_URN)
        assert daemon.reads[1:] == [(FOLLOWED_URN, 3), (FOLLOWED_URN, 4), (FOLLOWED_URN, 5)]
        assert _sequences(app) == [1, 2, 3, 4, 5, 6]
        assert app.session.tr_sel == 5, "following keeps the cursor on the newest block"
        assert "· following" in _strip(app)

    _drive(daemon, scenario)


def test_the_refresh_timer_reads_no_transcript() -> None:
    """Over three timer firings the usage read is repeated and the transcript is not."""
    daemon = _CountingDaemon()

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open_followed(app, pilot)
        reads, usage = len(daemon.reads), daemon.usage_reads
        assert isinstance(app.console_clock, FakeClock)
        for _ in range(TIMER_FIRINGS):
            app.console_clock.advance(LIVE_REFRESH_SECONDS)
            app.tick()
            await settle(pilot)
        assert len(daemon.reads) == reads, "the timer re-read the transcript"
        assert daemon.usage_reads == usage + TIMER_FIRINGS, "the timer did not fire"

    _drive(daemon, scenario)


def test_a_run_with_no_line_yet_reads_its_first_line_from_the_start() -> None:
    """An empty stream holds cursor zero, so its first notice reads from the start."""
    daemon = _CountingDaemon()

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open_followed(app, pilot, QUIET_RUN)
        assert _sequences(app) == []
        await _announce(daemon, app, pilot, QUIET_URN)
        assert daemon.reads == [(QUIET_URN, 0), (QUIET_URN, 0)]
        assert _sequences(app) == [1]
        assert app.session.tr_sel == 0

    _drive(daemon, scenario)


def test_a_notice_for_another_run_reads_nothing() -> None:
    daemon = _CountingDaemon()

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open_followed(app, pilot)
        reads = len(daemon.reads)
        await _announce(daemon, app, pilot, BESIDE_URN)
        assert len(daemon.reads) == reads
        assert _sequences(app) == [1, 2, 3]

    _drive(daemon, scenario)


def test_a_notice_while_another_route_is_on_screen_reads_nothing() -> None:
    """Off the transcript no notice reads; coming back reads once, past the held cursor."""
    daemon = _CountingDaemon()

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open_followed(app, pilot)
        app.reset(SessionSetup(route="activity"))
        await settle(pilot)
        reads = len(daemon.reads)
        await _announce(daemon, app, pilot, FOLLOWED_URN)
        await _announce(daemon, app, pilot, FOLLOWED_URN)
        assert len(daemon.reads) == reads, "a notice read while the transcript was away"
        await _open_followed(app, pilot)
        assert daemon.reads[reads:] == [(FOLLOWED_URN, 3)], "the return catches up once"
        assert _sequences(app) == [1, 2, 3, 4, 5]

    _drive(daemon, scenario)


def test_a_notice_that_is_no_append_or_does_not_validate_reads_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    daemon = _CountingDaemon()

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open_followed(app, pilot)
        seam = app.seam
        assert seam is not None
        reads = len(daemon.reads)
        await seam.take_notice(_notice(FOLLOWED_URN, 4, kind=StoreKind.EVENT))
        malformed = _notice(FOLLOWED_URN, 4).model_copy(update={"payload": {"run_ref": "x"}})
        with caplog.at_level(logging.DEBUG, logger="eawf.surfaces.tui.console.seam"):
            await seam.take_notice(malformed)
        await settle(pilot)
        assert len(daemon.reads) == reads
        assert "take_notice skip" in caplog.text

    _drive(daemon, scenario)


def test_a_failed_cursor_read_keeps_the_frame_and_the_next_notice_reads_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    daemon = _CountingDaemon()

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open_followed(app, pilot)
        daemon.refuse_events = True
        with caplog.at_level(logging.WARNING, logger="eawf.surfaces.tui.console.app"):
            await _announce(daemon, app, pilot, FOLLOWED_URN)
        assert "transcript tail read failed" in caplog.text
        assert _sequences(app) == [1, 2, 3], "the frame keeps what it held"
        daemon.refuse_events = False
        await _announce(daemon, app, pilot, FOLLOWED_URN)
        assert daemon.reads[-1] == (FOLLOWED_URN, 3)
        assert _sequences(app) == [1, 2, 3, 4, 5]

    _drive(daemon, scenario)


def test_following_a_read_that_holds_nothing_is_refused() -> None:
    """The seam follows only an answer it holds for what the route on screen is about."""
    daemon = _CountingDaemon()

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        seam = app.seam
        assert seam is not None
        with pytest.raises(ValueError, match="holds nothing to follow"):
            await seam.follow_live(TRANSCRIPT_READ)
        await _open_followed(app, pilot)
        with pytest.raises(KeyError):
            await seam.follow_live("no.such.read")

    _drive(daemon, scenario)


# ---------- holding and following with the keys ----------


def test_space_end_and_the_arrows_hold_and_follow_while_blocks_arrive() -> None:
    """Space toggles, End resumes on the newest block, the arrows hold, f still toggles."""
    daemon = _CountingDaemon()

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await _open_followed(app, pilot)
        s = app.session
        assert (s.tr_sel, s.follow) == (2, True)
        await pilot.press("up")
        await settle(pilot)
        assert (s.tr_sel, s.follow) == (1, False)
        assert "· held · 0 new" in _strip(app)
        assert daemon.reads == [(FOLLOWED_URN, 0)], "a key on the open transcript reads nothing"
        # held: the cursor stays where it is while blocks arrive, and counts them
        await _announce(daemon, app, pilot, FOLLOWED_URN)
        await _announce(daemon, app, pilot, FOLLOWED_URN)
        assert s.tr_sel == 1
        assert "· held · 2 new" in _strip(app)
        await pilot.press("down")
        await settle(pilot)
        assert s.tr_sel == 2 and "· held · 2 new" in _strip(app), "an arrow keeps the count"
        await pilot.press("space")
        await settle(pilot)
        assert (s.tr_sel, s.follow) == (4, True)
        assert "· following" in _strip(app) and "new" not in _strip(app)
        # following: the cursor rides the newest block
        await _announce(daemon, app, pilot, FOLLOWED_URN)
        assert s.tr_sel == 5
        await pilot.press("space")
        await settle(pilot)
        assert (s.tr_sel, s.follow) == (5, False)
        assert s.log[0].key == "Space"
        await _announce(daemon, app, pilot, FOLLOWED_URN)
        assert s.tr_sel == 5 and "· held · 1 new" in _strip(app)
        await pilot.press("end")
        await settle(pilot)
        assert (s.tr_sel, s.follow) == (6, True)
        assert "· following" in _strip(app)
        assert s.log[0].key == "End"
        await pilot.press("f")
        await settle(pilot)
        assert "· held · 0 new" in _strip(app)
        await pilot.press("f")
        await settle(pilot)
        assert (s.tr_sel, s.follow) == (6, True)
        # End on the newest block it already follows moves nothing and says so
        await pilot.press("end")
        await settle(pilot)
        assert (s.tr_sel, s.follow) == (6, True)
        assert "already on the newest block" in s.toasts[-1].text

    _drive(daemon, scenario)


@pytest.mark.parametrize(
    ("follow", "held_from", "total", "word"),
    [
        (True, None, 0, "following"),
        (True, None, 9, "following"),
        (False, None, 0, "held · 0 new"),
        (False, 0, 1, "held · 1 new"),
        (False, 3, 3, "held · 0 new"),
        (False, 3, 4, "held · 1 new"),
        # a hold kept from a longer Run counts nothing rather than below zero
        (False, 7, 2, "held · 0 new"),
    ],
    ids=["following-empty", "following", "held-empty", "single", "none-new", "one-new", "longer"],
)
def test_the_strip_says_following_or_how_many_arrived_while_held(
    follow: bool, held_from: int | None, total: int, word: str
) -> None:
    session = Session(follow=follow, tr_held_from=held_from)
    assert follow_word(session, total) == word


def test_a_hold_point_of_the_wrong_type_is_refused() -> None:
    session = Session()
    with pytest.raises(ValueError, match="tr_held_from"):
        session.tr_held_from = "three"  # type: ignore[assignment]


# ---------- the feed carries the notice from the bus to the seam ----------


def test_the_projection_feed_writes_an_append_notice_as_its_raw_push() -> None:
    (frame,) = _projection_frames(_notice(FOLLOWED_URN, 4))
    pushed = orjson.loads(frame)
    assert pushed["method"] == "event.push"
    assert pushed["params"]["event"]["payload"] == {"run_ref": FOLLOWED_URN, "tail_sequence": 4}


def test_only_the_projection_feed_subscribes_to_append_notices() -> None:
    async def nothing(_value: object) -> None:
        return None

    callbacks = StateBindingCallbacks(on_state=nothing, on_degraded=nothing)
    projection = StateBinding(None, callbacks, subscribe_method=PROJECTION_SUBSCRIBE_METHOD)
    epoch_one = StateBinding(None, callbacks)
    assert projection._subscribe_params()["kinds"] == [
        StoreKind.EVENT.value,
        StoreKind.RUN_EVENTS_APPENDED.value,
    ]
    assert epoch_one._subscribe_params()["kinds"] == [StoreKind.EVENT.value]


def test_a_pushed_notice_reaches_the_event_sink_without_a_state_read() -> None:
    """The projection feed hands a notice to its event sink alone; no resume id is taken."""
    heard: list[Envelope] = []
    states: list[object] = []

    async def on_event(envelope: Envelope) -> None:
        heard.append(envelope)

    async def on_state(state: object) -> None:
        states.append(state)

    async def on_degraded(_flag: bool) -> None:
        return None

    async def body() -> None:
        binding = StateBinding(
            None,
            StateBindingCallbacks(on_state=on_state, on_degraded=on_degraded, on_event=on_event),
            subscribe_method=PROJECTION_SUBSCRIBE_METHOD,
        )
        loop = asyncio.get_running_loop()
        (frame,) = _projection_frames(_notice(FOLLOWED_URN, 4))
        binding._handle_push_line(loop, frame)
        # an undecodable frame is dropped, never raised into the feed
        binding._handle_push_line(loop, b"not json\n")
        for _ in range(3):
            await asyncio.sleep(0)
        assert "since_event_id" not in binding._subscribe_params()

    asyncio.run(body())
    assert [envelope.kind for envelope in heard] == [StoreKind.RUN_EVENTS_APPENDED]
    assert states == []
