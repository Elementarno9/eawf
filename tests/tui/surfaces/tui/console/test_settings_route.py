"""The Settings route and its stack card, drawn from the effective-settings view, and edit.

Requirement rows proved here, by id:

- ``UI-052``: ``source_layer``, ``override_chain[]`` and ``editable_at[]`` are typed
  against the nine-value config ``Layer`` enum, and the five file layers are the ones
  the lens cycles.
- ``UI-053``: ``settings.stack`` renders every layer's value or absence with its kind and
  a template place, the winner, the lens and one sentence, then the second tier only
  when stated; the effective revision sits in its head and it is total at 80 columns.
- ``UI-057``: Settings is one route whose rail lists five alphabetical categories filing
  every catalog section exactly once, with the writable chain, the per-key glyph and a
  docked readout, Tab and Shift-Tab over sections, Escape leaving an edit first, and the
  packet's keybar.

The edit port is proved end to end on a tmp tree: a key pressed on the route becomes a
request to the daemon's own layered-config verb, the layer file changes, and the value
the frame shows next is the daemon's re-read rather than what the console asked for.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pytest
import yaml
from pydantic import ValidationError

from eawf import __version__
from eawf.kernel.config.layered import LAYER_ORDER, WRITABLE_LAYERS, Layer
from eawf.kernel.config.registry.leaf_catalog import LEAF_KEY_REGISTRY
from eawf.kernel.projection.settings import (
    LAYER_PLACES,
    LENS_LAYERS,
    SETTINGS_CATEGORIES,
    EffectiveSettingsView,
    LayerKind,
    SettingsLeaf,
    build_settings_view,
)
from eawf.platform.lint.eawf026_settings_categories import category_assignment_defects
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.config import set_layer_value, unset_layer_value
from eawf.runtime.daemon.methods.projection import SETTINGS_READ_METHOD
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import LeadReceded, Receded, View
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import (
    SETTING_SET_METHOD,
    SETTING_UNSET_METHOD,
    OperationStatus,
    SettingRequest,
    VerbRequest,
    address_setting,
)
from eawf.surfaces.tui.console.paint import Part, paint
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.provenance import (
    GLYPH_DEFAULT,
    GLYPH_INHERITS,
    GLYPH_SHADOWED,
    GLYPH_WINS,
    coerce,
    glyph,
    rail_width,
)
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
BRANCH = "probe"
SIZES = ((80, 24), (120, 30), (160, 40))

#: A bool the catalog lets global, workspace and repo write, and no other layer.
BOOL_KEY = "telemetry.enabled"
#: A literal with three allowed values, writable at global, workspace and repo.
LITERAL_KEY = "estimation.eu_basis"
#: An int writable at global, workspace and repo, whose default has two digits.
INT_KEY = "estimation.eu_minutes"
#: A list writable at all five file layers.
LIST_KEY = "profiles.enabled"
#: An int the config registry holds to [1, 16], writable at repo.
RANGED_KEY = "planning.max_parallel_waves"
#: A key no layer may write.
LOCKED_KEY = "schema_version"
#: A mapping no console editor rebuilds, which the console leaves to its file.
MAPPING_KEY = "economics.governor"

ROUTE_KEYBAR = "↑↓ field   Tab section   Enter edit   l layer   x unset   Esc back"
STACK_KEYBAR = "↑↓ layer   Esc close"


class _Host:
    """The dispatcher's host: a held clock and a quit nobody asks for."""

    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """End nothing; the tests never quit."""


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


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _view(tree: Path) -> EffectiveSettingsView:
    return build_settings_view(
        workspace=tree,
        repo=tree,
        scope_id=SCOPE,
        cursor=41208,
        generated_at=AT,
        env={},
        branch=BRANCH,
    )


def _on(session: Session, view: EffectiveSettingsView, key: str) -> Session:
    """Put the route cursor on ``key``."""
    section = view.leaf(key).section
    assert section is not None
    session.set_sec = view.sections().index(section)
    session.set_key = [leaf.key for leaf in view.keys_of(section)].index(key)
    return session


