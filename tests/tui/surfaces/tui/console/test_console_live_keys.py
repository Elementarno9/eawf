"""Live on this repository's own tree: every advertised key acts, and every drill has a subject.

A console that passes every fixture suite can still advertise a key that does nothing on
the tree an operator opens: a list with one row still offering ``↑↓``, a frame with no
filter field still offering ``\\``, an Enter that opens nothing. This suite serves this
repository's ``.ea`` tree read-only over a private socket, opens every registered route,
and presses each key its keybar advertises from a fresh reset; the frame must change,
because keys shown are the keys that work. The drills are walked too: each one opens the
record the caret was on, and none reaches a prototype record.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.harness import settle
from eawf.surfaces.tui.console.keybar import KEY, ROUTE_KEYS
from eawf.surfaces.tui.console.keymap import ATTENTION_JUMP_KEY, GLOBAL_KEYS, HELP_KEY, MOTION
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from tests.tui.surfaces.tui.console.test_console_journey_assertions import advertised
from tests.tui.surfaces.tui.console.test_console_live_smoke import (
    REPO_ROOT,
    authority_digests,
    live_console,
    render_setup,
    require_epoch2_repository,
)

#: The frame size every route is walked at.
SIZE = 1
#: The detail routes, each also walked on the first record its read model holds.
DETAIL_ROUTES = ("track", "milestone", "batch.detail", "task.detail", "run.detail")
#: The prototype records a live tree must never reach.
PROTOTYPE_IDS = frozenset(
    {"MLS-0001", "MLS-0004", "MLS-0007", "CAM-0001", "CLM-0004", "REL-0001", "RUN-538453eb"}
)
#: Of a pair of keys, the one that moves off a fresh frame is tried first.
_FIRST = {"ArrowDown": 0, "PageDown": 0, "End": 0, "ArrowRight": 0}


#: Every token a keybar pair may open with; the legend a frame docks beside its keybar
#: opens with none of them.
_TOKENS = {e.token for table in ROUTE_KEYS.values() for e in table} | {
    e.token for e in KEY.values()
}


def _pairs(bar: str) -> list[str]:
    """Return the ``key label`` pairs a keybar row advertises."""
    pairs = [pair for pair in re.split(r" {3,}", bar.strip()) if pair]
    return [pair for pair in pairs if any(pair.startswith(f"{t} ") for t in _TOKENS)]


async def _acts(app: ConsoleApp, pilot: Any, setup: SessionSetup, base: str, key: str) -> bool:
    """Return whether ``key`` pressed on a fresh ``setup`` changes the frame."""
    await render_setup(app, pilot, setup)
    app.press_key(key)
    text, _cycles = await settle(pilot)
    return text != base


async def _setups(app: ConsoleApp, pilot: Any, seam: Any) -> list[SessionSetup]:
    """Return every route's setup, and each detail route's on its first held record."""
    setups: list[SessionSetup] = []
    for route in REGISTRY.ids:
        if route == "entry":
            continue
        setups.append(SessionSetup(route=route, size=SIZE))
        if route in DETAIL_ROUTES:
            await render_setup(app, pilot, setups[-1])
            model = seam.projection_for(REGISTRY.by_id[route].key)
            keys = [row.key for row in getattr(model, "rows", ())]
            if keys:
                setups.append(SessionSetup(route=route, size=SIZE, subjId=keys[0]))
    return setups


def test_j4_06_live_every_advertised_key_acts_on_every_route(tmp_path: Path) -> None:
    """Each pair on each route's keybar moves the frame when one of its keys is pressed."""
    require_epoch2_repository()
    before = authority_digests(REPO_ROOT)

    async def body() -> tuple[list[str], int]:
        silent: list[str] = []
        pressed = 0
        async with (
            live_console(REPO_ROOT, tmp_path / "runtime") as (app, seam),
            app.run_test(size=SIZES[SIZE]) as pilot,
        ):
            for setup in await _setups(app, pilot, seam):
                base = await render_setup(app, pilot, setup)
                for pair in _pairs(base.split("\n")[-1]):
                    keys = sorted(advertised(pair), key=lambda k: _FIRST.get(k, 1))
                    acted = False
                    for key in keys:
                        pressed += 1
                        if await _acts(app, pilot, setup, base, key):
                            acted = True
                            break
                    if keys and not acted:
                        silent.append(f"{setup.route} {setup.subj_id or ''}: {pair}")
        return silent, pressed

    silent, pressed = asyncio.run(body())
    assert not silent, "advertised but silent:\n" + "\n".join(silent)
    assert pressed > 100
    assert authority_digests(REPO_ROOT) == before, "the live serve wrote to the authority tree"


