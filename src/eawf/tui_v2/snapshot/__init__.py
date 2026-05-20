"""C06 snapshot + cast harness for the ``tui_v2`` operator surface.

Two deterministic, CI-stable capture surfaces driven by Textual's
``App.run_test()`` Pilot:

* :mod:`~eawf.tui_v2.snapshot.pilot_harness` — captures a running
  screen's rendered terminal as **plain ASCII text** (one line per
  terminal row, trailing whitespace trimmed). Per the C06 brief Q-new1
  OVERRIDE, snapshots are ASCII text — not SVG / binary — so the goldens
  are diffable in code review, scrub-safe (no machine paths or PII leak
  through a rendered pane), and free of the Python-version + Textual-
  version byte-drift that SVG ``export_screenshot`` output carries.
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

This module closes the C06 TUI band. The migration verdict for the
legacy ``src/eawf/tui/`` parallel tree (per the C06 brief §7.5-§7.7 and
the Codex C06-I010 ratification) is recorded here as the band's
single source of truth:

1. **The legacy ``src/eawf/tui/`` tree STAYS through the close of this
   phase.** It is not deleted in this band — it remains a parallel,
   working surface so an operator who hits a regression in ``tui_v2``
   can fall back.
2. **Bare ``eawf`` defaults to ``tui_v2``.** The interactive bare-command
   dispatch (:func:`eawf.cli.app._dispatch_tui`) launches the Textual
   :class:`~eawf.tui_v2.app.EaApp`; this flip already landed earlier in
   the band and is confirmed by this band's tests.
3. **``EAWF_TUI_LEGACY=1`` is the escape hatch.** Setting it routes the
   interactive bare-``eawf`` path back to the legacy
   :func:`eawf.tui.app.run_tui` for one alpha cycle. The non-TTY /
   ``--plain`` / ``--no-input`` fallback continues to use the legacy
   deterministic status renderer regardless of the flag.
4. **Deletion of ``src/eawf/tui/`` is DEFERRED to a follow-up phase.**
   The brief's §7.5-W04 "delete the legacy tree" step does **not** run in
   this phase — the deletion (and the salvage of any shared constants)
   moves to a later phase per the C06-I010 verdict, keeping the
   defense-in-depth fallback available for the alpha cycle.
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