def _session(route: str = "settings") -> Session:
    session = Session()
    session.route = route
    session.lens = "repo"
    return session


def _frame(
    fixture: Fixture, view: EffectiveSettingsView, session: Session, *, w: int = 80, h: int = 24
) -> list[str]:
    return render_route(View(session=session, fixture=fixture, w=w, h=h, settings=view))


def _press(
    fixture: Fixture,
    view: EffectiveSettingsView,
    session: Session,
    keys: list[str],
    *,
    send: Any = None,
    shift: bool = False,
) -> None:
    for key in keys:
        ctx = Ctx(
            session=session,
            fixture=fixture,
            host=_Host(),
            w=80,
            h=24,
            settings=view,
            send=send,
        )
        dispatch(ctx, key, shift)


class _Link:
    """A daemon link that takes every verb and records it."""

    def __init__(self) -> None:
        self.sent: list[VerbRequest] = []

    def __call__(self, request: VerbRequest) -> bool:
        self.sent.append(request)
        return True


# ---------- UI-052: the three layer fields are typed against the Layer enum ----------


def test_ui052_the_layer_fields_are_typed_against_the_nine_value_enum() -> None:
    """The model refuses a layer word the kernel's enum does not have."""
    hints = get_type_hints(SettingsLeaf)
    assert Layer in get_args(hints["source_layer"])
    assert get_args(hints["override_chain"]) == (Layer, ...)
    assert get_args(hints["editable_at"]) == (Layer, ...)
    assert [layer.value for layer in Layer] == list(LAYER_ORDER)
    assert len(Layer) == 9


def test_ui052_the_lens_cycles_the_five_file_layers_in_precedence() -> None:
    """The writable layers sit between the built-in defaults and the runtime layers."""
    assert LENS_LAYERS == (Layer.GLOBAL, Layer.WORKSPACE, Layer.REPO, Layer.BRANCH, Layer.LOCAL)
    assert tuple(layer.value for layer in LENS_LAYERS) == WRITABLE_LAYERS
    assert {LAYER_PLACES[layer][0] for layer in LENS_LAYERS} == {LayerKind.FILE}


def test_ui052_a_policy_scope_is_refused_as_a_layer(tree: Path) -> None:
    """A compiled-Run scope word never passes as a config layer."""
    leaf = _view(tree).leaf(BOOL_KEY)
    with pytest.raises(ValidationError):
        SettingsLeaf.model_validate({**leaf.model_dump(mode="json"), "source_layer": "task"})
    with pytest.raises(ValidationError):
        SettingsLeaf.model_validate({**leaf.model_dump(mode="json"), "override_chain": ["policy"]})


def test_ui052_the_chain_is_every_losing_layer_lowest_first(tree: Path) -> None:
    """A local win over repo and the built-in default names both, lowest first."""
    _write(tree / ".ea" / "config.yaml", "profiles:\n  enabled: [alpha]\n")
    _write(tree / ".ea" / "local" / "config.yaml", "profiles:\n  enabled: [beta]\n")

    leaf = _view(tree).leaf(LIST_KEY)

    assert leaf.source_layer is Layer.LOCAL
    assert leaf.override_chain == (Layer.BUILT_IN, Layer.REPO)
    assert leaf.effective.value == "[beta]"


def test_ui052_editable_at_is_the_catalog_allowlist_in_precedence(tree: Path) -> None:
    """The layers a key may be written at come from the catalog, in enum order."""
    view = _view(tree)

    assert view.leaf(BOOL_KEY).editable_at == (Layer.GLOBAL, Layer.WORKSPACE, Layer.REPO)
    assert view.leaf(LOCKED_KEY).editable_at == ()
    for leaf in view.leaves:
        entry = LEAF_KEY_REGISTRY.get(leaf.key)
        if entry is not None and not entry.reserved:
            assert {layer.value for layer in leaf.editable_at} == set(entry.writable_layers)


