"""Deterministic asciinema cast generator for the ``tui`` surface.

Drives a Textual app via ``App.run_test()`` Pilot, captures the rendered
screen as plain ASCII text at a fixed monotonic cadence, and composes an
`asciinema v2 <https://docs.asciinema.org/manual/asciicast/v2/>`_ cast
for docs / demos. There is **no real-time terminal recording**: frame
timestamps are synthesised from a fixed ``frame_ms`` interval, so the
resulting cast is byte-stable across
machines and CI runs (a header ``timestamp`` of ``0`` keeps the file
reproducible).

A *script* is a list of ``(action, arg)`` steps the harness applies
between frames:

* ``("press", "<key>")`` — drive a keypress (e.g. ``("press", "down")``)
* ``("pause", "<seconds>")`` — settle the app (e.g. ``("pause", "0.1")``)

An initial frame is captured before the first step; one frame is captured
after each step. The captured frames are normalised
(:func:`~eawf.tui.snapshot.pilot_harness.normalize_snapshot`) so the
volatile header clock does not churn the cast.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from eawf.tui.snapshot.pilot_harness import capture_screen_text, normalize_snapshot

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from textual.pilot import Pilot

#: Default inter-frame interval in milliseconds. The default is 50 ms
#: (higher fidelity than a 100 ms recommendation); casts run minutes not
#: hours so the doubled frame count is acceptable.
DEFAULT_FRAME_MS: int = 50

#: asciinema cast terminal geometry (columns x rows). Matches the cast
#: header so the rendered frames line up with a 120x40 viewer.
CAST_WIDTH: int = 120
CAST_HEIGHT: int = 40


async def record_cast(
    pilot: Pilot[object],
    script: Sequence[tuple[str, str]],
    *,
    frame_ms: int = DEFAULT_FRAME_MS,
) -> list[tuple[float, str]]:
    """Drive the app through *script*, capturing one ASCII frame per step.

    Call inside an ``async with app.run_test() as pilot:`` block, after an
    initial ``await pilot.pause()`` so the first frame is settled. One
    frame is captured before the first step and one after each step; frame
    timestamps advance by ``frame_ms`` per step (synthesised, not
    wall-clock, so the cast is deterministic).

    Args:
        pilot: The live :class:`~textual.pilot.Pilot` from
            ``app.run_test()``, already settled.
        script: Ordered ``(action, arg)`` steps — ``action`` is
            ``"press"`` (``arg`` is the key) or ``"pause"`` (``arg`` is
            the seconds to settle).
        frame_ms: Fixed inter-frame interval in milliseconds.

    Returns:
        The captured frames as ``(timestamp_seconds, screen_text)`` pairs,
        in order, with the header clock normalised.

    Raises:
        ValueError: When a step names an unknown action.
    """
    app = pilot.app
    frames: list[tuple[float, str]] = []
    frames.append((0.0, normalize_snapshot(capture_screen_text(app))))
    for step, (action, arg) in enumerate(script, start=1):
        if action == "press":
            await pilot.press(arg)
        elif action == "pause":
            await pilot.pause(float(arg))
        else:
            raise ValueError(f"unknown cast action: {action!r}")
        # Synthesise the timestamp from the integer step count so float
        # accumulation never leaks noise into a committed cast golden.
        elapsed_s = round(step * frame_ms / 1000.0, 3)
        frames.append((elapsed_s, normalize_snapshot(capture_screen_text(app))))
    return frames


def write_cast(
    frames: Sequence[tuple[float, str]],
    out_path: Path,
    *,
    title: str = "eawf TUI",
) -> None:
    """Write *frames* to *out_path* as an asciinema v2 cast.

    The first line is the cast header (version 2, fixed geometry, a
    deterministic ``timestamp`` of ``0``); each subsequent line is an
    output event ``[ts, "o", screen_text]``. The file is byte-stable for
    a given frame sequence, so it can be committed as a golden.

    Args:
        frames: ``(timestamp_seconds, screen_text)`` pairs from
            :func:`record_cast`.
        out_path: Destination ``.cast`` file (parents created if absent).
        title: Cast title recorded in the header.
    """
    header = {
        "version": 2,
        "width": CAST_WIDTH,
        "height": CAST_HEIGHT,
        "timestamp": 0,
        "title": title,
    }
    lines = [json.dumps(header, separators=(",", ":"))]
    lines.extend(json.dumps([ts, "o", screen], separators=(",", ":")) for ts, screen in frames)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = [
    "CAST_HEIGHT",
    "CAST_WIDTH",
    "DEFAULT_FRAME_MS",
    "record_cast",
    "write_cast",
]
