"""The console's sequence properties, asserted over journeys the driven harness replays.

A static golden proves what one screen says; these prove what the console does next. Every
journey here is keys fed through the golden harness into the real :class:`ConsoleApp` under
the toolkit's pilot -- the pack's own journeys over the prototype registers it recorded
them against, and the port's journeys (``journeys-port.json``, recorded from the render
path) over a seam that reads an epoch-2 document as one principal. A daemon-side event a
journey needs between two keys -- a keyed patch, a re-read, a reconnect -- goes through the
seam's own entry points, never into the session.

Requirement rows proved here, by test-name prefix: PRX-053, PRX-054, PRX-055, PRX-056,
PRX-057, PRX-058, PRX-059, PRX-060, PRX-061, PRX-065 and PRX-066. PRX-062's overlay and card
journeys are asserted in ``test_overlay_state_models`` and ``test_enter_opened_cards``, and
their replay is asserted here with every other port journey.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from eawf.kernel.projection.attention import attention_mine, build_attention_view
from eawf.kernel.projection.compute import (
    ROUTE_READ_MODELS,
    RUN_STATE_KIND,
    KeyedPatch,
    PatchEntry,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.connection import (
    READ_METHOD_TEMPLATE,
    ConnectionValue,
    ReconnectDisposition,
)
from eawf.kernel.projection.registers import ATTENTION_ROUTE, build_register_view
from eawf.kernel.projection.transcript import TRANSCRIPT_ROUTE
from eawf.kernel.runtime.control import ControlDisposition
from eawf.kernel.runtime.events import ChildRunPayload, MessageSummaryPayload, RunEventKind
from eawf.runtime.daemon.methods.console_records import REPOSITORY_READ_METHOD
from eawf.runtime.vcs.repository_read import RepositoryAnswer
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.cards import Card
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import TOAST_DWELL, FakeClock
from eawf.surfaces.tui.console.harness import Harness, Journey, load_contract, settle
from eawf.surfaces.tui.console.operations import OperationResult, OperationStatus, Operator
from eawf.surfaces.tui.console.registry import ROUTES
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES
from tests.tui.surfaces.tui.console import journey_support as js
from tests.tui.surfaces.tui.console import test_bulk_per_item_results as bulk
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies
from tests.tui.surfaces.tui.console import test_transcript_blocks as tb

CONTRACT = load_contract(js.LAYOUT.sequences)
NEEDS_YOU = "NEEDS YOU"
_BADGE = re.compile(r"!(\d+) NEEDS YOU")
_NAMES = {
    "Esc": "Escape",
    "PageUp": "PageUp",
    "PageDown": "PageDown",
    "Home": "Home",
    "End": "End",
    "Enter": "Enter",
    "Tab": "Tab",
}
_ARROWS = {"↑": "ArrowUp", "↓": "ArrowDown", "←": "ArrowLeft", "→": "ArrowRight"}
#: The subject each entity route is opened on, per world.
_HELD_SUBJECTS: dict[str, str] = {
    "run.detail": "RUN-00000002",
    "transcript": "RUN-00000002",
    "task.detail": "TSK-0001",
    "track": "TRK-CORE",
    "milestone": "MLS-0100",
    "batch.detail": "BAT-0100",
}
_SUBJECTS: dict[str, dict[str, str]] = {
    js.QUIET: _HELD_SUBJECTS,
    js.HELD: {
        "run.detail": "RUN-00000002",
        "transcript": "RUN-00000002",
        "task.detail": "TSK-0001",
        "track": "TRK-CORE",
        "milestone": "MLS-0100",
        "batch.detail": "BAT-0100",
    },
    js.PROTOTYPE: {
        "run.detail": "RUN-538453eb",
        "transcript": "RUN-538453eb",
        "task.detail": "EAWF-0042",
        "track": "TRK-RUNTIME",
        "milestone": "MLS-0001",
        "batch.detail": "BAT-0002",
    },
}


def pack_journey(journey_id: str) -> Journey:
    """Return the pack's recorded journey ``journey_id``."""
    return next(j for j in CONTRACT.journeys if j.id == journey_id)


def keys_of(journey: Journey) -> list[str]:
    """Return the keys a journey presses after its reset."""
    return [step.key for step in journey.steps[1:] if step.key is not None]


def port(journey_id: str) -> Journey:
    """Return the recorded port journey ``journey_id``, after requiring its replay matched."""
    result = js.replayed().results[journey_id]
    assert result.ok, f"{journey_id}: {result.detail}"
    return next(j.journey for j in js.load_port_journeys().journeys if j.journey.id == journey_id)


def frames(journey: Journey) -> list[str]:
    """Return every step's recorded frame, the reset's first."""
    return [step.frame for step in journey.steps]


def badge(frame: str) -> list[int]:
    """Return every ``!N NEEDS YOU`` count the frame prints."""
    return [int(n) for n in _BADGE.findall(frame)]


def advertised(keybar: str) -> list[str]:
    """Return the dispatcher name of every key a keybar row advertises."""
    keys: list[str] = []
    for pair in re.split(r" {3,}", keybar.strip()):
        for word in pair.split(" "):
            if word in _NAMES:
                keys.append(_NAMES[word])
            elif word and all(ch in _ARROWS for ch in word):
                keys.extend(_ARROWS[ch] for ch in word)
            elif len(word) == 1 and word not in "…—":
                keys.append(word)
            else:
                break
    return keys


async def _pack_walk(journey_id: str, *, verbose: bool = False) -> tuple[list[str], Harness]:
    """Drive a pack journey's keys through the prototype console and return its frames."""
    journey = pack_journey(journey_id)
    async with js.driven(js.prototype_app(), js.pack_normaliser()) as harness:
        harness.app.verbose = verbose
        shots = await js.walk(
            harness,
            journey.setup.model_dump(by_alias=True, exclude_defaults=True),
            keys_of(journey),
            journey_id,
        )
        return shots, harness


async def _replay_pack(journey_ids: Sequence[str]) -> dict[str, Any]:
    async with js.driven(js.prototype_app(), js.pack_normaliser()) as harness:
        return {jid: await harness.journey(pack_journey(jid)) for jid in journey_ids}


# ---------- every recorded port journey replays from the render path ----------