def test_ui052_a_key_no_layer_states_has_no_source_and_reads_unknown(tree: Path) -> None:
    """The empty boundary: a catalog key nothing states names no layer at all."""
    view = _view(tree)
    unstated = next(leaf for leaf in view.leaves if leaf.source_layer is None)

    assert unstated.stack == ()
    assert unstated.override_chain == ()
    assert unstated.effective.value is None


def _layer_cells(rows: list[str]) -> list[str]:
    """Return the stack card's layer column, top to bottom, box and cursor stripped."""
    head = next(i for i, row in enumerate(rows) if "LAYER" in row and "WHERE" in row)
    return [row.strip("│ ").lstrip("▸").split()[0] for row in rows[head + 1 : head + 10]]


def test_ui052_no_settings_frame_names_a_layer_the_enum_lacks(tree: Path, fixture: Fixture) -> None:
    """The stack card's layer column is exactly the enum, in precedence order."""
    view = _view(tree)
    rows = _frame(fixture, view, _on(_session("settings.stack"), view, BOOL_KEY))

    assert _layer_cells(rows) == list(LAYER_ORDER)


# ---------- UI-057: one route, five categories, every catalog section once ----------


def test_ui057_every_catalog_section_is_filed_exactly_once() -> None:
    """The category table is total over the kernel's catalog, the lint the rail relies on."""
    domains = {entry.domain for entry in LEAF_KEY_REGISTRY.values()}

    assert category_assignment_defects(domains) == ()


def test_ui057_the_assignment_lint_names_each_way_it_fails() -> None:
    """Missing, doubled and phantom sections are each named rather than passed."""
    filed = {s for _name, members in SETTINGS_CATEGORIES for s in members}

    assert category_assignment_defects(filed | {"newsection"}) == (
        "section 'newsection' is filed under no category",
    )
    assert category_assignment_defects(filed - {"vcs"}) == (
        "category table files 'vcs', which the catalog lacks",
    )


def test_ui057_the_rail_lists_five_alphabetical_categories_and_their_sections(tree: Path) -> None:
    """Categories and sections are alphabetical, and every section appears once."""
    view = _view(tree)
    names = [category.name for category in view.rail]

    assert names == ["execution", "identity", "interface", "quality", "system"]
    for category in view.rail:
        assert list(category.sections) == sorted(category.sections)
    sections = view.sections()
    assert len(sections) == len(set(sections))
    assert set(sections) == {
        entry.domain for entry in LEAF_KEY_REGISTRY.values() if not entry.reserved
    }


def test_ui057_a_rail_name_is_never_clipped(tree: Path, fixture: Fixture) -> None:
    """The rail is as wide as its longest name plus the marker, so a tall frame shows all."""
    view = _view(tree)
    longest = max(len(section) for section in view.sections())
    rows = _frame(fixture, view, _session(), w=160, h=60)
    rail = "\n".join(row[: rail_width(view, wide=True)] for row in rows)

    assert rail_width(view, wide=True) == max(17, longest + 2)
    for section in view.sections():
        assert f"  {section}" in rail or f"▸ {section}" in rail


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_ui057_the_route_draws_at_every_size_with_the_packet_keybar(
    tree: Path, fixture: Fixture, w: int, h: int
) -> None:
    """The keybar reads as the packet states it, and the chain sits above the table."""
    # unset removes the lens layer's own value, so the key is set at repo to offer it
    _write(tree / ".ea" / "config.yaml", "telemetry:\n  enabled: false\n")
    view = _view(tree)
    rows = _frame(fixture, view, _on(_session(), view, BOOL_KEY), w=w, h=h)

    assert len(rows) == h
    wide = "   i stack   \\ filter" if w >= 120 else ""
    assert rows[-1].strip() == ROUTE_KEYBAR + wide
    # the tree's workspace root is its repo root, so the workspace layer is no other place
    assert "global › repo › branch › local" in "\n".join(rows)  # noqa: RUF001
    assert f"{len(view.rail)} categories" in rows[1]