#: The keys every route is pressed with beside its own table: the global grammar and
#: the motion keys, each of which the refusal gate admits on every route. Ctrl+C is left
#: out because it quits.
_CLAIMABLE = (
    *(key for g in GLOBAL_KEYS for key in g.keys if key != "ctrl+c"),
    *sorted(MOTION),
    HELP_KEY,
    ATTENTION_JUMP_KEY,
)


#: A key that steps back, and the key that first moves the caret off the top for it: a
#: fresh frame's caret is at the first row, where stepping back is plainly at the edge.
_BACKWARD = {"ArrowUp": "ArrowDown", "k": "ArrowDown", "PageUp": "PageDown", "Home": "End"}


def _claimed(trace: str | None) -> bool:
    """Return whether the key a session last logged was claimed by some handler."""
    return trace is not None and not trace.endswith("→ unclaimed")


def test_c2_09_live_every_claimed_key_acts_or_says_why(tmp_path: Path) -> None:
    """A key some handler claims either changes the frame or raises a toast naming why.

    A claimed key whose only answer is a key-log line the frame never draws reads, to the
    operator, as a key that does nothing; the unclaimed ones are refused and stay silent.
    """
    require_epoch2_repository()

    async def body() -> tuple[list[str], int]:
        silent: list[str] = []
        pressed = 0
        async with (
            live_console(REPO_ROOT, tmp_path / "runtime", launched=True) as (app, seam),
            app.run_test(size=SIZES[SIZE]) as pilot,
        ):
            for setup in await _setups(app, pilot, seam):
                table = {k for e in ROUTE_KEYS.get(setup.route, ()) for k in e.keys}
                for key in dict.fromkeys((*sorted(table), *_CLAIMABLE)):
                    base = await render_setup(app, pilot, setup)
                    if key in _BACKWARD:
                        app.press_key(_BACKWARD[key])
                        base, _cycles = await settle(pilot)
                    toasts = len(app.session.toasts)
                    app.session.trace = None
                    app.press_key(key)
                    text, _cycles = await settle(pilot)
                    pressed += 1
                    s = app.session
                    if _claimed(s.trace) and text == base and len(s.toasts) <= toasts:
                        silent.append(f"{setup.route} {setup.subj_id or ''}: {key} ({s.trace})")
        return silent, pressed

    silent, pressed = asyncio.run(body())
    assert not silent, "claimed but silent:\n" + "\n".join(silent)
    assert pressed > 500


def test_j1_01_j1_05_j1_08_live_drills_carry_the_row_under_the_caret(tmp_path: Path) -> None:
    """Home, Activity and Search open the row the caret is on; a Run stays that Run."""
    require_epoch2_repository()

    async def body() -> list[tuple[str, str | None, str | None]]:
        walked: list[tuple[str, str | None, str | None]] = []
        async with (
            live_console(REPO_ROOT, tmp_path / "runtime") as (app, _seam),
            app.run_test(size=SIZES[SIZE]) as pilot,
        ):
            for route, keys in (
                ("scope.home", ["ArrowDown"]),
                ("activity", ["ArrowDown"]),
                ("search", ["ArrowDown"]),
            ):
                await render_setup(app, pilot, SessionSetup(route=route, size=SIZE))
                for key in keys:
                    app.press_key(key)
                caret = app.session.sel_id
                app.press_key("Enter")
                await settle(pilot)
                walked.append((route, caret, app.session.subj_id))
            await render_setup(app, pilot, SessionSetup(route="run.detail", size=SIZE))
            pinned = app.session.subj_id
            for key in ("ArrowDown", "ArrowDown", "End"):
                app.press_key(key)
            await settle(pilot)
            walked.append(("run.detail", pinned, app.session.subj_id))
        return walked

    walked = asyncio.run(body())
    for route, caret, subject in walked:
        assert caret is not None, route
        assert subject == caret, f"{route}: the drill opened {subject}, the caret was on {caret}"
        assert subject not in PROTOTYPE_IDS, route


