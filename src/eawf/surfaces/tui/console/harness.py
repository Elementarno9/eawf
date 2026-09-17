"""The golden harness: one render entry point for the replayer and the generator.

:meth:`Harness.render` is the one path a golden frame is produced by, and its order is
fixed: reset the session from the setup (which fixes the frame size), turn the verbose
flag off, push each rack entry through the notify path, turn the verbose flag on for a
verbose state, resize the terminal to the session's size, render, press each key through
the dispatcher, render, settle, capture the compositor's strips whole, check the grid, and
turn the verbose flag off again. The terminal follows the session's size before any key
because a key handler reads the frame size. The console clock is held throughout, so a
frame depends only on its setup, its keys and its rack.

The replayer runs the journeys first and the frames after them in one app, so the frames
prove the one reset as well as the renderers. Every comparison goes through the
normalisation map.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from textual.app import App
from textual.pilot import Pilot

from eawf.surfaces.tui.console.app import TOOLKIT_KEYS, ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.normalisation import Normaliser, load_map
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from eawf.surfaces.tui.console.tokens import Severity

logger = logging.getLogger(__name__)

# The frame files of a golden contract, in replay order.
FRAME_FILES: tuple[str, ...] = (
    "frames-routes-80.json",
    "frames-routes-120.json",
    "frames-routes-160.json",
    "frames-overlays.json",
    "frames-connection.json",
    "frames-entry.json",
    "frames-notifications.json",
)
JOURNEY_FILE = "journeys.json"
INDEX_FILE = "index.json"
SETTLE_MAX_CYCLES = 5
VISIBLE_SPACE = "·"
ESCAPE = "\x1b"
_PILOT_KEYS: Mapping[str, str] = {name: pilot for pilot, name in TOOLKIT_KEYS.items()}
_SHIFT_PREFIX = "Shift-"
_ENTRY_STEPS: Mapping[str, int] = {"]": 1, "[": -1}


class GoldenLayout(BaseModel):
    """Where a golden contract's parts live under its root.

    Attributes:
        fixture: The fixture registers directory.
        sequences: The frame and journey sequences directory.
        normalisation_map: The pack-to-port normalisation map file.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture: Path
    sequences: Path
    normalisation_map: Path

    @classmethod
    def under(cls, root: Path) -> GoldenLayout:
        """Return the layout of a contract rooted at ``root``."""
        return cls(
            fixture=root / "fixture",
            sequences=root / "sequences",
            normalisation_map=root / "normalisation-map.json",
        )


class RackEntry(BaseModel):
    """One toast a frame state raises before its keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = "done"
    text: str | None = None
    sev: Severity = Severity.INFO


class FrameState(BaseModel):
    """One recorded frame: its setup, keys, rack and exact text.

    ``note`` and ``entry_state`` are provenance and are never compared.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: str | None = None
    setup: SessionSetup = Field(default_factory=SessionSetup)
    size: tuple[int, int]
    invariants: dict[str, Any] = Field(default_factory=dict)
    keys: tuple[str, ...] = ()
    rack: tuple[RackEntry, ...] = ()
    frame: str
    note: str | None = None
    entry_state: str | None = None


class JourneyStep(BaseModel):
    """One journey step: the key pressed, the projection after it and the exact frame."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str | None = None
    after: dict[str, Any]
    frame: str


class Journey(BaseModel):
    """One recorded journey; ``title`` and ``proves`` are provenance and never compared."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    proves: str | None = None
    setup: SessionSetup = Field(default_factory=SessionSetup)
    auto_opens: int | None = None
    steps: tuple[JourneyStep, ...]