@pytest.mark.parametrize("journey_id", [s.id for s in js.PORT_SPECS])
def test_every_port_journey_replays_its_recorded_projection_and_frame(journey_id: str) -> None:
    result = js.replayed().results[journey_id]
    assert result.ok, (
        f"{journey_id}: {result.detail}\n exp |{result.expected}|\n got |{result.actual}|"
    )


def test_the_port_journey_file_records_exactly_the_specs_in_order() -> None:
    recorded = js.load_port_journeys().journeys
    assert [(j.world, j.journey.id) for j in recorded] == [(s.world, s.id) for s in js.PORT_SPECS]
    for item, spec in zip(recorded, js.PORT_SPECS, strict=True):
        assert keys_of(item.journey) == list(spec.keys), spec.id
        assert item.journey.proves == spec.proves


def test_the_harness_log_records_every_key_it_sends_with_the_consoles_response() -> None:
    replay = js.replayed()
    for spec in js.PORT_SPECS:
        sent = replay.sent[spec.id]
        assert [entry.key for entry in sent] == list(spec.keys), spec.id
        assert all(entry.response for entry in sent), spec.id


# ---------- PRX-055: position survives drill and back ----------


@pytest.mark.parametrize("journey_id", ["J1", "J19", "J23"])
def test_prx_055_a_pack_journey_returns_to_the_row_its_drill_left(journey_id: str) -> None:
    """J1, J19 and J23: the frame after the last Esc is the frame the first drill left."""
    shots, harness = asyncio.run(_pack_walk(journey_id))
    keys = keys_of(pack_journey(journey_id))
    drill = keys.index("Enter")
    assert shots[drill] == shots[-1], f"{journey_id}: the return is not the row the drill left"
    assert asyncio.run(_replay_pack([journey_id]))[journey_id].ok
    assert harness.sent[-1].response.endswith("selection restored")


def test_prx_055_the_selected_id_survives_four_events_between_drill_and_return() -> None:
    """Patch, sort, filter and replay repair land between the drill and Esc; the id holds.

    The filter is the route's own and a route the operator is not on cannot be typed into,
    so it is typed before the drill: the back step must restore it with the selection.
    """
    daemon = js.DocumentDaemon(bodies.DOCUMENT)

    async def body() -> tuple[str, str | None, list[str]]:
        async with js.driven(js.held_app(daemon), size=1) as harness:
            app, seam = harness.app, harness.app.seam
            assert seam is not None
            await js.walk(harness, {"route": "activity", "size": 1}, ["ArrowDown"])
            chosen = app.session.sel_id
            assert chosen == "RUN-00000002"
            for key in ("\\", "B", "o", "u", "n", "d", "Enter", "Enter"):
                await harness.press(key, "prx-055")
            await settle(harness.pilot)
            assert app.session.route == "run.detail"
            # a keyed patch adds a Run that sorts above the chosen one
            await seam.apply_patch(_patch("activity", "RUN-00000000", "run", "RUNNING"))
            # a sort change: the daemon re-serves the held rows in the other order
            held = seam.projection_for("activity")
            assert held is not None
            seam._hold("activity", held.model_copy(update={"rows": tuple(reversed(held.rows))}))
            # a replay repair: the route is re-read whole at the daemon's cursor
            daemon.document = {**bodies.DOCUMENT, "run": {**bodies.DOCUMENT["run"]}}
            await seam.load("activity")
            await harness.press("Escape", "prx-055")
            text, _cycles = await settle(harness.pilot)
            caret = [row for row in text.split("\n") if row.startswith(" ▸ RUN-")]
            return chosen, app.session.sel_id, caret

    chosen, returned, caret = asyncio.run(body())
    assert returned == chosen
    assert len(caret) == 1 and caret[0].startswith(f" ▸ {chosen}")


def test_prx_055_the_port_journey_restores_the_held_run_by_id() -> None:
    journey = port("PJ04")
    before, after = journey.steps[1], journey.steps[-1]
    assert (after.after["route"], after.after["back_depth"]) == ("activity", 0)
    assert before.frame == after.frame


def _patch(
    route: str, key: str, collection: str, status: str, *, sequence: int = 41209
) -> KeyedPatch:
    """Return a keyed patch replacing one row of ``route`` at ``sequence``."""
    return KeyedPatch.model_validate(
        {
            "schema_version": "1.0",
            "projection_kind": ROUTE_READ_MODELS[route],
            "routes": [route],
            "scope_id": bodies.SCOPE,
            "canonical_sequence": sequence,
            "entries": [
                PatchEntry.model_validate(
                    {
                        "key": key,
                        "urn": f"{bodies.ROOT}/{collection.replace('_', '-')}/{key}",
                        "collection": collection,
                        "revision": 1,
                        "status": status,
                    }
                ).model_dump(mode="json")
            ],
        }
    )


# ---------- PRX-056: the go prefix is visible while armed and cancellable ----------


def test_prx_056_j2_the_armed_prefix_is_drawn_and_escape_cancels_it_in_place() -> None:
    shots, harness = asyncio.run(_pack_walk("J2"))
    armed = shots[1].split("\n")
    assert any(row.startswith(" GO ") for row in armed), "the go drawer is not drawn"
    assert armed[-1].strip() == "g … destination   Esc cancel"
    assert shots[2] == shots[0], "Esc left the frame it cancelled from"
    assert [entry.response for entry in harness.sent] == [
        "g → prefix armed",
        "Esc → prefix cancelled",
    ]
    assert harness.app.session.route == "scope.home"
    assert harness.app.session.prefix is None


def test_prx_056_j3_every_destination_lands_with_an_empty_back_stack() -> None:
    journey = pack_journey("J3")
    landed: list[str] = []

    async def body() -> None:
        async with js.driven(js.prototype_app(), js.pack_normaliser()) as harness:
            await js.walk(harness, {}, [], "J3")
            for key in keys_of(journey):
                await harness.press(key, "J3")
                await settle(harness.pilot)
                s = harness.app.session
                if key == "g":
                    assert s.prefix == "g"
                    assert harness.app.frame_rows[-1].strip() == "g … destination   Esc cancel"
                else:
                    assert s.prefix is None
                    assert len(s.back) == 0, f"g {key} left a back stack"
                    landed.append(s.route)

    asyncio.run(body())
    assert len(landed) == 12
    assert len(set(landed)) == 12
    assert asyncio.run(_replay_pack(["J2", "J3"]))["J3"].ok


