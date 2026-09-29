"""CON-051, CON-052, CON-058: the consequence card over a real daemon on a seeded tree.

The canary walk's planning step seeds an epoch-2 tree -- an active Track with an open
Milestone under it, an active Batch, a planned Task -- and this suite serves it over a
private socket through the real daemon's connection handler. The console is built the way
``eawf ui`` builds it for such a tree, with a seam acting as the walk's operator.

Three things are proved on the live path. A claim previews the edge from the Task's own
status and revision, writes nothing while it previews, and once confirmed shows the
daemon's committed answer, which the tree then holds. A Track retire that the daemon's
own guard refuses shows that refusal, with its guard and remediation, and leaves the
tree unmoved. A Batch move the request itself cannot satisfy is refused on the card and
never reaches the daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest import mock

from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.epoch2_root import RootIdentity
from eawf.runtime.daemon.runtime_dir import ensure_runtime_dir
from eawf.runtime.daemon.server import handle_connection
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.harness import settle
from eawf.surfaces.tui.console.mutation import Card
from eawf.surfaces.tui.console.operations import Operator
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SessionSetup
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context
from tests.integration.workflow.release._canary_acceptance_walk import (
    OPERATOR,
    PROJECT_CODE,
    Walker,
    _plan,
)
from tests.tui.surfaces.tui.console.test_console_live_smoke import (
    _RUNTIME_DIR_ENV,
    _LoopbackClient,
    _socket_dir,
)

TASK = f"{PROJECT_CODE}-0001"
TRACK = "TRK-CANARY"
BATCH = "BAT-0001"
SIZE = (120, 30)


def _seed(tmp_path: Path) -> tuple[Walker, Path]:
    """Seed the planned tree: the walk's first step and nothing after it."""
    runtime_root = tmp_path / "runtime"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with mock.patch.object(tempfile, "tempdir", str(scratch)):
        walker = Walker(tmp_path / "repo", runtime_root)
        _plan(walker)
    return walker, runtime_root


@contextlib.asynccontextmanager
async def _console(walker: Walker, runtime_root: Path) -> AsyncIterator[ConsoleApp]:
    """Serve the seeded tree through the real handler and yield a console acting as its operator."""
    ctx = method_context(runtime_root)
    ctx.bus = EventBus()
    socket_dir = _socket_dir()
    previous = os.environ.get(_RUNTIME_DIR_ENV)
    os.environ[_RUNTIME_DIR_ENV] = str(socket_dir)
    try:
        ensure_runtime_dir()
        sock_path = str(socket_dir / "eawfd.sock")
        server = await asyncio.start_unix_server(
            lambda r, w: handle_connection(r, w, ctx), path=sock_path
        )
        try:
            seam = ProjectionSeam(
                route="task.detail",
                scope_id=RootIdentity.of(walker.root).root_id,
                state_path=None,
                repo_root=walker.root,
                operator=Operator(principal=OPERATOR),
                daemon_client_factory=lambda: _LoopbackClient(sock_path),
            )
            app = ConsoleApp(chrome=load_chrome(), seam=seam, clock=FakeClock())
            await seam.connect()
            try:
                yield app
            finally:
                await seam.disconnect()
        finally:
            server.close()
            await server.wait_closed()
    finally:
        if previous is None:
            os.environ.pop(_RUNTIME_DIR_ENV, None)
        else:
            os.environ[_RUNTIME_DIR_ENV] = previous
        shutil.rmtree(socket_dir, ignore_errors=True)


async def _open(app: ConsoleApp, pilot: Any, route: str, subject: str) -> None:
    """Put the console on ``route`` at ``subject`` with that route's projection held."""
    app.reset(SessionSetup.model_validate({"route": route, "subjId": subject}))
    seam = app.seam
    assert seam is not None
    seam.retarget(route)
    await seam.load(route)
    app.render_frame()
    await settle(pilot)


async def _keys(app: ConsoleApp, pilot: Any, *keys: str) -> str:
    """Press ``keys`` through the app, let every write settle, and return the frame."""
    for key in keys:
        app.press_key(key)
        await app.workers.wait_for_complete()
        await pilot.pause()
    app.render_frame()
    text, _cycles = await settle(pilot)
    return text


def _card(app: ConsoleApp) -> Card:
    card = app.session.mutation
    assert isinstance(card, Card)
    return card


def test_con_051_a_live_claim_previews_then_shows_the_committed_outcome(tmp_path: Path) -> None:
    walker, runtime_root = _seed(tmp_path)
    task_urn = walker.urn("task", TASK)
    before = walker.revision(task_urn)

    async def body() -> tuple[str, str]:
        async with _console(walker, runtime_root) as app, app.run_test(size=SIZE) as pilot:
            await _open(app, pilot, "task.detail", TASK)
            preview = await _keys(app, pilot, ".", "l")
            assert walker.revision(task_urn) == before, "a preview writes nothing"
            answered = await _keys(app, pilot, "Enter")
            return preview, answered

    preview, answered = asyncio.run(body())

    assert f"task {TASK} moves PLANNED → CLAIMED" in preview
    assert f"revision {before} · PLANNED · exact" in preview
    assert "Enter confirm" in preview
    assert walker.stored(task_urn)["status"] == "CLAIMED"
    assert walker.revision(task_urn) == before + 1
    assert "confirmed" in answered
    assert f"revision {before} → {before + 1} · committed" in " ".join(answered.split())


def test_con_058_a_daemon_guard_refusal_is_shown_and_the_tree_is_unmoved(tmp_path: Path) -> None:
    walker, runtime_root = _seed(tmp_path)
    track_urn = walker.urn("track", TRACK)
    before = walker.revision(track_urn)

    async def body() -> tuple[str, str, Card]:
        async with _console(walker, runtime_root) as app, app.run_test(size=SIZE) as pilot:
            await _open(app, pilot, "track", TRACK)
            preview = await _keys(app, pilot, ".", "r")
            answered = await _keys(app, pilot, "Enter")
            return preview, answered, _card(app)

    preview, answered, card = asyncio.run(body())

    flat = " ".join(preview.split())
    assert "only if no milestone under the track is still open" in flat
    assert [row.disposition.value for row in card.results] == ["rejected"]
    assert "transition_guard_failed" in card.results[0].detail
    assert "no_open_milestones" in card.results[0].detail
    assert "rejected" in answered
    assert walker.stored(track_urn)["status"] == "ACTIVE"
    assert walker.revision(track_urn) == before


def test_con_052_a_move_the_request_cannot_satisfy_never_reaches_the_daemon(
    tmp_path: Path,
) -> None:
    walker, runtime_root = _seed(tmp_path)
    batch_urn = walker.urn("batch", BATCH)
    before = walker.revision(batch_urn)

    async def body() -> tuple[str, int]:
        async with _console(walker, runtime_root) as app, app.run_test(size=SIZE) as pilot:
            await _open(app, pilot, "batch.detail", BATCH)
            preview = await _keys(app, pilot, ".", "d", "Enter")
            seam = app.seam
            assert seam is not None
            return preview, len(seam.outstanding)

    preview, outstanding = asyncio.run(body())

    assert "refused before sending · missing_transition_fields" in preview
    assert "current_head_binding" in " ".join(preview.split())
    assert outstanding == 0
    assert walker.revision(batch_urn) == before
