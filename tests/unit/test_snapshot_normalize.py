"""The volatile cells :func:`normalize_snapshot` neutralises before a golden compares.

A captured TUI frame carries three cells that are NOT a function of the bound
fixture state: the header wall-clock, the environment-dependent daemon-degraded
banner, and the pulse dot's phase. Each would drift a committed golden for a
reason that has nothing to do with what the pane renders -- the time of day, the
host's daemon, or how fast the runner got from mount to capture.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.render.snapshot_normalize import normalize_snapshot

pytestmark = pytest.mark.unit

#: The bright / dim pulse pair the running dot and the footer heartbeat fade
#: between on a timer.
_LIT = "•"
_DIM = "◦"


def test_normalize_snapshot_collapses_the_dim_pulse_phase() -> None:
    """A dim-phase capture normalises to the same text as a bright-phase one.

    The dot's phase is decided by when the timer last fired, so two captures of
    an identical pane differ only in this glyph. Every committed golden holds
    the bright phase; a slow host that catches the dim one must not redden it.
    """
    lit_frame = f"DISPATCH\nNOW\n  {_LIT} W01  —  — no data\nNEXT  —"
    dim_frame = f"DISPATCH\nNOW\n  {_DIM} W01  —  — no data\nNEXT  —"

    assert normalize_snapshot(dim_frame) == normalize_snapshot(lit_frame)
    assert _DIM not in normalize_snapshot(dim_frame)


def test_normalize_snapshot_rewrites_the_wall_clock() -> None:
    """The header clock becomes a fixed placeholder, so goldens survive the day."""
    assert "HH:MM UTC" in normalize_snapshot("Eä  workspace        16:04 UTC")
    assert "16:04 UTC" not in normalize_snapshot("Eä  workspace        16:04 UTC")


def test_normalize_snapshot_drops_the_daemon_banner() -> None:
    """The daemon-degraded banner is environment-dependent, so it is dropped.

    It also embeds the runtime socket path, which must never reach a committed
    golden.
    """
    framed = "daemon socket unavailable; socket=/tmp/x EAWF_RUNTIME_DIR=/tmp/y\nROADMAP\nP30"

    normalised = normalize_snapshot(framed)

    assert "daemon socket unavailable" not in normalised
    assert "EAWF_RUNTIME_DIR=" not in normalised
    assert normalised == "ROADMAP\nP30"


def test_normalize_snapshot_leaves_a_clean_frame_untouched() -> None:
    """Boundary: a frame with no volatile cell is returned byte-identical."""
    frame = "ROADMAP\nP30  active\nBACKLOG  —"

    assert normalize_snapshot(frame) == frame
