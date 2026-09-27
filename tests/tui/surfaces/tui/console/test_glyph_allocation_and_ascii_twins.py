"""Every glyph the console draws is one row of the glyph table, read per column class.

The table fixture ``tests/fixtures/console/glyph-table.json`` is the packet's seed: each
glyph with its code point, its East-Asian-Width class, the meaning it carries in each
column class it may occupy, and its one-cell ASCII twin there. The console's own table in
:mod:`eawf.surfaces.tui.console.tokens` must equal it, the plain path twins from it, and
the collision audit runs per column class rather than per frame, so ``~`` may be the
derived marker in a value column and the heartbeat kind in a kind column but never two
things in one class.

The rows here prove CON-165 (settings provenance glyphs), CON-166 (the declared no-value
glyph) and CON-167 (one meaning per glyph per column class).
"""

# ruff: noqa: RUF001 -- the en dash and the prose quotes are the glyphs under test

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.projection.truth import TruthState
from eawf.kernel.state.enums import MeasurementQuality
from eawf.surfaces.tui.console.cells import NO_VALUE, ValueCell
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.plain import ascii_twin, plain_rows, render_plain
from eawf.surfaces.tui.console.reads import attn_cell
from eawf.surfaces.tui.console.renderers.settings import BUILT_IN, UNDEFINED, at, resolve, state
from eawf.surfaces.tui.console.session import Session, SessionSetup
from eawf.surfaces.tui.console.tokens import (
    BOUND_TWINS,
    CONNECTION,
    GLYPH_TABLE,
    QUALITY,
    TRUTH,
    ColumnClass,
    GlyphRow,
    audit_classes,
)
from eawf.surfaces.tui.console.width import cell_len
from tests.tui.surfaces.tui.console.test_chrome_sweeps import RENDERED, body_rows

TESTS_ROOT = Path(__file__).resolve().parents[4]
TABLE_PATH = TESTS_ROOT / "fixtures" / "console" / "glyph-table.json"
GOLDEN_ROOT = TESTS_ROOT / "fixtures" / "console" / "golden"


class FixtureRow(BaseModel):
    """One glyph-table fixture row, closed so a misspelt field is a load failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    glyph: str
    code_point: str
    eaw: str
    roles: dict[ColumnClass, str]
    twins: dict[ColumnClass, str]


class GlyphTableFixture(BaseModel):
    """The whole fixture: its spec, when it was measured and its rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: str
    measured: str
    rows: tuple[FixtureRow, ...]


TABLE = GlyphTableFixture.model_validate(json.loads(TABLE_PATH.read_text(encoding="utf-8")))
ROWS = {row.glyph: row for row in TABLE.rows}

#: The markers the header's state slot owns; ``●`` is left out because the cursor class
#: legitimately draws it as the selected enum option and the dated roadmap marker.
STATE_SLOT_ONLY = "◑◐◌►▲◈◇▪"


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    return load_fixture(GOLDEN_ROOT / "fixture")


# ---------- CON-167: the table, its classes and its per-class audit ----------


def test_con_167_the_fixture_loads_strict() -> None:
    raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    raw["rows"][0]["meaning"] = "a stray field"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GlyphTableFixture.model_validate(raw)


def test_con_167_the_fixture_rejects_an_unknown_column_class() -> None:
    raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    raw["rows"][0]["twins"] = {"margin": "?"}
    with pytest.raises(ValidationError):
        GlyphTableFixture.model_validate(raw)


def test_con_167_each_glyph_is_one_row() -> None:
    assert len(ROWS) == len(TABLE.rows)
    assert len({row.glyph for row in GLYPH_TABLE}) == len(GLYPH_TABLE)


def test_con_167_the_console_table_equals_the_fixture() -> None:
    console = {row.glyph: dict(row.twins) for row in GLYPH_TABLE}
    seed = {row.glyph: dict(row.twins) for row in TABLE.rows}
    assert console == seed


@pytest.mark.parametrize("row", TABLE.rows, ids=lambda row: f"U+{ord(row.glyph):04X}")
def test_con_167_code_point_and_width_class_are_measured(row: FixtureRow) -> None:
    assert len(row.glyph) == 1
    assert row.code_point == f"U+{ord(row.glyph):04X}"
    assert row.eaw == unicodedata.east_asian_width(row.glyph)
    assert row.eaw not in {"W", "F"}
    assert cell_len(row.glyph) == 1


