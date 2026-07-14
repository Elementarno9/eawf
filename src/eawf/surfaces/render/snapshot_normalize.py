"""Pure snapshot-text normalisation, free of any Textual dependency.

The TUI snapshot harness
(:mod:`eawf.surfaces.tui.snapshot.pilot_harness`) captures a live
Textual screen and normalises two volatile cells before comparing
against a golden ``.txt`` fixture: the header wall-clock and the
environment-dependent daemon-degraded banner.

That normalisation is a pure string transform with no Textual coupling,
so it lives here -- in the textual-free ``render`` layer -- rather than
inside the Pilot harness. A second caller, the ``/mockup`` golden-capture
seam, needs the same transform at plan time but must NOT pull Textual
(it runs in a CI / non-TUI authoring context). Hosting the pure function
here keeps a single implementation: :mod:`pilot_harness` re-imports
:func:`normalize_snapshot` from this module instead of duplicating it.
"""

from __future__ import annotations

import re

#: Matches the header wall-clock cell (``16:04 UTC``) so it can be
#: neutralised to a fixed placeholder -- the one non-deterministic element
#: of the rendered chrome (everything else derives from fixture state).
_CLOCK_RE = re.compile(r"\d{2}:\d{2} UTC")

#: Stable replacement for the wall-clock cell.
_CLOCK_PLACEHOLDER: str = "HH:MM UTC"

#: The dim (off-phase) pulse glyph. The DISPATCH band's running dot and the
#: footer heartbeat fade bright<->dim on a timer, so a captured frame holds
#: whichever phase the timer happened to be in when the screen was read -- a
#: coin flip decided by how fast the host ran, not by what the pane renders.
#: Collapsing it to the bright glyph makes the capture phase-independent. The
#: dim glyph appears nowhere else in the rendered chrome, so the rewrite cannot
#: mask any other content.
_PULSE_DIM_GLYPH: str = "\u25e6"

#: The bright (on-phase) pulse glyph every committed golden was captured on.
_PULSE_LIT_GLYPH: str = "\u2022"

#: Matches the daemon-degraded banner the app top-docks when the daemon
#: socket is unavailable (``daemon socket unavailable; polling state.json |
#: socket=<path> EAWF_RUNTIME_DIR=<...>``). Its PRESENCE is environment-
#: dependent (a CI runner with no live daemon renders it; a dev box with the
#: daemon up does not), so it would drift a golden across machines; it also
#: embeds the runtime socket PATH, which must never land in a committed
#: golden. The banner line(s) are dropped from the normalised capture so
#: snapshots assert their own content, not the ambient daemon state.
_DAEMON_BANNER_MARKERS: tuple[str, ...] = (
    "daemon socket unavailable",
    "EAWF_RUNTIME_DIR=",
)


def normalize_snapshot(text: str) -> str:
    """Neutralise the non-deterministic cells of a captured frame.

    Three volatile elements are neutralised:

    * the header wall-clock (``HH:MM UTC``), rewritten to a fixed
      placeholder so goldens stay byte-stable across the time of day;
    * the daemon-degraded banner the app top-docks when the daemon socket
      is unavailable -- its presence is environment-dependent (rendered on
      a CI runner with no live daemon, absent on a dev box with one up) and
      it embeds the runtime socket path, so its line(s) are dropped; and
    * the pulse dot's phase. The running dot and the footer heartbeat fade
      bright<->dim on a timer, so the captured phase depends on how fast the
      host ran between mount and capture -- not on what the pane renders. The
      dim glyph collapses to the bright one, which is the phase every
      committed golden holds.

    Everything else in the frame is a deterministic function of the bound
    fixture state.

    Args:
        text: A captured screen text block.

    Returns:
        The text with volatile cells replaced by stable placeholders and
        the env-dependent daemon banner removed.
    """
    clocked = _CLOCK_RE.sub(_CLOCK_PLACEHOLDER, text)
    clocked = clocked.replace(_PULSE_DIM_GLYPH, _PULSE_LIT_GLYPH)
    if not any(marker in clocked for marker in _DAEMON_BANNER_MARKERS):
        # Banner-free: return unchanged so the byte-shape (incl. any trailing
        # newline) is identical to the pre-banner-strip behaviour.
        return clocked
    kept = [
        line
        for line in clocked.splitlines()
        if not any(marker in line for marker in _DAEMON_BANNER_MARKERS)
    ]
    result = "\n".join(kept)
    if clocked.endswith("\n"):
        result += "\n"
    return result


__all__ = ["normalize_snapshot"]
