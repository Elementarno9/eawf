"""Plain-text capture of a mounted Textual screen.

Captures a running Textual screen's rendered terminal as **plain text**, driven
by Textual's ``App.run_test()`` Pilot, rather than the SVG
``App.export_screenshot`` output: the text is diffable in review, carries
exactly what an operator sees (so the secrets gate inspects the surface that
ships) and does not drift with font metrics across Textual versions.

The capture reads the active screen's compositor
(:meth:`textual.screen.Screen.render_strips`), which renders the topmost screen
on the stack, a base screen or a pushed modal overlay alike.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.app import App

#: Env var that, when set to ``"1"``, makes a golden test (re)write its fixture
#: from the live capture instead of comparing against it. CI runs **without**
#: it, so a drift fails the build.
SNAPSHOT_REGEN_ENV: str = "EAWF_SNAPSHOT_REGEN"


def capture_screen_text(app: App[object]) -> str:
    """Capture the app's active screen as plain text.

    Renders the topmost screen on the stack row by row via its compositor,
    joining the per-row text with newlines. Trailing whitespace is trimmed per
    row, and trailing all-blank rows are dropped: a modal overlay renders only
    its own box, and the count of empty terminal rows below it is not part of
    the frame.

    The app MUST already be mounted and settled: call after
    ``await pilot.pause()`` inside an ``async with app.run_test()`` block.

    Args:
        app: The live :class:`~textual.app.App` under a Pilot harness.

    Returns:
        The rendered screen as a newline-joined text block (no trailing blank
        rows, no trailing newline).
    """
    compositor = app.screen._compositor
    rows = [strip.text.rstrip() for strip in compositor.render_strips()]
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows)