# ---------- PRX-057: every key acts or refuses; an unclaimed key is silent ----------


def test_prx_057_j4_a_mutation_refuses_under_offline_snapshot_naming_why() -> None:
    shots, harness = asyncio.run(_pack_walk("J4"))
    answer, deny, menu = harness.sent[0], harness.sent[1], harness.sent[2]
    for entry in (answer, deny):
        assert entry.logged
        assert "unavailable — OFFLINE SNAPSHOT · the daemon cannot be reached" in entry.response
    # the verb stays in the menu, with the live reason beside it
    menu_rows = shots[3].split("\n")
    assert menu.logged
    answer_row = next(row for row in menu_rows if re.match(r"^\s+a\s+answer\s", row))
    # the reason names the state that refused it; at 80 columns its tail is clipped
    assert "OFFLINE SNAPSHOT · the daemon cannot be" in answer_row
    assert "OFFLINE SNAPSHOT" in menu_rows[0]


def test_prx_057_j13_an_unclaimed_key_leaves_frame_projection_and_log_untouched() -> None:
    shots, harness = asyncio.run(_pack_walk("J13"))
    assert all(shot == shots[0] for shot in shots)
    assert all(not entry.logged and not entry.moved for entry in harness.sent)
    assert [entry.response for entry in harness.sent] == ["q → unclaimed"] * 3
    assert harness.app.session.log == []
    assert harness.app.session.toasts == []


def test_prx_057_j13_under_verbose_the_diagnostics_row_names_the_unclaimed_key() -> None:
    shots, harness = asyncio.run(_pack_walk("J13", verbose=True))
    for shot in shots[1:]:
        verbose = [row for row in shot.split("\n") if row.startswith(" VERBOSE")]
        assert verbose == [verbose[0]]
        assert "q → unclaimed · no handler on this route" in verbose[0]
    assert all(entry.logged for entry in harness.sent)


def test_prx_057_the_verbose_goldens_replay_with_their_diagnostics_row() -> None:
    states = [s for s in CONTRACT.states if s.id.startswith("verbose/activity@")]
    assert len(states) == 3

    async def body() -> list[Any]:
        async with js.driven(js.prototype_app(), js.pack_normaliser()) as harness:
            return [await harness.frame(state) for state in states]

    for state, result in zip(states, asyncio.run(body()), strict=True):
        assert result.ok, f"{state.id}: {result.detail}"
        assert any(row.startswith(" VERBOSE ") for row in state.frame.split("\n"))


def _sweep(world: str) -> dict[str, list[tuple[str, str]]]:
    """Press every key each route's bar advertises, from a fresh reset, in ``world``."""
    builders: dict[str, Callable[[], ConsoleApp]] = {
        js.PROTOTYPE: js.prototype_app,
        js.HELD: lambda: js.held_app(js.DocumentDaemon(bodies.DOCUMENT)),
        js.QUIET: lambda: js.held_app(js.DocumentDaemon(js.QUIET_DOCUMENT)),
    }
    build = builders[world]

    async def body() -> dict[str, list[tuple[str, str]]]:
        answers: dict[str, list[tuple[str, str]]] = {}
        async with js.driven(build()) as harness:
            for spec in ROUTES:
                setup: dict[str, Any] = {"route": spec.id, "size": 2}
                if spec.id in _SUBJECTS[world]:
                    setup["subjId"] = _SUBJECTS[world][spec.id]
                shot = (await js.walk(harness, setup, []))[0]
                pressed: list[tuple[str, str]] = []
                for key in advertised(shot.split("\n")[-1]):
                    if key == "Escape":
                        continue
                    await js.walk(harness, setup, [key])
                    pressed.append((key, harness.sent[-1].response))
                answers[spec.id] = pressed
        return answers

    return asyncio.run(body())


@pytest.mark.parametrize("world", [js.PROTOTYPE, js.HELD, js.QUIET])
def test_prx_057_every_advertised_key_acts_or_refuses_on_every_route(world: str) -> None:
    answers = _sweep(world)
    silent = [
        f"{route}: {key} ({response})"
        for route, pressed in answers.items()
        for key, response in pressed
        if "unclaimed" in response
    ]
    assert not silent, "advertised but silent:\n" + "\n".join(silent)
    # a frame advertises only the keys that act on what it holds, so the sparse worlds
    # offer fewer; the floor still proves the sweep pressed keys on every route
    assert sum(len(pressed) for pressed in answers.values()) > 60


# ---------- PRX-058: no canonical mutation before its consequence preview ----------

_PANES = ("ACTION", "TARGET", "EFFECTS", "NOT", "IF STALE", "AUTHORITY")


def _panes(frame: str) -> list[str]:
    return [label for label in _PANES if re.search(rf"^ {label}\s", frame, re.MULTILINE)]


@pytest.mark.parametrize("journey_id", ["PJ01", "PJ02"])
def test_prx_058_a_linked_card_names_its_six_panes_before_anything_is_sent(journey_id: str) -> None:
    journey = port(journey_id)
    preview = next(step for step in journey.steps if step.after["overlay"] == "consequence")
    assert _panes(preview.frame) == list(_PANES)
    assert re.search(r"revision \d+ · \w+ · exact", preview.frame)
    assert "Esc cancel — nothing happens" in preview.frame.split("\n")[-1]
    assert js.replayed().sent[journey_id][-1].response == "Esc → cancelled — nothing happened"


def test_prx_058_a_control_is_sent_only_when_its_card_is_confirmed() -> None:
    daemon = js.DocumentDaemon(bodies.DOCUMENT)

    async def body() -> tuple[int, int]:
        async with js.driven(js.held_app(daemon)) as harness:
            await js.walk(harness, {"route": "run.detail", "subjId": "RUN-00000002"}, [".", "n"])
            previewed = len(daemon.writes)
            await harness.press("Enter", "prx-058")
            await harness.app.workers.wait_for_complete()
            return previewed, len(daemon.writes)

    previewed, confirmed = asyncio.run(body())
    assert (previewed, confirmed) == (0, 1)


