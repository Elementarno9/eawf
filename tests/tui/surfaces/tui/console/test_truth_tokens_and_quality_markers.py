"""Truth tokens and quality markers render through every console renderer, distinctly.

Each test names the packet row it proves, CON-063 to CON-076. The subject is the value
cell a renderer draws a truth field through, the spans a painter reads marks off a
finished row, the ASCII allocation plain mode renders, and the native frames that draw
truth fields. A purged, an invalidated, an unavailable, a denied and an unknown value
each keep their own token, and a genuine zero stays a value.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from rich.color import Color as RichColor
from rich.segment import Segment
from rich.style import Style
from textual.color import Color
from textual.filter import LineFilter, Monochrome

from eawf.kernel.config.registry.config_keys import CONFIG_REGISTRY
from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, build_route_projection
from eawf.kernel.projection.spine import SpineView, build_spine_view
from eawf.kernel.projection.transcript import LANES
from eawf.kernel.projection.truth import (
    Freshness,
    Precision,
    TruthField,
    TruthKind,
    TruthState,
)
from eawf.kernel.runtime.events import RunEventKind
from eawf.kernel.state.enums import MeasurementQuality
from eawf.surfaces.tui.console.app import MARK_META, Body, ConsoleApp, compose_frame
from eawf.surfaces.tui.console.cells import (
    NO_VALUE,
    QUALITY_WORD,
    REASON_CELLS,
    Mark,
    Span,
    ValueCell,
    spans,
    value_cell,
)
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.harness import Contract, FrameState, load_contract
from eawf.surfaces.tui.console.normalisation import Normaliser, load_map
from eawf.surfaces.tui.console.plain import ascii_twin, plain_rows, render_plain
from eawf.surfaces.tui.console.registry import REGISTRY, RouteGroup
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.read_model import counts
from eawf.surfaces.tui.console.renderers.transcript import NATIVE_GLYPH, TR_GLYPH
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from eawf.surfaces.tui.console.tokens import (
    CONNECTION,
    QUALITY,
    TRUTH,
    Glyph,
    all_glyphs,
    truth_cell,
)
from eawf.surfaces.tui.console.width import cell_len
from tests.tui.surfaces.tui.console.test_transcript_blocks import _event as transcript_event
from tests.tui.surfaces.tui.console.test_transcript_blocks import _frame as transcript_frame
from tests.tui.surfaces.tui.console.test_transcript_blocks import (
    _summarized as transcript_summarized,
)
from tests.tui.surfaces.tui.console.test_transcript_blocks import _view as transcript_view

TESTS_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_ROOT = TESTS_ROOT / "fixtures" / "console" / "golden"
AT_REVISION = 1
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"

#: Allocation A, as the packet's glyph table ratifies it: glyph and one-cell ASCII twin.
RATIFIED_TRUTH = {
    "unknown": ("?", "?"),
    "unavailable": ("∅", "-"),
    "denied": ("⊘", "x"),
    "purged": ("✗", "X"),
    "invalidated": ("!", "!"),
    "zero": ("0", "0"),
}
RATIFIED_QUALITY = {"measured": ("", ""), "derived": ("~", "~"), "estimated": ("≈", "^")}
RATIFIED_CONNECTION = {
    "LIVE": ("●", "*"),
    "LIVE / PARTIAL": ("◑", "+"),
    "DEGRADED": ("◐", "%"),
    "DISCONNECTED": ("◌", "o"),
    "REPLAYING": ("►", ">"),
    "GAP DETECTED": ("▲", "^"),
    "SNAPSHOT LOADING": ("◈", "~"),
    "SNAPSHOT REQUIRED": ("◇", "<"),
    "OFFLINE SNAPSHOT": ("▪", "#"),
}

#: The five absences a value slot holds instead of a value.
ABSENT: tuple[TruthState, ...] = tuple(s for s in TruthState if s is not TruthState.KNOWN)

#: A quality marker set apart from its numeral, which CON-064 forbids.
SPACED_MARKER = re.compile(r"(?<![\w.])[~≈] \d")


def _field(
    state: TruthState = TruthState.KNOWN,
    *,
    value: str | None = "4.62",
    quality: MeasurementQuality = MeasurementQuality.EXACT,
    reason: str = "producer silent",
    producer: str = "rate card",
) -> TruthField[str]:
    """Return one validated truth field in ``state``."""
    known = state is TruthState.KNOWN
    return TruthField[str](
        value=value if known else None,
        state=state,
        truth_kind=TruthKind.STORED,
        producer=producer,
        producer_revision=AT_REVISION,
        precision=(
            Precision.EXACT if quality is MeasurementQuality.EXACT else Precision.APPROXIMATE
        )
        if known
        else Precision.UNAVAILABLE,
        measurement_quality=quality if known else MeasurementQuality.UNAVAILABLE,
        freshness=Freshness.LIVE,
        provenance_refs=("urn:probe:1",) if known else (),
        missing_reason=None if known else reason,
    )


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the tracked prototype registers."""
    return load_fixture(GOLDEN_ROOT / "fixture")


