"""Settings show what is in force, which layer set it, and which layers it overrode.

The settings surface used to draw a catalog the console carried, so a value appeared with
no statement of where it came from. That is the one thing an operator needs: the layer
that wins is the only place editing the value has any effect, and a repo value a local
file quietly overrides looks exactly like one in force.

Both settings routes now draw a :class:`~eawf.kernel.projection.settings.SettingsView`
read through ``projection.settings.read``. The suite pins four things about it: every leaf
names the layer that set it and the layers it overrode, lowest precedence first; the merge
engine remains the single authority on which layer wins; the read writes nothing, which is
what lets a console open the surface against an operator's real config; and both frames
draw from one read, so the stack card cannot disagree with the row it was opened from.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.config.layered import LAYER_ORDER
from eawf.kernel.projection.read_models import ReadModelKind
from eawf.kernel.projection.settings import (
    SETTINGS_ROUTE,
    SETTINGS_ROUTES,
    SETTINGS_STACK_ROUTE,
    SettingsView,
    build_settings_view,
    layer_overlays,
    render_value,
)
from eawf.kernel.projection.truth import TruthState
from eawf.runtime.daemon.methods import registered_methods
from eawf.runtime.daemon.methods.projection import ROUTE_READ_METHODS, SETTINGS_READ_METHOD
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.provenance import NOTHING_OVERRIDDEN, OVERRIDDEN
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session

#: When the probe views are stamped. The digest does not cover the stamp.
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

#: The scope every probe view is built for.
SCOPE = "EAWF"

#: The branch the probe names, so the layer walk never shells out to git for it.
BRANCH = "probe"

#: A key no built-in default states, so its stack is exactly the layers the probe wrote.
PROBE_KEY = "probe.leaf"

#: A key the built-in defaults state, so its stack always carries that floor.
FLOOR_KEY = "schema_version"


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repo root whose global, repo and local layers the probe owns.

    The global layer lives under the home directory, so the home is redirected here: a
    suite that read the operator's real global config would pass or fail by accident.
    """
    home = tmp_path / "home"
    (home / ".config" / "eawf").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    repo = tmp_path / "repo"
    (repo / ".ea" / "local").mkdir(parents=True)
    return repo


