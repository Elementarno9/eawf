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
(:func:`~eawf.surfaces.tui.snapshot.pilot_harness.normalize_snapshot`) so the
volatile header clock does not churn the cast.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from eawf.surfaces.tui.snapshot.pilot_harness import capture_screen_text, normalize_snapshot

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

#: Header key under which the provenance stamp is nested. asciinema v2
#: players ignore unknown top-level header keys, so a namespaced object
#: carries the source commit + fixture id without breaking playback.
PROVENANCE_KEY: str = "eawf"


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
    source_commit: str | None = None,
    fixture_id: str | None = None,
) -> None:
    """Write *frames* to *out_path* as an asciinema v2 cast.

    The first line is the cast header (version 2, fixed geometry, a
    deterministic ``timestamp`` of ``0``); each subsequent line is an
    output event ``[ts, "o", screen_text]``. The file is byte-stable for
    a given frame sequence, so it can be committed as a golden.

    When ``source_commit`` or ``fixture_id`` is supplied, a provenance
    stamp is nested under the :data:`PROVENANCE_KEY` header key so the
    cast records which build produced it. asciinema players ignore the
    unknown key, and omitting both keeps the header byte-identical to the
    pre-provenance output (so existing committed goldens do not churn).

    Args:
        frames: ``(timestamp_seconds, screen_text)`` pairs from
            :func:`record_cast`.
        out_path: Destination ``.cast`` file (parents created if absent).
        title: Cast title recorded in the header.
        source_commit: Commit SHA the evidence was rendered from. When
            ``None`` the stamp omits the field.
        fixture_id: Identifier of the scenario / fixture the cast
            exercises. When ``None`` the stamp omits the field.
    """
    header: dict[str, object] = {
        "version": 2,
        "width": CAST_WIDTH,
        "height": CAST_HEIGHT,
        "timestamp": 0,
        "title": title,
    }
    provenance = _provenance_stamp(source_commit, fixture_id)
    if provenance:
        header[PROVENANCE_KEY] = provenance
    lines = [json.dumps(header, separators=(",", ":"))]
    lines.extend(json.dumps([ts, "o", screen], separators=(",", ":")) for ts, screen in frames)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _provenance_stamp(source_commit: str | None, fixture_id: str | None) -> dict[str, str]:
    """Build the provenance sub-object, omitting unset fields.

    Returns an empty dict when neither field is set so the caller can
    skip the header key entirely and keep byte-stable output.
    """
    stamp: dict[str, str] = {}
    if source_commit is not None:
        stamp["source_commit"] = source_commit
    if fixture_id is not None:
        stamp["fixture_id"] = fixture_id
    return stamp


def read_cast_provenance(cast_path: Path) -> tuple[str | None, str | None]:
    """Read the provenance stamp back out of an asciinema cast.

    Parses the cast header (first line) and returns the embedded
    ``source_commit`` and ``fixture_id`` recorded under
    :data:`PROVENANCE_KEY`. A cast written without provenance (or whose
    stamp omits one field) yields ``None`` for the missing value, so the
    reader round-trips both the stamped and the pre-provenance form.

    Args:
        cast_path: Path to a ``.cast`` file written by :func:`write_cast`.

    Returns:
        A ``(source_commit, fixture_id)`` tuple; each element is ``None``
        when the cast does not record that field.

    Raises:
        ValueError: When *cast_path* is empty or its header line is not a
            JSON object.
    """
    text = cast_path.read_text(encoding="utf-8")
    first_line = text.split("\n", 1)[0]
    if not first_line.strip():
        raise ValueError(f"empty cast header: {cast_path!r}")
    header = json.loads(first_line)
    if not isinstance(header, dict):
        raise ValueError(f"cast header is not a JSON object: {cast_path!r}")
    stamp = header.get(PROVENANCE_KEY)
    if not isinstance(stamp, dict):
        return (None, None)
    source_commit = stamp.get("source_commit")
    fixture_id = stamp.get("fixture_id")
    return (
        source_commit if isinstance(source_commit, str) else None,
        fixture_id if isinstance(fixture_id, str) else None,
    )


__all__ = [
    "CAST_HEIGHT",
    "CAST_WIDTH",
    "DEFAULT_FRAME_MS",
    "PROVENANCE_KEY",
    "read_cast_provenance",
    "record_cast",
    "write_cast",
]