@pytest.fixture(scope="module")
def contract() -> Contract:
    """Return the tracked golden contract."""
    return load_contract(GOLDEN_ROOT / "sequences")


@pytest.fixture(scope="module")
def normaliser(contract: Contract) -> Normaliser:
    """Return the normaliser that turns a pack frame into the port's expected frame."""
    return Normaliser(load_map(GOLDEN_ROOT / "normalisation-map.json"), contract.frames_by_id())


def _expected(normaliser: Normaliser, state: FrameState) -> list[str]:
    """Return the port's expected Unicode frame for one recorded state."""
    return normaliser.expected(state.id, state.frame).split("\n")


#: A spine route whose native frame is the shared record table, one status column per row.
TABLE_ROUTE = "track"


def _spine(route: str, statuses: dict[str, TruthField[str]]) -> SpineView:
    """Return a spine read model of ``route`` whose rows state the given statuses."""
    kind = ROUTE_COLLECTIONS[route][0].value
    document: dict[str, Any] = {
        kind: {
            key: {"urn": f"urn:eawf:{SCOPE}:{kind}:{key}", "revision": 1, "status": "RUNNING"}
            for key in statuses
        }
    }
    projection = build_route_projection(
        route=route, document=document, cursor=41208, scope_id=SCOPE, generated_at=AT
    )
    spine = build_spine_view(projection)
    rows = tuple(
        dataclasses.replace(row, fields={**row.fields, "status": statuses[row.key]})
        if row.key in statuses
        else row
        for row in spine.rows
    )
    return dataclasses.replace(spine, rows=rows)


def _native(fixture: Fixture, spine: SpineView, *, w: int = 120) -> list[str]:
    """Return the native frame ``spine`` renders."""
    session = Session()
    session.route = spine.route
    return render_route(View(session=session, fixture=fixture, w=w, h=30, projection=spine))


# ---------- CON-063: Allocation A is the one allocation ----------


def test_con_063_the_token_tables_are_allocation_a() -> None:
    def table(glyphs: dict[str, Glyph]) -> dict[str, tuple[str, str]]:
        return {name: (g.unicode, g.ascii) for name, g in glyphs.items()}

    assert table(TRUTH) == RATIFIED_TRUTH
    assert table(QUALITY) == RATIFIED_QUALITY
    assert table(CONNECTION) == RATIFIED_CONNECTION


def test_con_063_a_glyph_carries_exactly_the_two_character_modes() -> None:
    assert [f.name for f in dataclasses.fields(Glyph)] == ["unicode", "ascii"]


def test_con_063_no_setting_selects_an_allocation() -> None:
    glyph_keys = [key for key in CONFIG_REGISTRY if "glyph" in key.key]
    assert glyph_keys
    for key in glyph_keys:
        assert set(key.choices or ()) <= {"auto", "ascii", "unicode"}, key.key
    assert not [key.key for key in CONFIG_REGISTRY if "allocation" in key.key]


def test_con_063_the_retired_token_names_are_gone() -> None:
    for retired in ("failed", "attention"):
        with pytest.raises(KeyError):
            truth_cell(retired)


# ---------- CON-064: the quality marker is a prefix against the numeral ----------


@pytest.mark.parametrize(
    ("quality", "slot"),
    [
        (MeasurementQuality.EXACT, "4.62"),
        (MeasurementQuality.RECONSTRUCTED, "~4.62"),
        (MeasurementQuality.ESTIMATED, "≈4.62"),
    ],
)
def test_con_064_quality_renders_as_a_prefix_with_no_space(
    quality: MeasurementQuality, slot: str
) -> None:
    cell = value_cell(_field(quality=quality))
    assert cell.slot == slot
    assert not SPACED_MARKER.search(cell.full)


