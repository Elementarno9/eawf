"""Live-test the console read path against a real daemon on an epoch-2 canary tree.

Every other console suite replays the golden contract's fixture path, so a
console that passes them all can still be dishonest live (the header saying
``LIVE`` while its seam is disconnected, a route falling back to invented
rows). This suite drives :class:`~eawf.surfaces.tui.console.app.ConsoleApp`
headless, through Textual's ``Pilot``, over a REAL daemon: it rebuilds the
accepted-canary tree W37's own walk produces
(:func:`tests.integration.workflow.release._canary_acceptance_walk.walk_canary`)
and serves it on a private unix socket this suite starts and stops itself,
then walks the five journeys named in the brief -- home, activity, attention,
run and settings -- at the three tracked frame widths.

The first pass of this suite found the walk exposing two real, live-only
gaps -- Attention refusing its whole read once an acceptance question
opened, and a just-accepted Milestone vanishing from every route that lists
Milestones the instant it compacted into its ledger -- and reported them
rather than working around them. W42 (``Eawf-Wave: P34-I01-W42``) fixed
both; this suite now asserts the fixed behaviour positively as a regression
guard, on the same walk and the same genuinely connected seam.

Nothing here reaches the operator's daemon. ``EAWF_RUNTIME_DIR`` is
redirected to a short, disposable directory this suite owns *before* the
seam's ``connect()`` is ever called, so the one call that resolves the
well-known runtime socket
(``StateBinding._daemon_socket_available`` in
``eawf.surfaces.tui.chassis.state_binding``, which reads
``runtime_dir() / "eawfd.sock"`` directly, independent of the injected
transport) finds this suite's own socket rather than
``~/.eawfd/eawfd.sock``. ``connect()`` is therefore genuinely safe to call,
and the header's ``LIVE`` value is proven from an actual connect/subscribe
handshake, not merely from a read that happens to succeed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import socket
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final
from unittest import mock

import orjson
import pytest

from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.epoch2_root import RootIdentity
from eawf.runtime.daemon.runtime_dir import ensure_runtime_dir
from eawf.runtime.daemon.server import handle_connection
from eawf.surfaces.tui.console.app import OUTER_GUTTER, ConsoleApp
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.harness import capture_cells, grid_errors, settle
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from eawf.surfaces.tui.launch import project_name
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context
from tests.integration.workflow.release._canary_acceptance_walk import CanaryWalk, walk_canary

#: The five journeys the brief names, in the console's own route-id spelling.
JOURNEYS: tuple[str, ...] = ("scope.home", "activity", "attention", "run.detail", "settings")

#: How long a socket answer already asked for is waited on; a failure guard only,
#: sized for a loaded CI runner, where a settings read can take over a second.
CALL_TIMEOUT_SECONDS = 10.0

#: The system temp dir, captured before anything under test might redirect it.
SYSTEM_TEMP_DIR = tempfile.gettempdir()

#: The env var ``runtime_dir()`` reads first; redirected per :func:`live_console`
#: call so the seam's real ``connect()`` probes this suite's own socket.
_RUNTIME_DIR_ENV = "EAWF_RUNTIME_DIR"

#: Where the rendered captures are filed as evidence for this rehearsal.
EVIDENCE_PATH = (
    Path(__file__).resolve().parents[4] / "fixtures/console/live/canary-live-frames.json"
)

#: Set to ``1`` to rewrite :data:`EVIDENCE_PATH` from a fresh run. Mirrors
#: W37's ``EAWF_RECORD_CANARY_WALK``: a test must not mutate a tracked file
#: on a normal run (it dirties the tree under ``just test`` and inside a
#: close gate), and every frame here carries a digest-derived scope id that
#: differs per run, so there is no fixed baseline a normal run could compare
#: against byte for byte either. The render-and-assert path never writes;
#: only this opt-in records a fresh capture.
RECORD_EVIDENCE_ENV: Final = "EAWF_RECORD_CONSOLE_LIVE"

#: The pending action the walk's ``/verify`` step opens and seals, and the
#: only row the Attention route now holds once it is walked. W38 first found
#: this route refusing its whole read live (``PendingAction`` carried no
#: ``urn``, and the shared row validator refuses any row that states none --
#: see ``kernel/state/epoch2/pending_action.py``); W42
#: (``Eawf-Wave: P34-I01-W42``) gave ``PendingAction`` its own ``urn`` field,
#: so this is now a positive assertion, not a documented gap.
ATTENTION_ACTION_KEY = "ACT-0001"

#: This repository's root: the this-repository mode serves its own ``.ea`` tree.
REPO_ROOT: Final = Path(__file__).resolve().parents[5]

#: Where the this-repository mode files its frames, images and findings table.
REPO_EVIDENCE_DIR: Final = REPO_ROOT / ".ea/artifacts/evidence/2026-09-dev5-console-live"

#: The authority files a read-only serve must leave byte for byte as it found them,
#: relative to :data:`REPO_ROOT`; every file under ``.ea/generations`` is added too.
AUTHORITY_FILES: Final = (".ea/state.json", ".ea/epoch2-opt-in.json")


class _LoopbackClient:
    """A daemon client that speaks to this suite's own server over one socket.

    Stands in for :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` --
    same buffered-line-reader shape, ``_reader`` included -- so both a
    one-shot ``call()`` and the seam's persistent subscribe loop (which
    reaches past ``call()`` into ``client._reader.readline()`` directly, per
    ``StateBinding._run_subscription``) work over this socket exactly as
    they would over the real one.
    """

    def __init__(self, sock_path: str) -> None:
        self._sock_path = sock_path
        self._sock: socket.socket | None = None
        self._reader: Any = None

    def __enter__(self) -> _LoopbackClient:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(CALL_TIMEOUT_SECONDS)
        self._sock.connect(self._sock_path)
        self._reader = self._sock.makefile("rb")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._reader is not None:
            with contextlib.suppress(OSError):
                self._reader.close()
            self._reader = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request frame and return the result the daemon answered with.

        Raises:
            RuntimeError: the daemon answered with a JSON-RPC error, or closed the
                socket before answering.
        """
        assert self._sock is not None and self._reader is not None, (
            "a call is made inside the client's context"
        )
        frame = orjson.dumps(
            {"jsonrpc": "2.0", "id": "console-live", "method": method, "params": params or {}}
        )
        self._sock.sendall(frame + b"\n")
        line = self._reader.readline()
        if not line:
            raise RuntimeError(f"the daemon closed the socket during {method}")
        reply = orjson.loads(line)
        if "error" in reply:
            raise RuntimeError(str(reply["error"].get("message", reply["error"])))
        return dict(reply["result"])


def _socket_dir() -> Path:
    """Return a short, disposable runtime dir under the real system temp dir.

    Short on purpose: an AF_UNIX bind address has about 104 bytes to live in
    on macOS, and ``<dir>/eawfd.sock`` has to fit inside that.
    """
    return Path(SYSTEM_TEMP_DIR) / f"eawf-w38-rt-{uuid.uuid4().hex[:8]}"


@contextlib.asynccontextmanager
async def live_console(
    repo_root: Path, runtime_root: Path, *, launched: bool = False
) -> AsyncIterator[tuple[ConsoleApp, ProjectionSeam]]:
    """Serve the epoch-2 tree at ``repo_root`` at an isolated well-known socket and connect.

    ``walk_canary`` itself calls ``asyncio.run`` per RPC (see ``Walker.verb``), so
    a canary must be walked by the caller *before* entering an event loop -- this
    helper only serves a finished tree, which is why it takes a root rather than
    building one; see ``tests/integration/workflow/release/_canary_acceptance_walk.py``.

    ``EAWF_RUNTIME_DIR`` is redirected to a fresh directory this call owns, and
    the daemon is served AT ``runtime_dir() / "eawfd.sock"`` -- the exact path
    ``StateBinding._daemon_socket_available`` probes -- so ``seam.connect()``
    finds a real, genuinely isolated daemon rather than degrading. The env var
    is restored and the directory removed on exit, so no test run leaks global
    state into a sibling test.

    Args:
        repo_root: The repository whose ``.ea`` tree resolves to epoch 2: an
            already-walked canary, or this repository itself.
        runtime_root: Where the daemon context this suite serves keeps its WAL --
            for a canary, the same directory its walk was produced against.
        launched: Whether to add what ``eawf ui`` adds around the frame -- the outer
            gutter and the project name in the crumb -- so a key's effect is judged on
            the frame an operator sees; the grid checks read the bare frame.

    Yields:
        The console (packaged chrome plus a live seam, as ``eawf ui`` builds it for an
        epoch-2 tree), already connected, and the seam itself.
    """
    ctx = method_context(runtime_root)
    ctx.bus = EventBus()
    socket_dir = _socket_dir()
    previous_runtime_dir = os.environ.get(_RUNTIME_DIR_ENV)
    os.environ[_RUNTIME_DIR_ENV] = str(socket_dir)
    try:
        ensure_runtime_dir()
        sock_path = str(socket_dir / "eawfd.sock")
        server = await asyncio.start_unix_server(
            lambda r, w: handle_connection(r, w, ctx), path=sock_path
        )
        try:
            seam = ProjectionSeam(
                route=JOURNEYS[0],
                scope_id=RootIdentity.of(repo_root).root_id,
                state_path=None,
                repo_root=repo_root,
                daemon_client_factory=lambda: _LoopbackClient(sock_path),
                scope_name=project_name(repo_root / ".ea/state.json", repo_root)
                if launched
                else "",
            )
            app = ConsoleApp(
                chrome=load_chrome(),
                seam=seam,
                clock=FakeClock(),
                gutter=OUTER_GUTTER if launched else 0,
            )
            await seam.connect()
            try:
                yield app, seam
            finally:
                await seam.disconnect()
        finally:
            server.close()
            await server.wait_closed()
    finally:
        if previous_runtime_dir is None:
            os.environ.pop(_RUNTIME_DIR_ENV, None)
        else:
            os.environ[_RUNTIME_DIR_ENV] = previous_runtime_dir
        shutil.rmtree(socket_dir, ignore_errors=True)


def walk_canary_isolated(tmp_path: Path) -> tuple[CanaryWalk, Path]:
    """Rebuild the accepted canary with its runtime dir redirected under ``tmp_path``.

    ``provision_canary`` allocates ``CanaryProvision.runtime_dir`` inside the
    canary's own tree, but ``tempfile.tempdir`` is still redirected so any
    other temp allocation the walk makes stays under ``tmp_path`` and this
    suite leaves nothing behind in the real temp dir.

    Returns:
        The walk, and the runtime root a caller serves the daemon from
        (``method_context`` needs the same directory the walk was built
        against).
    """
    runtime_root = tmp_path / "runtime"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with mock.patch.object(tempfile, "tempdir", str(scratch)):
        walk = walk_canary(tmp_path / "repo", runtime_root)
    return walk, runtime_root


async def render_setup(app: ConsoleApp, pilot: Any, setup: SessionSetup) -> str:
    """Reset ``app`` to ``setup``, follow its frame size, render, settle, and capture.

    Mirrors :meth:`~eawf.surfaces.tui.console.harness.Harness.render`'s own order
    (reset, follow size, render, settle) without the golden harness's normaliser,
    which this live rehearsal has no tracked frame to compare against.
    """
    app.reset(setup)
    w, h = SIZES[app.session.size]
    if (app.size.width, app.size.height) != (w, h):
        await pilot.resize_terminal(w, h)
        await pilot.pause()
    app.render_frame()
    text, _cycles = await settle(pilot)
    return text


def authority_digests(repo_root: Path) -> dict[str, str]:
    """Return the sha256 of every authority file under ``repo_root``, keyed by relative path."""
    paths = [repo_root / rel for rel in AUTHORITY_FILES]
    paths += sorted(p for p in (repo_root / ".ea/generations").rglob("*") if p.is_file())
    return {
        p.relative_to(repo_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
        if p.is_file()
    }


def require_epoch2_repository() -> None:
    """Skip unless this repository's ``.ea`` tree resolves to epoch 2.

    The root comes from this file's own location, never from ``EA_STATE``, so a
    close runner that exports it at a sandbox copy of the state file cannot
    redirect which tree this mode serves.
    """
    authority = resolve_authority(REPO_ROOT / ".ea")
    if authority.epoch != 2:
        pytest.skip(f"this repository resolves to epoch 1 ({authority.gap})")


def test_walk_canary_admits_the_milestone_this_suite_keys_on(tmp_path: Path) -> None:
    """Boundary precondition, daemon-free: the walk still keys its Milestone MLS-0001.

    Fast and cheap on purpose -- a guard against the walk helper's own shape
    drifting out from under the live-render assertions below, without paying
    for a daemon and a Pilot session just to check it.
    """
    walk, _runtime_root = walk_canary_isolated(tmp_path)

    assert walk.milestone_urn.rsplit("/", 1)[1] == "MLS-0001"
    assert walk.milestone_status
    assert walk.bundle is not None


def test_the_accepted_milestone_still_leaves_the_live_document(tmp_path: Path) -> None:
    """The document-level half of the compaction fact this suite first found live.

    ``walk_canary`` always drives its Milestone to acceptance -- a terminal
    status -- and a terminal record is still compacted out of the live
    document (matching the pattern ``delivery_approval.py::_batches``
    documents for Batches: "A finished Batch is compacted out of the
    document into its ledger"). W42 did not change this half: it taught
    routes to merge the ledger's terminal rows back in
    (``kernel/projection/compute.py``'s ``ledger_rows`` parameter, wired
    through ``runtime/daemon/methods/projection.py``), not to keep the row
    in the document. The route-level half -- that ``scope.home`` renders
    MLS-0001 anyway -- is asserted positively in
    ``test_console_live_journeys_render_the_canarys_own_ids_at_every_tracked_width``,
    which is also where the daemon-served, seam-held projection this
    document-only read cannot see is exercised.
    """
    walk, runtime_root = walk_canary_isolated(tmp_path)
    ctx = method_context(runtime_root)
    context = ctx.native_root_context(walk.canary.root / ".ea")

    with context.session([walk.milestone_urn]) as session:
        document = session.read_document()

    assert document_rows(document, Epoch2Collection.MILESTONE) == {}
    assert "TRK-CANARY" in document_rows(document, Epoch2Collection.TRACK)


def test_console_live_journeys_render_the_canarys_own_ids_at_every_tracked_width(
    tmp_path: Path,
) -> None:
    """CR-01: the five journeys render real rows at 80, 120 and 160 columns.

    Home and Activity are the routes CR-01 names explicitly: ``scope.home``
    binds Track and Milestone, ``activity`` binds Run. The expected ids are
    read off the seam's own held projection rather than hardcoded, so a
    rename inside the canary walk cannot make this pass for the wrong
    reason -- it would simply assert against whatever the walk now produces.

    The walk carries its Milestone to acceptance, a terminal status that
    ``test_the_accepted_milestone_still_leaves_the_live_document`` proves
    removes it from the live document -- and, since W42, ``scope.home``
    still renders it anyway, because the route now merges the Milestone
    ledger's terminal rows back in. This suite asserts that positively:
    the Milestone row's ``urn`` and ``status`` are read off the same held
    projection, not just its key.

    Attention, Run and Settings are walked too (the brief's five journeys).
    Run and Settings are checked for a held, well-formed frame. Attention
    is checked positively as well, since W42 also gave ``PendingAction`` a
    ``urn`` of its own: the walk's ``/verify`` step opens and seals exactly
    one question, and the route now holds exactly that one row.

    Setting :data:`RECORD_EVIDENCE_ENV` rewrites :data:`EVIDENCE_PATH` from
    this run; a normal run only renders and asserts, so it never mutates a
    tracked file.
    """

    walk, runtime_root = walk_canary_isolated(tmp_path)

    async def body() -> dict[str, str]:
        frames: dict[str, str] = {}
        async with (
            live_console(walk.canary.root, runtime_root) as (app, seam),
            app.run_test(size=SIZES[0]) as pilot,
        ):
            for size_index, (w, h) in enumerate(SIZES):
                for route in JOURNEYS:
                    text = await render_setup(
                        app, pilot, SessionSetup(route=route, size=size_index)
                    )
                    label = f"{route}@{w}x{h}"
                    frames[label] = text
                    assert "NOT HELD" not in text, f"{label} drew the unknown frame"
                    errors = grid_errors(text, (w, h), capture_cells(app))
                    assert not errors, f"{label}: {'; '.join(errors)}"

            home = seam.projection_for("scope.home")
            activity = seam.projection_for("activity")
            attention = seam.projection_for("attention")
            assert home is not None, "scope.home was walked, so its route must be held"
            assert activity is not None, "activity was walked, so its route must be held"
            assert attention is not None, "attention was walked, so its route must be held"
            track_ids = [r.key for r in home.rows if r.collection is Epoch2Collection.TRACK]
            milestone_rows = [r for r in home.rows if r.collection is Epoch2Collection.MILESTONE]
            run_ids = [r.key for r in activity.rows]
            assert track_ids, "the canary tree seeds one Track"
            # W42 merges the accepted Milestone's terminal ledger row back into
            # scope.home, so the route holds it even though the document does not
            # (test_the_accepted_milestone_still_leaves_the_live_document).
            assert [r.key for r in milestone_rows] == ["MLS-0001"]
            milestone_row = milestone_rows[0]
            assert milestone_row.urn == walk.milestone_urn
            assert milestone_row.status.value == "COMPLETED"
            assert run_ids, "the canary tree dispatches one Run"
            # W42 gave PendingAction its own urn; the walk's /verify step opens and
            # seals exactly this one question, so Attention holds exactly one row.
            assert [r.key for r in attention.rows] == [ATTENTION_ACTION_KEY]
            action_row = attention.rows[0]
            assert action_row.urn == f"{walk.container}/pending-action/{ATTENTION_ACTION_KEY}"
            assert action_row.status.value == "SEALED"

            milestone_ids = [row.key for row in milestone_rows]
            for w, h in SIZES:
                home_text = frames[f"scope.home@{w}x{h}"]
                activity_text = frames[f"activity@{w}x{h}"]
                for key in (*track_ids, *milestone_ids):
                    assert key in home_text, f"{key} missing from scope.home@{w}x{h}"
                for key in run_ids:
                    assert key in activity_text, f"{key} missing from activity@{w}x{h}"
        return frames

    frames = asyncio.run(body())

    if os.environ.get(RECORD_EVIDENCE_ENV) == "1":
        EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_PATH.write_text(
            json.dumps(
                {
                    "schema_version": "console-live-frames/v1",
                    "journeys": list(JOURNEYS),
                    "widths": [f"{w}x{h}" for w, h in SIZES],
                    "frames": frames,
                },
                indent=1,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )


def test_header_connection_value_is_live_only_while_the_seam_holds_a_read(
    tmp_path: Path,
) -> None:
    """CR-01: the header reads LIVE only while the seam's own link says so.

    ``live_console`` calls ``seam.connect()`` for real before yielding: the
    probe inside ``connect()`` resolves ``runtime_dir() / "eawfd.sock"``
    (``EAWF_RUNTIME_DIR`` redirected to this call's own isolated directory),
    finds the daemon this suite is serving there, and starts the genuine
    subscribe handshake -- this is not a read that happens to succeed while
    the transport sits unconnected. The header derives its connection label
    from ``seam.connection`` (:meth:`ConsoleApp._sync_conn`), never from a
    value the console asserts on its own account, and a route read after
    that connect calls the link live and complete. A seam that is then told
    to disconnect calls it exactly what the wire now says, and the header
    must never keep drawing ``LIVE`` once that has happened.
    """

    walk, runtime_root = walk_canary_isolated(tmp_path)

    async def body() -> tuple[str, str]:
        async with (
            live_console(walk.canary.root, runtime_root) as (app, seam),
            app.run_test(size=SIZES[0]) as pilot,
        ):
            live_text = await render_setup(app, pilot, SessionSetup(route="scope.home"))
            await seam.disconnect()
            app.render_frame()
            disconnected_text, _cycles = await settle(pilot)
            return live_text, disconnected_text

    live_text, disconnected_text = asyncio.run(body())
    live_header = live_text.splitlines()[0]
    disconnected_header = disconnected_text.splitlines()[0]

    assert "● LIVE" in live_header
    assert "PARTIAL" not in live_header
    assert "DISCONNECTED" not in live_header
    assert "DISCONNECTED" in disconnected_header
    assert "LIVE" not in disconnected_header


def test_console_live_journeys_serve_this_repository_read_only(tmp_path: Path) -> None:
    """CR-01: this repository's own projection, served live, draws no prototype literal.

    The canary above proves the read path on a tree a test built; this mode
    proves it on the tree an operator actually opens. A real daemon serves
    :data:`REPO_ROOT`'s ``.ea`` tree over the same isolated socket, the same
    five journeys render at every :data:`SIZES` width, and every frame must be
    on its grid and free of the prototype's literals
    (``test_console_live_no_fixture.assert_no_fixture_literal``). The header of
    every held route must name this repository's own root id, so a pass cannot
    come from a console drawing some other tree. The authority files are
    digested before and after, so a serve that wrote to the tree reds.

    ``scope.home`` must hold this repository's Milestones -- rows the cutover
    imported, which state no URN of their own -- and draw the first of them at
    every width.

    Setting :data:`RECORD_EVIDENCE_ENV` also files each frame's text and a
    Textual SVG screenshot under :data:`REPO_EVIDENCE_DIR`.
    """
    # Imported here: that module imports this one at load time.
    from tests.tui.surfaces.tui.console.test_console_live_no_fixture import (
        assert_no_fixture_literal,
    )

    require_epoch2_repository()
    root_id = RootIdentity.of(REPO_ROOT / ".ea").root_id
    before = authority_digests(REPO_ROOT)

    held: set[str] = set()

    async def body() -> tuple[dict[str, str], dict[str, str]]:
        frames: dict[str, str] = {}
        images: dict[str, str] = {}
        async with (
            live_console(REPO_ROOT, tmp_path / "runtime") as (app, seam),
            app.run_test(size=SIZES[0]) as pilot,
        ):
            for size_index, (w, h) in enumerate(SIZES):
                for route in JOURNEYS:
                    text = await render_setup(
                        app, pilot, SessionSetup(route=route, size=size_index)
                    )
                    label = f"{route}@{w}x{h}"
                    errors = grid_errors(text, (w, h), capture_cells(app))
                    assert not errors, f"{label}: {'; '.join(errors)}"
                    projection = seam.projection_for(route)
                    if projection is not None:
                        assert root_id in text.splitlines()[0], f"{label} names another tree"
                        # a row names the record it is filed under, so both are the tree's
                        held.update(r.key for r in projection.rows)
                        held.update(r.parent_key for r in projection.rows if r.parent_key)
                    frames[label] = text
                    images[label] = app.export_screenshot(title=label)
            home = seam.projection_for("scope.home")
            assert home is not None, "scope.home was walked, so its route must be held"
            milestone_ids = [r.key for r in home.rows if r.collection is Epoch2Collection.MILESTONE]
        assert milestone_ids, "this repository's imported Milestones must be read"
        for w, h in SIZES:
            assert milestone_ids[0] in frames[f"scope.home@{w}x{h}"], f"scope.home@{w}x{h}"
        return frames, images

    frames, images = asyncio.run(body())

    assert_no_fixture_literal(frames, held=frozenset(held))
    assert authority_digests(REPO_ROOT) == before, "the live serve wrote to the authority tree"

    if os.environ.get(RECORD_EVIDENCE_ENV) == "1":
        for kind, captures, suffix in (("frames", frames, "txt"), ("images", images, "svg")):
            target = REPO_EVIDENCE_DIR / kind
            target.mkdir(parents=True, exist_ok=True)
            for label, content in captures.items():
                (target / f"{label}.{suffix}").write_text(content, encoding="utf-8")


def test_planted_prototype_register_reds_this_repositorys_frame_check(tmp_path: Path) -> None:
    """Gate-fire proof: the same live console, falling back to the prototype registers, reds.

    Each journey is first rendered clean over this repository's own projection.
    Then the registers the golden harness loads are planted into that console and
    its link to the daemon is dropped: the seeded shape of the honesty-floor
    defect, a console that loses its read model and draws the prototype's rows
    instead of saying so. The frame check must name a literal on every journey.
    """
    from tests.tui.surfaces.tui.console.test_console_live_no_fixture import (
        FIXTURE_DIR,
        assert_no_fixture_literal,
    )

    require_epoch2_repository()

    held: set[str] = set()

    async def body() -> tuple[dict[str, str], dict[str, str]]:
        live: dict[str, str] = {}
        planted: dict[str, str] = {}
        async with (
            live_console(REPO_ROOT, tmp_path / "runtime") as (app, seam),
            app.run_test(size=SIZES[0]) as pilot,
        ):
            for route in JOURNEYS:
                live[route] = await render_setup(app, pilot, SessionSetup(route=route))
                projection = seam.projection_for(route)
                if projection is not None:
                    held.update(r.key for r in projection.rows)
                    held.update(r.parent_key for r in projection.rows if r.parent_key)
            app.fixture = load_fixture(FIXTURE_DIR)
            app.seam = None
            for route in JOURNEYS:
                planted[route] = await render_setup(app, pilot, SessionSetup(route=route))
        return live, planted

    live, planted = asyncio.run(body())

    assert_no_fixture_literal(live, held=frozenset(held))
    for route, text in planted.items():
        with pytest.raises(AssertionError, match="fixture literal"):
            assert_no_fixture_literal({route: text})
