"""Replay the tracked golden contract against the Textual chassis.

Journeys run before frames (the contract's ordering rule: the frames must still match in
a session the journeys left twenty-five journeys deep in state). Every frame is compared
whole; a failure names the first differing row with spaces made visible as a middle dot.
"""

from __future__ import annotations

import fnmatch
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..chassis.app import ConsoleApp
from ..chassis.fixture import load_fixture
from ..chassis.session import SIZES, FakeClock
from ..goldens import SEQUENCES_DIR
from ..harness.keymap import SIMULATOR_KEYS, to_pilot_key
from ..harness.settle import settle

FRAME_FILES = (
    "frames-routes-80.json",
    "frames-routes-120.json",
    "frames-routes-160.json",
    "frames-overlays.json",
    "frames-connection.json",
    "frames-entry.json",
    "frames-notifications.json",
)
VISIBLE_SPACE = "·"


@dataclass(slots=True)
class Result:
    id: str
    kind: str
    ok: bool
    detail: str = ""
    first_diff_row: int | None = None
    expected: str | None = None
    actual: str | None = None
    settle_cycles: int = 1
    ms: float = 0.0
    steps: list[dict[str, Any]] = field(default_factory=list)


def visible(row: str) -> str:
    return row.replace(" ", VISIBLE_SPACE)


def first_diff(expected: str, actual: str) -> tuple[int | None, str, str, str]:
    A, B = expected.split("\n"), actual.split("\n")
    if len(A) != len(B):
        return (-1, f"row count {len(B)} expected {len(A)}", "", "")
    for i, (a, b) in enumerate(zip(A, B, strict=True)):
        if a != b:
            return (i, f"row {i} differs", a, b)
    return (None, "", "", "")


def diff_proj(expected: dict[str, Any], got: dict[str, Any]) -> list[str]:
    return [
        f"{k} {expected[k]}≠{got[k]}"
        for k in got
        if k in expected and str(expected[k]) != str(got[k])
    ]


