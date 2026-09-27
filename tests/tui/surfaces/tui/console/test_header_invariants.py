"""The console header holds its invariants at 80, 120 and 160 columns (CON-086, CON-168).

The state slot is the same at every size and names exactly one value: one of the nine
connection values, or a process value on the entry layer, where no attention count
renders. The count renders only when positive. The crumb starts at the brand once, joins
steps with the ratified separator, names the Escape path while history exists without
naming a place twice, and folds from the middle when it is too wide. Every golden header
row keeps the same shape, and the port matches the test-only chassis on its inputs.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import activate_crumb
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.frame import make_room
from eawf.surfaces.tui.console.header import (
    ELLIPSIS,
    SCOPE_SLOT,
    CrumbPart,
    CrumbRun,
    ProcessValue,
    attention_count,
    crumb_at,
    crumb_from_history,
    crumb_runs,
    header_row,
    state_slot,
)
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.plain import ascii_twin, legend_line, plain_rows, render_plain
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from eawf.surfaces.tui.console.tokens import BRAND, CONNECTION, CRUMB_SEP
from eawf.surfaces.tui.console.width import cell_len, pad
from tests.tui.surfaces.tui.console.test_chrome_sweeps import GOLDEN_ROOT, RENDERED

WIDTHS = [w for w, _h in SIZES]
SCOPE = "eawf-core"
CHASSIS = "tests.snapshots.tui.console.console_chassis.chassis"
SEQUENCES = Path(__file__).resolve().parents[4] / "fixtures" / "console" / "golden" / "sequences"
_SLOT = re.compile(r"(?:!\d[\d,]* NEEDS YOU  )?(\S) ([A-Z][A-Z /]*[A-Z])$")
_SIZED = re.compile(r"^(?P<base>.+)@(?P<w>\d+)$")

# Crumbs the ported renderers hand the header, scope slot included.
CRUMBS = (
    f" {BRAND} ▸ {SCOPE}",
    f" {BRAND} ▸ {SCOPE} ▸ Activity",
    f" {BRAND} ▸ … ▸ BAT-0002 ▸ RUN-538453eb",
    f" {BRAND} ▸ … ▸ Trust ▸ MLS-0004",
    f" {BRAND} ▸ palette",
    f" {BRAND} ▸ help · activity",
    f" {BRAND} ▸ {SCOPE} ▸ Backlog ▸ A-rather-long-draft-identifier-that-fills-the-row",
)
# Back stacks as (route, subject) steps, oldest first.
HISTORIES: tuple[tuple[tuple[str, str | None], ...], ...] = (
    (),
    (("scope.home", None),),
    (("scope.home", None), ("activity", None), ("run.detail", "RUN-538453eb")),
    (("attention", None), ("run.detail", "RUN-538453eb"), ("transcript", None)),
    (
        ("activity", None),
        ("batch.detail", "BAT-0002"),
        ("task.detail", "EAWF-0042"),
        ("run.detail", "RUN-538453eb"),
        ("git.pr", None),
        ("history", None),
        ("settings", None),
        ("health", None),
    ),
)


def _session(
    route: str = "activity",
    *,
    conn: str = "LIVE",
    history: Sequence[tuple[str, str | None]] = (),
) -> Session:
    session = Session(route=route, conn=conn)
    for step_route, subj in history:
        session.back.push(route=step_route, sel=0, subj=subj)
    return session


def _row(session: Session, crumb: str, w: int, *, needs: int = 4) -> str:
    return header_row(session, crumb=crumb, scope=SCOPE, needs=needs, w=w)


def _golden_headers() -> list[tuple[str, int, str, bool]]:
    """Return every golden header as (frame id, width, row, whether it is pre-session)."""
    found: list[tuple[str, int, str, bool]] = []
    for path in sorted(SEQUENCES.glob("frames-*.json")):
        for state in json.loads(path.read_text(encoding="utf-8"))["states"]:
            entry = state["setup"].get("route") == "entry" or state["id"].startswith("entry/")
            row = state["frame"].split("\n")[0]
            found.append((state["id"], state["size"][0], row, entry))
    return found


@pytest.mark.parametrize("conn", sorted(CONNECTION))
def test_header_row_state_slot_is_identical_at_every_size(conn: str) -> None:
    rows = [_row(_session(conn=conn), CRUMBS[1], w) for w in WIDTHS]
    slot = f"!4 NEEDS YOU  {CONNECTION[conn].unicode} {conn}"
    assert [cell_len(row) for row in rows] == WIDTHS
    assert all(row.endswith(slot) for row in rows)


def test_header_row_healthy_live_has_no_suffix_at_any_size() -> None:
    for w in WIDTHS:
        row = _row(_session(), CRUMBS[1], w, needs=0)
        assert row.endswith(" ● LIVE")
        assert row.rstrip().endswith("● LIVE")
        assert "NEEDS YOU" not in row


def test_header_row_count_renders_only_when_positive() -> None:
    assert "!0" not in _row(_session(), CRUMBS[1], 80, needs=0)
    assert _row(_session(), CRUMBS[1], 80, needs=1).endswith("!1 NEEDS YOU  ● LIVE")
    assert _row(_session(), CRUMBS[1], 120, needs=1204).endswith("!1,204 NEEDS YOU  ● LIVE")


def test_attention_count_zero_is_empty() -> None:
    assert attention_count(0) == ""
    assert attention_count(41208) == "!41,208 NEEDS YOU  "


def test_attention_count_negative_raises_value_error() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        attention_count(-1)


@pytest.mark.parametrize("conn", ["", "live", "CONNECTED", "LIVE / complete"])
def test_state_slot_unknown_connection_raises_value_error(conn: str) -> None:
    with pytest.raises(ValueError, match="not a connection value"):
        state_slot(conn)


def test_state_slot_partial_value_keeps_its_slash() -> None:
    assert state_slot("LIVE / PARTIAL") == "◑ LIVE / PARTIAL"


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("crumb", CRUMBS)
def test_header_row_brand_leads_once_with_the_ratified_separator(crumb: str, w: int) -> None:
    row = _row(_session(history=HISTORIES[2]), crumb, w)
    assert row.startswith(f" {BRAND}{CRUMB_SEP}")
    assert row.count(BRAND) == 1
    assert cell_len(row) == w


def test_header_row_fills_the_scope_slot_when_it_fits() -> None:
    row = _row(_session(), f" {BRAND}{SCOPE_SLOT}BAT-0002 ▸ RUN-538453eb", 80)
    assert row.startswith(f" {BRAND} ▸ {SCOPE} ▸ BAT-0002 ▸ RUN-538453eb ")


def test_header_row_keeps_the_scope_slot_when_the_scope_does_not_fit() -> None:
    scope = "s" * 70
    row = header_row(_session(), crumb=CRUMBS[2], scope=scope, needs=0, w=80)
    assert row.startswith(CRUMBS[2])
    assert scope not in row


def test_header_row_slot_wider_than_the_frame_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        _row(_session(), CRUMBS[1], 10)


def test_crumb_from_history_empty_back_stack_returns_the_crumb() -> None:
    assert crumb_from_history(_session(), CRUMBS[2], scope=SCOPE) == CRUMBS[2]


def test_crumb_from_history_names_the_escape_path() -> None:
    session = _session("transcript", history=HISTORIES[2])
    crumb = crumb_from_history(session, f" {BRAND} ▸ {SCOPE} ▸ Transcript", scope=SCOPE)
    assert crumb == f" {BRAND} ▸ {SCOPE} ▸ Activity ▸ RUN-538453eb ▸ Transcript"


def test_crumb_from_history_skips_the_current_route_and_the_leaf() -> None:
    history = (("activity", None), ("transcript", None), ("run.detail", "RUN-538453eb"))
    session = _session("transcript", history=history)
    crumb = crumb_from_history(session, f" {BRAND} ▸ RUN-538453eb", scope=SCOPE)
    assert crumb == f" {BRAND} ▸ {SCOPE} ▸ Activity ▸ RUN-538453eb"


def test_crumb_from_history_names_each_place_once() -> None:
    history = (
        ("activity", None),
        ("run.detail", "RUN-1"),
        ("activity", None),
        ("run.detail", "RUN-2"),
        ("activity", None),
    )
    session = _session("transcript", history=history)
    crumb = crumb_from_history(session, f" {BRAND} ▸ Transcript", scope=SCOPE)
    steps = crumb.strip().split(CRUMB_SEP)
    assert steps == [BRAND, SCOPE, "RUN-1", "RUN-2", "Activity", "Transcript"]
    assert len(steps) == len(set(steps))


@pytest.mark.parametrize("w", WIDTHS)
def test_header_row_folds_history_from_the_middle(w: int) -> None:
    session = _session("transcript", history=HISTORIES[4])
    row = _row(session, f" {BRAND} ▸ {SCOPE} ▸ Transcript", w)
    assert cell_len(row) == w
    assert row.endswith("!4 NEEDS YOU  ● LIVE")
    crumb = row[: -cell_len("!4 NEEDS YOU  ● LIVE")].rstrip()
    assert crumb.endswith(f"{CRUMB_SEP}Transcript")
    if w == 80:
        assert crumb.startswith(f" {BRAND} ▸ {SCOPE} ▸ {ELLIPSIS} ▸ ")
        assert crumb.count(ELLIPSIS) == 1


def test_header_row_fold_keeps_brand_and_scope_when_the_leaf_overflows() -> None:
    session = _session("backlog", history=HISTORIES[4])
    row = _row(session, CRUMBS[6], 80)
    assert cell_len(row) == 80
    assert row.startswith(f" {BRAND} ▸ {SCOPE} ▸ {ELLIPSIS} ▸ A-rather-long")
    assert row.endswith("!4 NEEDS YOU  ● LIVE")


@pytest.mark.parametrize("w", WIDTHS)
def test_header_row_entry_layer_shows_the_process_value_only(w: int) -> None:
    process = ProcessValue(glyph="◈", label="RESOLVING")
    session = _session("entry", conn="DEGRADED", history=HISTORIES[2])
    row = header_row(session, crumb=f" {BRAND}", scope=SCOPE, needs=4, w=w, process=process)
    assert cell_len(row) == w
    assert row.endswith(" ◈ RESOLVING")
    assert row.startswith(f" {BRAND} ")
    assert "NEEDS YOU" not in row
    assert "DEGRADED" not in row


def test_header_row_entry_without_process_raises_value_error() -> None:
    with pytest.raises(ValueError, match="process value"):
        _row(_session("entry"), f" {BRAND}", 80)


def test_header_row_process_off_the_entry_layer_raises_value_error() -> None:
    process = ProcessValue(glyph="◈", label="RESOLVING")
    with pytest.raises(ValueError, match="process value"):
        header_row(_session(), crumb=CRUMBS[1], scope=SCOPE, needs=0, w=80, process=process)


@pytest.mark.parametrize(("glyph", "label"), [("中", "WIDE"), ("", "EMPTY"), ("◈◈", "TWO")])
def test_process_value_glyph_not_one_cell_raises_value_error(glyph: str, label: str) -> None:
    with pytest.raises(ValueError, match="exactly one cell"):
        ProcessValue(glyph=glyph, label=label)


def test_process_value_blank_label_raises_value_error() -> None:
    with pytest.raises(ValueError, match="needs a label"):
        ProcessValue(glyph="◈", label=" ")


@pytest.mark.parametrize(("frame_id", "w", "row", "entry"), _golden_headers())
def test_header_invariants_hold_on_every_golden_header(
    frame_id: str, w: int, row: str, entry: bool
) -> None:
    assert cell_len(row) == w, frame_id
    assert row.count(BRAND) == 1, frame_id
    assert "!0 " not in row, frame_id
    slot = _SLOT.search(row)
    assert slot is not None, frame_id
    if entry:
        assert row.startswith(f" {BRAND} "), frame_id
        assert "NEEDS YOU" not in row, frame_id
    else:
        assert row.startswith(f" {BRAND}{CRUMB_SEP}"), frame_id
        assert slot.group(2) in CONNECTION, frame_id
        assert slot.group(1) == CONNECTION[slot.group(2)].unicode, frame_id


def test_header_slot_is_identical_across_sizes_on_every_golden_state() -> None:
    slots: dict[str, set[str]] = {}
    for frame_id, _w, row, _entry in _golden_headers():
        sized = _SIZED.match(frame_id)
        slot = _SLOT.search(row)
        if sized and slot:
            slots.setdefault(sized.group("base"), set()).add(slot.group(0))
    assert slots
    assert {base: found for base, found in slots.items() if len(found) != 1} == {}


def test_header_row_matches_the_test_only_chassis() -> None:
    # parity holds only while both copies exist; the skip marks the chassis' removal
    frame = pytest.importorskip(f"{CHASSIS}.frame", exc_type=ModuleNotFoundError)
    chassis_session = pytest.importorskip(f"{CHASSIS}.session", exc_type=ModuleNotFoundError)
    fixture_mod = pytest.importorskip(f"{CHASSIS}.fixture", exc_type=ModuleNotFoundError)
    attention = pytest.importorskip(f"{CHASSIS}.attention", exc_type=ModuleNotFoundError)
    fixture = fixture_mod.load_fixture()
    needs = attention.open_count(fixture)
    compared = 0
    for conn in sorted(CONNECTION):
        for history in HISTORIES[:4]:
            for crumb in CRUMBS[:6]:
                theirs = chassis_session.Session(route="run.detail", conn=conn)
                for step_route, subj in history:
                    theirs.back.push(step_route, 0, subj)
                ours = _session("run.detail", conn=conn, history=history)
                for w in WIDTHS:
                    expected = frame.header_row(theirs, fixture, crumb, w)
                    got = header_row(ours, crumb=crumb, scope=fixture.scope, needs=needs, w=w)
                    assert got == expected, (conn, history, crumb, w)
                    compared += 1
    for index, state in enumerate(fixture.proto.entry):
        theirs = chassis_session.Session(route="entry", entry_sel=index)
        process = ProcessValue(glyph=state.glyph, label=state.state)
        for w in WIDTHS:
            expected = frame.header_row(theirs, fixture, f" {BRAND}", w)
            got = header_row(
                _session("entry"), crumb=f" {BRAND}", scope=SCOPE, needs=0, w=w, process=process
            )
            assert got == expected
            compared += 1
    assert compared == len(CONNECTION) * 4 * 6 * 3 + len(fixture.proto.entry) * 3


# ---------- CON-086: the breadcrumb's grammar, styling and links ----------


def _steps(row: str) -> list[CrumbRun]:
    return [r for r in crumb_runs(row) if r.part not in (CrumbPart.SEP, CrumbPart.SLOT)]


@pytest.mark.parametrize(("frame_id", "w", "row", "entry"), _golden_headers())
def test_con_086_every_golden_crumb_runs_rejoin_to_the_row_and_name_each_place_once(
    frame_id: str, w: int, row: str, entry: bool
) -> None:
    runs = crumb_runs(row)
    assert "".join(run.text for run in runs) == row, frame_id
    assert runs[-1].end == w, frame_id
    steps = _steps(row)
    assert steps[0].part is CrumbPart.BRAND and steps[0].start == 1, frame_id
    assert [r.text for r in crumb_runs(row) if r.part is CrumbPart.SEP] == [CRUMB_SEP] * (
        len(steps) - 1
    ), frame_id
    places = [s.text for s in steps if s.part is not CrumbPart.FOLD]
    assert len(places) == len(set(places)), frame_id
    assert "\x1b" not in row, frame_id


@pytest.mark.parametrize(
    ("state_id", "leaf"),
    [
        ("route/campaign@80", "CAM-0001"),
        ("route/run.detail@80", "RUN-538453eb"),
        ("route/batch.detail@120", "BAT-0003"),
    ],
)
def test_con_086_an_entity_route_steps_by_its_id_never_its_route_word(
    state_id: str, leaf: str
) -> None:
    rows = RENDERED[state_id]
    steps = _steps(rows[0])
    assert steps[-1].text == leaf
    assert steps[-1].part is CrumbPart.LEAF
    words = {REGISTRY.route_word(route) for route in REGISTRY.by_id}
    assert steps[-1].text not in words


def test_con_086_the_crumb_parts_classify_brand_steps_ids_fold_and_leaf() -> None:
    crumb = f" {BRAND} ▸ {SCOPE} ▸ … ▸ BAT-0002 ▸ Activity ▸ RUN-538453eb"
    row = pad(crumb, 100) + "● LIVE"
    parts = [(r.text, r.part, r.back, r.link) for r in _steps(row)]
    assert parts == [
        (BRAND, CrumbPart.BRAND, 5, False),
        (SCOPE, CrumbPart.STEP, 4, True),
        (ELLIPSIS, CrumbPart.FOLD, 3, False),
        ("BAT-0002", CrumbPart.ID, 2, True),
        ("Activity", CrumbPart.STEP, 1, True),
        ("RUN-538453eb", CrumbPart.LEAF, 0, False),
    ]


@pytest.mark.parametrize("row", ["", " ", f" {BRAND}", f" {BRAND}" + " " * 70 + "◈ RESOLVING"])
def test_con_086_crumb_runs_of_a_bare_or_entry_header(row: str) -> None:
    runs = crumb_runs(row)
    assert "".join(r.text for r in runs) == row
    assert not any(r.link for r in runs)


def test_con_086_crumb_at_finds_only_linked_steps() -> None:
    row = _row(_session(), f" {BRAND} ▸ {SCOPE} ▸ Activity ▸ RUN-538453eb", 80)
    assert crumb_at(row, 1) is None
    at_scope = crumb_at(row, row.index(SCOPE))
    assert at_scope is not None and at_scope.text == SCOPE
    assert crumb_at(row, row.index("Activity") + 3) is not None
    assert crumb_at(row, row.index("RUN-538453eb")) is None
    assert crumb_at(row, row.index(" ▸ ") + 1) is None
    assert crumb_at(row, 79) is None
    assert crumb_at(row, 500) is None


def test_con_086_the_header_styles_ride_on_segments_never_the_text() -> None:
    from eawf.surfaces.tui.console.paint import Part, paint

    row = f" {BRAND}{CRUMB_SEP}{SCOPE}{CRUMB_SEP}BAT-0002{CRUMB_SEP}RUN-538453eb   ● LIVE"
    strokes = paint(row, Part.HEADER)
    assert "".join(s.text for s in strokes) == row
    by_text = {s.text: s for s in strokes}
    assert by_text[BRAND].bold and by_text[BRAND].surface == "brand"
    assert by_text[CRUMB_SEP].surface == "rail" and not by_text[CRUMB_SEP].bold
    assert by_text["RUN-538453eb"].bold and not by_text["RUN-538453eb"].underline
    assert by_text["BAT-0002"].underline and by_text["BAT-0002"].surface == "hint"
    assert not by_text[SCOPE].bold and not by_text[SCOPE].underline


class _Host:
    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        return self._clock

    def quit(self) -> None:
        """A crumb walk never quits."""


def _walk(session: Session, label: str, back: int = 1, part: CrumbPart = CrumbPart.STEP) -> None:
    fixture = load_fixture(GOLDEN_ROOT / "fixture")
    ctx = Ctx(session=session, fixture=fixture, host=_Host(), w=80, h=24, verbose=False)
    activate_crumb(ctx, CrumbRun(text=label, part=part, start=1, back=back))


def test_con_086_activating_a_history_step_walks_back_to_it() -> None:
    session = _session("transcript", history=HISTORIES[2])
    _walk(session, "Activity", back=2)
    assert session.route == "activity"
    assert [e.route for e in session.back.items()] == ["scope.home"]


def test_con_086_activating_an_id_step_restores_that_entity() -> None:
    session = _session("transcript", history=HISTORIES[2])
    _walk(session, "RUN-538453eb", back=1, part=CrumbPart.ID)
    assert (session.route, session.subj_id) == ("run.detail", "RUN-538453eb")


def test_con_086_activating_the_scope_walks_to_scope_home() -> None:
    session = _session("transcript", history=HISTORIES[2])
    _walk(session, "eawf-core", back=3)
    assert session.route == "scope.home"
    assert not session.back


def test_con_086_activating_a_containment_step_climbs_one_level() -> None:
    session = Session(route="campaign", conn="LIVE")
    session.subj_id = "CAM-0001"
    _walk(session, "Research", back=1)
    assert session.route != "campaign"
    assert not session.back


@pytest.mark.parametrize(
    ("label", "part", "back"),
    [
        ("RUN-538453eb", CrumbPart.LEAF, 0),
        (BRAND, CrumbPart.BRAND, 3),
        (ELLIPSIS, CrumbPart.FOLD, 2),
    ],
)
def test_con_086_activating_the_leaf_brand_or_fold_is_a_no_op(
    label: str, part: CrumbPart, back: int
) -> None:
    session = _session("run.detail", history=HISTORIES[1])
    session.subj_id = "RUN-538453eb"
    before = (session.route, session.subj_id, len(session.back))
    _walk(session, label, back=back, part=part)
    assert (session.route, session.subj_id, len(session.back)) == before


def test_con_086_a_crumb_step_under_an_overlay_is_a_no_op() -> None:
    session = _session("transcript", history=HISTORIES[2])
    session.overlay = "help"
    _walk(session, "Activity", back=2)
    assert session.route == "transcript"


def test_con_086_clicking_a_crumb_step_in_the_console_walks_to_it() -> None:
    async def body() -> None:
        app = ConsoleApp(load_fixture(GOLDEN_ROOT / "fixture"), FakeClock())
        async with app.run_test(size=(80, 24)) as pilot:
            app.reset(SessionSetup(route="activity"))
            app.press_key("Enter")
            await pilot.pause()
            row = app.frame_rows[0]
            assert app.session.route == "run.detail"
            await pilot.click("#header", offset=(row.index("Activity") + 2, 0))
            await pilot.pause()
            assert app.session.route == "activity"
            await pilot.click("#header", offset=(1, 0))
            await pilot.pause()
            assert app.session.route == "activity"

    asyncio.run(body())


# ---------- CON-168: the glyph legend shares the keybar or takes the rows above it ----------


def _frame(w: int, keys: str, facts: int, h: int = 24) -> list[str]:
    body = [pad("~4.62 derived", w), *(pad(f"fact {i}", w) for i in range(facts))]
    body.extend(" " * w for _ in range(h - 1 - len(body)))
    return [*body, pad(keys, w)]


def test_con_168_at_80_columns_the_legend_takes_the_two_rows_above_the_keybar() -> None:
    rows = plain_rows(_frame(80, " ↑↓ row   Enter open   Esc back", 5))
    assert rows[-3].startswith(" TRUTH TOKENS")
    assert "bare measured" in rows[-2]
    assert rows[-1].startswith(" up/dn row   Enter open   Esc back")
    assert [cell_len(r) for r in rows] == [80] * 24


def test_con_168_where_the_whole_line_fits_the_legend_takes_one_row() -> None:
    keys = " " + "   ".join(f"k{i} verb{i}" for i in range(12))
    rows = plain_rows(_frame(160, keys, 5, h=40))
    assert rows[-2] == pad(" " + legend_line(), 160)
    assert rows[-1] == pad(keys, 160)


def test_con_168_the_legend_shares_a_keybar_row_that_holds_both() -> None:
    rows = plain_rows(_frame(160, " Esc back", 5, h=40))
    assert rows[-1].startswith(" Esc back")
    assert rows[-1].endswith(legend_line() + " ")
    assert cell_len(rows[-1]) == 160
    assert not any(r.startswith(" TRUTH TOKENS") for r in rows[:-1])


def test_con_168_the_legend_never_displaces_a_bound_key() -> None:
    keys = " " + "   ".join(f"k{i} verb" for i in range(3))
    width = cell_len(keys) + 3 + cell_len(legend_line())
    rows = plain_rows(_frame(width, keys, 2))
    assert rows[-1].rstrip() == keys
    assert rows[-2].startswith(" TRUTH TOKENS")


def test_con_168_a_frame_of_facts_gives_up_its_thin_rules_and_nothing_else() -> None:
    body = [pad("~4.62 derived", 80)]
    for i in range(11):
        body.extend([pad(f"fact {i}", 80), "─" * 80])
    rows = plain_rows([*body[:23], pad(" Esc back", 80)])
    facts = [r for r in rows[:-3] if r.strip(" -")]
    assert [r.split()[1] for r in facts[1:]] == [str(i) for i in range(11)]
    assert sum(1 for r in rows if set(r) == {"-"}) == 9
    assert rows[-3].startswith(" TRUTH TOKENS")
    assert len(rows) == 24


def test_con_168_the_densest_planning_screen_carries_the_legend_and_every_fact() -> None:
    fixture = load_fixture(GOLDEN_ROOT / "fixture")
    unicode = RENDERED["route/campaign@80"]
    plain = render_plain(fixture, SessionSetup(route="campaign"), conn="LIVE")
    assert plain[-3].startswith(" TRUTH TOKENS")
    assert plain[-1].split() == ascii_twin(unicode[-1]).replace("^v", "up/dn").split()
    facts = [ascii_twin(r) for r in unicode[:-1] if set(r) != {"─"}]
    assert [r for r in plain[:-3] if set(r) != {"-"}] == facts
    assert len(plain) == len(unicode)


@pytest.mark.parametrize("n", [-1, -5])
def test_con_168_make_room_refuses_a_negative_budget(n: int) -> None:
    with pytest.raises(ValueError, match="negative"):
        make_room(["a"], n)


@pytest.mark.parametrize(
    ("body", "n", "kept"),
    [
        (["a", "b"], 0, ["a", "b"]),
        ([], 0, []),
        (["a", " ", " "], 2, ["a"]),
        (["a", "──", "b", "──", "c", " "], 2, ["a", "──", "b", "c"]),
        (["a", "──", "b", " "], 3, None),
        (["a", "b"], 1, None),
        ([], 1, None),
    ],
)
def test_con_168_make_room_frees_blank_rows_then_rules_bottom_up(
    body: list[str], n: int, kept: list[str] | None
) -> None:
    assert make_room(body, n) == kept