def test_prx_058_bulk_previews_the_count_every_id_and_the_unknown_pane() -> None:
    journey = port("PJ03")
    card = frames(journey)[-1]
    assert journey.steps[-1].after["overlay"] == "consequence"
    assert "2 milestones selected" in card
    assert bulk.MS_FIRST in card and bulk.MS_SECOND in card
    assert re.search(r"^ UNKNOWN\s", card, re.MULTILINE)
    assert _panes(card) == list(_PANES)
    assert "Enter confirm all 2" in card.split("\n")[-1]


def test_prx_058_bulk_writes_one_row_per_target_and_the_unknown_stays_until_reconcile() -> None:
    """The answers arrive in either order, so the rows are read off the card, not the rack."""
    daemon = js.DocumentDaemon(bulk.DOCUMENT, lost=frozenset({bulk.MS_SECOND}))
    # the home cursor starts on the first Milestone leaf, never on the Track above it
    marks = [" ", "ArrowDown", " ", ".", bulk.ACTIVATE]

    async def body() -> tuple[list[Any], list[Any], list[Any], list[Any], list[Any], str]:
        async with js.driven(js.held_app(daemon)) as harness:
            await js.walk(harness, {"route": "scope.home"}, marks)
            before = list(daemon.writes)
            await harness.press("Enter", "prx-058")
            await harness.app.workers.wait_for_complete()
            sent = list(daemon.writes)
            answered = _rows(harness.app)
            harness.app.render_frame()
            results, _cycles = await settle(harness.pilot)
            await harness.press("ArrowDown", "prx-058")
            await settle(harness.pilot)
            kept = _rows(harness.app)
            await harness.press("n", "prx-058")
            await harness.app.workers.wait_for_complete()
            return before, sent, answered, kept, daemon.writes[len(sent) :], results

    before, sent, answered, kept, reconciled, results = asyncio.run(body())
    assert before == []
    assert sorted(params["urn"].rsplit("/", 1)[1] for _method, params in sent) == [
        bulk.MS_FIRST,
        bulk.MS_SECOND,
    ]
    assert (
        answered
        == kept
        == [
            (bulk.MS_FIRST, ControlDisposition.CONFIRMED),
            (bulk.MS_SECOND, ControlDisposition.UNKNOWN),
        ]
    )
    assert [params["urn"].rsplit("/", 1)[1] for _method, params in reconciled] == [bulk.MS_SECOND]
    # the results card carries the control-ledger line of the write the cursor is on
    assert re.search(r"^ LEDGER\s+CONFIRMED\s+activate · MLS-0201", results, re.MULTILINE)


def _rows(app: ConsoleApp) -> list[tuple[str, ControlDisposition]]:
    card = app.session.mutation
    assert isinstance(card, Card)
    return [(row.key, row.disposition) for row in card.results]


@pytest.mark.parametrize("journey_id", ["J5", "J12"])
def test_prx_058_the_pack_journeys_preview_first_and_escape_writes_nothing(journey_id: str) -> None:
    shots, harness = asyncio.run(_pack_walk(journey_id))
    card = next(shot for shot in shots if "Enter confirm" in shot.split("\n")[-1])
    for label in ("ASKING", "AT", "EFFECTS", "NOT", "IF STALE"):
        assert re.search(rf"^ {label}\s", card, re.MULTILINE), label
    assert harness.sent[-1].response.endswith("unchanged, nothing was written")
    assert asyncio.run(_replay_pack([journey_id]))[journey_id].ok


# ---------- PRX-059: the header count appears once and reconciles ----------

_GO = ["h", "a", "n", "b", "t", "r", "s", "y", "d", "i", "l", "u"]


async def _walk_every_route(harness: Harness, setup: dict[str, Any]) -> list[str]:
    """Walk every go destination, then the help, palette and action surfaces, capturing each."""
    shots = [(await js.walk(harness, setup, []))[0]]
    for key in _GO:
        shots.extend((await js.walk(harness, setup, ["g", key]))[1:])
    for keys in (["?"], ["/"], ["g", "a", "."], ["g", "a", "g"]):
        shots.extend((await js.walk(harness, setup, keys))[1:])
    return shots


def _mine(document: dict[str, Any], principal: str) -> int:
    register = build_register_view(
        build_route_projection(
            route=ATTENTION_ROUTE,
            document=document,
            cursor=bodies.CURSOR,
            scope_id=bodies.SCOPE,
            generated_at=bodies.AT,
        )
    )
    return int(attention_mine(register, principal=principal).value or 0)


@pytest.mark.parametrize("principal", [bodies.ME, bodies.OTHER])
def test_prx_059_the_badge_renders_once_with_this_principals_count_on_every_frame(
    principal: str,
) -> None:
    mine = _mine(bodies.DOCUMENT, principal)
    assert mine > 0

    async def body() -> list[str]:
        app = js.held_app(js.DocumentDaemon(bodies.DOCUMENT), principal=principal)
        async with js.driven(app) as harness:
            return await _walk_every_route(harness, {"size": 1})

    for shot in asyncio.run(body()):
        assert badge(shot) == [mine], shot.split("\n")[0]
        assert shot.count(NEEDS_YOU) == 1
        assert "!0" not in shot


def test_prx_059_the_all_principals_total_renders_only_on_attention_and_home() -> None:
    async def body() -> list[str]:
        async with js.driven(js.held_app(js.DocumentDaemon(bodies.DOCUMENT))) as harness:
            return await _walk_every_route(harness, {"size": 1})

    for shot in asyncio.run(body()):
        crumb = shot.split("\n")[0]
        if "all principals" in shot.lower():
            assert crumb.rstrip().endswith("● LIVE")
            assert "▸ Needs you" in crumb or re.match(r"^ Eä ▸ EAWF\s", crumb), crumb


def test_prx_059_at_zero_no_frame_prints_the_badge() -> None:
    assert _mine(js.QUIET_DOCUMENT, bodies.ME) == 0

    async def body() -> list[str]:
        async with js.driven(js.held_app(js.DocumentDaemon(js.QUIET_DOCUMENT))) as harness:
            return await _walk_every_route(harness, {"size": 1})

    for shot in asyncio.run(body()):
        assert NEEDS_YOU not in shot
        assert "!0" not in shot


def test_prx_059_a_notice_never_changes_the_count() -> None:
    async def body() -> tuple[str, str, int]:
        async with js.driven(js.held_app(js.DocumentDaemon(bodies.DOCUMENT))) as harness:
            before = (await js.walk(harness, {"route": "activity"}, []))[0]
            harness.app.raise_toast("a notice", title="notice")
            harness.app.render_frame()
            after, _cycles = await settle(harness.pilot)
            return before, after, len(harness.app.session.toasts)

    before, after, toasts = asyncio.run(body())
    assert toasts == 1
    assert badge(before) == badge(after) == [1]


