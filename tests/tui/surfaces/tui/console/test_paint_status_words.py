"""The painter's state words: a word is coloured only where it states a state.

``passed`` is a verdict as a cell of its own and prose anywhere else, and an exception
bucket keeps its severity whether its count is known, unknown or estimated.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.console.paint import Part, paint


def _surface(row: str, text: str) -> str | None:
    return next(s.surface for s in paint(row, Part.BODY) if s.text.strip() == text)


def _painted(row: str, text: str) -> bool:
    return any(s.text.strip() == text and s.surface for s in paint(row, Part.BODY))


# ---------- V-02: passed is a verdict only as a cell ----------


@pytest.mark.parametrize(
    "row",
    [
        " CHECKS       ? unknown · no check outcome is recorded, so none reads passed",
        "│  budget passed      once per revision   budget notify fraction   │",
        " CHECKS       0 checks · 0 failed · 0 warn · 0 unknown · 0 passed",
    ],
)
def test_v02_passed_in_prose_is_not_coloured(row: str) -> None:
    assert not _painted(row, "passed")


@pytest.mark.parametrize(
    "row",
    [
        "   policy gate                       passed     receipt EVT-3301",
        "   artifact build                    passed",
    ],
)
def test_v02_passed_as_a_verdict_cell_is_ok(row: str) -> None:
    assert _surface(row, "passed") == "ok"


# ---------- V-13: a bucket keeps its colour whatever its count ----------


@pytest.mark.parametrize(
    ("row", "bucket", "surface"),
    [
        ("│ unknown control outcome   ?", "unknown control outcome", "warn"),
        ("│ lost or stale             ?", "lost or stale", "err"),
        ("│ checking or integrating   ?", "checking or integrating", "info"),
        ("│ failed                    ?", "failed", "err"),
        ("│ running                 ≈39", "running", "ok"),
        ("│ needs operator            3", "needs operator", "warn"),
    ],
)
def test_v13_a_bucket_keeps_its_severity_with_an_unknown_or_estimated_count(
    row: str, bucket: str, surface: str
) -> None:
    assert _surface(row, bucket) == surface


def test_v13_a_bucket_word_in_prose_without_a_count_stays_plain() -> None:
    assert not _painted("   the lease failed · retry pending", "failed")
