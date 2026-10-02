"""One key table feeds the keybar, the refusal gate and the help overlay.

Each test names the console requirement row it proves. The route tables in
:mod:`~eawf.surfaces.tui.console.keybar` and the global grammar in
:mod:`~eawf.surfaces.tui.console.keymap` are the only place a binding is spelled: every
route frame draws its bar from them, the dispatcher's refusal gate admits exactly them, and
help prints them (CON-031, CON-098). Help lists THIS ROUTE -- the route's key table
verbatim, including any pair the keybar dropped for width -- then EVERYWHERE, the global
keys that act on the route, then the guarded-quit sentence. The keys go through the
production dispatcher over the tracked registers, the way the app presses them.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.keybar import (
    ADMITTED_SHIFTED,
    GAP,
    KEY,
    MARGIN,
    ROUTE_KEYS,
    KeyEntry,
    KeyKind,
    is_shifted,
    pick,
    route_pairs,
)
from eawf.surfaces.tui.console.keymap import (
    ALIASES,
    ATTENTION_JUMP_KEY,
    DRAWER_KEYS,
    GLOBAL_KEYS,
    HELP_KEY,
    MODIFIERS,
    MOTION,
    OVERLAY_KEYS,
    Where,
    acts_here,
    allowlist,
    native_keys,
    route_keys,
)
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.overlays import render_overlay
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.session import SIZES, Session

from .overlay_support import frame_of, prototype, session_on, view_of

_ENTRY = re.compile(r"^   (?P<token>\S.*?)  +(?P<label>\S.*?)\s*$")
#: Routes whose table the frame draws natively or from the prototype, every one keyed.
HELP_ROUTES: tuple[str, ...] = tuple(sorted(r for r in ROUTE_KEYS if r != "entry"))
_ABBREVIATED = ("PgUp", "PgDn", "↑/↓", "Tab/⇧Tab", "Ctrl-C", "Esc Esc quits only if")
_SIMULATOR = ("cycle frame size", "[ ] state", "state N of")


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the prototype registers the golden contract replays."""
    return prototype()


def _block(rows: list[str], title: str) -> list[tuple[str, str]]:
    """Return the token and label pairs listed under block ``title``."""
    at = next(i for i, row in enumerate(rows) if row.strip() == title)
    pairs: list[tuple[str, str]] = []
    for row in rows[at + 1 :]:
        found = _ENTRY.match(row)
        if found is None:
            break
        pairs.append((found["token"], found["label"]))
    return pairs


def _help(route: str, fixture: Fixture) -> list[str]:
    return render_overlay("help", view_of(session_on(route, overlay="help"), fixture, 2))


@pytest.mark.parametrize("route", HELP_ROUTES)
def test_con_098_help_prints_the_routes_key_table_verbatim(route: str, fixture: Fixture) -> None:
    table = route_keys(session_on(route), fixture)
    assert _block(_help(route, fixture), "THIS ROUTE") == [(e.token, e.label) for e in table]


@pytest.mark.parametrize("route", HELP_ROUTES)
def test_con_098_help_prints_the_global_grammar_and_the_guarded_quit(
    route: str, fixture: Fixture
) -> None:
    rows = _help(route, fixture)
    everywhere = [(key.token, key.text) for key in GLOBAL_KEYS if acts_here(key, route)]
    assert _block(rows, "EVERYWHERE") == everywhere
    assert any(row.startswith(" Esc Esc quits only at scope home") for row in rows)


@pytest.mark.parametrize("route", HELP_ROUTES)
def test_con_098_help_advertises_no_key_the_route_does_not_bind(
    route: str, fixture: Fixture
) -> None:
    session = session_on(route)
    bound = allowlist(session, fixture)
    for entry in route_keys(session, fixture):
        assert set(entry.keys) <= bound, entry.token
    for global_key in GLOBAL_KEYS:
        assert all(key == "ctrl+c" or key in bound for key in global_key.keys)


@pytest.mark.parametrize("route", HELP_ROUTES)
def test_con_098_help_names_keys_in_full_and_lists_no_simulator_affordance(
    route: str, fixture: Fixture
) -> None:
    text = "\n".join(_help(route, fixture))
    for token in (*_ABBREVIATED, *_SIMULATOR):
        assert token not in text


def test_con_098_help_lists_a_pair_the_keybar_dropped_for_width(fixture: Fixture) -> None:
    narrow = frame_of(session_on("activity"), fixture, 0)[-1]
    assert ". actions" not in narrow
    rows = render_overlay("help", view_of(session_on("activity", overlay="help"), fixture, 0))
    assert (".", "actions") in _block(rows, "THIS ROUTE")