@dataclass(frozen=True, slots=True)
class Contract:
    """A loaded golden contract: its index, frame states and journeys."""

    index: dict[str, Any]
    states: tuple[FrameState, ...]
    journeys: tuple[Journey, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        """Return every journey id, then every frame id."""
        return tuple(j.id for j in self.journeys) + tuple(s.id for s in self.states)

    def frames_by_id(self) -> dict[str, str]:
        """Return every frame state's text by id."""
        return {s.id: s.frame for s in self.states}


def load_contract(sequences: Path) -> Contract:
    """Load a golden contract's sequences strict.

    Raises:
        FileNotFoundError: a sequence file is missing.
        pydantic.ValidationError: a record carries an unknown key or a malformed field.
    """
    index = json.loads((sequences / INDEX_FILE).read_text(encoding="utf-8"))
    states: list[FrameState] = []
    for name in FRAME_FILES:
        data = json.loads((sequences / name).read_text(encoding="utf-8"))
        states.extend(FrameState.model_validate(raw) for raw in data["states"])
    raw_journeys = json.loads((sequences / JOURNEY_FILE).read_text(encoding="utf-8"))["journeys"]
    journeys = tuple(Journey.model_validate(raw) for raw in raw_journeys)
    return Contract(index, tuple(states), journeys)


@dataclass(slots=True)
class Result:
    """The replay outcome of one frame or journey.

    Attributes:
        id: The frame or journey id.
        kind: The frame kind, or ``journey``.
        ok: Whether every compared frame and projection matched.
        detail: The first failure, empty on a match.
        first_diff_row: The first differing row of the failing frame.
        expected: The expected row there.
        actual: The rendered row there.
        settle_cycles: The settle cycles the frame needed; one under a held clock.
        steps: One record per journey step.
    """

    id: str
    kind: str
    ok: bool
    detail: str = ""
    first_diff_row: int | None = None
    expected: str | None = None
    actual: str | None = None
    settle_cycles: int = 1
    steps: list[dict[str, Any]] = field(default_factory=list)


def visible(row: str) -> str:
    """Return ``row`` with its spaces made visible."""
    return row.replace(" ", VISIBLE_SPACE)


def diff_projection(expected: Mapping[str, Any], got: Mapping[str, Any]) -> list[str]:
    """Return a ``key want≠got`` note per recorded projection key the session disagrees on."""
    return [
        f"{k} {expected[k]}≠{got[k]}"
        for k in got
        if k in expected and str(expected[k]) != str(got[k])
    ]


def capture_rows(app: App[Any]) -> list[str]:
    """Return the screen as the compositor painted it, one row per strip, spaces kept."""
    return [strip.text for strip in app.screen._compositor.render_strips()]


def capture(app: App[Any]) -> str:
    """Return the painted screen as one text, rows joined by newlines."""
    return "\n".join(capture_rows(app))


def capture_cells(app: App[Any]) -> list[int]:
    """Return every painted row's width in cells."""
    return [strip.cell_length for strip in app.screen._compositor.render_strips()]


async def settle(pilot: Pilot[Any]) -> tuple[str, int]:
    """Wait for the screen to settle and return its capture and the cycles it took.

    The pilot's pause is idle-based, so one pause is never taken as a finished frame:
    workers are drained, then captures are pumped until two agree.
    """
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    previous = capture(pilot.app)
    cycles = 1
    while cycles < SETTLE_MAX_CYCLES:
        await pilot.pause()
        current = capture(pilot.app)
        if current == previous:
            return current, cycles
        previous = current
        cycles += 1
    return previous, cycles


def pilot_key(key: str) -> str:
    """Return the toolkit's name for a dispatcher key name."""
    if key.startswith(_SHIFT_PREFIX):
        return "shift+" + pilot_key(key[len(_SHIFT_PREFIX) :])
    return _PILOT_KEYS.get(key, key)


def grid_errors(text: str, size: tuple[int, int], cells: Sequence[int]) -> list[str]:
    """Return why a captured frame is off its grid: rows, cell widths, escape sequences."""
    w, h = size
    rows = text.split("\n")
    errors = [f"{len(rows)} rows, not {h}"] if len(rows) != h else []
    errors += [f"row {i} is {n} cells, not {w}" for i, n in enumerate(cells) if n != w]
    errors += [f"row {i} carries an escape sequence" for i, r in enumerate(rows) if ESCAPE in r]
    return errors


@dataclass(frozen=True, slots=True)
class Capture:
    """One rendered frame and the settle cycles it needed."""

    text: str
    cycles: int
    grid: tuple[str, ...]


class Harness:
    """Drive one mounted console through recorded states.

    Args:
        app: The console, mounted under ``pilot`` with a held clock.
        pilot: The toolkit's test pilot.
        normaliser: The map the replay compares through and whose rewrites decide which
            pack keys are harness actions.
    """

    def __init__(self, app: ConsoleApp, pilot: Pilot[Any], normaliser: Normaliser) -> None:
        self.app = app
        self.pilot = pilot
        self.normaliser = normaliser

    async def follow_size(self) -> None:
        """Resize the terminal to the session's frame size when they differ."""
        w, h = SIZES[self.app.session.size]
        if (self.app.size.width, self.app.size.height) != (w, h):
            await self.pilot.resize_terminal(w, h)
            await self.pilot.pause()

    async def press(self, key: str, contract_id: str) -> None:
        """Press one recorded key, performing a simulated key as its harness action."""
        if key in self.normaliser.simulated_keys(contract_id):
            await self._simulate(key)
            return
        await self.pilot.press(pilot_key(key))

    async def _simulate(self, key: str) -> None:
        """Perform a pack key the console does not bind: a size cycle or an entry step."""
        s = self.app.session
        if key == "w":
            s.size = (s.size + 1) % len(SIZES)
            await self.follow_size()
        elif key in _ENTRY_STEPS:
            n = len(self.app.fixture.proto.entry)
            s.entry_sel = (s.entry_sel + _ENTRY_STEPS[key]) % n
            s.path_sel = 0
        self.app.render_frame()

    async def render(
        self,
        setup: SessionSetup,
        *,
        contract_id: str,
        keys: Sequence[str] = (),
        rack: Sequence[RackEntry] = (),
        verbose: bool = False,
    ) -> Capture:
        """Render one frame state: the one render entry point.

        Args:
            setup: The reset argument, fixing the route, subject, overlay and size.
            contract_id: The frame's id, which selects the map's key rewrites.
            keys: Keys pressed after the reset, in order.
            rack: Toasts raised before the keys.
            verbose: Whether the frame is a verbose state.
        """
        app = self.app
        app.reset(setup)
        app.verbose = False
        await self.follow_size()
        app.render_frame()
        for entry in rack:
            app.raise_toast(entry.text or entry.title, title=entry.title, sev=entry.sev)
        app.verbose = verbose
        try:
            for key in keys:
                await self.press(key, contract_id)
            app.render_frame()
            text, cycles = await settle(self.pilot)
            size = (app.size.width, app.size.height)
            grid = tuple(grid_errors(text, size, capture_cells(app)))
        finally:
            app.verbose = False
        return Capture(text, cycles, grid)

    async def frame(self, state: FrameState) -> Result:
        """Replay one frame state and compare it through the map."""
        kind = state.kind or "route"
        try:
            shot = await self.render(
                state.setup,
                contract_id=state.id,
                keys=state.keys,
                rack=state.rack,
                verbose=state.kind == "verbose",
            )
        except Exception as exc:  # a renderer defect is a result row, not a crashed replay
            return Result(state.id, kind, False, f"exception: {type(exc).__name__}: {exc}")
        verdict = self.normaliser.compare(state.id, state.frame, shot.text)
        if shot.grid and verdict.ok:
            return Result(state.id, kind, False, "; ".join(shot.grid))
        if verdict.ok:
            return Result(state.id, kind, True, settle_cycles=shot.cycles)
        return Result(
            state.id,
            kind,
            False,
            verdict.detail,
            verdict.row,
            verdict.expected,
            verdict.actual,
            shot.cycles,
        )

    async def journey(self, journey: Journey) -> Result:
        """Replay one journey, comparing the projection and the frame at every step."""
        app = self.app
        app.reset(journey.setup)
        await self.follow_size()
        app.render_frame()
        result = Result(journey.id, "journey", True)
        for i, step in enumerate(journey.steps):
            try:
                if i and step.key is not None:
                    await self.press(step.key, journey.id)
                text, cycles = await settle(self.pilot)
            except Exception as exc:  # a renderer defect is a result row, not a crashed replay
                detail = f"step {i}: exception {type(exc).__name__}: {exc}"
                result.steps.append({"step": i, "key": step.key, "ok": False, "detail": detail})
                result.ok, result.detail = False, result.detail or detail
                break
            self._record_step(result, i, step, text, cycles)
        if result.ok and app.session.auto_opens != 0:
            result.ok = False
            result.detail = f"auto-opens is {app.session.auto_opens}"
        return result

    def _record_step(
        self, result: Result, i: int, step: JourneyStep, text: str, cycles: int
    ) -> None:
        projection = diff_projection(step.after, self.app.session.projection())
        verdict = self.normaliser.compare(result.id, step.frame, text)
        ok = not projection and verdict.ok
        result.steps.append(
            {
                "step": i,
                "key": step.key,
                "ok": ok,
                "proj": projection,
                "frame": verdict.detail,
                "row": verdict.row,
                "cycles": cycles,
            }
        )
        if ok or not result.ok:
            return
        key = f" [{step.key}]" if step.key else ""
        notes = projection + ([verdict.detail] if verdict.detail else [])
        result.ok = False
        result.detail = f"step {i}{key} · " + " · ".join(notes)
        result.first_diff_row = verdict.row
        result.expected = verdict.expected
        result.actual = verdict.actual


def select(
    contract: Contract,
    *,
    frame_globs: Sequence[str] | None = None,
    journey_ids: Sequence[str] | None = None,
) -> tuple[list[Journey], list[FrameState]]:
    """Return the journeys and frames a replay selects; no selection means all of them."""
    if not frame_globs and not journey_ids:
        return list(contract.journeys), list(contract.states)
    journeys = [j for j in contract.journeys if journey_ids and j.id in journey_ids]
    states = [
        s
        for s in contract.states
        if frame_globs and any(fnmatch.fnmatchcase(s.id, g) for g in frame_globs)
    ]
    return journeys, states


async def replay(
    layout: GoldenLayout,
    *,
    frame_globs: Sequence[str] | None = None,
    journey_ids: Sequence[str] | None = None,
) -> list[Result]:
    """Replay a golden contract in one console, journeys first, then frames.

    Args:
        layout: Where the contract lives.
        frame_globs: Frame id globs to select; with ``journey_ids`` also unset, all frames.
        journey_ids: Journey ids to select.

    Returns:
        One result per replayed journey, then one per replayed frame.
    """
    contract = load_contract(layout.sequences)
    normaliser = Normaliser(load_map(layout.normalisation_map), contract.frames_by_id())
    journeys, states = select(contract, frame_globs=frame_globs, journey_ids=journey_ids)
    app = ConsoleApp(load_fixture(layout.fixture), FakeClock())
    results: list[Result] = []
    async with app.run_test(size=SIZES[0]) as pilot:
        harness = Harness(app, pilot, normaliser)
        for journey in journeys:
            results.append(await harness.journey(journey))
        for state in states:
            results.append(await harness.frame(state))
    failed = sum(1 for r in results if not r.ok)
    logger.info("console replay finished", extra={"results": len(results), "failed": failed})
    return results


@dataclass(frozen=True, slots=True)
class Generated:
    """What a regeneration wrote and what it refused to write."""

    written: tuple[Path, ...]
    moved: tuple[str, ...]
    grid_failures: tuple[str, ...]


async def regenerate(layout: GoldenLayout, out: Path) -> Generated:
    """Render every frame state of a contract through the one entry point into ``out``.

    Each frame file is rewritten with the port's render in place of a recorded frame only
    where the render moved, and a frame whose capture is off its grid is collected rather
    than written. Journeys are left to the replay, which compares each of their steps.

    Args:
        layout: Where the contract lives.
        out: The directory the regenerated frame files are written to; it must exist and
            must not be the contract's own sequences directory.

    Raises:
        ValueError: ``out`` is the contract's own sequences directory.
        FileNotFoundError: ``out`` does not exist.
    """
    if out.resolve() == layout.sequences.resolve():
        raise ValueError("regeneration never writes over the tracked sequences")
    if not out.is_dir():
        raise FileNotFoundError(f"regeneration target {out} does not exist")
    contract = load_contract(layout.sequences)
    normaliser = Normaliser(load_map(layout.normalisation_map), contract.frames_by_id())
    app = ConsoleApp(load_fixture(layout.fixture), FakeClock())
    rendered: dict[str, str] = {}
    failures: list[str] = []
    async with app.run_test(size=SIZES[0]) as pilot:
        harness = Harness(app, pilot, normaliser)
        for state in contract.states:
            shot = await harness.render(
                state.setup,
                contract_id=state.id,
                keys=state.keys,
                rack=state.rack,
                verbose=state.kind == "verbose",
            )
            if shot.grid:
                failures.append(f"{state.id}: {'; '.join(shot.grid)}")
            else:
                rendered[state.id] = shot.text
    return _write_frames(layout, out, rendered, tuple(failures))


def _write_frames(
    layout: GoldenLayout, out: Path, rendered: Mapping[str, str], failures: tuple[str, ...]
) -> Generated:
    written: list[Path] = []
    moved: list[str] = []
    for name in FRAME_FILES:
        data = json.loads((layout.sequences / name).read_text(encoding="utf-8"))
        for raw in data["states"]:
            fresh = rendered.get(raw["id"])
            if fresh is not None and fresh != raw["frame"]:
                raw["frame"] = fresh
                moved.append(raw["id"])
        target = out / name
        target.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        written.append(target)
    return Generated(tuple(written), tuple(moved), failures)
