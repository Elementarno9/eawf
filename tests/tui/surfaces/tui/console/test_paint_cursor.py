"""The painter's cursor: which row is grounded and which caret takes the accent.

The cursor row is the one whose pane leads with the caret, directly or after its label;
a caret anywhere else in a row is a separator in prose and is painted as prose.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.console.paint import Part, paint


def _grounded(row: str) -> bool:
    return any(s.ground == "cursor" for s in paint(row, Part.BODY))


def _caret_painted(row: str) -> bool:
    return any(s.text == "▸" and s.surface == "caret" for s in paint(row, Part.BODY))


# ---------- V-07: the cursor row is grounded, the section strip is not ----------


@pytest.mark.parametrize(
    "row",
    [
        "  BATCHES    ▸ BAT-0101 · ACTIVE",
        "  TASKS      ▸ EAWF-0101 · RUNNING  Close the console route registry",
        "             ▸ BAT-0102 · PLANNED",
        "   ▸ MLS-0100 Close out P36        COMPLETED",
    ],
)
def test_v07_a_cursor_row_is_grounded_whether_or_not_a_label_leads_it(row: str) -> None:
    assert _grounded(row)


@pytest.mark.parametrize(
    "row",
    [
        "               [glance]  try   changes   evidence   risks   raw",
        "  BATCHES      BAT-0101 · ACTIVE",
        " LAYER ▸ repo · in force from built-in",
    ],
)
def test_v07_a_row_without_a_leading_caret_is_not_grounded(row: str) -> None:
    assert not _grounded(row)


# ---------- V-09: only a leading caret takes the accent ----------


@pytest.mark.parametrize(
    "row",
    [
        " 6 categories · 36 sections · in EXECUTION ▸ planning",
        " LAYER ▸ repo · in force from built-in · effective revision 345",
    ],
)
def test_v09_a_caret_in_prose_is_not_painted_as_the_cursor(row: str) -> None:
    assert not _caret_painted(row)


@pytest.mark.parametrize(
    "row",
    [
        "   ▸ MLS-0100 Close out P36",
        "  BATCHES    ▸ BAT-0101 · ACTIVE",
        "▸ planning       │   – max_parallel_waves     4",  # noqa: RUF001
        "  agents         │ ▸ – approval               ask",  # noqa: RUF001
        "│ ▸needs permission   yes                 attention projection   │",
    ],
)
def test_v09_the_caret_a_pane_leads_with_takes_the_accent(row: str) -> None:
    assert _caret_painted(row)
