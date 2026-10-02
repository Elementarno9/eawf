"""The settings, operations and entry frames read the way the design pack draws them.

The settings route opens on the
pack's section, closes its rail before the keybar, chooses a value from a vertical list
that keeps the stored value marked, filters its keys, and draws the stack as a boxed,
read-only overlay. The operations frames speak in operator words -- no storage name, no
requirement code, no clipped sentence -- and the entry layer prints its commands whole.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eawf.kernel.projection.integration import PULL_REQUEST_PRODUCER
from eawf.kernel.projection.operations import DISPATCH_QUEUE_PRODUCER, SANDBOX_DECISION_PRODUCER
from eawf.kernel.projection.settings import (
    EffectiveSettingsView,
    build_settings_view,
    catalog_section_order,
)
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.attach import EntryCommand, failed_state, onboarding_state
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import Breadth, View, breadth_of
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.paint import Part, paint
from eawf.surfaces.tui.console.renderers.entry import render_state
from eawf.surfaces.tui.console.renderers.provenance import rail_width, step
from eawf.surfaces.tui.console.renderers.read_model import noun, wrapped
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console import test_native_route_frames as nrf
from tests.tui.surfaces.tui.console import test_settings_route as sr

#: A requirement row id such as ``RUN-059``, which reads as a Run id in operator copy.
REQUIREMENT_ID = re.compile(r"\b[A-Z]{2,4}-\d{3}\b")


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repo root whose layers the test owns, the home redirected beside it."""
    home = tmp_path / "home"
    (home / ".config" / "eawf").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    repo = tmp_path / "repo"
    (repo / ".ea" / "local").mkdir(parents=True)
    return repo


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the golden fixture the console is built over."""
    return load_fixture(Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture")


def _view(tree: Path) -> EffectiveSettingsView:
    return build_settings_view(
        workspace=tree, repo=tree, scope_id="EAWF", cursor=41208, generated_at=sr.AT, env={}
    )


def _settings(
    fixture: Fixture, view: EffectiveSettingsView, key: str, *, w: int = 80, h: int = 24
) -> list[str]:
    return sr._frame(fixture, view, sr._on(sr._session(), view, key), w=w, h=h)


def _spans(row: str) -> dict[str, tuple[str | None, bool]]:
    """Return each painted run's text with its surface and weight."""
    return {s.text: (s.surface, s.bold) for s in paint(row, Part.BODY)}


# ---------- the history keybar keeps copy and the way back at 80 ----------


def test_the_history_bar_keeps_copy_and_back_at_80() -> None:
    """The paging pairs give way before ``Esc back`` and ``y copy`` do."""
    bar = keybar([entry.pair() for entry in native_keys("history", windowed=True)], 80)
    assert "y copy" in bar
    assert "Esc back" in bar
    assert "Home End" not in bar


def test_paging_is_kept_when_dropping_it_would_not_save_the_global() -> None:
    """A bar too wide even without its pages keeps them and loses its globals as before."""
    pairs = [("↑↓", "row"), ("PageUp PageDown", "page"), ("a", "x" * 70), ("Esc", "back")]
    bar = keybar(pairs, 80)
    assert "PageUp PageDown" in bar
    assert "Esc" not in bar


# ---------- a linked console opens Settings on planning ----------


def test_a_linked_console_opens_settings_on_planning() -> None:
    """The section cursor is placed in the kernel catalog's rail order, not the prototype's."""
    seam = ProjectionSeam(route="settings", scope_id="EAWF", state_path=None)
    app = ConsoleApp(chrome=load_chrome(), seam=seam, clock=FakeClock())
    app.reset(None)
    assert catalog_section_order()[app.session.set_sec] == "planning"


def test_the_catalog_order_is_the_order_a_built_view_draws(tree: Path) -> None:
    assert _view(tree).sections() == catalog_section_order()


# ---------- the rail, category and glyph colours ----------


def test_the_rail_selection_and_categories_take_the_accent(tree: Path, fixture: Fixture) -> None:
    rows = _settings(fixture, _view(tree), sr.LITERAL_KEY, w=120, h=30)
    category = next(row for row in rows if row.startswith("QUALITY"))
    selected = next(row for row in rows if row.startswith("▸ estimation"))
    assert _spans(category)["QUALITY"] == ("brand", True)
    assert _spans(selected)["estimation"] == ("brand", True)


@pytest.mark.parametrize(
    ("glyph", "surface"),
    [("=", "ok"), ("≠", "warn"), ("·", "dim"), ("–", "dim")],  # noqa: RUF001
)
def test_each_key_glyph_is_coloured_by_what_it_says(glyph: str, surface: str) -> None:
    row = f"  prose        │ ▸ {glyph} level                  strict      repo      "
    assert _spans(row)[glyph] == (surface, surface in ("ok", "warn"))