def test_con_064_the_derived_marker_is_the_plain_tilde_never_the_small_one() -> None:
    assert QUALITY["derived"].unicode == "~" == QUALITY["derived"].ascii
    assert "\u02dc" not in all_glyphs()  # the modifier small tilde reads as estimated


def test_con_064_no_expected_frame_sets_a_marker_apart_from_its_numeral(
    contract: Contract, normaliser: Normaliser
) -> None:
    spaced = [
        state.id
        for state in contract.states
        if any(SPACED_MARKER.search(row) for row in _expected(normaliser, state))
    ]
    assert spaced == []


# ---------- CON-065: each absence is exactly one token in the value slot ----------


@pytest.mark.parametrize("state", ABSENT)
def test_con_065_an_absent_value_renders_exactly_its_one_token(state: TruthState) -> None:
    cell = value_cell(_field(state))
    assert cell.slot == TRUTH[state.value].unicode
    assert cell_len(cell.slot) == 1
    assert cell.mark is Mark(state.value)


def test_con_065_the_denied_token_is_never_the_shadowed_glyph() -> None:
    assert value_cell(_field(TruthState.DENIED)).slot == "⊘"
    assert "≠" not in {g.unicode for g in TRUTH.values()}


def test_con_065_the_native_frame_draws_all_five_absences_distinctly(fixture: Fixture) -> None:
    keys = {state: f"TRK-{i:04d}" for i, state in enumerate(ABSENT, start=1)}
    spine = _spine(TABLE_ROUTE, {key: _field(state) for state, key in keys.items()})
    rows = _native(fixture, spine)
    drawn = {}
    for state, key in keys.items():
        row = next(r for r in rows if key in r)
        drawn[state] = row.split("track", 1)[1].split()[0]
    assert drawn == {state: TRUTH[state.value].unicode for state in ABSENT}
    assert len(set(drawn.values())) == len(ABSENT)


# ---------- CON-066: a genuine zero is a value ----------


def test_con_066_a_measured_zero_renders_zero_and_says_so() -> None:
    cell = value_cell(_field(value="0"))
    assert (cell.slot, cell.mark, cell.reason) == ("0", Mark.ZERO, "measured · a real zero")


def test_con_066_zero_is_distinct_from_every_absence() -> None:
    zero = value_cell(_field(value="0")).slot
    assert zero not in {value_cell(_field(state)).slot for state in ABSENT}


def test_con_066_an_unavailable_quality_is_never_a_marker() -> None:
    cell = value_cell(_field(value=None, quality=MeasurementQuality.UNAVAILABLE))
    assert (cell.slot, cell.mark, cell.reason) == (NO_VALUE, None, "")
    assert value_cell(_field(TruthState.UNAVAILABLE)).slot == "∅"
    assert "∅" not in {g.unicode for g in QUALITY.values()}


def test_con_066_the_native_frame_draws_a_zero_as_zero(fixture: Fixture) -> None:
    spine = _spine(TABLE_ROUTE, {"TRK-0001": _field(value="0")})
    row = next(r for r in _native(fixture, spine) if "TRK-0001" in r)
    assert "track       0 measured · a real zero" in row


# ---------- CON-067: ASCII twins, one for one ----------


def test_con_067_every_token_twin_is_one_ascii_cell_like_its_glyph() -> None:
    for table in (TRUTH, QUALITY, CONNECTION):
        for glyph in table.values():
            assert glyph.ascii.isascii()
            assert cell_len(glyph.ascii) == cell_len(glyph.unicode)


def test_con_067_no_two_value_twins_share_a_meaning() -> None:
    twins = [g.ascii for g in (*TRUTH.values(), *QUALITY.values()) if g.ascii]
    assert len(twins) == len(set(twins))


def test_con_067_the_derived_marker_is_byte_identical_in_both_modes() -> None:
    assert ascii_twin("~4.62") == "~4.62"
    assert ascii_twin("≈18.80 ∅ ⊘ ✗") == "^18.80 - x X"


