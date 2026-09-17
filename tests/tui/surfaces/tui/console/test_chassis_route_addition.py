"""A console route is one registry row, and the reachability audit is green.

Adding a trivial route row changes no other console module (header, keybar and token
modules included) and no epoch-1 ``theme.tcss`` rule, every derived index picks the row
up, and every route has a door, with the pack's ``timeline`` keyed ``roadmap`` and
``notifications`` a global route.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from eawf.surfaces.tui.console import registry
from eawf.surfaces.tui.console.registry import (
    REGISTRY,
    ROUTES,
    Door,
    DoorKind,
    RouteFamily,
    RouteRegistry,
    RouteSpec,
    kind_of,
    route_for_id,
    route_of,
    unreachable_routes,
)

CONSOLE_DIR = Path(registry.__file__).resolve().parent
THEME_TCSS = CONSOLE_DIR.parent / "theme.tcss"
TESTS_ROOT = Path(__file__).resolve().parents[4]
NORMALISATION_MAP = TESTS_ROOT / "fixtures" / "console" / "golden" / "normalisation-map.json"
CHASSIS_REGISTRY = "tests.snapshots.tui.console.console_chassis.chassis.registry"

# Epoch-1 UserScreen and placeholder rules that stay until the release candidate retires them.
EPOCH1_SELECTORS = (
    ".section {",
    ".section-title {",
    "#attention {",
    "#effort {",
    "#portfolio {",
    ".placeholder-notice {",
    ".feed-empty {",
)

TRIVIAL = RouteSpec(
    id="spike.trivial",
    family=RouteFamily.DIAGNOSTICS,
    palette_visible=True,
    go_letter="z",
    step_leaf="Trivial",
    via_leaf="Trivial",
    parent=("scope.home", None),
    tab_owner="rows",
    word="trivial",
)


def _shared_digest() -> dict[str, str]:
    """Return the digest of every console module but the registry, plus the stylesheet."""
    paths = [p for p in sorted(CONSOLE_DIR.rglob("*.py")) if p.name != "registry.py"]
    digests = {
        p.relative_to(CONSOLE_DIR).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
    }
    digests["theme.tcss"] = hashlib.sha256(THEME_TCSS.read_bytes()).hexdigest()
    return digests


Index = dict[str, object] | tuple[str, ...]


def _indexes(reg: RouteRegistry) -> dict[str, Index]:
    """Return every derived index of ``reg`` as plain comparable data."""
    return {
        "ids": reg.ids,
        "by_key": {k: s.id for k, s in reg.by_key.items()},
        "go_map": dict(reg.go_map),
        "route_list": reg.route_list,
        "step_leaves": dict(reg.step_leaves),
        "via_leaves": dict(reg.via_leaves),
        "parents": dict(reg.parents),
        "tab_owners": dict(reg.tab_owners),
        "overlay_routes": tuple(sorted(reg.overlay_routes)),
        "words": {r: reg.route_word(r) for r in reg.ids},
    }


def _without(index: Index, route: str) -> Index:
    """Return ``index`` with every entry that names ``route`` removed."""
    if isinstance(index, dict):
        return {k: v for k, v in index.items() if route not in (k, v)}
    return tuple(x for x in index if x != route)


def test_route_registry_trivial_row_changes_no_shared_module() -> None:
    before = _shared_digest()
    assert {"tokens.py", "width.py", "session.py", "theme.tcss"} <= set(before)
    extended = RouteRegistry((*ROUTES, TRIVIAL))
    assert extended.by_id["spike.trivial"] is TRIVIAL
    assert _shared_digest() == before
    assert REGISTRY.routes == ROUTES
    tcss = THEME_TCSS.read_text(encoding="utf-8")
    assert [s for s in EPOCH1_SELECTORS if s not in tcss] == []


def test_route_registry_trivial_row_reaches_every_index() -> None:
    extended = RouteRegistry((*ROUTES, TRIVIAL))
    assert extended.by_key["spike.trivial"] is TRIVIAL
    assert extended.go_map["z"] == "spike.trivial"
    assert "spike.trivial" in extended.route_list
    assert list(extended.route_list) == sorted(extended.route_list)
    assert extended.step_leaf("spike.trivial", None) == "Trivial"
    assert extended.step_leaf("spike.trivial", "RUN-1") == "RUN-1"
    assert extended.via_leaves["spike.trivial"] == "Trivial"
    assert extended.parents["spike.trivial"] == ("scope.home", None)
    assert extended.tab_owners["spike.trivial"] == "rows"
    assert extended.route_word("spike.trivial") == "trivial"
    assert extended.doors("spike.trivial") == (
        Door(kind=DoorKind.GO, key="z"),
        Door(kind=DoorKind.PALETTE),
    )


def test_route_registry_trivial_row_leaves_existing_indexes_unchanged() -> None:
    base = _indexes(REGISTRY)
    extended = _indexes(RouteRegistry((*ROUTES, TRIVIAL)))
    assert {name: _without(index, "spike.trivial") for name, index in extended.items()} == base
    assert extended != base


def test_unreachable_routes_registry_is_green() -> None:
    assert unreachable_routes(ROUTES) == ()
    assert len(REGISTRY.routes) == 34
    assert "".join(sorted(REGISTRY.go_map)) == "abdhilnrstuy"
    assert len(REGISTRY.route_list) == 14


@pytest.mark.parametrize("route", REGISTRY.ids)
def test_route_registry_every_route_declares_a_door(route: str) -> None:
    doors = REGISTRY.doors(route)
    assert doors
    for door in doors:
        if door.origin is not None:
            assert door.origin in REGISTRY.by_id


def test_route_registry_roadmap_keyed() -> None:
    spec = REGISTRY.by_key["roadmap"]
    assert spec.id == "timeline"
    assert "timeline" not in REGISTRY.by_key
    assert spec.family == RouteFamily.PLANNING
    assert REGISTRY.go_map["t"] == "timeline"
    assert "timeline" in REGISTRY.route_list
    assert all(s.key == s.id for s in ROUTES if s.id != "timeline")


def test_route_registry_notifications_global() -> None:
    spec = REGISTRY.by_key["notifications"]
    assert spec.family == RouteFamily.DIAGNOSTICS
    assert not spec.subject_required
    assert spec.fixed_subject is None
    assert spec.parent is None
    assert REGISTRY.subj_now("notifications", None) is None
    doors = REGISTRY.doors("notifications")
    assert Door(kind=DoorKind.GO, key="i") in doors
    assert Door(kind=DoorKind.PALETTE) in doors
    assert all(door.origin is None for door in doors)
    assert "notifications" in REGISTRY.overlay_routes


def test_route_registry_corrections_match_normalisation_map() -> None:
    entries = json.loads(NORMALISATION_MAP.read_text(encoding="utf-8"))["entries"]
    corrections = {
        e["route_id"]: (e["route_key"], e["route_family"]) for e in entries if e.get("route_id")
    }
    assert corrections == {
        "timeline": ("roadmap", "planning"),
        "notifications": ("notifications", "diagnostics"),
    }
    for route_id, (key, family) in corrections.items():
        spec = REGISTRY.by_id[route_id]
        assert (spec.key, spec.family) == (key, family)


def test_unreachable_routes_doorless_row_reported() -> None:
    doorless = RouteSpec(id="spike.doorless", family=RouteFamily.DIAGNOSTICS)
    assert unreachable_routes((*ROUTES, doorless)) == ("spike.doorless",)
    with pytest.raises(ValueError, match=re.escape("routes with no door: spike.doorless")):
        RouteRegistry((*ROUTES, doorless))


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            RouteSpec(id="entry", family=RouteFamily.SPINE, palette_visible=True),
            "id registered twice: entry",
        ),
        (
            RouteSpec(id="spike.a", key="roadmap", family=RouteFamily.SPINE, palette_visible=True),
            "key registered twice: roadmap",
        ),
        (
            RouteSpec(id="spike.a", family=RouteFamily.SPINE, go_letter="h"),
            "g letter registered twice: h",
        ),
        (
            RouteSpec(
                id="spike.a",
                family=RouteFamily.SPINE,
                palette_visible=True,
                parent=("spike.gone", None),
            ),
            "unregistered routes: spike.gone",
        ),
        (
            RouteSpec(
                id="spike.a",
                family=RouteFamily.SPINE,
                doors=(Door(kind=DoorKind.ROUTE_KEY, key="Enter", origin="spike.gone"),),
            ),
            "unregistered routes: spike.gone",
        ),
    ],
)
def test_route_registry_invalid_row_raises_value_error(row: RouteSpec, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        RouteRegistry((*ROUTES, row))


def test_route_registry_empty_table_raises_value_error() -> None:
    # every drill table target must be registered, so no route set short of them builds
    with pytest.raises(ValueError, match="unregistered routes"):
        RouteRegistry(())


def test_route_registry_unknown_route_doors_raises_key_error() -> None:
    with pytest.raises(KeyError):
        REGISTRY.doors("spike.gone")


@pytest.mark.parametrize("kind", [DoorKind.LIGHT_VERB, DoorKind.ROUTE_KEY])
def test_door_local_kind_without_origin_raises_value_error(kind: DoorKind) -> None:
    with pytest.raises(ValueError, match="needs origin route"):
        Door(kind=kind, key="x")


@pytest.mark.parametrize("kind", [DoorKind.GO, DoorKind.PALETTE, DoorKind.DRILL])
def test_door_global_kind_with_origin_raises_value_error(kind: DoorKind) -> None:
    with pytest.raises(ValueError, match="takes no origin route"):
        Door(kind=kind, origin="activity")


def test_route_spec_declared_global_door_raises_value_error() -> None:
    with pytest.raises(ValueError, match="declares go doors"):
        RouteSpec(id="spike.a", family=RouteFamily.SPINE, doors=(Door(kind=DoorKind.GO, key="q"),))


def test_route_spec_key_defaults_to_id() -> None:
    assert RouteSpec(id="spike.a", family=RouteFamily.SPINE).key == "spike.a"


@pytest.mark.parametrize(
    ("route", "subj", "expected"),
    [
        ("scope.home", None, ""),
        ("track", None, "Runtime"),
        ("task.detail", None, "task"),
        ("batch.detail", None, "batch.detail"),
        ("batch.detail", "BAT-0001", "BAT-0001"),
        ("spike.gone", None, "spike.gone"),
    ],
)
def test_route_registry_step_leaf_prefers_subject_then_leaf_then_word(
    route: str, subj: str | None, expected: str
) -> None:
    assert REGISTRY.step_leaf(route, subj) == expected


@pytest.mark.parametrize(
    ("route", "entity_id", "expected"),
    [
        ("evidence", None, "CLM-0004"),
        ("trust", None, "MLS-0007"),
        ("run.detail", "RUN-1", "RUN-1"),
        ("activity", None, None),
        ("spike.gone", None, None),
    ],
)
def test_route_registry_subj_now_falls_back_to_fixed_subject(
    route: str, entity_id: str | None, expected: str | None
) -> None:
    assert REGISTRY.subj_now(route, entity_id) == expected


@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        (None, None),
        ("", None),
        ("RUN-538453eb", "run.detail"),
        ("EAWF-0091", "task.detail"),
        ("EVT-2201", "receipt"),
        ("CLM-0004", "evidence"),
        ("EVD-0001", "evidence"),
        ("TRK-0001", None),
        ("XYZ-0001", None),
    ],
)
def test_route_for_id_maps_prefix_to_its_drill_route(
    entity_id: str | None, expected: str | None
) -> None:
    assert route_for_id(entity_id) == expected


@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        ("", None),
        ("EAWF-0091", "task.detail"),
        ("EVT-2201", "run.detail"),
        ("QST-0001", "attention"),
        ("TRK-0001", "track"),
        ("XYZ-0001", None),
    ],
)
def test_route_of_maps_first_four_characters(entity_id: str, expected: str | None) -> None:
    assert route_of(entity_id) == expected


@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        (None, "entity"),
        ("", "entity"),
        ("RUN-1", "run"),
        ("EAWF-0091", "task"),
        ("ZZZ-1", "entity"),
    ],
)
def test_kind_of_names_the_entity_kind(entity_id: str | None, expected: str) -> None:
    assert kind_of(entity_id) == expected


def test_route_registry_matches_the_test_only_chassis() -> None:
    # parity holds only while both copies exist; the skip marks the chassis' removal
    theirs = pytest.importorskip(CHASSIS_REGISTRY, exc_type=ModuleNotFoundError)
    columns = ("id", "key", "family", "subject_required", "palette_visible", "go_letter")
    columns += ("step_leaf", "parent", "tab_owner", "via_leaf")
    assert [tuple(getattr(s, c) for c in columns) for s in ROUTES] == [
        tuple(getattr(s, c) for c in columns) for s in theirs.ROUTES
    ]
    assert dict(REGISTRY.go_map) == theirs.GO_MAP
    assert REGISTRY.route_list == theirs.ROUTE_LIST
    assert dict(REGISTRY.step_leaves) == theirs.STEP_LEAF
    assert dict(REGISTRY.via_leaves) == theirs.VIA_LEAF
    assert dict(REGISTRY.parents) == theirs.PARENT_ROUTE
    assert dict(REGISTRY.tab_owners) == theirs.TAB_OWNER
    assert REGISTRY.overlay_routes == set(theirs.OVERLAY_ROUTES)
    for route in REGISTRY.ids:
        assert REGISTRY.route_word(route) == theirs.route_word(route)
        assert REGISTRY.subj_now(route, None) == theirs.subj_now(route, None)
    assert dict(registry.ROUTE_OF) == theirs.ROUTE_OF
    assert dict(registry.KIND) == theirs.KIND
    assert registry.SECTIONS == theirs.SECTIONS
    assert registry.OVERLAYS == theirs.OVERLAYS
    assert registry.OVERLAY_ARROWS == theirs.OVERLAY_ARROWS
    assert registry.DRAWERS == theirs.DRAWERS
    for entity_id in (
        "RUN-1",
        "BAT-1",
        "MLS-1",
        "REL-1",
        "CAM-1",
        "EAWF-1",
        "EVT-1",
        "CLM-1",
        "EVD-1",
        "TRK-1",
        "QST-1",
        "X",
    ):
        assert route_for_id(entity_id) == theirs.route_for_id(entity_id)
        assert route_of(entity_id) == theirs.route_of(entity_id)
        assert kind_of(entity_id) == theirs.kind_of(entity_id)