@pytest.mark.parametrize("row", TABLE.rows, ids=lambda row: f"U+{ord(row.glyph):04X}")
def test_con_167_a_row_names_a_meaning_and_a_twin_in_every_class_it_occupies(
    row: FixtureRow,
) -> None:
    assert row.roles
    assert set(row.roles) == set(row.twins)
    for twin in row.twins.values():
        assert twin.isascii()
        assert cell_len(twin) == 1


def test_con_167_twins_repeat_in_a_class_only_as_declared_bound_forms() -> None:
    audit_classes(GLYPH_TABLE)
    by_class: dict[tuple[ColumnClass, str], set[str]] = {}
    for row in TABLE.rows:
        for cls, twin in row.twins.items():
            if cls is not ColumnClass.STRUCTURE:
                by_class.setdefault((cls, twin), set()).add(row.glyph)
    repeated = {key: glyphs for key, glyphs in by_class.items() if len(glyphs) > 1}
    assert repeated == {key: set(glyphs) for key, glyphs in BOUND_TWINS.items()}


def test_con_167_the_audit_reds_on_a_twin_with_two_meanings_in_one_class() -> None:
    clash = (
        GlyphRow("≠", {ColumnClass.PROVENANCE: "#"}),
        GlyphRow("▪", {ColumnClass.PROVENANCE: "#"}),
    )
    with pytest.raises(ValueError, match="provenance"):
        audit_classes(clash)


def test_con_167_the_audit_reds_on_a_glyph_listed_twice() -> None:
    twice = (GlyphRow("~", {ColumnClass.VALUE: "~"}), GlyphRow("~", {ColumnClass.KIND: "~"}))
    with pytest.raises(ValueError, match="listed twice"):
        audit_classes(twice)


def test_con_167_the_audit_reds_on_a_two_cell_twin() -> None:
    with pytest.raises(ValueError, match="one ASCII cell"):
        audit_classes((GlyphRow("≈", {ColumnClass.VALUE: "~="}),))


@pytest.mark.parametrize("empty", [(), (GlyphRow("·", {}),)])
def test_con_167_the_audit_refuses_an_empty_table_or_a_classless_row(
    empty: tuple[GlyphRow, ...],
) -> None:
    with pytest.raises(ValueError, match=r"no column class|empty"):
        audit_classes(empty)


def test_con_167_every_token_glyph_is_a_table_row_with_the_same_twin() -> None:
    for table in (TRUTH, QUALITY, CONNECTION):
        for glyph in table.values():
            if glyph.unicode and not glyph.unicode.isascii():
                assert set(ROWS[glyph.unicode].twins.values()) == {glyph.ascii}


def test_con_167_every_glyph_a_frame_draws_is_a_table_row() -> None:
    prose = set("—’“”")
    drawn = {ch for rows in RENDERED.values() for row in rows for ch in row if not ch.isascii()}
    assert drawn - set(ROWS) - prose == set()


def test_con_167_a_state_slot_marker_is_drawn_only_in_the_state_slot() -> None:
    misplaced = {
        frame_id: row
        for frame_id, rows in RENDERED.items()
        for row in body_rows(rows)
        if any(marker in row for marker in STATE_SLOT_ONLY)
    }
    assert misplaced == {}


def test_con_167_a_running_campaign_step_draws_the_running_mark_never_replaying() -> None:
    rows = RENDERED["route/campaign@120"]
    graph = next(row for row in rows if row.startswith(" GRAPH"))
    assert "⋯ 5" in graph
    assert "►" not in graph


def test_con_167_the_middle_dot_twins_by_its_class() -> None:
    assert ascii_twin("4 mine · 4 known") == "4 mine - 4 known"
    assert plain_rows(["│ ▸ · inherited_key   6", " Esc back"])[0] == "| > . inherited_key   6"
    assert plain_rows(["│   · inherited_key   6", " Esc back"])[0] == "|   . inherited_key   6"


# ---------- CON-166: the declared no-value glyph ----------


def test_con_166_the_no_value_glyph_is_the_en_dash_twinned_to_underscore() -> None:
    assert NO_VALUE == "–"
    assert set(ROWS[NO_VALUE].twins.values()) == {"_"}
    assert ascii_twin("due –") == "due _"
    assert TRUTH["unavailable"].ascii == "-"


def test_con_166_a_no_value_cell_wears_no_token_and_no_reason() -> None:
    cell = ValueCell(
        value=None, state=TruthState.KNOWN, quality=MeasurementQuality.EXACT, basis="due"
    )
    assert (cell.slot, cell.mark, cell.reason) == (NO_VALUE, None, "")