def _write(path: Path, body: str) -> None:
    """Write one layer's YAML, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _view(tree: Path, *, cursor: int = 41208) -> SettingsView:
    """Return the settings view over the probe tree, with no env layer in play."""
    return build_settings_view(
        workspace=tree,
        repo=tree,
        scope_id=SCOPE,
        cursor=cursor,
        generated_at=AT,
        env={},
        branch=BRANCH,
    )


def _frame(route: str, view: SettingsView, *, sel: int = 0, width: int = 120) -> list[str]:
    """Return the console frame ``route`` renders from ``view``."""
    session = Session()
    session.route = REGISTRY.by_key[route].id
    session.sel = sel
    rendered = View(
        session=session,
        fixture=load_fixture(
            Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"
        ),
        w=width,
        h=24,
        settings=view,
    )
    return render_route(rendered)


def _leaf_row(rows: list[str], key: str) -> str:
    """Return the frame row that draws ``key``."""
    return next(row for row in rows if key in row)


# ---------- the verb, and the routes it serves ----------


def test_the_settings_route_is_served_by_its_own_read_verb() -> None:
    """One read answers both settings routes, and it is not a route projection."""
    assert SETTINGS_ROUTES == (SETTINGS_ROUTE, SETTINGS_STACK_ROUTE)
    assert SETTINGS_READ_METHOD == "projection.settings.read"
    assert SETTINGS_READ_METHOD in registered_methods()
    assert SETTINGS_READ_METHOD not in ROUTE_READ_METHODS


def test_the_console_reads_the_settings_view_through_that_verb(tree: Path) -> None:
    """The seam asks the daemon for it by name and holds what comes back."""
    view = _view(tree)
    asked: list[str] = []

    async def _call(method: str, _params: dict[str, object]) -> dict[str, object]:
        asked.append(method)
        return view.model_dump(mode="json")

    seam = ProjectionSeam(route=SETTINGS_ROUTE, scope_id=SCOPE, state_path=None)
    seam.binding.call = _call  # type: ignore[method-assign]

    assert seam.settings is None

    held = asyncio.run(seam.load_settings())

    assert asked == [SETTINGS_READ_METHOD]
    assert seam.settings is held
    assert held.digest == view.digest


def test_the_view_states_the_read_model_the_registry_binds(tree: Path) -> None:
    """The header names the model the settings route declares, not a row projection."""
    view = _view(tree)

    assert view.header.projection_kind is ReadModelKind.EFFECTIVE_SETTINGS_VIEW
    assert REGISTRY.read_models["settings"] is ReadModelKind.EFFECTIVE_SETTINGS_VIEW
    assert REGISTRY.read_models["settings.stack"] is ReadModelKind.SETTINGS_LAYER_STACK
    assert view.header.source_cursor == "41208"
    assert view.header.scope_id == SCOPE


# ---------- the layer that set a leaf, and the layers it overrode ----------


def test_a_leaf_one_layer_states_overrode_nothing(tree: Path) -> None:
    """The single boundary: one layer, one stack entry, and it is the winner."""
    _write(tree / ".ea" / "config.yaml", "probe:\n  leaf: from-repo\n")

    leaf = _view(tree).leaf(PROBE_KEY)

    assert leaf.winning_layer == "repo"
    assert leaf.effective.value == "from-repo"
    assert leaf.overridden() == ()
    assert [(entry.layer, entry.wins) for entry in leaf.stack] == [("repo", True)]


def test_a_local_value_wins_and_the_stack_names_every_layer_it_overrode(tree: Path) -> None:
    """The stack is lowest precedence first, so the winner is last and the losers precede it."""
    # the fixture redirected the home, so this is the probe's own global layer
    _write(Path.home() / ".config" / "eawf" / "config.yaml", "probe:\n  leaf: from-global\n")
    _write(tree / ".ea" / "config.yaml", "probe:\n  leaf: from-repo\n")
    _write(tree / ".ea" / "branches" / f"{BRANCH}.yaml", "probe:\n  leaf: from-branch\n")
    _write(tree / ".ea" / "local" / "config.yaml", "probe:\n  leaf: from-local\n")

    leaf = _view(tree).leaf(PROBE_KEY)

    assert leaf.winning_layer == "local"
    assert leaf.effective.value == "from-local"
    assert leaf.overridden() == ("global", "repo", "branch")
    assert [entry.value for entry in leaf.stack] == [
        "from-global",
        "from-repo",
        "from-branch",
        "from-local",
    ]
    assert [entry.wins for entry in leaf.stack] == [False, False, False, True]


def test_a_layer_above_the_winner_is_never_in_the_stack(tree: Path) -> None:
    """A layer that stated the leaf and sat above the winner would be the winner."""
    _write(tree / ".ea" / "config.yaml", "probe:\n  leaf: from-repo\n")

    leaf = _view(tree).leaf(PROBE_KEY)
    above = LAYER_ORDER[LAYER_ORDER.index("repo") + 1 :]

    assert not [entry for entry in leaf.stack if entry.layer in above]


def test_a_layer_that_states_nothing_for_a_leaf_is_left_out(tree: Path) -> None:
    """A layer with no value for a key is absent from that key's stack, not an empty row."""
    _write(tree / ".ea" / "config.yaml", "probe:\n  leaf: from-repo\n")
    _write(tree / ".ea" / "local" / "config.yaml", "probe:\n  other: from-local\n")

    leaf = _view(tree).leaf(PROBE_KEY)

    assert "local" not in {entry.layer for entry in leaf.stack}
    assert leaf.winning_layer == "repo"


def test_the_built_in_floor_is_the_winner_when_nothing_overrides_it(tree: Path) -> None:
    """Every built-in default is a leaf, and it wins until a file layer states it."""
    leaf = _view(tree).leaf(FLOOR_KEY)

    assert leaf.winning_layer == "built-in"
    assert leaf.overridden() == ()
    assert leaf.effective.state is TruthState.KNOWN


def test_every_leaf_names_a_layer_and_ends_its_stack_with_the_winner(tree: Path) -> None:
    """The whole read, not one probe key: one winner per leaf and it is the last entry."""
    _write(tree / ".ea" / "config.yaml", "probe:\n  leaf: from-repo\n")

    for leaf in _view(tree).leaves:
        assert leaf.winning_layer in LAYER_ORDER
        assert leaf.stack
        assert leaf.stack[-1].layer == leaf.winning_layer
        assert [entry.wins for entry in leaf.stack].count(True) == 1


# ---------- the read writes nothing ----------


def test_reading_the_settings_view_writes_no_config(tree: Path) -> None:
    """The console opens the surface against a real config without touching a byte of it."""
    layers = {
        tree / ".ea" / "config.yaml": "probe:\n  leaf: from-repo\n",
        tree / ".ea" / "local" / "config.yaml": "probe:\n  leaf: from-local\n",
    }
    for path, body in layers.items():
        _write(path, body)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in layers}

    _view(tree)
    _view(tree)

    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in layers} == before
    assert not (tree / ".ea" / "branches").exists()