@pytest.mark.parametrize("size", range(len(SIZES)))
def test_con_098_help_is_the_same_table_at_every_size(size: int, fixture: Fixture) -> None:
    rows = render_overlay("help", view_of(session_on("activity", overlay="help"), fixture, size))
    wide = _help("activity", fixture)
    assert _block(rows, "THIS ROUTE") == _block(wide, "THIS ROUTE")
    assert _block(rows, "EVERYWHERE") == _block(wide, "EVERYWHERE")


def test_con_098_a_route_with_no_table_says_only_the_global_set_applies(fixture: Fixture) -> None:
    rows = _help("no.such.route", fixture)
    assert _block(rows, "THIS ROUTE") == []
    assert "   no route-local key — only the global set below applies" in [r.rstrip() for r in rows]


ROOT = Path(__file__).resolve().parents[5]
CONSOLE = ROOT / "src" / "eawf" / "surfaces" / "tui" / "console"
ROUTES = sorted(ROUTE_KEYS)
WIDTHS = [w for w, _h in SIZES]


class _Host:
    """The dispatcher's host: a held clock and a quit that records it was asked."""

    def __init__(self) -> None:
        self.quits = 0
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """Record that the console was asked to end."""
        self.quits += 1


def _fixture() -> Fixture:
    return load_fixture(ROOT / "tests" / "fixtures" / "console" / "golden" / "fixture")


FIXTURE = _fixture()


def _session(route: str, subj: str | None = None) -> Session:
    session = Session()
    session.route = route
    session.subj_id = subj
    compose_frame(View(session=session, fixture=FIXTURE, w=120, h=30))
    return session


def _press(session: Session, *keys: str, host: _Host | None = None) -> _Host:
    host = host or _Host()
    for key in keys:
        dispatch(Ctx(session=session, fixture=FIXTURE, host=host, w=120, h=30), key, False)
        compose_frame(View(session=session, fixture=FIXTURE, w=120, h=30))
    return host


def _bar_pieces(frame: list[str]) -> list[str]:
    """Return the ``key label`` pieces of a frame's keybar, a legend past a blank cut off."""
    pieces = frame[-1][MARGIN:].rstrip().split(GAP)
    return pieces[: pieces.index("")] if "" in pieces else pieces


def _piece(entry: KeyEntry) -> str:
    return " ".join(entry.pair())


# ---------- CON-031: one table for the keybar, the gate and help ----------


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("route", ROUTES)
def test_con031_every_bar_pair_is_a_row_of_the_route_table(route: str, w: int) -> None:
    """CON-031: a pair on a frame's bar that the table does not hold fails the contract.

    A frame may show part of its table as its state changes, and a named key may carry a
    state's label from the shared vocabulary (``Esc clear bucket``), but it never spells a
    pair of its own.
    """
    session = Session()
    session.route = route
    frame = compose_frame(View(session=session, fixture=FIXTURE, w=w, h=40))
    table = {*ROUTE_KEYS[route], *native_keys(route, windowed=True)}
    tokens = {entry.token for entry in table}
    admitted = {_piece(entry) for entry in table}
    admitted |= {_piece(entry) for entry in KEY.values() if entry.token in tokens}
    stray = [piece for piece in _bar_pieces(frame) if piece not in admitted]
    assert not stray, f"{route}@{w} draws pairs its table lacks: {stray}"


@pytest.mark.parametrize("route", ROUTES)
def test_con031_help_prints_the_route_table_and_each_global_that_acts_there(route: str) -> None:
    """CON-031: help's THIS ROUTE block is the route's table, EVERYWHERE the acting globals."""
    session = Session()
    session.route = route
    session.overlay = "help"
    text = "\n".join(compose_frame(View(session=session, fixture=FIXTURE, w=160, h=60)))
    for entry in ROUTE_KEYS[route]:
        assert entry.token in text and entry.label in text, (route, entry)
    for key in GLOBAL_KEYS:
        row = f"   {key.token}"
        listed = any(line.startswith(row) and key.text in line for line in text.split("\n"))
        assert listed is acts_here(key, route), (route, key.token)


@pytest.mark.parametrize("route", ROUTES)
def test_con031_the_gate_admits_the_table_the_globals_and_nothing_else(route: str) -> None:
    """CON-031: the gate is the table, the global grammar, motion, aliases and the ``!`` jump."""
    session = Session()
    session.route = route
    table = {key for entry in route_keys(session, FIXTURE) for key in entry.keys}
    grammar = {key for global_key in GLOBAL_KEYS for key in global_key.keys}
    expected = table | grammar | MOTION | MODIFIERS | {HELP_KEY, ATTENTION_JUMP_KEY}
    expected |= {alias for alias, key in ALIASES.items() if key in expected}
    assert allowlist(session, FIXTURE) == frozenset(expected)


