"""The console header holds its invariants at 80, 120 and 160 columns.

The state slot is the same at every size and names exactly one value: one of the nine
connection values, or a process value on the entry layer, where no attention count
renders. The count renders only when positive. The crumb starts at the brand once, joins
steps with the ratified separator, names the Escape path while history exists without
naming a place twice, and folds from the middle when it is too wide. Every golden header
row keeps the same shape, and the port matches the test-only chassis on its inputs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from eawf.surfaces.tui.console.header import (
    ELLIPSIS,
    SCOPE_SLOT,
    ProcessValue,
    attention_count,
    crumb_from_history,
    header_row,
    state_slot,
)
from eawf.surfaces.tui.console.session import SIZES, Session
from eawf.surfaces.tui.console.tokens import BRAND, CONNECTION, CRUMB_SEP
from eawf.surfaces.tui.console.width import cell_len

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
