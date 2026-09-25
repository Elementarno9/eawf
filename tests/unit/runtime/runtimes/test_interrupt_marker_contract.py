"""Pin the vendor transcript's interrupt-marker contract against a recorded shape.

The interrupt markers are Claude Code's wording, not ours: when the operator hits
Esc, Claude writes a marker row immediately before the ``turn_duration`` row that
closes the turn, and that row carries the turn's WALL CLOCK. If a Claude Code
release rewords the marker, the parser stops recognising it and every interrupted
turn is silently booked as work -- hours apiece. Nothing else would notice, since
an inflated duration is an increase and reads as a productive session.

The fixture (``fixtures/claude_interrupt_transcript.jsonl``) is synthesized with
the key set and nesting of real transcript rows and neutral content: one completed
turn, three interrupted turns (a list-content marker, a string-content marker, and
an ``agents_killed`` row), and a final completed turn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.runtimes.claude.transcript_counters import (
    MEASURE_VERSION,
    _is_interrupt_signal,
    _is_turn_duration_row,
    aggregate_transcript_counters,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "claude_interrupt_transcript.jsonl"

#: Which turns in the fixture were interrupted, in order.
_EXPECTED_INTERRUPTS = [False, True, True, True, False]

#: The two completed turns' figures; every interrupted turn is excluded.
_COMPLETED_MS = 30_000 + 20_000


def _rows(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _interrupt_pattern(rows: list[dict[str, Any]]) -> list[bool]:
    """Return, per ``turn_duration`` row, whether its predecessor marks an interrupt."""
    return [
        index > 0 and _is_interrupt_signal(rows[index - 1])
        for index, row in enumerate(rows)
        if _is_turn_duration_row(row)
    ]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "altered.jsonl"
    path.write_text(text, encoding="utf-8")
    return path


def test_interrupt_marker_recognised_on_recorded_fixture() -> None:
    rows = _rows(_FIXTURE.read_text(encoding="utf-8"))

    assert _interrupt_pattern(rows) == _EXPECTED_INTERRUPTS


def test_interrupt_marker_excludes_interrupted_turns_from_duration() -> None:
    counters = aggregate_transcript_counters(_FIXTURE)

    assert counters is not None
    assert counters.api_duration_ms == _COMPLETED_MS
    assert counters.measure_version == MEASURE_VERSION


@pytest.mark.parametrize(
    ("original", "altered", "turn_index"),
    [
        ("[Request interrupted by user for tool use]", "[Request halted for tool use]", 1),
        ('"[Request interrupted by user]"', '"[Request halted]"', 2),
        ('"subtype": "agents_killed"', '"subtype": "agents_reaped"', 3),
    ],
)
def test_interrupt_marker_altered_fixture_reds(
    tmp_path: Path, original: str, altered: str, turn_index: int
) -> None:
    """A reworded marker is no longer recognised, and its turn's wall clock is booked.

    This is the red half of the contract: the check above passes only while the
    parser recognises Claude's current wording, so a vendor rewording shows up as
    this very diff between the recorded and the altered fixture.
    """
    text = _FIXTURE.read_text(encoding="utf-8")
    assert text.count(original) == 1

    rows = _rows(text.replace(original, altered))
    pattern = _interrupt_pattern(rows)
    counters = aggregate_transcript_counters(_write(tmp_path, text.replace(original, altered)))

    assert pattern != _EXPECTED_INTERRUPTS
    assert pattern[turn_index] is False
    assert counters is not None
    assert counters.api_duration_ms > _COMPLETED_MS


def test_interrupt_marker_fixture_is_scrubbed() -> None:
    """The fixture carries neither a working directory nor a home path."""
    text = _FIXTURE.read_text(encoding="utf-8")

    assert '"cwd"' not in text
    assert "/" + "Users" + "/" not in text
    assert "/" + "home" + "/" not in text


def test_measure_version_bumped_for_as_of_reading() -> None:
    """Splitting a straddling turn changed what the counters mean, so the version moved."""
    assert MEASURE_VERSION == 8
