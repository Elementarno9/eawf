"""What the driven journey suites share: the worlds a console is driven in, and the port journeys.

A journey is driven by keys alone, through the real :class:`ConsoleApp` under the
toolkit's pilot, by the golden harness -- never through the dispatcher directly. It is
driven in one of two kinds of world: the prototype registers the pack's golden contract
replays, or a held world whose seam reads an epoch-2 document through the binding's
client factory, the way a daemon would answer it, acting as one principal.

The port's own journeys are recorded from the render path into
:data:`PORT_JOURNEYS_PATH` by :meth:`Harness.record` and replayed against it. To re-record
after an intended change, run the suites with ``EAWF_RECORD_CONSOLE_JOURNEYS=1`` and
review every moved frame in the diff; a normal run never writes the file.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.economics.spend import CostCeilingView, RunUsageView
from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, build_route_projection
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE, RECONNECT_METHOD_TEMPLATE
from eawf.kernel.projection.run_timeline import reduce_timeline
from eawf.kernel.runtime.dispatch_queue import DispatchControl, DispatchPlan, DispatchQueueView
from eawf.kernel.runtime.events import RunEventRecord
from eawf.kernel.store.changes import ChangePage
from eawf.runtime.daemon.methods.console_records import (
    BOOT_RECOVERY_READ_METHOD,
    HISTORY_CHANGES_READ_METHOD,
)
from eawf.runtime.daemon.methods.dispatch_queue import DISPATCH_QUEUE_READ_METHOD
from eawf.runtime.daemon.methods.pause import PAUSE_READ_METHOD
from eawf.runtime.daemon.methods.question import QUESTION_READ_METHOD
from eawf.runtime.daemon.methods.run import RUN_EVENTS_READ_METHOD
from eawf.runtime.daemon.methods.run_liveness import RUN_STALLS_READ_METHOD
from eawf.runtime.daemon.methods.spend import (
    RUN_USAGE_READ_METHOD,
    SPEND_CEILING_READ_METHOD,
    RunUsageReadParams,
)
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.harness import (
    GoldenLayout,
    Harness,
    Journey,
    Result,
    load_contract,
    settle,
)
from eawf.surfaces.tui.console.normalisation import Normaliser, load_map
from eawf.surfaces.tui.console.operations import (
    CONTROL_METHOD,
    DISPATCH_CONTROL_METHOD,
    Operator,
)
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from tests.tui.surfaces.tui.console import test_bulk_per_item_results as bulk
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies

GOLDEN_ROOT: Final = Path(__file__).resolve().parents[4] / "fixtures/console/golden"
LAYOUT: Final = GoldenLayout.under(GOLDEN_ROOT)

#: The port's own journeys, recorded from the render path beside the pack's journey file.
PORT_JOURNEYS_PATH: Final = GOLDEN_ROOT / "journeys-port.json"

#: Set to ``1`` to re-record :data:`PORT_JOURNEYS_PATH` from the render path.
RECORD_ENV: Final = "EAWF_RECORD_CONSOLE_JOURNEYS"

#: The worlds a journey is driven in: the pack's prototype registers; the probe tree held
#: for one principal; two planned Milestones and two Runs whose second write answer is lost;
#: the probe tree with nothing open and nothing queued; and the probe tree with both its
#: Tasks filed under its one Batch, so a Task has a sibling to walk to.
PROTOTYPE, HELD, BULK, QUIET, TREE = "prototype", "held", "bulk", "quiet", "tree"

#: The probe tree with its one sealed action and no open one, and no draft or deferred Task.
QUIET_DOCUMENT: Final[dict[str, Any]] = {
    **bodies.DOCUMENT,
    "pending_action": {"ACT-0003": bodies.DOCUMENT["pending_action"]["ACT-0003"]},
}


#: The probe tree with its second Task filed under the Batch the first one is in.
TREE_DOCUMENT: Final[dict[str, Any]] = {
    **bodies.DOCUMENT,
    "task": {
        **bodies.DOCUMENT["task"],
        "TSK-0002": {
            **bodies.DOCUMENT["task"]["TSK-0002"],
            "batch_ref": f"{bodies.ROOT}/batch/BAT-0100",
        },
    },
}


class PortJourney(BaseModel):
    """One recorded port journey and the world it is driven in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    world: str
    journey: Journey