def load_sequences(
    seq: Path = SEQUENCES_DIR,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the ``(index, frame states, journeys)`` of the tracked contract.

    Args:
        seq: Directory holding ``index.json``, the seven ``frames-*.json`` files and
            ``journeys.json``. Defaults to the tracked golden sequences.

    Raises:
        FileNotFoundError: a contract file is missing from ``seq``.
    """
    index = json.loads((seq / "index.json").read_text(encoding="utf-8"))
    states: list[dict[str, Any]] = []
    for name in FRAME_FILES:
        data = json.loads((seq / name).read_text(encoding="utf-8"))
        for st in data["states"]:
            st["_file"] = name
            states.append(st)
    journeys = json.loads((seq / "journeys.json").read_text(encoding="utf-8"))["journeys"]
    return index, states, journeys


class Replayer:
    def __init__(self, app: ConsoleApp, pilot: Any) -> None:
        self.app = app
        self.pilot = pilot

    async def ensure_size(self, size_index: int) -> None:
        w, h = SIZES[size_index]
        if (self.app.size.width, self.app.size.height) != (w, h):
            await self.pilot.resize_terminal(w, h)
            await self.pilot.pause()

    async def press(self, key: str) -> None:
        # `w` reaches the dispatcher like any key: an open palette (or any typing state)
        # consumes it as text before the size case, exactly as the prototype's handler order
        if key == "Shift-Tab":
            await self.pilot.press("shift+tab")
        else:
            await self.pilot.press(to_pilot_key(key))
        if key in SIMULATOR_KEYS and self.app.session.simulator:
            await self.ensure_size(self.app.session.size)
            self.app.render_frame()

    async def frame(self, st: dict[str, Any]) -> Result:
        t0 = time.perf_counter()
        setup = st.get("setup") or {}
        self.app.reset(setup)
        await self.ensure_size(self.app.session.size)
        # the prototype's resetSession ends with render(), so a route has published what its
        # keys read (the timeline's marker count, a record's navigation) before the first press
        self.app.render_frame()
        for t in st.get("rack") or ():
            self.app.notify_toast(
                t.get("text") or t.get("title"), t.get("title", "done"), t.get("sev", "info")
            )
        if st.get("kind") == "verbose":
            self.app.session.verbose = True
        try:
            for key in st.get("keys") or ():
                await self.press(key)
            self.app.render_frame()
            text, cycles = await settle(self.pilot)
        except Exception as exc:  # a renderer defect is a result row, not a crashed run
            self.app.session.verbose = False
            return Result(
                st["id"],
                st.get("kind", "route"),
                False,
                f"exception: {type(exc).__name__}: {exc}",
                ms=(time.perf_counter() - t0) * 1000,
            )
        self.app.session.verbose = False
        row, why, exp, got = first_diff(st["frame"], text)
        ms = (time.perf_counter() - t0) * 1000
        if row is None:
            return Result(st["id"], st.get("kind", "route"), True, settle_cycles=cycles, ms=ms)
        return Result(st["id"], st.get("kind", "route"), False, why, row, exp, got, cycles, ms)

    async def journey(self, j: dict[str, Any]) -> Result:
        t0 = time.perf_counter()
        self.app.reset(j.get("setup") or {})
        await self.ensure_size(self.app.session.size)
        self.app.render_frame()
        steps: list[dict[str, Any]] = []
        ok = True
        detail = ""
        first_row: int | None = None
        exp_row = got_row = None
        for i, st in enumerate(j["steps"]):
            try:
                if i:
                    await self.press(st["key"])
                text, cycles = await settle(self.pilot)
            except Exception as exc:
                ok = False
                detail = f"step {i}: exception {type(exc).__name__}: {exc}"
                steps.append({"step": i, "key": st.get("key"), "ok": False, "detail": detail})
                break
            pd = diff_proj(st["after"], self.app.session.projection())
            row, why, exp, got = first_diff(st["frame"], text)
            step_ok = not pd and row is None
            steps.append(
                {
                    "step": i,
                    "key": st.get("key"),
                    "ok": step_ok,
                    "proj": pd,
                    "frame": why,
                    "row": row,
                    "cycles": cycles,
                }
            )
            if not step_ok and ok:
                ok = False
                detail = (
                    f"step {i}"
                    + (f" [{st['key']}]" if st.get("key") else "")
                    + " · "
                    + " · ".join(pd + ([why] if why else []))
                )
                first_row, exp_row, got_row = row, exp, got
        if ok and self.app.session.auto_opens != 0:
            ok = False
            detail = f"auto-opens is {self.app.session.auto_opens}"
        return Result(
            j["id"],
            "journey",
            ok,
            detail,
            first_row,
            exp_row,
            got_row,
            1,
            (time.perf_counter() - t0) * 1000,
            steps,
        )


async def run(
    *,
    frame_globs: list[str] | None = None,
    journey_ids: list[str] | None = None,
    simulator: bool = True,
    seq: Path = SEQUENCES_DIR,
) -> list[Result]:
    """Replay the contract in one app instance, journeys first, then frames.

    Args:
        frame_globs: Frame id globs to select, or ``None`` for every frame when no
            journey ids are given either.
        journey_ids: Journey ids to select, or ``None``.
        simulator: Keep the pack's simulator affordances (the help ``w`` row and the
            entry ``[ ] state`` pair) so the pack compares as it was recorded.
        seq: Directory holding the tracked contract.

    Returns:
        One :class:`Result` per replayed journey, then one per replayed frame.
    """
    _index, states, journeys = load_sequences(seq)
    run_all = not frame_globs and not journey_ids
    sel_states = (
        states
        if run_all
        else [
            s
            for s in states
            if frame_globs and any(fnmatch.fnmatchcase(s["id"], g) for g in frame_globs)
        ]
    )
    sel_journeys = (
        journeys if run_all else [j for j in journeys if journey_ids and j["id"] in journey_ids]
    )
    app = ConsoleApp(load_fixture(), FakeClock(), simulator=simulator)
    app.session.rack_hold = True
    results: list[Result] = []
    async with app.run_test(size=(80, 24)) as pilot:
        rp = Replayer(app, pilot)
        for j in sel_journeys:
            results.append(await rp.journey(j))
        for st in sel_states:
            results.append(await rp.frame(st))
    return results