@pytest.mark.parametrize(
    "conn", ["LIVE", "GAP DETECTED", "OFFLINE SNAPSHOT", "DISCONNECTED", "DEGRADED"]
)
def test_prx_059_every_connection_frame_prints_the_attention_routes_mine_count(conn: str) -> None:
    async def body() -> tuple[list[str], str]:
        async with js.driven(js.prototype_app()) as harness:
            shots = [
                (await js.walk(harness, {"route": route, "conn": conn}, []))[0]
                for route in ("scope.home", "activity", "run.detail", "settings")
            ]
            attention = (await js.walk(harness, {"route": "attention", "conn": conn}, []))[0]
            return shots, attention

    shots, attention = asyncio.run(body())
    mine = int(re.search(r"^ (\d+) mine ·", attention, re.MULTILINE).group(1))  # type: ignore[union-attr]
    for shot in [*shots, attention]:
        assert badge(shot) == [mine]
        assert shot.count(NEEDS_YOU) == 1


def test_prx_059_live_the_canary_badge_agrees_with_its_attention_register(tmp_path: Any) -> None:
    """Over a real daemon on a walked epoch-2 tree, every destination agrees with Attention."""
    from tests.tui.surfaces.tui.console.test_console_live_smoke import (
        live_console,
        walk_canary_isolated,
    )

    walk, runtime_root = walk_canary_isolated(tmp_path)

    async def body() -> tuple[list[str], int | None]:
        async with (
            live_console(walk.canary.root, runtime_root) as (app, seam),
            app.run_test(size=SIZES[1]) as pilot,
        ):
            harness = Harness(app, pilot, js.port_normaliser())
            shots = await _walk_every_route(harness, {"size": 1})
            held = seam.projection_for(ATTENTION_ROUTE)
            principal = app.principal()
            mine = (
                int(attention_mine(build_register_view(held), principal=principal).value or 0)
                if held is not None and principal is not None
                else None
            )
            return shots, mine

    shots, mine = asyncio.run(body())
    for shot in shots:
        assert "!0" not in shot
        assert badge(shot) == ([mine] if mine else [])


def test_prx_059_the_empty_attention_frame_prints_no_badge_and_names_its_next_move() -> None:
    (frame,) = frames(port("PJ07"))
    assert NEEDS_YOU not in frame.split("\n")[0]
    assert "!0" not in frame
    assert re.search(r"^ NOTHING YET\s+nothing is open at revision 41,208", frame, re.MULTILINE)
    assert re.search(r"^ WHAT TO DO\s+nothing\. Runs continue without you\.", frame, re.MULTILINE)


def test_the_empty_backlog_frame_names_its_revision_and_next_move() -> None:
    (frame,) = frames(port("PJ08"))
    assert re.search(
        r"^ NOTHING QUEUED\s+no draft and no deferred Task at revision 41,208", frame, re.MULTILINE
    )
    assert NEEDS_YOU not in frame


def test_con_149_u_climbs_to_the_task_and_escape_still_goes_back() -> None:
    """CON-149, on a held tree: ``u`` lands on the Run's own Task; Escape goes back."""
    journey = port("PJ09")
    assert js.spec("PJ09").world == js.TREE
    routes = [(s.after["route"], s.after["subjId"]) for s in journey.steps]
    run = routes[1][1]
    assert run in js.TREE_DOCUMENT["run"]
    task = js.TREE_DOCUMENT["run"][run]["scope"]["task_ref"].rsplit("/", 1)[-1]
    assert routes == [
        ("activity", None),
        ("run.detail", run),
        ("task.detail", task),
        ("run.detail", run),
    ]


def test_con_149_brackets_walk_the_batch_and_keep_the_crumb_depth() -> None:
    """CON-149, on a held tree: ``]`` and ``[`` walk the Tasks filed under one Batch."""
    journey = port("PJ10")
    assert js.spec("PJ10").world == js.TREE
    assert [s.after["subjId"] for s in journey.steps] == ["TSK-0001", "TSK-0002", "TSK-0001"]
    depths = {s.frame.split("\n")[0].count("▸") for s in journey.steps}
    assert len(depths) == 1
    assert {s.after["back_depth"] for s in journey.steps} == {0}


@pytest.mark.parametrize(("journey_id", "left"), [("PJ06", "activity"), ("PJ11", "run.detail")])
def test_con_149_bang_opens_the_top_open_action_and_keeps_the_departure(
    journey_id: str, left: str
) -> None:
    """CON-149: ``!`` opens the top item's own detail, never the bare route, and pushes."""
    journey = port(journey_id)
    after = journey.steps[-1].after
    assert after["route"] == "attention"
    assert after["overlay"] in {"consequence", "question", "pause"}
    assert after["back_depth"] == 1
    assert journey.setup.route == left


# ---------- PRX-054 and PRX-060: the bucket partitions sum alike everywhere ----------

_RAIL = re.compile(r"│\s+▸?(?P<sub>↳ )?(?P<name>[a-z/ ]+?)\s+(?P<count>≈?\d+|\?|∅)\s*$")
_STRIP_ITEM = re.compile(r"^▸?(?P<sub>↳ )?(?P<name>[a-z/ ]+?) (?P<count>≈?\d+|\?|∅)$")


def _rail(frame: str) -> dict[str, str]:
    """Return the bucket rail's counts, each sub-bucket keyed under its parent."""
    counts: dict[str, str] = {}
    parent = ""
    for row in frame.split("\n"):
        found = _RAIL.search(row)
        if found is None:
            continue
        name = found["name"].strip()
        if found["sub"]:
            name = f"{parent} > {name}"
        else:
            parent = name
        counts[name] = found["count"]
    return counts


def _strip(frame: str) -> dict[str, str]:
    """Return the counts the 80-column strip shows, its window edges dropped."""
    row = next(r for r in frame.split("\n") if r.startswith(" BUCKETS"))
    counts: dict[str, str] = {}
    parent = ""
    for cell in row[len(" BUCKETS") :].strip().split(" · "):
        found = _STRIP_ITEM.match(cell.strip())
        if found is None:
            continue
        name = found["name"].strip()
        if found["sub"]:
            name = f"{parent} > {name}"
        else:
            parent = name
        counts[name] = found["count"]
    return counts


