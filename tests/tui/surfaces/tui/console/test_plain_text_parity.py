"""Plain mode is the console's own frame in the ASCII allocation, not a second layout.

The parity oracle is the tracked golden contract read through its ASCII twins: every
recorded frame, normalised to what the port renders and then twinned glyph for glyph, is
what plain mode must return for that frame's setup and keys. Holding the two together is
what keeps a non-interactive caller, a screen reader and an export reading exactly what
the frame says, in the order it says it, at the same H rows of W cells.

Plain mode renders through the frame renderer rather than through the toolkit, so this
runs over the whole frame census in a second rather than as a bounded sample. The rack is
the one thing it cannot reproduce: a toast is a timed notice the console sweeps off the
frame after its dwell, and the rack-bearing states are named here so a new one cannot
quietly slip out of the gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.harness import Contract, FrameState, load_contract
from eawf.surfaces.tui.console.normalisation import Normaliser, load_map
from eawf.surfaces.tui.console.plain import (
    ASCII_TWINS,
    OFFLINE_SNAPSHOT,
    ascii_twin,
    plain_rows,
    plain_text,
    render_plain,
)
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from eawf.surfaces.tui.console.width import cell_len

TESTS_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_ROOT = TESTS_ROOT / "fixtures" / "console" / "golden"
ESCAPE = "\x1b"

#: The recorded states whose frame stands under a toast; plain mode raises no toast.
RACK_STATES: tuple[str, ...] = (
    "toast/one@80",
    "toast/three@80",
    "toast/cap@80",
    "toast/one@120",
    "toast/three@120",
    "toast/cap@120",
    "toast/one@160",
    "toast/three@160",
    "toast/cap@160",
)


@pytest.fixture(scope="module")
def contract() -> Contract:
    """Return the tracked contract, loaded strict."""
    return load_contract(GOLDEN_ROOT / "sequences")


@pytest.fixture(scope="module")
def normaliser(contract: Contract) -> Normaliser:
    """Return the normaliser that turns a pack frame into the port's expected frame."""
    return Normaliser(load_map(GOLDEN_ROOT / "normalisation-map.json"), contract.frames_by_id())


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the registers plain mode renders from."""
    return load_fixture(GOLDEN_ROOT / "fixture")


@pytest.fixture(scope="module")
def parity_states(contract: Contract) -> tuple[FrameState, ...]:
    """Return every recorded state plain mode reproduces: all of them but the rack ones."""
    return tuple(state for state in contract.states if not state.rack)


def _twin_golden(normaliser: Normaliser, state: FrameState) -> list[str]:
    """Return the ASCII twin of the port's expected frame for ``state``."""
    return plain_rows(normaliser.expected(state.id, state.frame).split("\n"))


def _render(fixture: Fixture, state: FrameState) -> list[str]:
    """Render ``state`` in plain mode under the connection value it recorded."""
    return render_plain(
        fixture,
        state.setup,
        keys=state.keys,
        conn=state.setup.conn or "LIVE",
        verbose=state.kind == "verbose",
    )


def test_the_rack_states_are_exactly_the_ones_left_out(
    contract: Contract, parity_states: tuple[FrameState, ...]
) -> None:
    left_out = tuple(state.id for state in contract.states if state.rack)
    assert left_out == RACK_STATES
    assert len(parity_states) == len(contract.states) - len(RACK_STATES)


def test_plain_mode_matches_the_ascii_twin_goldens(
    fixture: Fixture, normaliser: Normaliser, parity_states: tuple[FrameState, ...]
) -> None:
    failed: list[str] = []
    for state in parity_states:
        want = _twin_golden(normaliser, state)
        got = _render(fixture, state)
        if want == got:
            continue
        row = next((i for i, (a, b) in enumerate(zip(want, got, strict=False)) if a != b), -1)
        failed.append(f"{state.id}: row {row}\n want |{want[row]}|\n  got |{got[row]}|")
    assert not failed, "\n".join(failed)


def test_plain_mode_returns_the_recorded_rows_of_the_recorded_width(
    fixture: Fixture, parity_states: tuple[FrameState, ...]
) -> None:
    off_grid: list[str] = []
    for state in parity_states:
        w, h = SIZES[state.setup.size]
        rows = _render(fixture, state)
        if len(rows) != h:
            off_grid.append(f"{state.id}: {len(rows)} rows, not {h}")
        off_grid += [
            f"{state.id}: row {i} is {cell_len(row)} cells, not {w}"
            for i, row in enumerate(rows)
            if cell_len(row) != w
        ]
    assert not off_grid, "\n".join(off_grid)


def test_plain_mode_carries_no_escape_sequence_and_no_wide_glyph(
    fixture: Fixture, parity_states: tuple[FrameState, ...]
) -> None:
    for state in parity_states[:20]:
        rows = _render(fixture, state)
        assert all(ESCAPE not in row for row in rows)
        assert all(row.isascii() for row in rows)


def test_plain_mode_defaults_to_the_offline_snapshot_connection(fixture: Fixture) -> None:
    rows = render_plain(fixture, SessionSetup(route="scope.home"))
    assert OFFLINE_SNAPSHOT in rows[0]


def test_plain_mode_rejects_a_connection_value_that_does_not_exist(fixture: Fixture) -> None:
    with pytest.raises(ValueError, match="is not a connection value"):
        render_plain(fixture, SessionSetup(), conn="UNPLUGGED")


def test_every_ascii_twin_is_one_ascii_cell() -> None:
    assert ASCII_TWINS
    wide = [g for g, twin in ASCII_TWINS.items() if not (twin.isascii() and cell_len(twin) == 1)]
    assert wide == []


def test_ascii_twin_leaves_ascii_alone_and_twins_the_rest() -> None:
    assert ascii_twin("") == ""
    assert ascii_twin("plain") == "plain"
    assert ascii_twin("─│") == "-|"


def test_ascii_twin_rejects_a_glyph_it_has_no_twin_for() -> None:
    with pytest.raises(ValueError, match="glyphs with no ASCII twin: 🌵"):
        ascii_twin("🌵")


def test_plain_rows_reject_an_escape_sequence() -> None:
    with pytest.raises(ValueError, match="row 1 carries an escape sequence"):
        plain_rows(["ok", f"bad{ESCAPE}[0m"])


def test_plain_rows_of_no_rows_is_empty() -> None:
    assert plain_rows([]) == []
    assert plain_text([]) == ""


def test_plain_text_writes_one_row_per_line() -> None:
    assert plain_text(["a─", "b│"]) == "a-\nb|"