def test_a_layer_with_no_file_is_not_created_by_reading_it(tree: Path) -> None:
    """A missing layer stays missing: the overlay walk reads, it never seeds."""
    overlays = layer_overlays(workspace=tree, repo=tree, branch=BRANCH)

    assert overlays == {}
    assert not (tree / ".ea" / "config.yaml").exists()


# ---------- the frames ----------


def test_the_settings_frame_shows_each_leaf_with_its_layer_and_what_it_overrode(
    tree: Path,
) -> None:
    """The list answers both questions on one row: what is in force, and who set it."""
    _write(tree / ".ea" / "config.yaml", "probe:\n  leaf: from-repo\n")
    _write(tree / ".ea" / "local" / "config.yaml", "probe:\n  leaf: from-local\n")
    view = _view(tree)
    at = [leaf.key for leaf in view.leaves].index(PROBE_KEY)

    rows = _frame("settings", view, sel=at)
    row = _leaf_row(rows, PROBE_KEY)

    assert "from-local" in row
    assert "local" in row
    assert "repo" in row
    assert f"{len(view.leaves):,} leaves" in rows[1]


def test_the_settings_frame_marks_a_leaf_nothing_overrode(tree: Path) -> None:
    """A leaf one layer states says so, rather than leaving the column blank."""
    _write(tree / ".ea" / "config.yaml", "probe:\n  leaf: from-repo\n")
    view = _view(tree)
    at = [leaf.key for leaf in view.leaves].index(PROBE_KEY)

    rows = _frame("settings", view, sel=at)

    assert NOTHING_OVERRIDDEN in _leaf_row(rows, PROBE_KEY)


def test_the_stack_card_draws_the_focused_leafs_whole_ladder(tree: Path) -> None:
    """The card is the same read as the list, so the two cannot disagree."""
    _write(tree / ".ea" / "config.yaml", "probe:\n  leaf: from-repo\n")
    _write(tree / ".ea" / "local" / "config.yaml", "probe:\n  leaf: from-local\n")
    view = _view(tree)
    at = [leaf.key for leaf in view.leaves].index(PROBE_KEY)

    rows = _frame("settings.stack", view, sel=at)
    body = "\n".join(rows)

    assert PROBE_KEY in body
    assert "in force from local" in body
    assert OVERRIDDEN in body
    assert "from-repo" in body


def test_both_settings_frames_read_one_view(tree: Path) -> None:
    """One read answers the list and the card, so one digest covers what both drew."""
    _write(tree / ".ea" / "config.yaml", "probe:\n  leaf: from-repo\n")
    view = _view(tree)

    assert _frame("settings", view)
    assert _frame("settings.stack", view)
    assert view.digest == _view(tree).digest


def test_a_changed_layer_changes_the_digest(tree: Path) -> None:
    """Config is not ordered by the cursor, so the digest tracks the leaves alone."""
    _write(tree / ".ea" / "config.yaml", "probe:\n  leaf: from-repo\n")
    before = _view(tree)

    _write(tree / ".ea" / "config.yaml", "probe:\n  leaf: edited\n")
    after = _view(tree)

    assert before.digest != after.digest
    assert before.header.source_cursor == after.header.source_cursor


# ---------- refusals and boundaries ----------


def test_a_negative_cursor_is_refused(tree: Path) -> None:
    """A cursor is a committed ordinal, so there is no view before the first one."""
    with pytest.raises(ValueError, match="never -1"):
        build_settings_view(
            workspace=tree,
            repo=tree,
            scope_id=SCOPE,
            cursor=-1,
            generated_at=AT,
            env={},
            branch=BRANCH,
        )


def test_an_unknown_key_raises_rather_than_answering_a_blank(tree: Path) -> None:
    """Addressing a leaf the merge never placed is an error, not an empty value."""
    with pytest.raises(KeyError):
        _view(tree).leaf("probe.nothing.states.this")


def test_the_zero_cursor_of_an_unread_workspace_is_admitted(tree: Path) -> None:
    """The floor boundary: a workspace that has committed nothing still reads its config."""
    view = _view(tree, cursor=0)

    assert view.header.source_cursor == "0"
    assert view.header.projection_revision == 1


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (True, "true"),
        (False, "false"),
        (None, "null"),
        ("", '""'),
        ("plain", "plain"),
        ([], "[]"),
        ([1, "two"], "[1, two]"),
        ({}, "{}"),
        ({"b": 1, "a": 2}, "{a, b}"),
        (3, "3"),
    ],
)
def test_every_value_renders_as_non_empty_text(value: object, text: str) -> None:
    """An empty string and an empty list are told apart from a layer stating nothing."""
    assert render_value(value) == text