def _top(counts: dict[str, str]) -> int:
    """Return the sum of the known top-level counts."""
    return sum(
        int(v.lstrip("≈"))
        for k, v in counts.items()
        if k != "all" and " > " not in k and v not in ("?", "∅")
    )


def _widths(route: str) -> list[str]:
    """Return ``route`` at the three widths, then scope home at 120 columns."""

    async def body() -> list[str]:
        async with js.driven(js.held_app(js.DocumentDaemon(bodies.DOCUMENT))) as harness:
            shots = [
                (await js.walk(harness, {"route": route, "size": n}, []))[0] for n in (0, 1, 2)
            ]
            return [*shots, (await js.walk(harness, {"route": "scope.home", "size": 1}, []))[0]]

    return asyncio.run(body())


def test_prx_054_the_activity_rail_strip_and_summary_state_one_partition() -> None:
    narrow, wide, widest, _home = _widths("activity")
    rail = _rail(wide)
    assert rail == _rail(widest)
    assert len([k for k in rail if " > " not in k]) == 8
    strip = _strip(narrow)
    assert all(rail[k] == v for k, v in strip.items() if k != "all"), strip
    runs = len(bodies.DOCUMENT["run"])
    assert int(strip["all"]) == _top(rail) == runs
    parent = "needs operator"
    subs = [int(v) for k, v in rail.items() if k.startswith(f"{parent} > ")]
    assert len(subs) == 7 and int(rail[parent]) == sum(subs)
    for shot in (narrow, wide, widest):
        assert re.search(rf"^ {runs} runs\b", shot, re.MULTILINE)


def test_prx_060_the_attention_strip_rail_summary_and_home_list_sum_alike() -> None:
    narrow, wide, widest, home = _widths("attention")
    rail = _rail(wide)
    assert rail == _rail(widest)
    assert len([k for k in rail if " > " not in k]) == 8
    strip = _strip(narrow)
    assert all(rail[k] == v for k, v in strip.items() if k != "all"), strip
    served = js.DocumentDaemon(bodies.DOCUMENT).answer(
        READ_METHOD_TEMPLATE.format(route=ATTENTION_ROUTE), {}
    )
    register = build_register_view(RouteProjection.model_validate(served))
    view = build_attention_view(register)
    items = view.items
    assert len({item.key for item in items}) == len(items), "an item sits in two buckets"
    # ``all`` is every item the buckets list, notices included
    assert int(strip["all"]) == _top(rail) == len(items)
    assert any(item.read_only for item in items), "the document must hold a notice"
    # a notice counts toward no principal, so the principal counts are the blocking items
    blocking = len(view.blocking())
    for shot in (narrow, wide, widest):
        summary = re.search(r"^ (\d+) mine · (\d+) all principals", shot, re.MULTILINE)
        assert summary is not None and int(summary.group(2)) == blocking
    listed = re.search(r"^ NEEDS OPERATOR\s+(\d+)", home, re.MULTILINE)
    others = re.search(r"^\s+(\d+) actions? open to other principals", home, re.MULTILINE)
    assert listed is not None and others is not None
    assert int(listed.group(1)) + int(others.group(1)) == blocking
    for producerless in ("rejected", "active"):
        assert rail[producerless] == "0", producerless
    assert rail["stalled"] == "0", "the document stands over no stalled Run"


def test_prx_060_a_bucket_filter_changes_no_rail_count() -> None:
    journey = port("PJ05")
    rails = [_rail(frame) for frame in frames(journey)]
    assert [step.after["bucket"] for step in journey.steps] == [None, "failed", "lost", None]
    assert all(rail == rails[0] for rail in rails)


@pytest.mark.parametrize("route", ["activity", "attention"])
def test_prx_054_prx_060_tab_marks_every_native_bucket_on_the_narrow_strip(route: str) -> None:
    async def body() -> list[str]:
        async with js.driven(js.held_app(js.DocumentDaemon(bodies.DOCUMENT))) as harness:
            return (await js.walk(harness, {"route": route, "size": 0}, ["Tab"] * 8))[1:]

    for shot in asyncio.run(body()):
        strip = next(r for r in shot.split("\n") if r.startswith(" BUCKETS"))
        assert "▸all" not in strip


# ---------- PRX-061: a deadline-bearing permission raised off Attention ----------


def test_prx_061_an_item_raised_on_settings_is_delivered_once_and_stays_counted() -> None:
    """Resolution recorded: the header count and ``!`` suffice; no toast outlives the cap.

    The item arrives by keyed patch while the operator is on settings. It is delivered to
    the eligible principal once, as a toast the rack records and then ages out of, and the
    header keeps counting it until it is answered; ``!`` reaches it from where the operator
    stands. A deadline of forty-five seconds is past the toast's dwell, so the header
    count is what stands until the provider's deadline, not the toast.
    """
    deadline = 45.0
    assert deadline > TOAST_DWELL

    async def body() -> tuple[list[Any], str, str, list[Any], str, str | None]:
        clock = FakeClock()
        seam = ProjectionSeam(
            route="scope.home",
            scope_id=bodies.SCOPE,
            state_path=None,
            clock=lambda: bodies.AT,
            daemon_client_factory=js.DocumentDaemon(bodies.DOCUMENT).client,
            operator=Operator(principal=bodies.ME),
        )
        app = ConsoleApp(chrome=load_chrome(), seam=seam, clock=clock)
        async with js.driven(app) as harness:
            await js.walk(harness, {}, ["g", "s"])
            assert app.session.route == "settings"
            await seam.apply_patch(_patch(ATTENTION_ROUTE, "ACT-0009", "pending_action", "WAITING"))
            arrived, _cycles = await settle(harness.pilot)
            delivered = list(app.session.toasts)
            clock.advance(deadline)
            app.tick()
            aged, _cycles = await settle(harness.pilot)
            await harness.press("!", "prx-061")
            jumped, _cycles = await settle(harness.pilot)
            return delivered, arrived, aged, list(app.session.log), jumped, app.session.route

    delivered, arrived, aged, log, jumped, route = asyncio.run(body())
    assert [toast.text for toast in delivered] == ["ACT-0009 needs answer"]
    assert badge(arrived) == badge(aged) == [2]
    assert "needs you" not in aged.split("\n")[-2].lower() or "ACT-0009" not in aged
    assert any("toast expired · needs you" in entry.note for entry in log)
    assert route == "attention"
    # CON-149: the jump opens the top item's own card and the header still counts both
    assert badge(jumped) == [2]
    assert " ▸ consequence · " in jumped.split("\n")[0]


