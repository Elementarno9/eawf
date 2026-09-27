"""The frame grammar holds on every frame the console draws, at all three sizes.

Each sweep renders every recorded state of the tracked contract through the console's own
frame composer -- the setup, then its keys, re-rendering after each as the app does -- and
reads the rows that come back, so a sweep proves the console rather than the pack it was
drawn from. The toast rack is left empty: a toast is painted over the body and is not a
row a renderer composes.

The sweeps prove CON-154 (the keybar is the only place a key is promised), CON-155 (a
frame states facts, never instructions), CON-156 (the caret sits against its row),
CON-157 (a group title is not a row), CON-158 (a value keeps its form and an id never
gives way), CON-109 (a long column value is shortened at a word) and CON-115 (the chassis
is built before, and never reaches into, the routes). Each sweep is paired with a seeded
defect it must red on, so a sweep that silently stopped matching cannot pass.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from eawf.surfaces.tui import console as console_package
from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import CARET, Grid, Table, View, snap_caret
from eawf.surfaces.tui.console.harness import load_contract
from eawf.surfaces.tui.console.keybar import GAP, MARGIN
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from eawf.surfaces.tui.console.width import cell_len, clip_words

TESTS_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_ROOT = TESTS_ROOT / "fixtures" / "console" / "golden"
CONSOLE_ROOT = Path(console_package.__file__).resolve().parent


class _Host:
    """A held clock and no quit: what a render outside the app dispatches against."""

    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        return self._clock

    def quit(self) -> None:
        """Do nothing: a sweep has no session to end."""


def render_state(fixture: Fixture, setup: SessionSetup, keys: Sequence[str] = ()) -> list[str]:
    """Return the frame the console composes for ``setup`` after ``keys``."""
    host = _Host()
    session = Session()
    session.reset(
        setup, settings_section_order=fixture.settings.section_order, now=host.clock.now()
    )
    w, h = SIZES[session.size]
    view = View(session=session, fixture=fixture, w=w, h=h, held=True)
    rows = compose_frame(view)
    for key in keys:
        dispatch(Ctx(session=session, fixture=fixture, host=host, w=w, h=h, verbose=False), key)
        rows = compose_frame(view)
    return rows


def _render_contract() -> dict[str, list[str]]:
    fixture = load_fixture(GOLDEN_ROOT / "fixture")
    contract = load_contract(GOLDEN_ROOT / "sequences")
    return {
        state.id: render_state(fixture, state.setup, state.keys)
        for state in contract.states
        if not state.rack
    }


#: Every recorded state the console draws, by frame id, rendered once for every sweep.
RENDERED: dict[str, list[str]] = _render_contract()


def body_rows(rows: Sequence[str]) -> list[str]:
    """Return the rows between the header and the keybar."""
    return list(rows[1:-1])


def keybar_pairs(bar: str) -> list[tuple[str, str]]:
    """Return the ``(token, label)`` pairs a keybar row advertises."""
    pairs: list[tuple[str, str]] = []
    for piece in bar[MARGIN:].split(GAP):
        token, _, label = piece.strip().partition(" ")
        if token and label:
            pairs.append((token, label))
    return pairs


def test_the_sweeps_cover_every_rack_free_state_at_three_sizes() -> None:
    assert len(RENDERED) >= 250
    widths = {len(rows[0]) for rows in RENDERED.values()}
    assert widths == {w for w, _h in SIZES}


# ---------- CON-154: the keybar is the only place a key is promised ----------


def restated_keys(rows: Sequence[str]) -> list[str]:
    """Return every keybar pair a row above the keybar restates as ``<key> <label>``."""
    found: list[str] = []
    for token, label in keybar_pairs(rows[-1]):
        pair = re.compile(rf"(?<!\S){re.escape(token)} +{re.escape(label)}(?![\w-])")
        found.extend(f"{token} {label}" for row in body_rows(rows) if pair.search(row))
    return found


def test_con_154_no_frame_restates_a_keybar_pair_above_the_keybar() -> None:
    restated = {fid: found for fid, rows in RENDERED.items() if (found := restated_keys(rows))}
    assert restated == {}


def test_con_154_the_sweep_reds_on_a_seeded_restatement() -> None:
    rows = [" header", " ↑↓ picks · Enter drill opens it", " ↑↓ row   Enter drill   Esc back"]
    assert restated_keys(rows) == ["Enter drill"]


def test_con_154_a_row_may_state_what_a_key_does_to_this_entity() -> None:
    rows = [" header", " Enter renders the report; nothing is written.", " Enter open   Esc back"]
    assert restated_keys(rows) == []


# ---------- CON-155: a frame states facts, never instructions ----------

_INSTRUCTION = re.compile(
    r"\b(?:press (?:Enter|Esc|Tab|a key|any key|↑↓)|click (?:a|the|to)\b|each one opens"
    r"|to drill\b|↑↓ moves|to select it|use the arrows)",
    re.IGNORECASE,
)


def instructions(rows: Sequence[str]) -> list[str]:
    """Return every body row that narrates what a key, a click or the interface does."""
    return [row.strip() for row in body_rows(rows) if _INSTRUCTION.search(row)]


def test_con_155_no_frame_narrates_the_interface() -> None:
    narrated = {fid: found for fid, rows in RENDERED.items() if (found := instructions(rows))}
    assert narrated == {}


@pytest.mark.parametrize(
    "row", [" 2 · each one opens", " press Enter to drill", " click a row to select it"]
)
def test_con_155_the_sweep_reds_on_a_seeded_instruction(row: str) -> None:
    assert instructions([" header", row, " Esc back"]) == [row.strip()]


def test_con_155_a_statement_of_fact_about_the_entity_stays() -> None:
    rows = [" header", " Migration never auto-applies; the command is shown.", " Esc back"]
    assert instructions(rows) == []


# ---------- CON-156: the caret sits against the row it marks ----------

_DETACHED = re.compile(rf"{CARET} {{2,}}\S")


def detached_carets(rows: Sequence[str]) -> list[str]:
    """Return every body row whose caret is separated from its text by a gutter of blanks."""
    return [row for row in body_rows(rows) if _DETACHED.search(row)]


def test_con_156_no_frame_draws_a_caret_clear_of_its_row() -> None:
    detached = {fid: found for fid, rows in RENDERED.items() if (found := detached_carets(rows))}
    assert detached == {}


def test_con_156_the_sweep_reds_on_a_seeded_gutter() -> None:
    assert detached_carets(["h", " ▸          EAWF-0091  Coalesce", "k"]) != []


@pytest.mark.parametrize(
    ("row", "snapped"),
    [
        (" ▸          EAWF-0091", "          ▸ EAWF-0091"),
        (" ▸  x", "  ▸ x"),
        (" ▸ x", " ▸ x"),
        ("   x", "   x"),
        ("", ""),
    ],
)
def test_con_156_snap_caret_moves_the_caret_and_keeps_the_width(row: str, snapped: str) -> None:
    assert snap_caret(row) == snapped
    assert cell_len(snap_caret(row)) == cell_len(row)


@pytest.mark.parametrize("mark", [2, 4])
def test_con_156_a_row_taking_the_caret_keeps_its_width(mark: int) -> None:
    table = Table([12, 10, 0], mark)
    cells = ["EAWF-0091", "DRAFT", "needs owner"]
    assert cell_len(table.row(cells, cur=True)) == cell_len(table.row(cells, cur=False))
    grid = Grid([12, 10, 0], mark)
    assert cell_len(grid.row(cells, True, 60)) == cell_len(grid.row(cells, False, 60))


def test_con_156_the_backlog_states_status_in_a_column_not_a_readout() -> None:
    rows = RENDERED["route/backlog@80"]
    assert "STATUS" in rows[3]
    assert not any("DEFINITION MISSING" in row or "PROMOTION" in row for row in rows)


# ---------- CON-157: a group title is not a row ----------

GROUP_TITLES = (
    "DRAFTS",
    "DEFERRED",
    "LOST",
    "FAILED",
    "NEEDS OPERATOR",
    "STALLED",
    "OVER BUDGET",
    "REJECTED",
)
_TITLE = re.compile(rf"^(?P<lead>[ {CARET}]*)(?P<title>{'|'.join(GROUP_TITLES)})(?:  \d+)?(?:  |$)")


def misplaced_titles(rows: Sequence[str]) -> list[str]:
    """Return every group title that wears a caret or sits off column 1."""
    bad: list[str] = []
    for row in body_rows(rows):
        found = _TITLE.match(row)
        if found and found.group("lead") != " ":
            bad.append(row)
    return bad


def test_con_157_every_group_title_sits_flush_at_column_one_without_a_caret() -> None:
    titled = [fid for fid, rows in RENDERED.items() if any(_TITLE.match(r) for r in rows)]
    assert len(titled) >= 6
    bad = {fid: found for fid, rows in RENDERED.items() if (found := misplaced_titles(rows))}
    assert bad == {}


@pytest.mark.parametrize("row", [" ▸ LOST  1", "   DRAFTS            STATUS", "▸DEFERRED"])
def test_con_157_the_sweep_reds_on_a_title_with_a_gutter_or_a_caret(row: str) -> None:
    assert misplaced_titles(["h", row, "k"]) == [row]


@pytest.mark.parametrize(("route", "prefix"), [("attention", "ACT-"), ("backlog", "EAWF-")])
def test_con_157_the_cursor_never_lands_on_a_title(route: str, prefix: str) -> None:
    fixture = load_fixture(GOLDEN_ROOT / "fixture")
    for presses in range(12):
        rows = render_state(fixture, SessionSetup(route=route), ["ArrowDown"] * presses)
        cursor = [row for row in body_rows(rows)[1:] if re.match(rf"^ *{CARET} ", row)]
        assert len(cursor) == 1, (presses, cursor)
        assert prefix in cursor[0], (presses, cursor[0])


def test_con_157_a_titles_columns_stay_on_the_row_grid() -> None:
    rows = RENDERED["route/backlog@80"]
    head = next(row for row in rows if row.startswith(" DRAFTS"))
    item = next(row for row in rows if "EAWF-0088" in row)
    assert head.index("STATUS") == item.index("needs criteria")


# ---------- CON-158: a value keeps its form, and an id never gives way ----------

_CLIPPED_ID = re.compile(r"\b(?:RUN-[0-9a-f]{0,7}|[A-Z]{2,5}-\d{0,3}|clau?d?e?|code?x?)…")
_POSITION = re.compile(rf"{CARET}|\[|█|\b\d+ of \d+\b")


def clipped_ids(rows: Sequence[str]) -> list[str]:
    """Return every identifier or provider name a row shortened."""
    return [found.group(0) for row in rows for found in _CLIPPED_ID.finditer(row)]


def test_con_158_no_identifier_or_provider_is_the_cell_that_gives_way() -> None:
    clipped = {fid: found for fid, rows in RENDERED.items() if (found := clipped_ids(rows))}
    assert clipped == {}


@pytest.mark.parametrize("row", ["RUN-5c55… stopped", "EAWF-00… open", "clau… 4m"])
def test_con_158_the_sweep_reds_on_a_clipped_identifier(row: str) -> None:
    assert clipped_ids([row]) != []


def test_con_158_a_frame_advertising_arrows_draws_the_position_they_move() -> None:
    unmarked = [
        fid
        for fid, rows in RENDERED.items()
        if "↑↓" in rows[-1] and not any(_POSITION.search(row) for row in body_rows(rows))
    ]
    assert unmarked == []


def test_con_158_a_reason_gives_way_before_the_id_in_its_row() -> None:
    grid = Grid([13, 26, 0])
    row = grid.row(["RUN-be1e085a", "stopped responding · resume or let go", "due –"], False, 60)  # noqa: RUF001
    assert "RUN-be1e085a" in row
    assert "stopped responding ·…" in row


# ---------- CON-109: a long column value is shortened at a word ----------


@pytest.mark.parametrize(
    ("text", "n", "clipped"),
    [
        ("stopped responding · resume or let go", 32, "stopped responding · resume or…"),
        ("final report rejected", 19, "final report…"),
        ("final report rejected", 21, "final report rejected"),
        ("final report rejected", 22, "final report rejected"),
        ("unbreakableidentifierword", 10, "unbreakab…"),
        ("a b", 1, "…"),
        ("x", 0, "x"[:0]),
    ],
)
def test_con_109_a_value_is_cut_at_a_word_with_a_trailing_ellipsis(
    text: str, n: int, clipped: str
) -> None:
    assert clip_words(text, n) == clipped
    assert cell_len(clip_words(text, n)) <= max(n, 0)


def test_con_109_a_negative_column_is_refused() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        clip_words("resume", -1)


def test_con_109_a_column_value_never_wraps() -> None:
    grid = Grid([13, 20, 0])
    row = grid.row(["ACT-0034", "stopped responding · resume or let go", "OPEN"], True, 80)
    assert "\n" not in row
    assert "stopped responding…" in row
    assert row.index("OPEN") == 1 + 2 + 13 + 20


SENTENCES = (
    "stopped responding · resume or let go",
    "past its 45m estimate; still running",
    "wants to write outside its batch",
    "one entity at two revisions",
)


@pytest.mark.parametrize("text", SENTENCES)
def test_con_109_every_cut_ends_at_a_word_unless_one_word_fills_the_column(text: str) -> None:
    for n in range(1, len(text)):
        clipped = clip_words(text, n)
        assert clipped.endswith("…")
        assert cell_len(clipped) <= n
        kept = clipped[:-1]
        assert text.startswith(kept)
        at_word = text[len(kept) : len(kept) + 1] == " "
        no_room = " " not in text[(n * 45) // 100 + 1 : n - 1]
        assert at_word or no_room or not kept, (n, clipped)


# ---------- CON-115: the chassis is built before, and never reaches into, the routes ----------

#: The five chassis steps of the build order, each a set of console modules.
CHASSIS_STEPS: tuple[frozenset[str], ...] = (
    frozenset({"tokens", "width", "format", "cells"}),
    frozenset({"registry", "session", "navigation", "chrome", "operations"}),
    frozenset({"header", "keybar", "palette", "action_menu", "keymap"}),
    frozenset({"seam", "reads"}),
    frozenset({"attention"}),
)
_ROUTE_PACKAGES = frozenset({"renderers", "overlays"})
_PREFIX = "eawf.surfaces.tui.console"


def _console_imports(path: Path) -> set[str]:
    """Return the first component of every console module ``path`` imports."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(_PREFIX):
            rest = node.module[len(_PREFIX) :].lstrip(".")
            if rest:
                names.add(rest.split(".", 1)[0])
            else:
                names.update(alias.name for alias in node.names)
    return names