@pytest.mark.parametrize("conn", sorted(set(CONNECTION) - {"LIVE"}))
@pytest.mark.parametrize("n", [0, 1, 41208])
def test_con_166_an_unvouched_count_is_labelled_known_never_a_dash(conn: str, n: int) -> None:
    cell = attn_cell(Session(conn=conn), n)
    assert NO_VALUE not in cell
    assert cell == f"{n} known"


@pytest.mark.parametrize("n", [0, 3])
def test_con_166_a_vouched_count_is_the_bare_number(n: int) -> None:
    assert attn_cell(Session(conn="LIVE"), n) == str(n)


#: An em dash standing as the whole of a trailing cell, before the end of the row or a rail.
_VALUE_EM_DASH = re.compile(r"(?:^|  )— *(?:│.*)?$")


def test_con_166_the_em_dash_sweep_reds_on_a_notice_row() -> None:
    assert _VALUE_EM_DASH.search("   ACT-0035 RUN-3fcd1d52 past its estimate   OPEN      \u2014")
    assert _VALUE_EM_DASH.search("   ACT-0035 still running   OPEN      \u2014         │  over")
    assert not _VALUE_EM_DASH.search("   confirmed   \u2014  the run never answered")


def test_con_166_no_frame_draws_an_em_dash_as_a_cell() -> None:
    cells = {
        frame_id: row
        for frame_id, rows in RENDERED.items()
        for row in body_rows(rows)
        if _VALUE_EM_DASH.search(row.rstrip())
    }
    assert cells == {}


def test_con_166_an_absent_deadline_reads_due_en_dash() -> None:
    rows = RENDERED["route/attention@80"]
    budget = next(row for row in rows if "ACT-0035" in row)
    assert budget.rstrip().endswith("due –")


# ---------- CON-165: settings provenance ----------

PROVENANCE = {"=", "≠", "·", "–"}


def test_con_165_the_four_provenance_glyphs_are_the_provenance_class() -> None:
    in_class = {row.glyph for row in GLYPH_TABLE if ColumnClass.PROVENANCE in row.twins}
    assert in_class == PROVENANCE
    twins = {g: ROWS[g].twins[ColumnClass.PROVENANCE] for g in PROVENANCE}
    assert twins == {"=": "=", "≠": "#", "·": ".", "–": "_"}


def test_con_165_enum_option_marks_twin_to_star_and_o() -> None:
    assert ROWS["●"].twins[ColumnClass.CURSOR] == "*"
    assert ROWS["○"].twins[ColumnClass.CURSOR] == "o"


def test_con_165_every_key_carries_the_glyph_its_layers_earn(fixture: Fixture) -> None:
    cfg = fixture.settings
    seen: set[str] = set()
    for section, keys in cfg.sections.items():
        for key in keys:
            default = at(key, 2)
            for lens in cfg.writable:
                field = state(cfg, section, key[0], default, lens)
                full = f"{section}.{key[0]}"
                here = cfg.stored.get(full, {}).get(lens, UNDEFINED)
                winner = resolve(cfg, section, key[0], default).winner_layer
                seen.add(field.glyph)
                assert field.glyph in PROVENANCE
                if here is UNDEFINED:
                    assert field.glyph == ("–" if winner == BUILT_IN else "·")
                elif winner == lens and field.glyph == "=":
                    continue
                else:
                    assert field.glyph == "≠", (full, lens)
                    assert field.winner_layer in field.why or "floor" in field.why
    assert {"=", "≠", "–"} <= seen


def test_con_165_a_denied_value_keeps_the_denied_token_never_the_shadowed_glyph() -> None:
    cell = ValueCell(
        value=None, state=TruthState.DENIED, quality=MeasurementQuality.EXACT, basis="policy"
    )
    assert cell.slot == "⊘"
    assert "≠" not in cell.full


def test_con_165_the_settings_frame_draws_its_glyphs_and_their_twins(fixture: Fixture) -> None:
    rows = RENDERED["route/settings@80"]
    assert any("▸ ≠ approval" in row for row in rows)
    assert any("= max_parallel_waves" in row for row in rows)
    assert any("– require_research_for_unknowns" in row for row in rows)
    assert any("○ ask" in row for row in rows) and any("● auto" in row for row in rows)
    plain = render_plain(fixture, SessionSetup(route="settings"), conn="LIVE")
    assert any("> # approval" in row for row in plain)
    assert any("_ require_research_for_unknowns" in row for row in plain)
    assert any("o ask" in row for row in plain) and any("* auto" in row for row in plain)