# ---------- PRX-053: the nine control outcomes stay distinguishable ----------


def test_prx_053_the_nine_outcomes_render_nine_different_ways() -> None:
    rendered: dict[ControlDisposition, tuple[str, str]] = {}
    for disposition in ControlDisposition:
        app = ConsoleApp(chrome=load_chrome(), clock=FakeClock())
        status = {
            ControlDisposition.CONFIRMED: OperationStatus.APPLIED,
            ControlDisposition.REJECTED: OperationStatus.REFUSED,
            ControlDisposition.SUPERSEDED: OperationStatus.SUPERSEDED,
        }.get(disposition, OperationStatus.OUTSTANDING)
        app.announce(
            OperationResult(
                operation_id="CTL-0000000000000001",
                target="RUN-00000002",
                status=status,
                detail=f"{disposition.value} detail",
                disposition=disposition,
            )
        )
        rendered[disposition] = (app.session.toasts[-1].title, app.session.log[0].note)
    assert len({title for title, _note in rendered.values()}) == 9
    assert len({note for _title, note in rendered.values()}) == 9


def test_prx_053_an_unknown_answer_stays_unknown_across_a_disconnect_until_a_fact_answers() -> None:
    daemon = js.DocumentDaemon(bodies.DOCUMENT, lost=frozenset({"RUN-00000002"}))

    async def body() -> list[tuple[str, ControlDisposition]]:
        seen: list[tuple[str, ControlDisposition]] = []
        async with js.driven(js.held_app(daemon)) as harness:
            app, seam = harness.app, harness.app.seam
            assert seam is not None
            await js.walk(harness, {"route": "run.detail", "subjId": "RUN-00000002"}, [".", "n"])
            await harness.press("Enter", "prx-053")
            await app.workers.wait_for_complete()
            seen.append(("lost", _outcome(app)))
            await seam.disconnect()
            app.render_frame()
            text, _cycles = await settle(harness.pilot)
            assert "DISCONNECTED" in text.split("\n")[0]
            seen.append(("disconnected", _outcome(app)))
            daemon.lost.clear()
            daemon.control_disposition = "confirmed"
            daemon.reconnect_answer = _reconnect(ReconnectDisposition.CURRENT)
            outcome = await seam.reconnect()
            for result in outcome.reconciled:
                app.announce(result)
            await settle(harness.pilot)
            seen.append(("replayed", _outcome(app)))
        return seen

    seen = asyncio.run(body())
    assert seen == [
        ("lost", ControlDisposition.UNKNOWN),
        ("disconnected", ControlDisposition.UNKNOWN),
        ("replayed", ControlDisposition.CONFIRMED),
    ]


def _outcome(app: ConsoleApp) -> ControlDisposition:
    card = app.session.mutation
    assert isinstance(card, Card)
    (row,) = card.results
    return row.disposition


def _reconnect(disposition: ReconnectDisposition, *, route: str = "run.detail") -> dict[str, Any]:
    replay = disposition is ReconnectDisposition.REPLAY
    return {
        "negotiation": {
            "schema_version": "1.0",
            "route": route,
            "disposition": disposition.value,
            "client_cursor": 41190 if replay else bodies.CURSOR,
            "server_cursor": bodies.CURSOR,
            "gap": {"first_sequence": 41191, "last_sequence": bodies.CURSOR} if replay else None,
            "retention": {"first_sequence": 1, "last_sequence": bodies.CURSOR},
        },
        "patches": [],
    }


# ---------- PRX-065: thinking, subagent and background are told apart ----------


def _transcript_events() -> tuple[Any, ...]:
    """Return a background command, a reasoning span open 18s, then a foreground command."""
    lines = (
        (tb._command(1, command_ref="CMD-000000b1", execution="background"), 0),
        (tb._event(2), 42),
        (tb._command(3, command_ref="CMD-000000f1"), 60),
    )
    return tuple(line.model_copy(update={"recorded_at": tb._at(at)}) for line, at in lines)


def _transcript_frames(
    events: tuple[Any, ...],
    *,
    children: dict[str, tuple[Any, ...]] | None = None,
    keys: Sequence[str] = (),
) -> list[str]:
    """Return the transcript of RUN-00000010 at every width, after ``keys``.

    The lines reach the console the way a live one reads them: the seam asks the daemon
    for the Run's stream and for each child's it names.
    """
    daemon = js.DocumentDaemon(tb.DOCUMENT)
    daemon.run_events = {tb.DOCUMENT["run"]["RUN-00000010"]["urn"]: events, **(children or {})}

    async def body() -> list[str]:
        seam = ProjectionSeam(
            route=TRANSCRIPT_ROUTE,
            scope_id=tb.SCOPE,
            state_path=None,
            clock=lambda: tb.AT,
            daemon_client_factory=daemon.client,
            operator=Operator(principal=bodies.ME),
        )
        app = ConsoleApp(chrome=load_chrome(), clock=FakeClock(), seam=seam)
        async with js.driven(app) as harness:
            setup = {"route": TRANSCRIPT_ROUTE, "subjId": "RUN-00000010"}
            return [
                (await js.walk(harness, {**setup, "size": n}, list(keys)))[-1]
                for n in range(len(SIZES))
            ]

    return asyncio.run(body())


def test_prx_065_thinking_and_background_are_told_apart_at_every_width() -> None:
    for shot in _transcript_frames(_transcript_events()):
        context = shot.split("\n")[1]
        assert "THINKING for 18s" in context
        # the one background command counts; the foreground command in flight does not
        assert "· 1 running in the background ·" in context
        assert "° thinking" in shot
        assert "» background" in shot
        assert "⋯ running" in shot


#: The child Run a subagent delegation started, and the delegation it answers.
_CHILD_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"
_DELEGATION = "delegation://claude/0a1b2c3d"


