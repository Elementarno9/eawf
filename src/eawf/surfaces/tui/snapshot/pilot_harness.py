"""ASCII-text snapshot harness for the ``tui`` operator surface.

Captures a running Textual screen's rendered terminal as **plain ASCII
text** for golden-fixture comparison, driven by Textual's
``App.run_test()`` Pilot. The snapshot artifact is ASCII text (not the
SVG ``App.export_screenshot`` output):

* **diffable** — a reviewer reads the golden ``.txt`` and the unified
  diff in a code-review tool;
* **scrub-safe** — the captured text is exactly what an operator sees,
  so the secrets/PII gate inspects the same surface that ships;
* **drift-free** — SVG output embeds font metrics + style segments that
  shift across Python and Textual versions; the plain-text row dump is
  stable as long as the layout is.

The capture reads the active screen's compositor
(:meth:`textual.screen.Screen.render_strips`), which renders the topmost
screen on the stack — a base scope screen or a pushed modal overlay
alike — so one capture path serves both screen and overlay fixtures.

Determinism: the only volatile cell in the rendered chrome is the
header wall-clock (``HH:MM UTC``). :func:`normalize_snapshot` rewrites it
to a fixed ``HH:MM UTC`` placeholder before comparison so the goldens do
not churn with the time of day; everything else in the frame is a pure
function of the bound (fixture) ``state.json``.
"""

from __future__ import annotations

import asyncio
import difflib
import itertools
import os
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Literal, cast

from eawf.surfaces.render.snapshot_normalize import normalize_snapshot

if TYPE_CHECKING:
    from pathlib import Path

    from textual.app import App
    from textual.pilot import Pilot

#: Env var that, when set to ``"1"``, makes :func:`assert_screen_snapshot`
#: (re)write the golden fixture from the live capture instead of
#: comparing against it. CI runs **without** this set, so a drift fails
#: the build; a developer regenerates with
#: ``EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/``.
SNAPSHOT_REGEN_ENV: str = "EAWF_SNAPSHOT_REGEN"

#: Upper bound on the settle-pump cycles. The read-only state binder
#: populates ``app.state`` and the widgets seed from it within a couple of
#: message-pump turns; this cap keeps :func:`settle_screen` from hanging
#: if a frame never stabilises (e.g. a live animation) while giving the
#: async state-load + widget-seed handshake ample room.
_SETTLE_MAX_CYCLES: int = 20

#: Upper bound on unified-diff lines embedded in a drift ``AssertionError``.
#: A full-screen frame is ~40 rows, so a real drift fits comfortably; the
#: cap only guards against an unbounded log dump if the two frames diverge
#: wholesale (e.g. an empty capture vs a populated golden).
_DRIFT_DIFF_MAX_LINES: int = 200


async def settle_screen(pilot: Pilot[object]) -> str:
    """Pump the app until its rendered frame stabilises, return that frame.

    The read-only state binder loads ``state.json`` and pushes it into
    the App reactive asynchronously, and each widget seeds from
    ``app.state`` on its own mount. A bare ``await pilot.pause()`` can
    therefore capture an in-between frame (state not yet bound → empty-
    scope placeholder), making a golden flaky under scheduler load. This
    helper pumps the message loop until two consecutive normalised
    captures match (or :data:`_SETTLE_MAX_CYCLES` is reached), so the
    snapshot + cast harness always captures the settled frame.

    Args:
        pilot: The live :class:`~textual.pilot.Pilot` from
            ``app.run_test()``.

    Returns:
        The settled, normalised screen text.
    """
    # Drain background workers (e.g. the GitPane git probe, which now runs off
    # the event loop) before sampling so the capture reflects the post-worker
    # frame rather than a pre-probe placeholder.
    await pilot.app.workers.wait_for_complete()
    previous = normalize_snapshot(capture_screen_text(pilot.app))
    for _ in range(_SETTLE_MAX_CYCLES):
        await pilot.pause()
        current = normalize_snapshot(capture_screen_text(pilot.app))
        if current == previous:
            return current
        previous = current
    return previous