def test_unset_is_not_offered_on_a_key_the_lens_layer_does_not_set(
    tree: Path, fixture: Fixture
) -> None:
    """A key with no value at the lens layer has nothing for ``x`` to remove."""
    view = _view(tree)
    rows = _frame(fixture, view, _on(_session(), view, BOOL_KEY), w=120, h=30)
    assert rows[-1].strip().startswith(ROUTE_KEYBAR.replace("x unset   ", ""))


def test_ui057_the_readout_names_type_meaning_and_allowed_values(
    tree: Path, fixture: Fixture
) -> None:
    """The docked readout is the key under the cursor, never a legend."""
    view = _view(tree)
    body = "\n".join(_frame(fixture, view, _on(_session(), view, LITERAL_KEY), w=120, h=30))

    assert "eu_basis · literal · one of api_duration | tokens | wall_clock" in body
    assert LEAF_KEY_REGISTRY[LITERAL_KEY].description[:40] in body


def test_ui057_the_glyph_is_derived_from_the_lens(tree: Path) -> None:
    """Set here and winning, set here and shadowed, inherited, and defaulted."""
    _write(tree / ".ea" / "config.yaml", "profiles:\n  enabled: [alpha]\n")
    _write(tree / ".ea" / "local" / "config.yaml", "profiles:\n  enabled: [beta]\n")
    leaf = _view(tree).leaf(LIST_KEY)

    assert glyph(leaf, Layer.LOCAL) == GLYPH_WINS
    assert glyph(leaf, Layer.REPO) == GLYPH_SHADOWED
    assert glyph(leaf, Layer.GLOBAL) == GLYPH_INHERITS
    assert glyph(_view(tree).leaf(BOOL_KEY), Layer.REPO) == GLYPH_DEFAULT


def test_ui057_tab_cycles_sections_and_shift_tab_steps_back(tree: Path, fixture: Fixture) -> None:
    """Tab wraps past the last section, and Shift-Tab wraps back past the first."""
    view = _view(tree)
    session = _session()
    count = len(view.sections())

    _press(fixture, view, session, ["Tab"])
    assert session.set_sec == 1
    _press(fixture, view, session, ["Tab"], shift=True)
    _press(fixture, view, session, ["Tab"], shift=True)
    assert session.set_sec == count - 1
    _press(fixture, view, session, ["Tab"])
    assert session.set_sec == 0
    assert session.set_key == 0


def test_ui057_escape_leaves_an_edit_before_it_leaves_the_route(
    tree: Path, fixture: Fixture
) -> None:
    """The first Escape closes the chooser and writes nothing; the route stays."""
    view = _view(tree)
    link = _Link()
    session = _on(_session(), view, BOOL_KEY)

    _press(fixture, view, session, ["Enter"], send=link)
    assert session.edit is not None
    _press(fixture, view, session, ["Escape"], send=link)

    assert session.edit is None
    assert session.route == "settings"
    assert link.sent == []


def test_ui057_l_cycles_the_lens_over_the_five_file_layers(tree: Path, fixture: Fixture) -> None:
    """One press per lens layer returns the lens to where it started; L steps it back."""
    view = _view(tree)
    session = _session()

    _press(fixture, view, session, ["l"])
    assert session.lens == "branch"
    _press(fixture, view, session, ["L"])
    assert session.lens == "repo"
    _press(fixture, view, session, ["l"] * len(view.lens_layers))
    assert session.lens == "repo"


# ---------- UI-053: the stack card ----------