def _child_started(sequence: int) -> Any:
    """Return the typed line stating a subagent's child Run started."""
    payload = ChildRunPayload.model_validate(
        {"child_run_ref": _CHILD_URN, "delegation_request_ref": _DELEGATION, "phase": "started"}
    )
    return tb._event(sequence, event_kind=RunEventKind.CHILD_RUN_STARTED, payload=payload)


def _with_a_subagent() -> tuple[Any, ...]:
    """Return the background command and open reasoning turn, then a child Run started."""
    child = _child_started(4).model_copy(update={"recorded_at": tb._at(60)})
    return (*_transcript_events(), child)


def _child_said(sequence: int, summary: str) -> Any:
    """Return one assistant message on the child Run's own stream."""
    return tb._event(
        sequence,
        run_ref=_CHILD_URN,
        event_kind=RunEventKind.MESSAGE_SUMMARIZED,
        payload=MessageSummaryPayload(message_role="assistant", summary=summary),
    )


def test_prx_065_a_subagent_child_run_is_stated_by_a_typed_event() -> None:
    for shot in _transcript_frames(_with_a_subagent()):
        context = shot.split("\n")[1]
        assert "» subagent" in shot
        assert "RUN-00000011 · started · working elsewhere" in shot
        # the child works elsewhere, so it counts with the background command
        assert "· 2 running in the background ·" in context


def test_prx_065_the_subagent_block_carries_its_now_found_and_reports_to_line() -> None:
    """PRX-065, CON-171, UI-072: the delegation opens on what the child does now, what it
    found and that it reports back, each read from the child Run's own stream."""
    children = {
        _CHILD_URN: (
            _child_said(1, "I will list the package first."),
            _child_said(2, "There are forty-two modules."),
        )
    }
    for shot in _transcript_frames(_with_a_subagent(), children=children, keys=["Enter"]):
        assert re.search(r"NOW +There are forty-two modules\.", shot)
        assert re.search(r"FOUND +There are forty-two modules\.", shot)
        assert re.search(r"REPORTS +reports back into RUN-00000010 when it ends", shot)


def test_prx_065_an_unreadable_child_transcript_says_so_rather_than_nothing() -> None:
    """CON-169, UI-072: a child whose stream cannot be read renders ``∅ unavailable``."""
    for shot in _transcript_frames(_with_a_subagent(), keys=["Enter"]):
        assert re.search(r"NOW +∅ unavailable · the child's", shot)
        assert "FOUND" not in shot


def test_prx_065_a_typed_reasoning_start_is_not_labelled_derived() -> None:
    for shot in _transcript_frames(_transcript_events()):
        state = next(row for row in shot.split("\n") if row.startswith(" STATE "))
        assert "derived" not in state


def test_prx_065_a_provider_with_no_start_marker_renders_thinking_labelled_derived() -> None:
    inferred = tuple(
        line.model_copy(update={"provenance": "eawf_derived"})
        if line.event_kind is RunEventKind.REASONING_STARTED
        else line
        for line in _transcript_events()
    )
    for shot in _transcript_frames(inferred):
        state = next(row for row in shot.split("\n") if row.startswith(" STATE "))
        assert "thinking · derived" in state


# ---------- PRX-066: a replaying Campaign shows nothing past its cursor ----------


def _replayed_campaign() -> tuple[Any, str, list[str]]:
    document = {
        **bodies.DOCUMENT,
        "campaign": {"CAM-0001": bodies._row("campaign", "CAM-0001", "ACTIVE", title="drift")},
    }
    daemon = js.DocumentDaemon(document)
    daemon.reconnect_answer = _reconnect(ReconnectDisposition.REPLAY, route="campaign")

    async def body() -> tuple[Any, str, list[str]]:
        async with js.driven(js.held_app(daemon, route="campaign")) as harness:
            seam = harness.app.seam
            assert seam is not None
            await js.walk(harness, {"route": "campaign", "subjId": "CAM-0001", "size": 1}, [])
            drawn: list[str] = []
            seam.watch(lambda _routes: drawn.append("\n".join(harness.app.frame_rows)))
            outcome = await seam.reconnect()
            harness.app.render_frame()
            closed, _cycles = await settle(harness.pilot)
            return outcome, closed, drawn

    return asyncio.run(body())


def test_prx_066_a_replayed_campaign_draws_no_finding_it_was_not_shown_promoted() -> None:
    outcome, closed, _drawn = _replayed_campaign()
    assert outcome.negotiation.disposition is ReconnectDisposition.REPLAY
    assert (outcome.negotiation.client_cursor, outcome.negotiation.server_cursor) == (41190, 41208)
    # nothing the replay carried promotes a finding, so none is drawn promoted
    assert "promoted" not in closed
    assert "the campaign plan has not been read yet" in closed


def test_prx_066_a_frame_is_drawn_under_replaying_before_the_replay_is_adopted() -> None:
    """The frame the watchers draw mid-replay heads with the replay; the head count is
    unknown here because this daemon serves no Campaign read, and the live suite
    (test_campaign_cards_live) holds the count against a real one.
    """
    outcome, closed, drawn = _replayed_campaign()
    assert drawn, "no frame was drawn while the link was replaying"
    assert "replaying 41,190 → 41,208 · ? unknown findings promoted after this point" in drawn[0]
    assert outcome.connection is not ConnectionValue.REPLAYING
    assert "replaying 41,190" not in closed


def test_the_fake_daemon_lists_runs_by_their_state_on_attention_as_the_daemon_does() -> None:
    """A failed Run still the newest attempt of an open Task is an Attention item."""
    answer = js.DocumentDaemon(bodies.DOCUMENT).answer(
        READ_METHOD_TEMPLATE.format(route=ATTENTION_ROUTE), {}
    )
    rows = RouteProjection.model_validate(answer).rows
    assert {row.key for row in rows if row.facts.get("kind") == RUN_STATE_KIND} == {"RUN-00000003"}


def test_the_fake_daemon_answers_the_repository_read_for_the_branch_asked() -> None:
    daemon = js.DocumentDaemon(bodies.DOCUMENT)
    asked = RepositoryAnswer.model_validate(
        daemon.answer(REPOSITORY_READ_METHOD, {"branch": "feature/x"})
    )
    checkout = RepositoryAnswer.model_validate(daemon.answer(REPOSITORY_READ_METHOD, {}))
    assert (asked.pull_request_branch, checkout.pull_request_branch) == ("feature/x", "main")