def test_con_067_the_keybar_re_lays_arrow_pairs_as_words() -> None:
    bar = " ↑↓ row   Enter run   Esc back" + " " * 50
    rows = plain_rows(["x" * 80, bar])
    assert rows[-1].startswith(" up/dn row   Enter run   Esc back")
    assert cell_len(rows[-1]) == 80


def test_con_067_a_keybar_whose_words_would_drop_a_key_keeps_its_glyph_twins() -> None:
    bar = " ←→ move"
    last = 0
    while cell_len(f"{bar}   k{last} verb") <= 79:
        bar += f"   k{last} verb"
        last += 1
    kept = plain_rows(["x" * 80, bar + " " * (80 - cell_len(bar))])[-1]
    assert kept.startswith(" <> move   k0 verb")
    assert cell_len(kept) == 80
    assert f"k{last - 1} verb" in kept


def test_con_067_a_keybar_with_no_arrow_is_left_alone() -> None:
    assert plain_rows(["a", "b│"]) == ["a", "b|"]


# ---------- CON-068 and CON-069: which fields carry a marker ----------

#: Every field class the rows name, and whether it is a measurement.
FIELD_CLASSES = {
    "measurement": False,
    "identifier": True,
    "ordinal": True,
    "position": True,
    "timestamp": True,
    "count of visible rows": True,
    "exact register count": True,
}


@pytest.mark.parametrize("field_class", sorted(FIELD_CLASSES))
@pytest.mark.parametrize(
    "quality", [MeasurementQuality.RECONSTRUCTED, MeasurementQuality.ESTIMATED]
)
def test_con_068_and_con_069_the_marker_follows_the_field_class(
    field_class: str, quality: MeasurementQuality
) -> None:
    exempt = FIELD_CLASSES[field_class]
    cell = value_cell(_field(value="12", quality=quality), exempt=exempt)
    assert cell.quality is quality  # the type keeps the quality where the glyph is dropped
    if exempt:
        assert (cell.slot, cell.mark, cell.reason) == ("12", None, "")
    else:
        assert cell.slot == QUALITY[QUALITY_WORD[quality]].unicode + "12"
        assert cell.mark is Mark(QUALITY_WORD[quality])


def test_con_068_an_incomplete_count_is_labelled_known_and_wears_no_marker(
    fixture: Fixture,
) -> None:
    spine = dataclasses.replace(_spine("run.detail", {"RUN-00000001": _field()}), complete=False)
    line = counts(spine)
    assert " · known · cursor 41,208" in line
    assert not any(span.mark for span in spans(line))


# ---------- CON-070: never-acted is an empty cell ----------


def test_con_070_never_acted_is_empty_in_both_modes_and_borrows_no_token() -> None:
    cell = truth_cell(None)
    assert cell == "" == ascii_twin(cell)
    assert cell not in {NO_VALUE, TRUTH["unavailable"].unicode, TRUTH["unavailable"].ascii}


# ---------- CON-071: every marked value states a bounded reason ----------


@pytest.mark.parametrize(
    ("cell", "reason"),
    [
        (value_cell(_field(quality=MeasurementQuality.RECONSTRUCTED)), "derived · rate card"),
        (
            value_cell(_field(quality=MeasurementQuality.ESTIMATED, producer="model p50")),
            "estimated · model p50",
        ),
        (
            value_cell(_field(TruthState.UNAVAILABLE, reason="probe uncertified")),
            "unavailable · probe uncertified",
        ),
        (value_cell(_field(value="0")), "measured · a real zero"),
        (value_cell(_field(TruthState.PURGED, reason="retention 7d")), "purged · retention 7d"),
        (
            value_cell(_field(TruthState.INVALIDATED, reason="head moved")),
            "invalidated · head moved",
        ),
        (
            value_cell(_field(TruthState.DENIED, reason="authority class")),
            "denied · authority class",
        ),
        (value_cell(_field(TruthState.UNKNOWN)), "unknown · producer silent"),
    ],
)
def test_con_071_a_marked_value_states_its_word_and_basis(cell: ValueCell, reason: str) -> None:
    assert cell.reason == reason
    assert cell.full == f"{cell.slot} {reason}"


def test_con_071_a_bare_measured_value_states_no_reason() -> None:
    cell = value_cell(_field())
    assert (cell.reason, cell.full) == ("", "4.62")