@pytest.mark.parametrize(("w", "h"), SIZES)
def test_ui053_the_stack_is_every_layer_with_kind_and_template_place(
    tree: Path, fixture: Fixture, w: int, h: int
) -> None:
    """Nine rows, each a value or its absence, a kind and a place that is never a path."""
    _write(tree / ".ea" / "config.yaml", "profiles:\n  enabled: [alpha]\n")
    _write(tree / ".ea" / "local" / "config.yaml", "profiles:\n  enabled: [beta]\n")
    view = _view(tree)
    rows = _frame(fixture, view, _on(_session("settings.stack"), view, LIST_KEY), w=w, h=h)
    body = "\n".join(rows)

    assert rows[-1].strip() == STACK_KEYBAR
    assert "effective revision 41,209" in rows[1]
    assert _layer_cells(rows) == list(LAYER_ORDER)
    for layer in LAYER_ORDER:
        kind, where = LAYER_PLACES[Layer(layer)]
        row = next(r for r in rows if f" {layer} " in r.replace("▸", " ") and kind.value in r)
        assert kind.value in row
        assert where[:20] in row
    assert str(tree) not in body
    assert "WINNING    local [beta]" in body
    assert "LENS       repo" in body
    assert "repo sets [alpha], and the local layer above it wins." in body


def test_ui053_the_second_tier_is_absent_until_stated(tree: Path, fixture: Fixture) -> None:
    """An empty constraint, capability or secret field draws no row at all."""
    view = _view(tree)
    body = "\n".join(_frame(fixture, view, _on(_session("settings.stack"), view, BOOL_KEY)))

    for label in (" CONSTRAINED BY  ", " NEEDS      ", " SECRET     "):
        assert label not in body


def test_ui053_a_constrained_degraded_secret_key_draws_the_whole_tuple_at_80(
    tree: Path, fixture: Fixture
) -> None:
    """Every tier-two field has a home at 80 by 24."""
    view = _view(tree)
    leaf = view.leaf(BOOL_KEY).model_copy(
        update={
            "constraint_chain": ("workspace profile",),
            "capability_requirement": "network.egress",
            "secret_ref": "ref://vault/deploy",  # pragma: allowlist secret
        }
    )
    held = view.model_copy(
        update={"leaves": tuple(leaf if x.key == BOOL_KEY else x for x in view.leaves)}
    )
    rows = _frame(fixture, held, _on(_session("settings.stack"), held, BOOL_KEY))
    body = "\n".join(rows)

    assert "CONSTRAINED BY  workspace profile" in body
    assert "NEEDS      network.egress · ? certification unknown" in body
    assert "SECRET     ref://vault/deploy · the value never renders" in body
    assert "⊘" not in "\n".join(_frame(fixture, held, _on(_session(), held, BOOL_KEY)))


def test_ui053_the_stack_opens_on_i_and_reads_the_same_view(tree: Path, fixture: Fixture) -> None:
    """The card is a sub-surface of the route: ``i`` pushes it, Escape returns."""
    view = _view(tree)
    session = _on(_session(), view, LITERAL_KEY)

    _press(fixture, view, session, ["i"])
    assert session.route == "settings.stack"
    assert f"KEY        {LITERAL_KEY}" in "\n".join(_frame(fixture, view, session))
    _press(fixture, view, session, ["Escape"])
    assert session.route == "settings"


# ---------- the edit port ----------


def test_edit_a_locked_key_is_refused_with_its_reason(tree: Path, fixture: Fixture) -> None:
    """No chooser opens for a key no layer may write."""
    view = _view(tree)
    session = _on(_session(), view, LOCKED_KEY)

    _press(fixture, view, session, ["Enter"])

    assert session.edit is None
    assert "locked" in session.log[0].note


def test_edit_a_lens_the_key_is_not_writable_at_offers_the_layers_that_are(
    tree: Path, fixture: Fixture
) -> None:
    """UI-053: the lens cannot hold the key, so Enter offers the layers that can instead."""
    view = _view(tree)
    session = _on(_session(), view, BOOL_KEY)
    session.lens = "local"

    _press(fixture, view, session, ["Enter"])

    assert session.edit == {
        "kind": "offer",
        "key": BOOL_KEY,
        "layers": ["repo", "global"],
        "idx": 0,
    }
    assert "local cannot hold telemetry.enabled" in session.log[0].note


def test_edit_a_mapping_is_left_to_its_file(tree: Path, fixture: Fixture) -> None:
    """A free-form value names the file it is edited in instead of opening a chooser."""
    view = _view(tree)
    session = _on(_session(), view, MAPPING_KEY)

    _press(fixture, view, session, ["Enter"])

    assert session.edit is None
    assert "<repo>/.ea/config.yaml" in session.log[0].note