def test_a_glyph_outside_a_settings_row_is_not_coloured() -> None:
    assert "=" not in _spans("  a = b")


def test_categories_are_upper_case_on_the_rail(tree: Path, fixture: Fixture) -> None:
    rows = _settings(fixture, _view(tree), sr.BOOL_KEY, w=160, h=60)
    rail = [row.split("│")[0].strip() for row in rows]
    assert "EXECUTION" in rail
    assert "execution" not in rail


# ---------- the rail is closed before the keybar ----------


@pytest.mark.parametrize(("w", "h"), sr.SIZES)
def test_the_rail_closes_on_its_own_row(tree: Path, fixture: Fixture, w: int, h: int) -> None:
    view = _view(tree)
    rows = _settings(fixture, view, sr.BOOL_KEY, w=w, h=h)
    rail = rail_width(view, wide=breadth_of(w) >= Breadth.WIDE)
    assert rows[-2] == "─" * rail + "┴" + "─" * (w - rail - 1)
    assert len(rows) == h


# ---------- the vertical chooser keeps the stored value marked ----------


def test_the_chooser_is_a_vertical_list_at_rest(tree: Path, fixture: Fixture) -> None:
    body = "\n".join(_settings(fixture, _view(tree), sr.LITERAL_KEY, w=120, h=30))
    assert "│    ● api_duration" in body
    assert "│    ○ tokens" in body
    assert "│    ○ wall_clock" in body


def test_pointing_at_a_value_keeps_the_stored_one_dotted(tree: Path, fixture: Fixture) -> None:
    view = _view(tree)
    session = sr._on(sr._session(), view, sr.LITERAL_KEY)
    sr._press(fixture, view, session, ["Enter", "ArrowDown"])
    body = "\n".join(sr._frame(fixture, view, session, w=120, h=30))
    assert "│  ▸ ○ tokens" in body
    assert "│    ● api_duration" in body
    assert "VALUE    ●" not in body


# ---------- the stack is a boxed, read-only overlay ----------


@pytest.mark.parametrize(("w", "h"), sr.SIZES)
def test_the_stack_is_a_boxed_read_only_card(tree: Path, fixture: Fixture, w: int, h: int) -> None:
    view = _view(tree)
    rows = sr._frame(
        fixture, view, sr._on(sr._session("settings.stack"), view, sr.BOOL_KEY), w=w, h=h
    )
    body = "\n".join(rows)
    assert rows[1].startswith(" LAYER ▸ repo")
    assert "┌─ STACK · nine layers" in body
    assert f"│ KEY        {sr.BOOL_KEY}" in body
    assert "Read only · a value is changed through the lens, never from here." in body
    assert any(row.startswith("└") and row.endswith("┘") for row in rows)


def test_a_full_second_tier_folds_the_card_rather_than_losing_a_row(
    tree: Path, fixture: Fixture
) -> None:
    view = _view(tree)
    leaf = view.leaf(sr.BOOL_KEY).model_copy(
        update={
            "deny_chain": ("org policy",),
            "constraint_chain": ("workspace profile",),
            "capability_requirement": "network.egress",
            "secret_ref": "ref://vault/deploy",  # pragma: allowlist secret
        }
    )
    held = view.model_copy(
        update={"leaves": tuple(leaf if x.key == sr.BOOL_KEY else x for x in view.leaves)}
    )
    rows = sr._frame(fixture, held, sr._on(sr._session("settings.stack"), held, sr.BOOL_KEY))
    body = "\n".join(rows)
    assert len(rows) == 24
    assert f"KEY        {sr.BOOL_KEY} · bool" in body
    for label in ("DENIED BY", "CONSTRAINED BY", "NEEDS", "SECRET", "LENS SETS"):
        assert f"│ {label}" in body, label


# ---------- the filter narrows the keys, and the wide bar advertises it ----------


def test_the_filter_narrows_keeps_and_clears(tree: Path, fixture: Fixture) -> None:
    view = _view(tree)
    session = sr._on(sr._session(), view, sr.LITERAL_KEY)
    sr._press(fixture, view, session, ["\\", "l", "e", "v"])
    typing = sr._frame(fixture, view, session, w=120, h=30)
    assert session.set_typing
    assert "\\lev▏" in typing[3]
    assert typing[-1].strip().startswith("type narrow")
    sr._press(fixture, view, session, ["Enter"])
    assert not session.set_typing and session.set_filter == "lev"
    sr._press(fixture, view, session, ["\\", "Escape"])
    assert session.set_filter == ""


