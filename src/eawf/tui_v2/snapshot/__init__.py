"""Snapshot + cast harness for the ``tui_v2`` operator surface.

Two deterministic, CI-stable capture surfaces driven by Textual's
``App.run_test()`` Pilot:

* :mod:`~eawf.tui_v2.snapshot.pilot_harness` — captures a running
  screen's rendered terminal as **plain ASCII text** (one line per
  terminal row, trailing whitespace trimmed). Snapshots are ASCII text
  — not SVG / binary — so the goldens are diffable in code review,
  scrub-safe (no machine paths or PII leak through a rendered pane), and
  free of the Python-version + Textual-version byte-drift that SVG
  ``export_screenshot`` output carries.
* :mod:`~eawf.tui_v2.snapshot.asciinema` — composes an asciinema v2
  cast of a scripted TUI session for docs / demos, using the same
  ASCII-text capture at a fixed monotonic cadence (no real-time terminal
  recording) so the cast is byte-stable across machines.

Both surfaces read the rendered screen via the active screen's
compositor (:meth:`textual.screen.Screen.render_strips`), which yields
the topmost screen — a base scope screen or a stacked modal overlay
alike — so a single capture path serves screen + overlay fixtures.

Legacy ``src/eawf/tui/`` migration verdict
------------------------------------------

The TUI band is closed and ``tui_v2`` is the sole TUI surface:

1. **The legacy ``src/eawf/tui/`` tree has been REMOVED.** Per the
   operator decision to defer the legacy TUI entirely, the parallel
   Rich tree (and its ``EAWF_TUI_LEGACY=1`` escape hatch) is gone; the
   content stays recoverable in git history.
2. **Bare ``eawf`` launches ``tui_v2``.** The interactive bare-command
   dispatch (:func:`eawf.cli.app._dispatch_tui`) launches the Textual
   :class:`~eawf.tui_v2.app.EaApp`.
3. **The non-TTY / ``--plain`` / ``--no-input`` fallback uses the
   ``tui_v2`` deterministic status emitter**
   (:func:`eawf.tui_v2.offline.emit_status`); the workspace dashboard
   text frame is rendered by :func:`eawf.tui_v2.offline.offline_render`.
"""

from __future__ import annotations

from eawf.tui_v2.snapshot.asciinema import record_cast, write_cast
from eawf.tui_v2.snapshot.pilot_harness import (
    SNAPSHOT_REGEN_ENV,
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)

__all__ = [
    "SNAPSHOT_REGEN_ENV",
    "assert_screen_snapshot",
    "capture_screen_text",
    "normalize_snapshot",
    "record_cast",
    "settle_screen",
    "write_cast",
]