def test_edit_the_chooser_previews_the_write_before_it_is_sent(
    tree: Path, fixture: Fixture
) -> None:
    """The preview names the key, the target layer's place and the value after."""
    view = _view(tree)
    session = _on(_session(), view, LITERAL_KEY)

    _press(fixture, view, session, ["Enter", "ArrowDown"])
    body = "\n".join(_frame(fixture, view, session, w=120, h=30))

    assert f"WRITES   {LITERAL_KEY} at repo · <repo>/.ea/config.yaml" in body
    assert "AFTER    tokens from repo becomes the value in force" in body
    assert "▸ ○ tokens" in body
    assert "● api_duration" in body
    assert _frame(fixture, view, session, w=120, h=30)[-1].strip().startswith("↑↓ choose")


def test_edit_a_shadowed_write_says_it_changes_nothing_in_force(
    tree: Path, fixture: Fixture
) -> None:
    """A write under a higher layer is previewed as inert, not as a change."""
    _write(tree / ".ea" / "local" / "config.yaml", "estimation:\n  eu_basis: wall_clock\n")
    view = _view(tree)
    session = _on(_session(), view, LITERAL_KEY)

    _press(fixture, view, session, ["Enter"])
    body = "\n".join(_frame(fixture, view, session, w=120, h=30))

    assert "changes nothing in force · the local layer above repo holds wall_clock" in body


def test_edit_enter_sends_a_typed_request_and_holds_no_optimistic_value(
    tree: Path, fixture: Fixture
) -> None:
    """The request carries the typed value; the frame still shows the value read before."""
    view = _view(tree)
    link = _Link()
    session = _on(_session(), view, INT_KEY)

    _press(
        fixture,
        view,
        session,
        ["Enter", "Backspace", "Backspace", "4", "5", "Enter", "Enter"],
        send=link,
    )

    assert link.sent == [SettingRequest(target=INT_KEY, layer="repo", value=45)]
    assert session.edit is None
    row = next(r for r in _frame(fixture, view, session) if "eu_minutes" in r)
    assert "30" in row


def test_edit_a_value_the_type_refuses_is_not_sent(tree: Path, fixture: Fixture) -> None:
    """A non-number for an int key keeps the chooser open and sends nothing."""
    view = _view(tree)
    link = _Link()
    session = _on(_session(), view, INT_KEY)

    _press(fixture, view, session, ["Enter", "Backspace", "Backspace", "x", "Enter"], send=link)

    assert link.sent == []
    assert session.edit is not None
    assert "not a whole number" in session.log[0].note


def test_edit_x_unsets_only_what_the_lens_layer_states(tree: Path, fixture: Fixture) -> None:
    """Nothing at the lens is nothing to unset; a stated value is sent as an unset."""
    _write(tree / ".ea" / "config.yaml", "estimation:\n  eu_basis: tokens\n")
    view = _view(tree)
    link = _Link()
    session = _on(_session(), view, LITERAL_KEY)
    session.lens = "global"

    _press(fixture, view, session, ["x"], send=link)
    assert link.sent == []
    assert "not set at global" in session.log[0].note

    session.lens = "repo"
    _press(fixture, view, session, ["x", "Enter"], send=link)
    assert link.sent == [SettingRequest(target=LITERAL_KEY, layer="repo", unset=True)]
    assert "falls back to built-in api_duration" in session.log[1].note


def test_edit_without_a_daemon_link_writes_nothing_and_says_so(
    tree: Path, fixture: Fixture
) -> None:
    """A console with no link never pretends an edit landed."""
    view = _view(tree)
    session = _on(_session(), view, BOOL_KEY)

    _press(fixture, view, session, ["Enter", "Enter"])

    assert "no daemon link · nothing was written" in session.log[0].note
    assert not (tree / ".ea" / "config.yaml").exists()