def test_con031_the_gate_refuses_a_letter_the_route_does_not_bind() -> None:
    """CON-031: ``m`` binds readiness on release and nothing on activity, so the gate refuses it."""
    session = _session("activity")
    _press(session, "m")
    assert session.trace == "m → unclaimed"
    assert session.overlay is None
    release = _session("release")
    _press(release, "m")
    assert release.overlay == "readiness"


def test_con031_the_gate_holds_an_overlay_to_its_own_keys() -> None:
    """CON-031: with an overlay open the gate is that overlay's table, and ``-`` dismisses."""
    session = Session()
    session.overlay = "consequence"
    assert allowlist(session, FIXTURE) == frozenset({*OVERLAY_KEYS["consequence"], "-"})
    session.overlay = "inspect"
    assert allowlist(session, FIXTURE) == frozenset({*DRAWER_KEYS["inspect"], "-"})
    session.overlay = None
    session.prefix = "g"
    assert allowlist(session, FIXTURE) == frozenset(DRAWER_KEYS["go"])


# The names a renderer gives the bar of its route, as against an editor state's own bar.
_ROUTE_BAR_NAMES = frozenset({"_KEYS", "ROUTE_KEYS", "STACK_KEYS"})


def test_con031_renderers_draw_their_route_bars_from_the_table() -> None:
    """CON-031: no route renderer spells its route bar as a pair literal of its own."""
    offenders: list[str] = []
    for path in sorted((CONSOLE / "renderers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if names & _ROUTE_BAR_NAMES and isinstance(node.value, ast.Tuple):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_con031_pick_names_only_labels_the_table_holds() -> None:
    """CON-031: a state-dependent bar picks entries by label and cannot spell a new one."""
    assert pick("settings", "field", "back") == [("↑↓", "field"), ("Esc", "back")]
    assert route_pairs("receipt") == (("y", "copy"), ("Esc", "back"))
    with pytest.raises(KeyError, match="no key labelled stack"):
        pick("receipt", "stack")
    with pytest.raises(KeyError):
        pick("nowhere", "back")
    assert pick("receipt") == []


# ---------- CON-033: lowercase verbs, and only ?, Y and ! shifted ----------


def _all_entries() -> list[KeyEntry]:
    return [entry for table in ROUTE_KEYS.values() for entry in table] + list(KEY.values())


def test_con033_every_mutation_and_answer_key_is_a_lowercase_letter() -> None:
    """CON-033: a mutation or lifecycle verb is lowercase and never shifted."""
    writes = [e for e in _all_entries() if e.kind in (KeyKind.PRIMARY, KeyKind.ANSWER)]
    assert writes
    for entry in writes:
        assert all(len(k) == 1 and k.islower() for k in entry.keys), entry


def test_con033_only_help_and_copy_urn_are_shifted_anywhere_a_frame_shows_keys() -> None:
    """CON-033: only ``?``, ``Y`` and help's ``!`` jump are shifted; ``*`` only in the menu."""
    shown = {key for entry in _all_entries() for key in entry.keys}
    shown |= {key for global_key in GLOBAL_KEYS for key in global_key.keys}
    shown |= {HELP_KEY}
    shown |= {key for keys in OVERLAY_KEYS.values() for key in keys}
    shown |= {key for name, keys in DRAWER_KEYS.items() if name != "actions" for key in keys}
    # help lists the global attention jump, which moves the frame and mutates nothing; no
    # key table may bind it, which the declaration check below still refuses
    assert {key for key in shown if is_shifted(key)} <= ADMITTED_SHIFTED | {ATTENTION_JUMP_KEY}
    assert {key for key in DRAWER_KEYS["actions"] if is_shifted(key)} == {"*"}


def test_con033_no_action_menu_letter_is_a_capital_but_copy_urn() -> None:
    """CON-033: a menu letter never uses case to carry a meaning; ``Y`` copies, as everywhere."""
    verbs = [verb for route in REGISTRY.ids for verb in FIXTURE.menus.verbs(route)]
    assert verbs
    capitals = [verb for verb in verbs if verb.key.isupper()]
    assert all(verb.key in ADMITTED_SHIFTED and not verb.mutates for verb in capitals)


def test_con033_menu_shifted_keys_are_only_copy_urn_and_select_all() -> None:
    """CON-033: inside the action menu only ``Y`` and the menu-local ``*`` are shifted."""
    keys = {verb.key for route in REGISTRY.ids for verb in FIXTURE.menus.verbs(route)}
    assert {key for key in keys if is_shifted(key)} <= {"Y", "*"}


@pytest.mark.parametrize("key", ["X", "M", "@", "!", "*", "~"])
def test_con033_a_shifted_key_is_refused_at_declaration(key: str) -> None:
    """CON-033: the table refuses a shifted binding before any frame can show it."""
    with pytest.raises(ValueError, match="binds shifted key"):
        KeyEntry("verb", (key,), KeyKind.PRIMARY)


@pytest.mark.parametrize("key", ["?", "Y", "x", "Enter", "PageUp", "\\", "["])
def test_con033_an_admitted_or_unshifted_key_is_accepted(key: str) -> None:
    """CON-033: ``?``, ``Y`` and every unshifted key declare cleanly."""
    assert KeyEntry("verb", (key,)).keys == (key,)


@pytest.mark.parametrize(
    ("key", "shifted"), [("a", False), ("A", True), ("@", True), ("", False), ("Tab", False)]
)
def test_con033_is_shifted_judges_single_keys_only(key: str, shifted: bool) -> None:
    """CON-033: a named key is never shifted, a capital or a symbol always is."""
    assert is_shifted(key) is shifted


# ---------- CON-035: / is the palette, \ the filter, Ctrl-F its alias ----------


@pytest.mark.parametrize("route", ["scope.home", "activity", "run.detail", "settings"])
def test_con035_slash_opens_the_palette_on_every_route(route: str) -> None:
    """CON-035: ``/`` opens the command palette wherever the operator stands."""
    session = _session(route)
    _press(session, "/")
    assert session.overlay == "palette"


@pytest.mark.parametrize("key", ["\\", "ctrl+f"])
def test_con035_backslash_and_ctrl_f_open_the_filter(key: str) -> None:
    """CON-035: ``\\`` filters the current list, and Ctrl-F is the same binding."""
    session = _session("activity")
    _press(session, key)
    assert session.typing is True
    assert session.overlay is None


def test_con035_no_table_reads_slash_filter() -> None:
    """CON-035: every table reads ``\\ filter`` and ``/ palette``, never ``/ filter``."""
    pairs = {entry.pair() for entry in _all_entries()}
    assert ("/", "filter") not in pairs
    assert ("\\", "filter") in pairs
    assert ("/", "palette") in pairs
    assert next(k for k in GLOBAL_KEYS if k.keys == ("/",)).text == "command palette"


def test_con035_filter_is_refused_where_the_route_binds_none() -> None:
    """CON-035: a route with no list filter leaves ``\\`` unclaimed rather than half-open."""
    session = _session("scope.home")
    _press(session, "\\")
    assert session.typing is False


# ---------- CON-047: help is the keymap for this route and this prefix state ----------


def _help_text(route: str, subj: str | None = None) -> str:
    session = Session()
    session.route = route
    session.subj_id = subj
    session.overlay = "help"
    return "\n".join(compose_frame(View(session=session, fixture=FIXTURE, w=160, h=60)))


def test_con047_depth_keys_are_taught_only_where_there_is_a_depth() -> None:
    """CON-047: ``u`` and ``[ ]`` are taught on a Task, not on Activity, which has no parent."""
    depth = [k for k in GLOBAL_KEYS if k.where is Where.DEPTH]
    assert {k.token for k in depth} == {"u", "[ ]"}
    task = _help_text("task.detail", "EAWF-0001")
    activity = _help_text("activity")
    for key in depth:
        assert f"   {key.token}" in task
        assert key.text not in activity


def test_con047_inspect_is_taught_only_where_the_route_binds_it() -> None:
    """CON-047: ``i`` is in help's EVERYWHERE block on run.detail and absent on activity."""
    inspect = next(k for k in GLOBAL_KEYS if k.where is Where.INSPECT)
    assert inspect.text in _help_text("run.detail", "RUN-538453eb")
    assert inspect.text not in _help_text("activity")


def test_con047_help_is_not_a_global_cheat_sheet() -> None:
    """CON-047: THIS ROUTE lists this route's table and not another route's verbs."""
    text = _help_text("activity")
    assert "THIS ROUTE" in text
    assert "resolve" not in text
    assert "readiness" not in text


def test_con047_help_in_the_prefix_state_keeps_the_prefix_and_its_drawer() -> None:
    """CON-047: ``?`` while ``g`` is armed keeps the prefix, whose drawer is its keymap."""
    session = _session("activity")
    _press(session, "g", "?")
    assert session.prefix == "g"
    frame = "\n".join(compose_frame(View(session=session, fixture=FIXTURE, w=120, h=30)))
    for letter in REGISTRY.go_map:
        assert f"g {letter}" in frame
    _press(session, "n")
    assert session.route == "attention"
    assert session.prefix is None


def test_con047_help_in_the_prefix_state_still_times_out() -> None:
    """CON-047: asking for help does not stretch the prefix past its own deadline."""
    session = _session("activity")
    host = _press(session, "g")
    deadline = session.prefix_deadline
    _press(session, "?", host=host)
    assert session.prefix_deadline == deadline


# ---------- CON-024: a text input owns every single key until Escape ----------


def test_con024_the_filter_field_swallows_every_single_key_verb() -> None:
    """CON-024: inside the filter, ``g``, ``.``, ``?`` and ``/`` are text, not verbs."""
    session = _session("activity")
    _press(session, "\\", "g", ".", "?", "/", "a")
    assert session.typing is True
    assert session.prefix is None
    assert session.overlay is None
    assert session.filters.get("activity") == "g.?/a"


def test_con024_escape_leaves_the_filter_and_goes_no_further() -> None:
    """CON-024: Escape leaves the input and never reaches the overlay stack or back stack."""
    session = _session("activity")
    _press(session, "\\", "x", "Escape")
    assert session.typing is False
    assert session.route == "activity"
    assert session.last_esc == pytest.approx(0.0)


def test_con024_escape_in_the_palette_at_scope_home_never_arms_the_quit() -> None:
    """CON-024: Escape inside the palette's input closes it and never arms the guarded quit."""
    session = _session("scope.home")
    host = _press(session, "/", "r", "u", "Escape")
    assert session.overlay is None
    assert session.last_esc == pytest.approx(0.0)
    assert host.quits == 0


def test_con024_the_reply_field_takes_digits_as_text_and_escape_keeps_the_question() -> None:
    """CON-024: a digit typed in the reply field is text, not an answer; Escape drops only it."""
    session = _session("attention")
    session.overlay = "question"
    _press(session, "w", "1", "2")
    assert session.reply == {"text": "12"}
    _press(session, "Escape")
    assert session.reply is None
    assert session.overlay == "question"


# ---------- CON-048: one read helper and one write gate ----------


def _calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }


@pytest.mark.parametrize("module", ["dispatch.py", "attention.py", "action_menu.py"])
def test_con048_key_paths_refuse_only_through_the_write_gate(module: str) -> None:
    """CON-048: no key path judges a write itself; a second refusal path is a defect."""
    calls = _calls(CONSOLE / module)
    assert not calls & {"can_mutate", "mut_reason", "binding_refusal"}, module


def test_con048_only_the_reads_module_judges_the_connection_state() -> None:
    """CON-048: the reads contract has one helper; nothing else tests ``.conn`` against a set.

    Naming the one state a frame draws a line for (``REPLAYING``) is presentation; judging
    whether a state admits a write or vouches for a count is the contract, and lives in
    :mod:`~eawf.surfaces.tui.console.reads` alone.
    """
    offenders: list[str] = []
    for path in sorted(CONSOLE.rglob("*.py")):
        if path.name == "reads.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            judged = any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
            left = node.left
            if isinstance(left, ast.Attribute) and left.attr == "conn" and judged:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_con048_offline_refuses_from_one_place_by_key_menu_and_card() -> None:
    """CON-048: the letter, the menu and the confirmed card all name the same reason."""
    reason = f"OFFLINE SNAPSHOT · {FIXTURE.proto.states.refuse['OFFLINE SNAPSHOT']}"
    by_key = _session("attention")
    by_key.conn = "OFFLINE SNAPSHOT"
    by_key.sel = 1
    _press(by_key, "x")
    assert by_key.trace is not None and by_key.trace.endswith(reason)
    by_menu = _session("attention")
    by_menu.conn = "OFFLINE SNAPSHOT"
    by_menu.sel = 1
    _press(by_menu, ".", "x")
    assert by_menu.trace == f"x → refused: {reason}"
    by_card = _session("run.detail", "RUN-538453eb")
    _press(by_card, ".", "n")
    assert by_card.overlay == "consequence"
    by_card.conn = "OFFLINE SNAPSHOT"
    _press(by_card, "Enter")
    assert by_card.trace is not None and by_card.trace.endswith(reason)
