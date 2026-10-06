"""Tests: the bounded line tail a live-output chunk carries.

:func:`capture_output_lines` splits a spawn's captured text into the bounded
TAIL of non-empty lines; the ring cap keeps the freshest output.
"""

from __future__ import annotations

import pytest

from eawf.runtime.daemon.dispatch_runner import (
    AGENT_OUTPUT_LINE_CAP,
    capture_output_lines,
)

pytestmark = pytest.mark.integration


def test_capture_output_lines_splits_and_drops_blanks() -> None:
    """Non-empty lines are kept in order; blank lines are dropped."""
    text = "first line\n\n  \nsecond line\nthird line\n"
    assert capture_output_lines(text) == ["first line", "second line", "third line"]


def test_capture_output_lines_keeps_freshest_tail_under_cap() -> None:
    """When over the cap only the LAST cap lines (the freshest tail) survive."""
    text = "\n".join(f"line {n}" for n in range(AGENT_OUTPUT_LINE_CAP + 50))
    kept = capture_output_lines(text)
    assert len(kept) == AGENT_OUTPUT_LINE_CAP
    # The freshest tail is kept (the last line is present, the first scrolled off).
    assert kept[-1] == f"line {AGENT_OUTPUT_LINE_CAP + 49}"
    assert kept[0] == f"line {50}"


def test_capture_output_lines_empty_is_empty() -> None:
    """An all-blank / empty text yields no lines."""
    assert capture_output_lines("") == []
    assert capture_output_lines("   \n\n  ") == []