def build_order_breaches(root: Path) -> dict[str, set[str]]:
    """Return each chassis module's imports of a route or of a later chassis step."""
    step_of = {name: i for i, step in enumerate(CHASSIS_STEPS) for name in step}
    breaches: dict[str, set[str]] = {}
    for name, step in step_of.items():
        path = root / f"{name}.py"
        imported = _console_imports(path)
        later = {m for m in imported if step_of.get(m, -1) > step}
        routes = imported & (_ROUTE_PACKAGES | {"app", "dispatch", "frame", "derive"})
        if later or routes:
            breaches[name] = later | routes
    return breaches


def test_con_115_every_chassis_step_module_exists() -> None:
    for step in CHASSIS_STEPS:
        for name in step:
            assert (CONSOLE_ROOT / f"{name}.py").is_file(), name


def test_con_115_no_chassis_module_reaches_into_a_route_or_a_later_step() -> None:
    assert build_order_breaches(CONSOLE_ROOT) == {}


def test_con_115_the_gate_reds_on_a_chassis_module_importing_a_renderer(tmp_path: Path) -> None:
    for step in CHASSIS_STEPS:
        for name in step:
            (tmp_path / f"{name}.py").write_text("", encoding="utf-8")
    (tmp_path / "keybar.py").write_text(
        f"from {_PREFIX}.renderers import render_route\nfrom {_PREFIX}.seam import X\n",
        encoding="utf-8",
    )
    assert build_order_breaches(tmp_path) == {"keybar": {"renderers", "seam"}}