def test_edit_a_branch_write_names_the_branch_the_view_read(tree: Path, fixture: Fixture) -> None:
    """The branch layer is addressed by the branch whose file the view read."""
    view = _view(tree)
    link = _Link()
    session = _on(_session(), view, LIST_KEY)
    session.lens = "branch"

    _press(fixture, view, session, ["Enter", "Enter", "Enter"], send=link)

    assert link.sent == [
        SettingRequest(target=LIST_KEY, layer="branch", value=["core"], branch=BRANCH)
    ]


@pytest.mark.parametrize(
    ("value_type", "text", "value"),
    [
        ("bool", "true", True),
        ("bool", "false", False),
        ("int", "0", 0),
        ("float", "0.5", 0.5),
        ("list_str", "", []),
        ("list_str", "a, b,, c", ["a", "b", "c"]),
        ("str", "", ""),
    ],
)
def test_edit_coerce_types_the_value_by_the_catalog_shape(
    tree: Path, value_type: str, text: str, value: object
) -> None:
    leaf = _view(tree).leaf(BOOL_KEY).model_copy(update={"value_type": value_type})
    assert coerce(leaf, text) == (value, "")


def test_edit_a_setting_request_refuses_what_no_layer_write_carries() -> None:
    with pytest.raises(ValueError, match="is not one of"):
        SettingRequest(target=BOOL_KEY, layer="env", value=True)
    with pytest.raises(ValueError, match="needs the branch"):
        SettingRequest(target=BOOL_KEY, layer="branch", value=True)
    with pytest.raises(ValueError, match="not a dotted settings key"):
        SettingRequest(target="a..b", layer="repo", value=True)


def test_edit_the_request_is_addressed_to_the_layered_config_verbs() -> None:
    """The operation id is the idempotency key; an unset carries no value."""
    written = address_setting(SettingRequest(target=BOOL_KEY, layer="repo", value=False))
    removed = address_setting(SettingRequest(target=BOOL_KEY, layer="repo", unset=True))

    assert written.method == SETTING_SET_METHOD
    assert dict(written.params) == {
        "layer": "repo",
        "key_path": ["telemetry", "enabled"],
        "idempotency_key": written.operation_id,
        "value": False,
    }
    assert removed.method == SETTING_UNSET_METHOD
    assert "value" not in removed.params


def _daemon_ctx(repo: Path) -> MethodContext:
    state_path = repo / ".ea" / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    return MethodContext(
        started_at="2026-09-27T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        state_path=state_path,
        idempotency_cache={},
    )


def test_edit_end_to_end_the_daemon_writes_the_layer_and_the_frame_shows_its_reread(
    tree: Path, fixture: Fixture
) -> None:
    """Keys on the route reach the daemon's config verb; the frame draws the re-read view."""
    daemon = _daemon_ctx(tree)
    asked: list[str] = []

    async def _call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        asked.append(method)
        if method == SETTING_SET_METHOD:
            return await set_layer_value(daemon, params)
        if method == SETTING_UNSET_METHOD:
            return await unset_layer_value(daemon, params)
        assert method == SETTINGS_READ_METHOD
        return _view(tree).model_dump(mode="json")

    seam = ProjectionSeam(route="settings", scope_id=SCOPE, state_path=None, repo_root=tree)
    seam.binding.call = _call  # type: ignore[method-assign]
    view = asyncio.run(seam.load_settings())
    link = _Link()
    session = _on(_session(), view, LITERAL_KEY)

    _press(fixture, view, session, ["Enter", "ArrowDown", "Enter", "Enter"], send=link)
    result = asyncio.run(seam.request(link.sent[0]))

    assert result.status is OperationStatus.APPLIED
    assert yaml.safe_load((tree / ".ea" / "config.yaml").read_text()) == {
        "estimation": {"eu_basis": "tokens"}
    }
    assert asked == [SETTINGS_READ_METHOD, SETTING_SET_METHOD, SETTINGS_READ_METHOD]
    held = seam.settings
    assert held is not None
    assert held.leaf(LITERAL_KEY).source_layer is Layer.REPO
    row = next(r for r in _frame(fixture, held, session) if "eu_basis" in r)
    assert "tokens" in row
    assert "repo" in row

    _press(fixture, held, session, ["Escape", "x", "Enter"], send=link)
    removed = asyncio.run(seam.request(link.sent[1]))

    assert removed.status is OperationStatus.APPLIED
    assert "estimation" not in (yaml.safe_load((tree / ".ea" / "config.yaml").read_text()) or {})
    assert seam.settings is not None
    assert seam.settings.leaf(LITERAL_KEY).source_layer is Layer.BUILT_IN