#: Each walk: where it starts, the keys pressed, and the place every key lands on as
#: ``(route, subject)``; ``None`` for a place the walk does not pin.
_WALKS: tuple[
    tuple[str, SessionSetup, tuple[tuple[str, tuple[str, str | None] | None], ...]], ...
] = (
    (
        "C2-02 u climbs a Run to its Track through the chain the rows state",
        SessionSetup(route="activity", size=SIZE),
        (
            ("ArrowDown", None),
            ("ArrowDown", None),
            ("Enter", ("run.detail", "RUN-00000005")),
            ("u", ("task.detail", "EAWF-0101")),
            ("u", ("batch.detail", "BAT-0101")),
            ("u", ("milestone", "MLS-0101")),
            ("u", ("track", "TRK-EAWF-CORE")),
            ("u", ("scope.home", None)),
        ),
    ),
    (
        "C2-02 [ ] walk the Milestones of one Track; Esc with no history climbs to it",
        SessionSetup(route="milestone", size=SIZE, subjId="MLS-0100"),
        (
            ("]", ("milestone", "MLS-0101")),
            ("[", ("milestone", "MLS-0100")),
            ("[", ("milestone", "MLS-0103")),
            ("Escape", ("track", "TRK-EAWF-CORE")),
        ),
    ),
    (
        "C2-02 a light verb opens on its Run and u climbs back to that Run",
        SessionSetup(route="run.detail", size=SIZE, subjId="RUN-00000005"),
        ((".", None), ("b", ("git.pr", "RUN-00000005")), ("u", ("run.detail", "RUN-00000005"))),
    ),
    (
        "C1-04 Esc from Trust climbs to the Milestone it is about",
        SessionSetup(route="trust", size=SIZE, subjId="MLS-0101"),
        (("Escape", ("milestone", "MLS-0101")),),
    ),
    (
        "C1-04 J2-19 Esc from a subjectless Trust, Evidence or Campaign lands on home",
        SessionSetup(route="trust", size=SIZE),
        (("Escape", ("scope.home", None)),),
    ),
    (
        "C2-03 the palette opens a held Milestone by its id",
        SessionSetup(route="scope.home", size=SIZE),
        (("/", None), ("m", None), ("l", None), ("s", None), ("Enter", ("milestone", "MLS-0100"))),
    ),
)


def test_c2_02_c1_04_c2_03_live_climbs_and_jumps_land_on_real_records(tmp_path: Path) -> None:
    """On this tree every climb, sibling step and palette jump lands on a record it holds."""
    require_epoch2_repository()

    async def body() -> list[str]:
        wrong: list[str] = []
        async with (
            live_console(REPO_ROOT, tmp_path / "runtime", launched=True) as (app, _seam),
            app.run_test(size=SIZES[SIZE]) as pilot,
        ):
            for name, setup, steps in _WALKS:
                await render_setup(app, pilot, setup)
                for key, want in steps:
                    app.press_key(key)
                    await settle(pilot)
                    got = (app.session.route, app.session.subj_id)
                    if got[1] in PROTOTYPE_IDS or (want is not None and got != want):
                        wrong.append(f"{name}: {key} → {got}, wanted {want}")
            for route in ("evidence", "campaign"):
                await render_setup(app, pilot, SessionSetup(route=route, size=SIZE))
                app.press_key("Escape")
                await settle(pilot)
                if (app.session.route, app.session.subj_id) != ("scope.home", None):
                    wrong.append(f"{route}: Esc → {app.session.route} {app.session.subj_id}")
        return wrong

    wrong = asyncio.run(body())
    assert not wrong, "\n".join(wrong)