@pytest.mark.parametrize(("extra", "clipped"), [(0, False), (1, True)])
def test_con_071_the_reason_is_bounded(extra: int, clipped: bool) -> None:
    head = "unknown · "
    basis = "b" * (REASON_CELLS - cell_len(head) + extra)
    reason = value_cell(_field(TruthState.UNKNOWN, reason=basis)).reason
    assert cell_len(reason) <= REASON_CELLS
    assert reason.endswith("…") is clipped


def test_con_071_the_native_status_column_states_token_and_reason(fixture: Fixture) -> None:
    field = _field(TruthState.PURGED, reason="retention 7d")
    row = next(r for r in _native(fixture, _spine(TABLE_ROUTE, {"TRK-1": field})) if "TRK-1" in r)
    assert "✗ purged · retention 7d" in row


# ---------- CON-072: the vocabulary is exactly the four qualities ----------


def test_con_072_the_rendering_vocabulary_is_the_four_measurement_qualities() -> None:
    assert set(QUALITY_WORD) == set(MeasurementQuality)
    assert set(QUALITY) == {"measured", "derived", "estimated"}
    markers = {m for m in Mark if m.value in QUALITY}
    assert markers == {Mark.DERIVED, Mark.ESTIMATED}


def test_con_072_a_reconstructed_value_renders_as_derived() -> None:
    cell = value_cell(_field(quality=MeasurementQuality.RECONSTRUCTED, producer="replayed"))
    assert (cell.slot, cell.mark, cell.reason) == ("~4.62", Mark.DERIVED, "derived · replayed")


def test_con_072_a_mark_outside_the_vocabulary_is_refused() -> None:
    with pytest.raises(ValueError, match="stale"):
        Mark("stale")


# ---------- CON-073: colour carries severity, the text carries the fact ----------


class _Deuteranopia(LineFilter):
    """Simulate green-blind vision by collapsing red and green onto one channel."""

    def apply(self, segments: list[Segment], background: Color) -> list[Segment]:
        out: list[Segment] = []
        for text, style, control in segments:
            if style is not None and style.color is not None:
                r, g, b = style.color.get_truecolor()
                mixed = (r + g) // 2
                style = style + Style(color=RichColor.from_rgb(mixed, mixed, b))
            out.append(Segment(text, style, control))
        return out


#: Routes painted under the colour-free filters: the transcript, and routes drawing truth
#: tokens and quality markers.
PAINTED_ROUTES = ("transcript", "health", "trust", "cost.ceiling", "unattended", "run.detail")


async def _paint(fixture: Fixture) -> list[tuple[str, list[Segment]]]:
    app = ConsoleApp(fixture, FakeClock())
    w, h = SIZES[1]
    painted: list[tuple[str, list[Segment]]] = []
    async with app.run_test(size=(w, h)) as pilot:
        for route in PAINTED_ROUTES:
            app.reset(SessionSetup(route=route, size=1))
            app.render_frame()
            await pilot.pause()
            body = app.query_one("#body", Body)
            for y in range(len(body.rows)):
                painted.append((route, list(body.render_line(y))))
    return painted


@pytest.fixture(scope="module")
def painted(fixture: Fixture) -> list[tuple[str, list[Segment]]]:
    """Return every painted body line of the painted routes, with its segments."""
    return asyncio.run(_paint(fixture))


def test_con_073_the_painter_carries_each_mark_on_its_segment(
    painted: list[tuple[str, list[Segment]]],
) -> None:
    marked = [
        (route, seg.text, seg.style.meta[MARK_META])
        for route, segments in painted
        for seg in segments
        if seg.style is not None and MARK_META in seg.style.meta
    ]
    assert {Mark.UNKNOWN, Mark.DERIVED, Mark.ESTIMATED} <= {Mark(m) for _r, _t, m in marked}
    for _route, text, mark in marked:
        assert [s.mark for s in spans(text)] == [Mark(mark)]


@pytest.mark.parametrize("line_filter", [Monochrome(), _Deuteranopia()], ids=["gray", "cvd"])
def test_con_073_every_mark_is_legible_with_colour_removed(
    painted: list[tuple[str, list[Segment]]], line_filter: LineFilter
) -> None:
    seen_transcript = False
    for route, segments in painted:
        filtered = line_filter.apply(segments, Color(0, 0, 0))
        text = "".join(seg.text for seg in filtered)
        assert text == "".join(seg.text for seg in segments)
        painted_marks = [
            Mark(seg.style.meta[MARK_META])
            for seg in segments
            if seg.style is not None and MARK_META in seg.style.meta
        ]
        assert [s.mark for s in spans(text) if s.mark] == painted_marks
        seen_transcript |= route == "transcript"
    assert seen_transcript