def test_edit_a_refused_write_changes_no_file_and_keeps_the_view(tree: Path) -> None:
    """The daemon refuses a layer the key is not writable at; nothing is re-read."""
    daemon = _daemon_ctx(tree)

    async def _call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == SETTING_SET_METHOD:
            try:
                return await set_layer_value(daemon, params)
            except ValueError as error:
                # the transport hands a handler's refusal back as an error envelope
                raise DaemonRpcError(-32602, str(error)) from error
        return _view(tree).model_dump(mode="json")

    seam = ProjectionSeam(route="settings", scope_id=SCOPE, state_path=None, repo_root=tree)
    seam.binding.call = _call  # type: ignore[method-assign]
    before = asyncio.run(seam.load_settings())

    result = asyncio.run(seam.request(SettingRequest(target=BOOL_KEY, layer="local", value=False)))

    assert result.status is OperationStatus.REFUSED
    assert "not writable from the local layer" in result.detail
    assert seam.outstanding == ()
    assert not (tree / ".ea" / "local" / "config.yaml").exists()
    assert seam.settings is before


@pytest.mark.parametrize(
    ("value", "reason"),
    [(17, "above maximum 16"), (0, "below minimum 1"), ("fast", "cannot coerce")],
)
def test_ui_053_the_console_shows_the_daemon_refusing_a_value_the_stack_rules_out(
    tree: Path, value: object, reason: str
) -> None:
    """A write outside the range the stack draws is refused by the daemon and said so."""
    daemon = _daemon_ctx(tree)

    async def _call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == SETTING_SET_METHOD:
            try:
                return await set_layer_value(daemon, params)
            except ValueError as error:
                raise DaemonRpcError(-32602, str(error)) from error
        return _view(tree).model_dump(mode="json")

    seam = ProjectionSeam(route="settings", scope_id=SCOPE, state_path=None, repo_root=tree)
    seam.binding.call = _call  # type: ignore[method-assign]
    before = asyncio.run(seam.load_settings())
    assert before.leaf(RANGED_KEY).constraint_chain == ("config registry range 1 to 16 · built-in",)

    result = asyncio.run(seam.request(SettingRequest(target=RANGED_KEY, layer="repo", value=value)))

    assert result.status is OperationStatus.REFUSED
    assert "validation_failed:" in result.detail
    assert reason in result.detail
    assert not (tree / ".ea" / "config.yaml").exists()
    assert seam.settings is before


# ---------- an open editor recedes the rail and the key list ----------


def test_an_open_editor_recedes_the_rail_and_keys_and_keeps_the_readout_lit(
    tree: Path, fixture: Fixture
) -> None:
    view = _view(tree)
    session = _on(_session(), view, BOOL_KEY)
    browsing = _frame(fixture, view, session)
    assert not any(isinstance(row, (Receded, LeadReceded)) for row in browsing)

    _press(fixture, view, session, ["Enter"], send=_Link())
    assert session.edit is not None
    rows = _frame(fixture, view, session)[3:-2]
    readout = next(i for i, row in enumerate(rows) if "├─" in row)
    assert all(isinstance(row, Receded) for row in rows[:readout])
    assert all(isinstance(row, LeadReceded) for row in rows[readout:])
    lit = [s for s in paint(rows[-1], Part.BODY) if s.text.strip() and s.surface != "recede"]
    assert lit, "the readout beside the rail is drawn as usual"