def test_a_filter_that_matches_nothing_says_so(tree: Path, fixture: Fixture) -> None:
    view = _view(tree)
    session = sr._on(sr._session(), view, sr.LITERAL_KEY)
    sr._press(fixture, view, session, ["\\", "z", "z", "z"])
    assert "nothing matches \\zzz" in "\n".join(sr._frame(fixture, view, session))


@pytest.mark.parametrize(("w", "advertised"), [(80, False), (119, False), (120, True)])
def test_stack_and_filter_are_advertised_from_120(
    tree: Path, fixture: Fixture, w: int, advertised: bool
) -> None:
    bar = _settings(fixture, _view(tree), sr.BOOL_KEY, w=w, h=24)[-1]
    assert ("i stack" in bar and "\\ filter" in bar) is advertised


# ---------- a number steps rather than being retyped ----------


def test_arrows_step_a_whole_number(tree: Path, fixture: Fixture) -> None:
    view = _view(tree)
    session = sr._on(sr._session(), view, sr.INT_KEY)
    sr._press(fixture, view, session, ["Enter", "ArrowUp", "ArrowUp", "ArrowDown"])
    assert session.edit is not None
    assert session.edit["kind"] == "num"
    assert session.edit["text"] == "31"


@pytest.mark.parametrize(
    ("value_type", "text", "up", "out"),
    [
        ("int", "", True, "1"),
        ("int", "0", False, "-1"),
        ("int", "x", True, "1"),
        ("float", "0.1", True, "0.15"),
        ("float", "1", False, "0.95"),
    ],
)
def test_step_boundaries(tree: Path, value_type: str, text: str, up: bool, out: str) -> None:
    leaf = (
        _view(tree)
        .leaf(sr.INT_KEY)
        .model_copy(update={"value_type": value_type, "value_range": None})
    )
    assert step(leaf, text, up) == out


# ---------- rail width, category case and the context row ----------


@pytest.mark.parametrize(("w", "floor"), [(80, 14), (119, 14), (120, 17), (160, 17)])
def test_the_rail_is_the_packet_width_at_least(tree: Path, w: int, floor: int) -> None:
    view = _view(tree)
    longest = max(len(name) for name in view.sections())
    assert rail_width(view, wide=breadth_of(w) >= Breadth.WIDE) == max(floor, longest + 2)


def test_the_context_row_reads_in_category_section(tree: Path, fixture: Fixture) -> None:
    rows = _settings(fixture, _view(tree), sr.LITERAL_KEY)
    assert " · in QUALITY ▸ estimation" in rows[1]


# ---------- the lens is bold in the chain ----------


def test_the_lens_layer_is_bold_in_the_chain() -> None:
    row = "EXECUTION     │  FLOW     global › workspace › [repo] › branch › local"  # noqa: RUF001
    assert _spans(row)["[repo]"] == (None, True)


# ---------- the notes head names the key within its section ----------


def test_the_notes_head_is_section_relative_and_the_meaning_wraps(
    tree: Path, fixture: Fixture
) -> None:
    rows = _settings(fixture, _view(tree), sr.LITERAL_KEY, w=80, h=30)
    body = "\n".join(rows)
    assert "│  eu_basis · literal" in body
    assert "│  estimation.eu_basis · literal" not in body
    meaning = sr.LEAF_KEY_REGISTRY[sr.LITERAL_KEY].description
    assert meaning.split()[-1] in body


# ---------- operator words in the operations frames ----------


@pytest.mark.parametrize(
    ("n", "name", "out"),
    [
        (0, "health_view", "0 checks"),
        (1, "health_view", "1 check"),
        (1, "sandbox_policy", "1 sandbox policy"),
        (2, "sandbox_policy", "2 sandbox policies"),
        (1, "batch", "1 batch"),
        (16, "batch", "16 batches"),
    ],
)
def test_a_register_is_counted_in_operator_words(n: int, name: str, out: str) -> None:
    assert noun(n, name) == out


@pytest.mark.parametrize("route", ["health", "sandbox.log"])
def test_no_storage_name_reaches_the_summary(route: str) -> None:
    summary = nrf._frame(route, w=120)[1]
    assert "_" not in summary


@pytest.mark.parametrize("route", ["crash.recovery", "history.diff"])
def test_the_body_and_summary_name_no_cursor(route: str) -> None:
    frame = nrf._frame(route, w=120, subject="TRK-CORE")
    assert "cursor" not in frame[1] or route == "crash.recovery"
    assert not any("after cursor" in row for row in frame)


@pytest.mark.parametrize(
    "producer", [SANDBOX_DECISION_PRODUCER, DISPATCH_QUEUE_PRODUCER, PULL_REQUEST_PRODUCER]
)
def test_a_missing_producer_is_named_without_a_requirement_id(producer: str) -> None:
    assert not REQUIREMENT_ID.search(producer)


