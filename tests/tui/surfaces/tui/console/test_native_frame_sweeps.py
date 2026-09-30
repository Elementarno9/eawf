"""The frame grammar holds on every frame the native console draws, not only on the pack's.

The chrome sweeps in ``test_chrome_sweeps`` read the prototype registers the golden pack
replays. These read the frames a native console draws from a read model: every route in
the registry, at all three sizes, before and after a short walk of keys, in two worlds.
One world is a real daemon serving the accepted canary tree over its own socket; the
other is a seam reading a richer epoch-2 document through the route projection producer,
so the groups, buckets and long lists a fresh canary does not hold are drawn too.

The sweeps prove CON-154 (the keybar is the only place a key is promised), CON-155 (a
frame states facts, never instructions), CON-156 (the caret sits against its row),
CON-157 (a group title is not a row), CON-158 (an identifier never gives way), CON-162
(each region of a multi-region frame windows on its own), CON-165 (four provenance
glyphs, and an unread key said in words), CON-166 (no em dash stands as a value) and
CON-167 (every drawn glyph is a table row, and state-slot markers stay in the slot).
Every widened predicate is paired with a seeded row it must red on.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterator, Sequence
from itertools import pairwise
from typing import Any

import pytest

from eawf.surfaces.tui.console.app import ConsoleApp, compose_frame
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers.provenance import (
    GLYPH_DEFAULT,
    GLYPH_INHERITS,
    GLYPH_SHADOWED,
    GLYPH_WINS,
    UNREAD,
)
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from tests.tui.surfaces.tui.console import journey_support as js
from tests.tui.surfaces.tui.console import test_chrome_sweeps as cs
from tests.tui.surfaces.tui.console import test_glyph_allocation_and_ascii_twins as gl
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies
from tests.tui.surfaces.tui.console.test_console_live_smoke import (
    live_console,
    render_setup,
    walk_canary_isolated,
)

#: The keys walked after each route's first frame; a frame is captured after each.
WALK: tuple[str, ...] = ("ArrowDown", "ArrowDown", "ArrowDown", "Tab", "ArrowDown")

#: The keys that open a route's filter, type into it and keep it, walked where it is bound.
FILTER_WALK: tuple[str, ...] = ("\\", "x", "Enter")

#: How many draft and deferred Tasks the held world adds, so the backlog windows.
DRAFTS, DEFERRED = 30, 8

#: A Task key longer than any column's default and sorted first, so a key that would give
#: way is drawn on the first frame of every list.
LONG_KEY = "CANARYW37-0001"

#: A deprecated key the live tree's local layer states, which no code reads any more.
UNREAD_KEY = "ship.require_audit_pass"


def _task(key: str, status: str, intent: str) -> dict[str, Any]:
    return {**bodies._row("task", key, status), "intent": intent}


def held_document() -> dict[str, Any]:
    """Return the probe tree with a backlog long enough to window in both groups."""
    tasks = dict(bodies.DOCUMENT["task"])
    tasks[LONG_KEY] = _task(LONG_KEY, "DRAFT", "Repair a conflicting delivery")
    for i in range(1, DRAFTS):
        tasks[f"TSK-{100 + i:04d}"] = _task(f"TSK-{100 + i:04d}", "DRAFT", f"Draft number {i}")
    for i in range(DEFERRED):
        tasks[f"TSK-{900 + i:04d}"] = _task(f"TSK-{900 + i:04d}", "DEFERRED", f"Later {i}")
    runs = dict(bodies.DOCUMENT["run"])
    runs["RUN-00000004"] = {
        **bodies._row("run", "RUN-00000004", "QUEUED"),
        "scope": {"purpose": "implement", "task_ref": f"{bodies.ROOT}/task/{LONG_KEY}"},
    }
    return {**bodies.DOCUMENT, "task": tasks, "run": runs}


async def _sweep(app: ConsoleApp, pilot: Any, world: str, frames: dict[str, list[str]]) -> None:
    """Render every route at every size, then each step of :data:`WALK`, into ``frames``."""
    for size, (w, _h) in enumerate(SIZES):
        for route in REGISTRY.ids:
            text = await render_setup(app, pilot, SessionSetup(route=route, size=size))
            frames[f"{world} {route}@{w}"] = text.splitlines()
            for step, key in enumerate(WALK):
                dispatch(app._ctx(), key, False)
                frames[f"{world} {route}@{w}+{step}"] = list(compose_frame(app.view()))
            if "\\" not in {token for token, _label in cs.keybar_pairs(text.splitlines()[-1])}:
                continue
            await render_setup(app, pilot, SessionSetup(route=route, size=size))
            for step, key in enumerate(FILTER_WALK):
                dispatch(app._ctx(), key, False)
                frames[f"{world} {route}@{w}\\{step}"] = list(compose_frame(app.view()))


async def _settings_keys(app: ConsoleApp, pilot: Any, frames: dict[str, list[str]]) -> None:
    """Walk every key of every live settings section, so every listed key row is drawn."""
    await render_setup(app, pilot, SessionSetup(route="settings", size=1))
    settings = app.view().settings
    assert settings is not None
    for section in settings.sections():
        for index in range(len(settings.keys_of(section))):
            frames[f"live settings@120#{section}.{index}"] = list(compose_frame(app.view()))
            dispatch(app._ctx(), "ArrowDown", False)
        dispatch(app._ctx(), "Tab", False)


@pytest.fixture(scope="module")
def native(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[str]]:
    """Return every native frame the sweeps read, keyed ``<world> <route>@<width>[+step]``."""
    walk, runtime_root = walk_canary_isolated(tmp_path_factory.mktemp("canary"))
    # a key no code reads, still stated by a file layer, is the one the route lists unread
    local = walk.canary.root / ".ea" / "local" / "config.yaml"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("ship:\n  require_audit_pass: true\n", encoding="utf-8")

    async def body() -> dict[str, list[str]]:
        frames: dict[str, list[str]] = {}
        async with (
            live_console(walk.canary.root, runtime_root) as (app, _seam),
            app.run_test(size=SIZES[0]) as pilot,
        ):
            await _sweep(app, pilot, "live", frames)
            await _settings_keys(app, pilot, frames)
        held = js.held_app(js.DocumentDaemon(held_document()))
        async with held.run_test(size=SIZES[0]) as pilot:
            await held.workers.wait_for_complete()
            await _sweep(held, pilot, "held", frames)
        return frames

    return asyncio.run(body())


def _found(
    frames: dict[str, list[str]], sweep: Callable[[Sequence[str]], Sequence[str]]
) -> dict[str, list[str]]:
    """Return what ``sweep`` finds, by frame, leaving out the frames it finds nothing in."""
    return {fid: list(found) for fid, rows in frames.items() if (found := sweep(rows))}


def test_the_native_sweep_covers_every_route_at_three_sizes_in_both_worlds(
    native: dict[str, list[str]],
) -> None:
    for world in ("live", "held"):
        for route in REGISTRY.ids:
            for w, _h in SIZES:
                assert f"{world} {route}@{w}" in native
    assert {len(rows[0]) for rows in native.values()} == {w for w, _h in SIZES}
    assert not any("NOT HELD" in rows[1] for fid, rows in native.items() if "scope.home" in fid)


# ---------- CON-154: the keybar is the only place a key is promised ----------

#: A key a body row names with what it does: a go chord, or a bound key and its verb.
_KEY_PROMISE = re.compile(
    r"(?<![\w-])(?P<key>g [a-z]|Enter|Esc|Tab|↑↓|[a-z])"
    r"(?: on the \w+ route)? (?:shows|opens|cycles|walks|clears|picks|keeps|starts)\b"
)


def promised_keys(rows: Sequence[str]) -> list[str]:
    """Return every key a body row promises that the frame's keybar does not advertise.

    A row may say what an advertised key will do to this entity; a key the bar does not
    carry, a go chord above all, is a promise made somewhere other than the keybar.
    """
    bar = {token for token, _label in cs.keybar_pairs(rows[-1])}
    return [
        row.strip()
        for row in cs.body_rows(rows)
        for found in _KEY_PROMISE.finditer(row)
        if found.group("key") not in bar
    ]


def test_con_154_no_native_frame_restates_a_keybar_pair(native: dict[str, list[str]]) -> None:
    assert _found(native, cs.restated_keys) == {}


def test_con_154_no_native_frame_promises_a_key_its_keybar_does_not_carry(
    native: dict[str, list[str]],
) -> None:
    assert _found(native, promised_keys) == {}


@pytest.mark.parametrize(
    "row",
    [
        " WHAT TO DO   nothing. Runs continue without you. g a shows what is executing.",
        " LENS       repo · l on the Settings route cycles the file layers",
        "   Esc clears the bucket",
    ],
)
def test_con_154_the_promise_sweep_reds_on_a_seeded_row(row: str) -> None:
    assert promised_keys([" header", row, " Tab buckets   . actions"]) == [row.strip()]


def test_con_154_an_advertised_key_may_say_what_it_does_here() -> None:
    rows = [
        " header",
        "   no events recorded yet · Enter opens the transcript",
        " Enter transcript",
    ]
    assert promised_keys(rows) == []


# ---------- CON-155: a frame states facts, never instructions ----------

#: A row holding one bold label and nothing else.
_LONE_LABEL = re.compile(r"^ *[A-Z][A-Z]+(?: [A-Z]+)* *$")
#: A table head: two or more capitalised column names apart by two or more blanks.
_HEAD = re.compile(r"^ *[A-Z][A-Z.·]*(?: [A-Z]+)*(?: {2,}[A-Z][A-Z.·]*(?: [A-Z]+)*)+ *$")


def lone_labels(rows: Sequence[str]) -> list[str]:
    """Return every label that takes a row of its own above the table head it introduces."""
    body = cs.body_rows(rows)
    return [a.strip() for a, b in pairwise(body) if _LONE_LABEL.match(a) and _HEAD.match(b)]


def test_con_155_no_native_frame_narrates_the_interface(native: dict[str, list[str]]) -> None:
    assert _found(native, cs.instructions) == {}


def test_con_155_no_native_label_takes_a_row_above_its_head(
    native: dict[str, list[str]],
) -> None:
    assert _found(native, lone_labels) == {}


def test_con_155_the_label_sweep_reds_on_a_seeded_label_row() -> None:
    rows = [" h", " TRACK RECORD", "    AGENT           ACCEPTED   REJECTED   RATE", " Esc back"]
    assert lone_labels(rows) == ["TRACK RECORD"]
    shared = [" h", " TRACK RECORD       ACCEPTED   REJECTED   RATE", " Esc back"]
    assert lone_labels(shared) == []


# ---------- CON-156: the caret sits against the row it marks ----------


def test_con_156_no_native_frame_draws_a_caret_clear_of_its_row(
    native: dict[str, list[str]],
) -> None:
    assert _found(native, cs.detached_carets) == {}


#: A caret with a gutter of blanks before it, deep in a row rather than in the gutter.
_DEEP_CARET = re.compile(r"^ {4,}▸ ")


def test_con_156_the_native_backlog_draws_its_caret_in_the_gutter(
    native: dict[str, list[str]],
) -> None:
    rows = [
        row
        for fid, frame in native.items()
        if " backlog@" in fid
        for row in cs.body_rows(frame)
        if "▸" in row
    ]
    assert rows
    assert [row for row in rows if _DEEP_CARET.match(row)] == []


# ---------- CON-157: a group title is not a row ----------


def test_con_157_every_native_group_title_sits_flush_without_a_caret(
    native: dict[str, list[str]],
) -> None:
    titled = {
        fid.split("@")[0] for fid, rows in native.items() if any(cs._TITLE.match(r) for r in rows)
    }
    assert {"held attention", "held backlog", "live backlog"} <= titled
    assert _found(native, cs.misplaced_titles) == {}


# ---------- CON-158: a value keeps its form, and an identifier never gives way ----------

#: An identifier, enumeration or truth token cut to an ellipsis: an upper-case token, a
#: key prefix, a provider name or the unknown token, with the ellipsis glued to it.
_CLIPPED = re.compile(
    r"(?<![\w-])(?:[A-Z][A-Z0-9_]+(?:-[0-9A-Za-z]*)?|[A-Z]+-|clau?d?e?|code?x?|\?)…"
)


def clipped_ids(rows: Sequence[str]) -> list[str]:
    """Return every identifier, enumeration or provider name a row shortened."""
    return [found.group(0) for row in rows for found in _CLIPPED.finditer(row)]


@pytest.mark.parametrize(
    "row",
    [
        "RUN-… writes it",
        " ▸ W37CANARY… Repair",
        "W37CANARY-00… QUEUED",
        "W37CANARY-0001…  QUEUED",
        "waiting for a slot    ?…",
    ],
)
def test_con_158_the_clip_sweep_reds_on_a_seeded_identifier(row: str) -> None:
    assert clipped_ids([row]) != []


def test_con_158_a_title_cut_at_a_word_is_not_an_identifier() -> None:
    assert clipped_ids(["   TRK-CANARY Rehearse native …", "Deliver one chan…"]) == []


def test_con_158_no_native_frame_cuts_an_identifier(native: dict[str, list[str]]) -> None:
    assert _found(native, clipped_ids) == {}


def test_con_158_a_long_task_key_is_drawn_whole_on_every_native_list(
    native: dict[str, list[str]],
) -> None:
    for route in ("backlog", "activity", "unattended"):
        for w, _h in SIZES:
            assert any(LONG_KEY in row for row in native[f"held {route}@{w}"]), (route, w)


def test_con_158_a_native_frame_advertising_arrows_draws_their_position(
    native: dict[str, list[str]],
) -> None:
    unmarked = [
        fid
        for fid, rows in native.items()
        if "↑↓" in rows[-1] and not any(cs._POSITION.search(row) for row in cs.body_rows(rows))
    ]
    assert unmarked == []


# ---------- CON-162: each region of a multi-region frame windows on its own ----------


def _region(frame: Sequence[str], title: str) -> list[str]:
    """Return the rows under group ``title`` up to the rule that closes it."""
    start = next(i for i, row in enumerate(frame) if row.startswith(f" {title} "))
    rows: list[str] = []
    for row in frame[start + 1 :]:
        if row.startswith("─"):
            break
        rows.append(row)
    return rows


def _states_extent_once(region: Sequence[str]) -> bool:
    ranged = sum(1 for row in region if row.startswith(" WINDOW"))
    edged = sum(1 for row in region if re.match(r"^ +… [\d,]+ (?:more|above|below)", row))
    return ranged + edged <= 1


def test_con_162_each_native_backlog_region_windows_on_its_own_cursor(
    native: dict[str, list[str]],
) -> None:
    """The focused group follows its cursor; the other shows its head; each states it once."""
    first = native["held backlog@80"]
    drafts, deferred = _region(first, "DRAFTS"), _region(first, "DEFERRED")
    assert len(drafts) >= 3 and len(deferred) >= 3
    assert any(row.startswith(" WINDOW") for row in drafts)
    assert "TSK-0900" in deferred[0] and "TSK-0901" in deferred[1]
    assert deferred[-1].strip() == f"… {DEFERRED - 2} more"
    walked = native["held backlog@80+2"]
    cursor = [row for row in _region(walked, "DRAFTS") if "▸" in row]
    assert len(cursor) == 1
    tabbed = native["held backlog@80+4"]
    drafts, deferred = _region(tabbed, "DRAFTS"), _region(tabbed, "DEFERRED")
    assert [row for row in deferred if "▸" in row], "the focused group draws its cursor"
    assert not [row for row in drafts if "▸" in row], "the unfocused group draws no cursor"
    assert drafts[-1].strip() == f"… {DRAFTS - 2} more"
    for fid, frame in native.items():
        if " backlog@" in fid and any(row.startswith(" DRAFTS ") for row in frame):
            for title in ("DRAFTS", "DEFERRED"):
                assert _states_extent_once(_region(frame, title)), (fid, title)


def test_con_162_the_extent_check_reds_on_a_range_beside_an_edge_count() -> None:
    assert not _states_extent_once(["   TSK-0001", " WINDOW    1–3 of 9", "   … 6 more"])  # noqa: RUF001


# ---------- CON-165: four provenance glyphs, and an unread key said in words ----------

PROVENANCE = {GLYPH_WINS, GLYPH_SHADOWED, GLYPH_INHERITS, GLYPH_DEFAULT}
#: A settings key row: the rail, the caret gutter, the provenance glyph, then the key.
_KEY_ROW = re.compile(r"│ [▸ ] (?P<glyph>\S) (?P<key>[a-z_][\w.]*) ")


def _key_rows(native: dict[str, list[str]]) -> Iterator[re.Match[str]]:
    for fid, frame in native.items():
        if fid.startswith("live settings@"):
            for row in frame:
                found = _KEY_ROW.search(row)
                if found is not None:
                    yield found


def test_con_165_every_native_key_row_wears_one_of_the_four_glyphs(
    native: dict[str, list[str]],
) -> None:
    glyphs = {found.group("glyph") for found in _key_rows(native)}
    assert glyphs
    assert glyphs <= PROVENANCE


def test_con_165_an_unread_key_says_unread_in_its_provenance_column_live(
    native: dict[str, list[str]],
) -> None:
    """The canary lists one key off the catalog; its FROM cell reads the word, no glyph."""
    name = UNREAD_KEY.split(".", 1)[1]
    rows = {
        row.rstrip()
        for fid, frame in native.items()
        if fid.startswith("live settings@120#")
        for row in frame
        if (found := _KEY_ROW.search(row)) is not None and found.group("key") == name
    }
    assert rows
    for row in rows:
        assert row.endswith(f" {UNREAD}"), row
        assert _KEY_ROW.search(row).group("glyph") in PROVENANCE  # type: ignore[union-attr]


# ---------- CON-166 and CON-167: the no-value glyph and the glyph table ----------


def test_con_166_no_native_frame_draws_an_em_dash_as_a_cell(
    native: dict[str, list[str]],
) -> None:
    def em_dash_cells(rows: Sequence[str]) -> list[str]:
        return [row for row in cs.body_rows(rows) if gl._VALUE_EM_DASH.search(row.rstrip())]

    assert _found(native, em_dash_cells) == {}


def test_con_166_a_native_attention_row_with_no_deadline_reads_due_en_dash(
    native: dict[str, list[str]],
) -> None:
    row = next(row for row in native["held attention@120"] if "ACT-0001" in row)
    assert "due –" in row  # noqa: RUF001


def test_con_167_every_glyph_a_native_frame_draws_is_a_table_row(
    native: dict[str, list[str]],
) -> None:
    prose = set("—’“”")  # noqa: RUF001

    def unlisted(rows: Sequence[str]) -> list[str]:
        return sorted({ch for row in rows for ch in row if not ch.isascii()} - set(gl.ROWS) - prose)

    assert _found(native, unlisted) == {}


def test_con_167_a_native_state_slot_marker_stays_in_the_state_slot(
    native: dict[str, list[str]],
) -> None:
    def misplaced(rows: Sequence[str]) -> list[str]:
        return [row for row in cs.body_rows(rows) if any(m in row for m in gl.STATE_SLOT_ONLY)]

    assert _found(native, misplaced) == {}