def capture_screen_text(app: App[object]) -> str:
    """Capture the app's active screen as plain ASCII text.

    Renders the topmost screen on the stack (a base scope screen or a
    pushed modal overlay) row-by-row via its compositor, joining the
    per-row text with newlines. Trailing whitespace is trimmed per row so
    the golden is not padded out to the terminal width on every line.

    The app MUST already be mounted and settled — call after
    ``await pilot.pause()`` inside an ``async with app.run_test()`` block.

    Trailing all-blank rows are dropped: a modal overlay renders only its
    own box, leaving a variable number of empty terminal rows below it
    whose count is not part of the meaningful frame. Trimming them keeps
    the golden anchored to the last content row so the comparison does not
    churn on incidental terminal-height padding.

    Args:
        app: The live :class:`~textual.app.App` under a Pilot harness.

    Returns:
        The rendered screen as a newline-joined ASCII-text block (no
        trailing blank rows, no trailing newline).
    """
    compositor = app.screen._compositor
    rows = [strip.text.rstrip() for strip in compositor.render_strips()]
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows)


def assert_screen_snapshot(app: App[object], golden_path: Path) -> None:
    """Compare the app's active screen against a golden ASCII fixture.

    Captures the active screen (:func:`capture_screen_text`), normalises
    the volatile clock cell (:func:`normalize_snapshot`), and asserts byte
    equality against *golden_path*. When :data:`SNAPSHOT_REGEN_ENV` is
    ``"1"`` the golden is (re)written from the live capture and no
    assertion runs — the regeneration escape hatch.

    Args:
        app: The live app under a Pilot harness, already settled.
        golden_path: Path to the golden ``.txt`` fixture. Created (with
            parents) on regen if absent.

    Raises:
        AssertionError: When the normalised capture differs from the
            golden and regeneration is not requested. The message embeds a
            line-tagged :func:`difflib.unified_diff` of golden (expected)
            vs capture (actual), capped at :data:`_DRIFT_DIFF_MAX_LINES`
            lines, so a residual CI drift shows the exact differing rows.
    """
    captured = normalize_snapshot(capture_screen_text(app))
    if os.environ.get(SNAPSHOT_REGEN_ENV) == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(captured + "\n", encoding="utf-8")
        return
    expected = golden_path.read_text(encoding="utf-8").rstrip("\n")
    if captured != expected:
        raise AssertionError(_drift_message(golden_path, expected, captured))


async def capture_mockup_golden_screen_text(
    *,
    scope: Literal["repo", "workspace", "user"],
    state_path: Path | None,
    mode: str | None,
    key_sequence: list[str],
    size: tuple[int, int],
) -> str:
    """Mount the TUI through Pilot and capture the target mockup screen.

    The mockup close gate needs the same stable text capture as the snapshot
    suite, but it reaches the target screen from a typed gate row rather than
    a hand-written pytest. This helper launches :class:`EaApp`, waits for the
    bound state to settle, optionally switches to a mode, optionally presses a
    key sequence to reach an overlay / subview, then returns the normalised
    active-screen text.

    Args:
        scope: Launch nav scope (``repo`` / ``workspace`` / ``user``).
        state_path: Fixture or live ``state.json`` path to bind, or ``None``.
        mode: Optional TUI mode to switch to before pressing keys.
        key_sequence: Textual key strings to press after mode switch.
        size: Pilot terminal size as ``(cols, rows)``.

    Returns:
        Normalised ASCII screen text with no trailing newline.
    """
    from eawf.surfaces.tui.app import EaApp

    app = EaApp(scope=scope, state_path=state_path)
    async with app.run_test(size=size) as raw_pilot:
        pilot = cast("Pilot[object]", raw_pilot)
        await settle_screen(pilot)
        if mode is not None:
            await app.switch_mode(mode)
            await settle_screen(pilot)
        for key in key_sequence:
            await pilot.press(key)
            await settle_screen(pilot)
        return await settle_screen(pilot)