@pytest.mark.parametrize("mark", list(Mark))
def test_con_073_a_marked_span_is_a_token_plus_its_word_or_a_marked_numeral(mark: Mark) -> None:
    if mark in (Mark.DERIVED, Mark.ESTIMATED):
        text = f"{QUALITY[mark.value].unicode}4.62"
    elif mark is Mark.ZERO:
        text = "0 measured"
    else:
        text = f"{TRUTH[mark.value].unicode} {mark.value}"
    assert spans(f" x {text} · y") == (Span(" x ", None), Span(text, mark), Span(" · y", None))


def test_con_073_a_token_beside_another_word_is_not_painted() -> None:
    assert spans("? help   ! error   ∅ not started") == (
        Span("? help   ! error   ∅ not started", None),
    )


def test_con_073_spans_of_an_empty_row_are_empty() -> None:
    assert spans("") == ()


def test_con_073_spans_rebuild_every_expected_row_exactly(
    contract: Contract, normaliser: Normaliser
) -> None:
    for state in contract.states:
        for row in _expected(normaliser, state):
            assert "".join(span.text for span in spans(row)) == row


# ---------- CON-074: ASCII mode on the densest screen of each group ----------


def _densest_per_group(contract: Contract, normaliser: Normaliser) -> dict[RouteGroup, FrameState]:
    """Return the tracked 80-column route frame with the most drawn rows, per group."""
    best: dict[RouteGroup, tuple[int, FrameState]] = {}
    for state in contract.states:
        route = state.setup.route
        if route is None or route not in REGISTRY.by_id:
            continue
        if state.rack or state.setup.overlay or state.keys:
            continue
        if state.size != SIZES[0]:
            continue
        drawn = sum(1 for row in _expected(normaliser, state) if row.strip())
        family = REGISTRY.by_id[route].group
        if family not in best or drawn > best[family][0]:
            best[family] = (drawn, state)
    return {family: state for family, (_n, state) in best.items()}


def _plain(fixture: Fixture, state: FrameState) -> list[str]:
    return render_plain(
        fixture, state.setup, keys=state.keys, conn=state.setup.conn or "LIVE", verbose=False
    )


def _has_legend(rows: list[str]) -> bool:
    return any(row.startswith(" TRUTH TOKENS") for row in rows[:-1])


def test_con_074_ascii_mode_renders_the_densest_screen_of_every_group(
    fixture: Fixture, contract: Contract, normaliser: Normaliser
) -> None:
    """The densest screen of every group that draws a token carries the legend.

    The frame composer's legend row budget frees blank rows and thin rules, so even a
    screen whose every other row states a fact keeps all of them and the legend (CON-168).
    """
    densest = _densest_per_group(contract, normaliser)
    assert len(densest) >= 6
    in_frame = 0
    for group, state in densest.items():
        rows = _plain(fixture, state)
        assert all(row.isascii() for row in rows), group
        assert [cell_len(row) for row in rows] == [SIZES[0][0]] * SIZES[0][1], group
        tokens = any(span.mark for row in _expected(normaliser, state) for span in spans(row))
        if not tokens:
            assert not _has_legend(rows), group
            continue
        assert _has_legend(rows), group
        in_frame += 1
    assert in_frame >= 3


def test_con_074_the_legend_never_takes_a_row_that_states_a_fact() -> None:
    body = ["~4.62 derived", *[f"fact {i}" for i in range(20)], " ", " Esc back"]
    assert plain_rows(body) == [*body[:-1], " Esc back"]


def test_con_074_the_legend_names_every_twin_the_frame_draws(fixture: Fixture) -> None:
    rows = render_plain(fixture, SessionSetup(route="cost.ceiling"))
    legend = "\n".join(rows)
    for name, glyph in TRUTH.items():
        word = "a real zero" if name == "zero" else name
        assert f"{glyph.ascii} {word}" in legend
    assert "bare measured  ~ derived  ^ estimated" in legend
    assert rows[-1].startswith(" up/dn row")