class PortJourneys(BaseModel):
    """The port journey file: what records it, and every journey it holds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: dict[str, str]
    journeys: tuple[PortJourney, ...]


@dataclass(frozen=True, slots=True)
class JourneySpec:
    """What a port journey walks: its keys from its setup, in its world.

    Attributes:
        id: The journey id.
        world: One of :data:`PROTOTYPE`, :data:`HELD`, :data:`BULK` or :data:`QUIET`.
        title: What the journey walks.
        proves: The requirement row it proves and how.
        setup: The reset argument it starts from.
        keys: The keys pressed after the reset.
    """

    id: str
    world: str
    title: str
    proves: str
    setup: Mapping[str, Any]
    keys: tuple[str, ...]


class DocumentDaemon:
    """A daemon that answers every route read from one epoch-2 document.

    A lifecycle move is answered the way the daemon's envelope answers a committed one, a
    Run control with the disposition it states, and a reconnect with what a test sets;
    the answer about a target in ``lost`` never arrives.

    Attributes:
        document: The document every route is projected from.
        lost: Targets whose write answers are lost on the way back.
        control_disposition: The disposition a Run control is answered with.
        reconnect_answer: What a reconnect is answered with; ``None`` serves none.
        run_events: Each Run's event lines, by Run URN, which a run-events read answers
            with; a Run not listed is unreadable.
        writes: Every write method and its parameters, in order.
    """

    def __init__(self, document: Mapping[str, Any], *, lost: frozenset[str] = frozenset()) -> None:
        self.document = dict(document)
        self.lost = set(lost)
        self.control_disposition = "requesting"
        self.reconnect_answer: dict[str, Any] | None = None
        self.run_events: dict[str, tuple[RunEventRecord, ...]] = {}
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self._routes = {
            READ_METHOD_TEMPLATE.format(route=route): route for route in ROUTE_COLLECTIONS
        }
        self._reconnects = {
            RECONNECT_METHOD_TEMPLATE.format(route=route) for route in ROUTE_COLLECTIONS
        }

    def _run_events(self, urn: str) -> dict[str, Any]:
        """Answer a run-events read as the daemon does, or fail for an unreadable Run."""
        if urn not in self.run_events:
            raise ConnectionError(f"the events of {urn} cannot be read")
        events = sorted(self.run_events[urn], key=lambda line: line.run_sequence)
        last = events[-1].run_sequence if events else 0
        return {
            "events": [line.model_dump(mode="json") for line in events],
            "quarantined": [],
            "gaps": [],
            "last_contiguous_sequence": last,
            "next_sequence": last + 1,
            "derivation_stopped": False,
            "run_status": "RUNNING",
            "stall": {
                "verdict": "live",
                "elapsed_seconds": 0.0,
                "interval_seconds": 300,
                "resume_method": "runtime.run.control.request",
                "resume_control": "resume",
            },
            "timeline": reduce_timeline(events).model_dump(mode="json"),
        }

    def _live_read(self, method: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
        """Answer a console live read the document settles, or ``None`` for any other call."""
        if method == RUN_STALLS_READ_METHOD:
            return {"stalls": [], "read_at": bodies.AT.isoformat()}
        if method == BOOT_RECOVERY_READ_METHOD:
            # no daemon start of this document recorded a recovery
            return {"last": None}
        if method in (QUESTION_READ_METHOD, PAUSE_READ_METHOD):
            # the document holds no question or pause these journeys walk
            return {}
        if method == RUN_USAGE_READ_METHOD:
            return self._run_usage(RunUsageReadParams.model_validate(params).urn.entity_key)
        if method == SPEND_CEILING_READ_METHOD:
            return self._ceiling()
        if method == HISTORY_CHANGES_READ_METHOD:
            # the document was written whole, never through a commit, so no change is on file
            return ChangePage().model_dump(mode="json")
        if method == DISPATCH_QUEUE_READ_METHOD:
            # no Run of the document was ever dispatched, so none carries a sealed budget
            return DispatchQueueView(
                runs=(),
                plan=DispatchPlan(slots=None, in_use=0),
                control=DispatchControl(),
                read_at=bodies.AT,
            ).model_dump(mode="json")
        return None

    def _run_usage(self, run_key: str) -> dict[str, Any]:
        """Answer a Run's usage read as the daemon does for a Run no meter has read yet."""
        if run_key not in self.document.get("run", {}):
            raise ConnectionError(f"the tree holds no {run_key}")
        return RunUsageView(
            run_key=run_key,
            tokens=None,
            cost_microusd=None,
            quality=None,
            cap_tokens=None,
            cap_cost_microusd=None,
            wall_seconds=None,
            typical_seconds=None,
            typical_runs=0,
        ).model_dump(mode="json")

    def _ceiling(self) -> dict[str, Any]:
        """Answer the ceiling read as the daemon does for an unbounded, unmetered tree."""
        return CostCeilingView(
            ceiling_tokens=None,
            ceiling_cost_microusd=None,
            live_runs=0,
            spent_tokens=0,
            held_tokens=0,
            spent_cost_microusd=0,
            spent_cost_pricing="priced",
            held_cost_microusd=0,
        ).model_dump(mode="json")

    def client(self) -> _Client:
        """Return one client over this daemon, as the binding's factory does."""
        return _Client(self)

    def answer(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Answer one call: a route read, a reconnect, or a write.

        Raises:
            ConnectionError: The write's target is lost, or the method is not served.
        """
        route = self._routes.get(method)
        if route is not None:
            return build_route_projection(
                route=route,
                document=self.document,
                cursor=bodies.CURSOR,
                scope_id=bodies.SCOPE,
                generated_at=bodies.AT,
            ).model_dump(mode="json")
        if method in self._reconnects and self.reconnect_answer is not None:
            return self.reconnect_answer
        if method == RUN_EVENTS_READ_METHOD:
            return self._run_events(str(params["urn"]))
        read = self._live_read(method, params)
        if read is not None:
            return read
        if method.startswith("projection."):
            raise ConnectionError(f"{method} is not served")
        self.writes.append((method, dict(params)))
        target = next((key for key in sorted(self.lost) if key in json.dumps(params)), None)
        if target is not None:
            raise ConnectionError(f"the answer about {target} was lost")
        if method == CONTROL_METHOD:
            return {"disposition": self.control_disposition}
        if method == DISPATCH_CONTROL_METHOD:
            return {"disposition": "confirmed"}
        revision = int(params.get("expected_revision") or 1)
        return {"status": "ok", "revision_before": revision, "revision_after": revision + 1}


class _Client:
    """A context-managed client over a :class:`DocumentDaemon`."""

    def __init__(self, daemon: DocumentDaemon) -> None:
        self._daemon = daemon

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._daemon.answer(method, params or {})


def held_app(
    daemon: DocumentDaemon, *, principal: str | None = bodies.ME, route: str = "scope.home"
) -> ConsoleApp:
    """Return a console whose seam reads ``daemon`` and acts as ``principal``."""
    seam = ProjectionSeam(
        route=route,
        scope_id=bodies.SCOPE,
        state_path=None,
        clock=lambda: bodies.AT,
        daemon_client_factory=daemon.client,
        operator=Operator(principal=principal) if principal is not None else None,
    )
    return ConsoleApp(chrome=load_chrome(), seam=seam, clock=FakeClock())


def prototype_app() -> ConsoleApp:
    """Return a console over the prototype registers the pack's contract replays."""
    return ConsoleApp(load_fixture(LAYOUT.fixture), FakeClock())


def pack_normaliser() -> Normaliser:
    """Return the pack-to-port map over the tracked contract's frames."""
    contract = load_contract(LAYOUT.sequences)
    return Normaliser(load_map(LAYOUT.normalisation_map), contract.frames_by_id())


def port_normaliser() -> Normaliser:
    """Return a map that rewrites nothing: a port journey is compared byte for byte."""
    return Normaliser(load_map(LAYOUT.normalisation_map), {}, rewrites=())


@contextlib.asynccontextmanager
async def driven(
    app: ConsoleApp, normaliser: Normaliser | None = None, *, size: int = 0
) -> AsyncIterator[Harness]:
    """Mount ``app`` under the pilot and yield the golden harness driving it."""
    async with app.run_test(size=SIZES[size]) as pilot:
        harness = Harness(app, pilot, normaliser or port_normaliser())
        await app.workers.wait_for_complete()
        await pilot.pause()
        yield harness


async def walk(
    harness: Harness, setup: Mapping[str, Any], keys: Sequence[str], contract_id: str = "walk"
) -> list[str]:
    """Reset to ``setup``, press ``keys`` one by one, and return every settled frame.

    The first frame is the reset's; one more follows each key.
    """
    app = harness.app
    app.reset(SessionSetup.model_validate(dict(setup)))
    await harness.follow_size()
    app.render_frame()
    frames = [(await settle(harness.pilot))[0]]
    for key in keys:
        await harness.press(key, contract_id)
        frames.append((await settle(harness.pilot))[0])
    return frames


@dataclass(slots=True)
class Replayed:
    """What replaying the port journeys found: one result each, and the keys sent."""

    results: dict[str, Result] = field(default_factory=dict)
    sent: dict[str, list[Any]] = field(default_factory=dict)


def load_port_journeys() -> PortJourneys:
    """Return the recorded port journeys, validated strict.

    Raises:
        FileNotFoundError: the journey file has not been recorded.
        pydantic.ValidationError: a record carries an unknown key or a malformed field.
    """
    return PortJourneys.model_validate_json(PORT_JOURNEYS_PATH.read_text(encoding="utf-8"))


_WORLDS: dict[str, Callable[[], ConsoleApp]] = {
    PROTOTYPE: prototype_app,
    HELD: lambda: held_app(DocumentDaemon(bodies.DOCUMENT)),
    BULK: lambda: held_app(DocumentDaemon(bulk.DOCUMENT, lost=frozenset({bulk.MS_SECOND}))),
    QUIET: lambda: held_app(DocumentDaemon(QUIET_DOCUMENT)),
    TREE: lambda: held_app(DocumentDaemon(TREE_DOCUMENT)),
}


def _spec(
    id: str, world: str, title: str, proves: str, setup: Mapping[str, Any], *keys: str
) -> JourneySpec:
    return JourneySpec(id, world, title, proves, dict(setup), tuple(keys))


_ATTENTION = {"route": "attention"}

#: Every port journey, in file order: what it walks and the row it proves.
PORT_SPECS: Final[tuple[JourneySpec, ...]] = (
    _spec(
        "PJ01",
        HELD,
        "a linked card previews before it commits",
        "PRX-058 over J5's walk: the six panes stand before Enter and Esc writes nothing",
        _ATTENTION,
        "a",
        "Escape",
    ),
    _spec(
        "PJ02",
        HELD,
        "a heavy verb on a linked Run still previews",
        "PRX-058 over J12's walk on a held Run",
        {"route": "run.detail", "subjId": "RUN-00000002"},
        ".",
        "n",
        "Escape",
    ),
    _spec(
        "PJ03",
        BULK,
        "bulk control previews every target before it sends",
        (
            "PRX-058 over CON-148: Space marks two, and the bulk verb previews the count, "
            "each id and the UNKNOWN pane"
        ),
        {"route": "scope.home"},
        " ",
        "ArrowDown",
        " ",
        ".",
        bulk.ACTIVATE,
    ),
    _spec(
        "PJ04",
        HELD,
        "position survives drill and back on a held register",
        "PRX-055: Esc restores the Run the drill left by its id",
        {"route": "activity"},
        "ArrowDown",
        "Enter",
        "Escape",
    ),
    _spec(
        "PJ05",
        HELD,
        "a bucket filter moves no rail count",
        "PRX-060: Tab picks a bucket, the rail keeps every count, Esc clears it",
        {"route": "attention", "size": 1},
        "Tab",
        "Tab",
        "Escape",
    ),
    _spec(
        "PJ06",
        HELD,
        "! opens this principal's top action",
        "CON-149's ! walk over a held register: its detail opens, the departure is kept",
        {"route": "activity"},
        "!",
    ),
    _spec(
        "PJ07",
        QUIET,
        "the empty Attention frame",
        "PRX-059 at zero: no badge, and the frame names its revision and next move",
        _ATTENTION,
    ),
    _spec(
        "PJ08",
        QUIET,
        "the empty Backlog frame",
        "the empty backlog names its revision and next move",
        {"route": "backlog"},
    ),
    _spec(
        "PJ09",
        TREE,
        "u climbs from a Run reached through Activity",
        "CON-149's u walk on a held tree: u lands on the Task, Esc still goes back",
        {"route": "activity"},
        "Enter",
        "u",
        "Escape",
    ),
    _spec(
        "PJ10",
        TREE,
        "brackets walk a Task's Batch",
        "CON-149's [ ] walk on a held tree: the sibling Task, then back, the crumb depth kept",
        {"route": "task.detail", "subjId": "TSK-0001"},
        "]",
        "[",
    ),
    _spec(
        "PJ11",
        PROTOTYPE,
        "! from a Run opens the top open action",
        "CON-149's ! walk over the prototype register: its detail opens, the Run is kept",
        {"route": "run.detail", "subjId": "RUN-538453eb"},
        "!",
    ),
    _spec(
        "PJ12",
        PROTOTYPE,
        "question detail",
        (
            "PRX-062: open, Esc leaves it open and unanswered, decline opens its consequence "
            "card, a digit answers"
        ),
        _ATTENTION,
        "ArrowDown",
        "ArrowDown",
        "Enter",
        "Escape",
        "Enter",
        "x",
        "Escape",
        "Enter",
        "1",
    ),
    _spec(
        "PJ13",
        PROTOTYPE,
        "pause detail",
        (
            "PRX-062: open from the lost Run, Esc leaves the pause unchanged, reconcile opens its "
            "consequence card"
        ),
        _ATTENTION,
        "Enter",
        "Escape",
        "Enter",
        "n",
        "Escape",
    ),
    _spec(
        "PJ14",
        PROTOTYPE,
        "readiness matrix",
        "PRX-062: m opens the matrix, the cursor walks its signals, Esc closes it",
        {"route": "release"},
        "m",
        "ArrowDown",
        "Escape",
    ),
    _spec(
        "PJ15",
        PROTOTYPE,
        "draft card",
        (
            "PRX-062: Enter opens the draft, promote is refused naming what is unanswered, the "
            "cursor walks its fields"
        ),
        {"route": "backlog"},
        "Enter",
        "p",
        "ArrowDown",
        "Enter",
        "Escape",
    ),
    _spec(
        "PJ16",
        PROTOTYPE,
        "marker card",
        "PRX-062: Enter opens the marker's record, nothing inside moves it, Esc closes it",
        {"route": "timeline"},
        "Enter",
        "i",
        "Escape",
    ),
    _spec(
        "PJ17",
        PROTOTYPE,
        "resolution card",
        "PRX-062: a purged target's card states its ending, Esc returns to its row",
        {"route": "history"},
        "Enter",
        "Escape",
    ),
    _spec(
        "PJ18",
        PROTOTYPE,
        "artifact card",
        "PRX-062: Esc returns to the opening row",
        {"route": "campaign"},
        "Tab",
        "Tab",
        "Enter",
        "Escape",
    ),
    _spec(
        "PJ19",
        PROTOTYPE,
        "rung card",
        "PRX-062: Esc returns to the opening row",
        {"route": "evidence"},
        "ArrowDown",
        "Enter",
        "Escape",
    ),
    _spec(
        "PJ20",
        PROTOTYPE,
        "step card",
        "PRX-062: Esc returns to the opening row",
        {"route": "campaign"},
        "ArrowDown",
        "Enter",
        "Escape",
    ),
)


def spec(journey_id: str) -> JourneySpec:
    """Return the port journey spec ``journey_id``.

    Raises:
        StopIteration: no spec carries that id.
    """
    return next(s for s in PORT_SPECS if s.id == journey_id)


async def _record(specs: Sequence[JourneySpec]) -> list[PortJourney]:
    recorded: list[PortJourney] = []
    for world, build in _WORLDS.items():
        async with driven(build()) as harness:
            for spec in (s for s in specs if s.world == world):
                journey = await harness.record(
                    spec.id,
                    title=spec.title,
                    proves=spec.proves,
                    setup=SessionSetup.model_validate(dict(spec.setup)),
                    keys=spec.keys,
                )
                recorded.append(PortJourney(world=world, journey=journey))
    order = [spec.id for spec in specs]
    return sorted(recorded, key=lambda item: order.index(item.journey.id))


def record_port_journeys(specs: Sequence[JourneySpec]) -> None:
    """Record every spec from the render path and write the journey file."""
    journeys = asyncio.run(_record(specs))
    payload = {
        "meta": {
            "source": "recorded from the console's render path by Harness.record",
            "rerecord": f"run the console suites with {RECORD_ENV}=1 and review the diff",
        },
        "journeys": [
            {
                "world": item.world,
                "journey": item.journey.model_dump(mode="json", by_alias=True, exclude_none=True),
            }
            for item in journeys
        ],
    }
    PORT_JOURNEYS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


async def _replay(journeys: Sequence[PortJourney]) -> Replayed:
    replayed = Replayed()
    for world, build in _WORLDS.items():
        async with driven(build()) as harness:
            for item in (j for j in journeys if j.world == world):
                start = len(harness.sent)
                replayed.results[item.journey.id] = await harness.journey(item.journey)
                replayed.sent[item.journey.id] = harness.sent[start:]
    return replayed


@functools.cache
def replayed() -> Replayed:
    """Replay every recorded port journey once per process, one console per world.

    Under :data:`RECORD_ENV` the file is re-recorded from the render path first.
    """
    if os.environ.get(RECORD_ENV) == "1":
        record_port_journeys(PORT_SPECS)
    return asyncio.run(_replay(load_port_journeys().journeys))