def capture_mockup_golden_screen_text_sync(
    *,
    scope: Literal["repo", "workspace", "user"],
    state_path: Path | None,
    mode: str | None,
    key_sequence: list[str],
    size: tuple[int, int],
) -> str:
    """Run :func:`capture_mockup_golden_screen_text` from a sync caller.

    The audit-DSL runner is synchronous, but daemon close-gate calls can happen
    while an event loop is already running. Mirror the existing TUI gate
    pattern: run inline when no loop is active, otherwise offload to a worker
    thread with its own loop.
    """

    def _run() -> str:
        return asyncio.run(
            capture_mockup_golden_screen_text(
                scope=scope,
                state_path=state_path,
                mode=mode,
                key_sequence=key_sequence,
                size=size,
            )
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


def mockup_golden_diff_detail(golden_path: Path, expected: str, captured: str) -> str:
    """Return a capped unified diff detail for a mockup golden mismatch.

    The first unified-diff hunk marker is surfaced in the opening sentence as
    ``region=...`` so close-gate errors name the changed region even when the
    caller truncates the multiline diff.
    """
    diff = difflib.unified_diff(
        expected.splitlines(),
        captured.splitlines(),
        fromfile=f"{golden_path.name} (expected)",
        tofile=f"{golden_path.name} (actual)",
        lineterm="",
    )
    capped = list(itertools.islice(diff, _DRIFT_DIFF_MAX_LINES + 1))
    if len(capped) > _DRIFT_DIFF_MAX_LINES:
        capped[_DRIFT_DIFF_MAX_LINES] = f"... (diff truncated at {_DRIFT_DIFF_MAX_LINES} lines)"
    region = next((line for line in capped if line.startswith("@@")), "whole-file")
    first_change = next(
        (
            line
            for line in capped
            if (line.startswith("-") and not line.startswith("---"))
            or (line.startswith("+") and not line.startswith("+++"))
        ),
        "none",
    )
    body = "\n".join(capped)
    return (
        f"mockup golden mismatch for {golden_path.name!r}: "
        f"region={region} first_change={first_change}\n{body}"
    )


def _drift_message(golden_path: Path, expected: str, captured: str) -> str:
    """Build the drift ``AssertionError`` message with a unified diff.

    Args:
        golden_path: Path to the golden fixture that drifted.
        expected: The golden text (the ``---`` side of the diff).
        captured: The live, normalised capture (the ``+++`` side).

    Returns:
        A multi-line message: a one-line header naming the fixture and the
        regen hatch, followed by a line-tagged unified diff capped at
        :data:`_DRIFT_DIFF_MAX_LINES` lines.
    """
    diff = difflib.unified_diff(
        expected.splitlines(),
        captured.splitlines(),
        fromfile=f"{golden_path.name} (expected)",
        tofile=f"{golden_path.name} (actual)",
        lineterm="",
    )
    capped = list(itertools.islice(diff, _DRIFT_DIFF_MAX_LINES + 1))
    if len(capped) > _DRIFT_DIFF_MAX_LINES:
        capped[_DRIFT_DIFF_MAX_LINES] = f"... (diff truncated at {_DRIFT_DIFF_MAX_LINES} lines)"
    body = "\n".join(capped)
    return (
        f"snapshot drift for {golden_path.name!r}; "
        f"regenerate with {SNAPSHOT_REGEN_ENV}=1 uv run pytest tests/snapshots/tui/\n"
        f"{body}"
    )


__all__ = [
    "SNAPSHOT_REGEN_ENV",
    "assert_screen_snapshot",
    "capture_mockup_golden_screen_text",
    "capture_mockup_golden_screen_text_sync",
    "capture_screen_text",
    "mockup_golden_diff_detail",
    "normalize_snapshot",
    "settle_screen",
]