def test_con_074_a_frame_with_no_token_carries_no_legend() -> None:
    assert plain_rows(["plain row", " Esc back"]) == ["plain row", " Esc back"]


# ---------- CON-075: column boundaries hold across the two modes ----------

_BOUNDARY = re.compile(r"(?:^|(?<=  ))\S")


def _boundaries(row: str) -> list[int]:
    """Return the cell offset of every column start: text after two spaces or at the edge."""
    return [cell_len(row[: m.start()]) for m in _BOUNDARY.finditer(row)]


def test_con_075_the_boundary_probe_reds_on_a_two_cell_twin() -> None:
    assert _boundaries("a  ≈b  c") != _boundaries("a  ^^b  c")
    assert _boundaries("a  ≈b  c") == _boundaries("a  ^b  c")


def test_con_075_every_column_boundary_holds_between_the_two_modes(
    contract: Contract, normaliser: Normaliser
) -> None:
    checked = 0
    for state in contract.states:
        if state.rack:
            continue
        unicode = _expected(normaliser, state)
        ascii_rows = plain_rows(unicode)
        # the legend frees only blank rows and thin rules, which have no column to hold
        facts = [r for r in unicode[:-1] if r.strip(" ─")]
        kept = [r for r in ascii_rows[:-1] if r.strip(" -")]
        head = next((i for i, r in enumerate(kept) if r.startswith(" TRUTH TOKENS")), None)
        if head is not None:
            kept = kept[:head]
            facts = facts[:head]
        assert len(facts) == len(kept), state.id
        for i, (u, a) in enumerate(zip(facts, kept, strict=True)):
            assert _boundaries(u) == _boundaries(a), f"{state.id} row {i}"
            checked += 1
    assert checked > 1000


# ---------- CON-076: an event row is a token and the full word ----------

_COALESCED = re.compile(r"^(?P<kind>[a-z]+) ×(?P<count>\d+) · (?P<span>\d+[smh])$")  # noqa: RUF001
_EVENT = re.compile(r"^(?P<glyph>\S) (?P<kind>[a-z]+)(?: · [a-z]+)?$")


def test_con_076_every_timeline_row_is_token_and_word_or_a_coalesced_span(
    fixture: Fixture,
) -> None:
    kinds = {row[1] for row in fixture.proto.timeline}
    assert any(_COALESCED.match(kind) for kind in kinds)
    for kind in kinds:
        coalesced = _COALESCED.match(kind)
        if coalesced is not None:
            assert int(coalesced.group("count")) > 1
            continue
        event = _EVENT.match(kind)
        assert event is not None, kind
        assert TR_GLYPH[event.group("kind")] == event.group("glyph"), kind


def test_con_076_the_run_frame_draws_each_event_kind_whole(fixture: Fixture) -> None:
    rows = render_plain(fixture, SessionSetup(route="run.detail"), conn="LIVE")
    unicode = "\n".join(
        compose_frame(
            View(
                session=_session_at("run.detail", fixture),
                fixture=fixture,
                w=SIZES[0][0],
                h=SIZES[0][1],
            )
        )
    )
    assert rows
    for kind in {row[1] for row in fixture.proto.timeline}:
        assert kind in unicode


def _session_at(route: str, fixture: Fixture) -> Session:
    session = Session()
    session.reset(
        SessionSetup(route=route),
        settings_section_order=fixture.settings.section_order,
        now=FakeClock().now(),
    )
    return session


def test_con_076_a_native_transcript_block_names_its_kind_in_full() -> None:
    frame = transcript_frame(transcript_view((transcript_event(1), transcript_summarized(2))))
    heads = [row for row in frame.split("\n") if _BLOCK_HEAD.match(row)]
    assert len(heads) == 2
    for row in heads:
        found = _BLOCK_HEAD.match(row)
        assert found is not None
        kind = RunEventKind(found.group("kind"))
        # the kind cell is the lane's glyph and its whole word, never an abbreviation
        assert found.group("word") == LANES[kind]
        assert found.group("glyph") == NATIVE_GLYPH[found.group("word")]


_BLOCK_HEAD = re.compile(
    r"^[ ▸]\d\d:\d\d:\d\d  (?P<glyph>\S) (?P<word>[a-z]+)\s+(?P<kind>[a-z_.]+) · "
)