@pytest.mark.parametrize("route", ["sandbox.log", "unattended", "git.pr"])
def test_no_frame_prints_a_requirement_id(route: str) -> None:
    body = "\n".join(nrf._frame(route, w=80, subject="BAT-0101"))
    assert not REQUIREMENT_ID.search(body)


# ---------- the recovery doors fit at 80 ----------


@pytest.mark.parametrize(("w", "wide"), [(80, False), (120, True), (160, True)])
def test_the_leaves_you_column_is_drawn_only_where_it_fits(w: int, wide: bool) -> None:
    frame = nrf._frame("crash.recovery", w=w)
    head = next(row for row in frame if "DOOR" in row and "COSTS" in row)
    assert ("LEAVES YOU" in head) is wide
    doors = [
        row for row in frame if row.lstrip(" ▸").startswith(("reattach", "replay", "read-only"))
    ]
    assert len(doors) == 3
    assert not any("…" in row for row in doors)


# ---------- the offline state says when no snapshot is held ----------


def test_the_offline_state_draws_an_absence_rather_than_an_empty_table() -> None:
    chrome = load_chrome()
    offline = next(state for state in chrome.entry if state.id == "offline")
    session = Session()
    session.route = "entry"
    view = View(session=session, fixture=Fixture.from_chrome(chrome), w=80, h=24)
    body = "\n".join(render_state(view, offline))
    assert "∅ no snapshot held" in body


# ---------- entry commands are shown whole, relative to where the shell stands ----------


@pytest.mark.parametrize(
    ("word", "out"),
    [
        ("{here}", "."),
        ("{here}/sub/dir", "sub/dir"),
        ("/elsewhere/root", "/elsewhere/root"),
        ("<KEY>", "<KEY>"),
        ("register", "register"),
    ],
)
def test_a_path_at_or_under_the_launch_folder_is_shown_relative(
    tmp_path: Path, word: str, out: str
) -> None:
    command = EntryCommand(argv=("repo", "register", word.format(here=tmp_path)), purpose="")
    assert command.shown(tmp_path) == f"eawf repo register {out}"
    assert command.line.endswith(word.format(here=tmp_path))


def test_a_long_command_wraps_instead_of_clipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    root = "/" + "/".join(["a-long-folder-name"] * 6)
    command = EntryCommand(argv=("repo", "register", root), purpose="register this folder")
    state = failed_state(load_chrome(), ("not registered",), (command,))
    session = Session()
    session.route = "entry"
    view = View(session=session, fixture=Fixture.from_chrome(load_chrome()), w=80, h=24)
    rows = render_state(view, state)
    assert len(rows) == 24
    assert not any("…" in row for row in rows)
    assert "".join(row.strip() for row in rows).count("a-long-folder-name") == 6
    assert state.commands == (command.line,)


# ---------- a long value wraps under its label ----------


def test_a_label_row_wraps_rather_than_clips() -> None:
    text = "∅ unavailable · no repository reader states the branch or its drift " * 2
    rows = wrapped("BRANCH", text.strip(), 80)
    assert len(rows) > 1
    assert rows[0].startswith(" BRANCH       ∅")
    assert rows[1].startswith(" " * 14)
    assert all(len(row) <= 80 for row in rows)


@pytest.mark.parametrize("text", ["", "one"])
def test_a_short_or_empty_value_is_one_row(text: str) -> None:
    assert wrapped("BRANCH", text, 80) == [f" {'BRANCH':<13}{text}"]


@pytest.mark.parametrize("route", ["cost.ceiling", "git.pr"])
def test_no_label_row_is_clipped_at_80(route: str) -> None:
    frame = nrf._frame(route, w=80, subject="BAT-0101")
    assert not any(row.rstrip().endswith("…") for row in frame[3:-1])


# ---------- the queue's window count sits in the label gutter ----------


def test_the_queue_window_row_shares_the_label_gutter() -> None:
    frame = nrf._frame("unattended", w=80)
    queue = next(row for row in frame if row.startswith(" QUEUE"))
    window = next(row for row in frame if row.startswith(" WINDOW"))
    assert len(queue) - len(queue[14:].lstrip()) == len(window) - len(window[14:].lstrip())
    assert window.startswith(" WINDOW       ")


def test_the_register_command_a_frame_hands_over_runs_off_a_tty(tmp_path: Path) -> None:
    """``repo register`` prompts unless ``--yes`` is passed, and a pasted line has no TTY."""
    state = onboarding_state(load_chrome(), tmp_path, registered=False)
    register = next(line for line in state.commands if " repo register " in line)
    assert register.endswith(" --yes")
