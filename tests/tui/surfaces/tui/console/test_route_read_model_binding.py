"""Every console route resolves to exactly one declared read model, and back.

The binding is held on the registry rows; the kernel declarations name the routes each
read model serves. Both directions must be equal, a registry whose rows disagree refuses
to build, and a row with no read model is listed as a specification hole.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from eawf.kernel.projection.read_models import (
    READ_MODEL_BY_KIND,
    READ_MODELS,
    ReadModelKind,
    ReadModelSpec,
    index_read_models,
    route_binding_mismatches,
)
from eawf.surfaces.tui.console.registry import (
    REGISTRY,
    ROUTES,
    RouteFamily,
    RouteRegistry,
    RouteSpec,
)

HOLE = RouteSpec(
    id="spike.hole",
    family=RouteFamily.DIAGNOSTICS,
    palette_visible=True,
    step_leaf="Hole",
)


def _binding(registry: RouteRegistry) -> dict[str, ReadModelKind]:
    """Return the read model each route key of ``registry`` binds."""
    return {registry.by_id[route].key: kind for route, kind in registry.read_models.items()}


def _served(read_models: tuple[ReadModelSpec, ...]) -> dict[str, ReadModelKind]:
    """Return the read model that names each route key in ``read_models``."""
    return {route: spec.kind for spec in read_models for route in spec.routes}


def _replace_row(route: str, **changes: object) -> tuple[RouteSpec, ...]:
    """Return ``ROUTES`` with the ``route`` row's columns replaced by ``changes``."""
    return tuple(dataclasses.replace(s, **changes) if s.id == route else s for s in ROUTES)


def _move_route(route: str, to: ReadModelKind) -> tuple[ReadModelSpec, ...]:
    """Return the declarations with ``route`` moved from its model to ``to``."""
    moved: list[ReadModelSpec] = []
    for spec in READ_MODELS:
        routes = tuple(r for r in spec.routes if r != route)
        if spec.kind is to:
            routes += (route,)
        moved.append(dataclasses.replace(spec, routes=routes))
    return tuple(moved)


def test_route_registry_read_models_every_route_resolves_to_one_model() -> None:
    assert REGISTRY.read_model_holes == ()
    assert tuple(REGISTRY.read_models) == REGISTRY.ids
    for route, kind in REGISTRY.read_models.items():
        assert REGISTRY.by_id[route].key in READ_MODEL_BY_KIND[kind].routes


def test_route_registry_read_models_equal_declarations_both_ways() -> None:
    assert _binding(REGISTRY) == _served(READ_MODELS)
    assert route_binding_mismatches(_binding(REGISTRY), READ_MODEL_BY_KIND) == ()


@pytest.mark.parametrize("kind", list(ReadModelKind))
def test_route_registry_read_models_every_model_serves_or_is_carried(kind: ReadModelKind) -> None:
    spec = READ_MODEL_BY_KIND[kind]
    served = {route for route, bound in REGISTRY.read_models.items() if bound is kind}
    assert {REGISTRY.by_id[route].key for route in served} == set(spec.routes)
    if not spec.routes:
        assert spec.parent is not None
        assert READ_MODEL_BY_KIND[spec.parent].routes


def test_route_registry_read_models_shared_and_keyed_routes() -> None:
    entity_routes = {"track", "batch.detail", "task.detail"}
    shared = {r for r, k in REGISTRY.read_models.items() if k is ReadModelKind.ENTITY_DETAIL_VIEW}
    assert shared == entity_routes
    assert REGISTRY.read_models["milestone"] is ReadModelKind.ACCEPTANCE_BUNDLE_VIEW
    assert REGISTRY.read_models["timeline"] is ReadModelKind.ROADMAP_VIEW
    assert READ_MODEL_BY_KIND[ReadModelKind.ROADMAP_VIEW].routes == ("roadmap",)
    assert READ_MODEL_BY_KIND[ReadModelKind.ACTIVITY_PAGE].parent is ReadModelKind.FLEET_QUERY_PAGE


def test_route_registry_read_models_entry_layer_has_no_projection() -> None:
    kind = REGISTRY.read_models["entry"]
    assert kind is ReadModelKind.PROCESS_FRAME
    assert not READ_MODEL_BY_KIND[kind].projection_backed
    unbacked = [k for k in ReadModelKind if not READ_MODEL_BY_KIND[k].projection_backed]
    assert unbacked == [ReadModelKind.PROCESS_FRAME]


def test_route_registry_read_models_sub_surface_uses_parent_sub_model() -> None:
    checked = 0
    for spec in ROUTES:
        if spec.family is not RouteFamily.ENTITY_SUB_SURFACES or spec.parent is None:
            continue
        parent_model = REGISTRY.read_models[spec.parent[0]]
        assert READ_MODEL_BY_KIND[REGISTRY.read_models[spec.id]].parent is parent_model
        checked += 1
    assert checked == 3


def test_route_registry_read_model_holes_lists_unbound_row() -> None:
    extended = RouteRegistry((*ROUTES, HOLE))
    assert extended.read_model_holes == ("spike.hole",)
    assert "spike.hole" not in extended.read_models
    assert dict(extended.read_models) == dict(REGISTRY.read_models)


def test_route_registry_unbound_named_row_raises_value_error() -> None:
    message = "route 'git.pr' binds no read model but git_pr_view names it"
    with pytest.raises(ValueError, match=re.escape(message)):
        RouteRegistry(_replace_row("git.pr", read_model=None))


def test_route_registry_row_bound_to_other_model_raises_value_error() -> None:
    message = "route 'git.pr' binds run_detail_view but git_pr_view names it"
    with pytest.raises(ValueError, match=re.escape(message)):
        RouteRegistry(_replace_row("git.pr", read_model=ReadModelKind.RUN_DETAIL_VIEW))


def test_route_registry_row_bound_to_silent_model_raises_value_error() -> None:
    row = dataclasses.replace(HOLE, read_model=ReadModelKind.HEALTH_VIEW)
    message = "route 'spike.hole' binds health_view but no read model names it"
    with pytest.raises(ValueError, match=re.escape(message)):
        RouteRegistry((*ROUTES, row))


def test_route_binding_mismatches_moved_route_reported_once() -> None:
    moved = index_read_models(_move_route("track", ReadModelKind.SEARCH_PAGE))
    assert route_binding_mismatches(_binding(REGISTRY), moved) == (
        "route 'track' binds entity_detail_view but search_page names it",
    )


def test_route_binding_mismatches_empty_tables_agree() -> None:
    assert route_binding_mismatches({}, {}) == ()


def test_route_binding_mismatches_sorted_by_route() -> None:
    binding = {"b": ReadModelKind.HEALTH_VIEW, "a": ReadModelKind.SEARCH_PAGE}
    assert route_binding_mismatches(binding, {}) == (
        "route 'a' binds search_page but no read model names it",
        "route 'b' binds health_view but no read model names it",
    )


def test_index_read_models_shipped_table_is_total() -> None:
    assert set(READ_MODEL_BY_KIND) == set(ReadModelKind)
    assert len(READ_MODELS) == len(ReadModelKind)


@pytest.mark.parametrize(
    ("declarations", "message"),
    [
        ((), "process_frame not declared"),
        (READ_MODELS[1:], "process_frame not declared"),
        ((*READ_MODELS, READ_MODELS[0]), "process_frame declared twice"),
    ],
)
def test_index_read_models_incomplete_table_raises_value_error(
    declarations: tuple[ReadModelSpec, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        index_read_models(declarations)


@pytest.mark.parametrize(
    ("kind", "changes", "message"),
    [
        (
            ReadModelKind.HEALTH_VIEW,
            {"routes": ("health", "search")},
            "route 'search' is served by health_view, search_page",
        ),
        (
            ReadModelKind.HEALTH_VIEW,
            {"routes": ()},
            "health_view serves no route and has no parent to carry it",
        ),
        (
            ReadModelKind.HEALTH_VIEW,
            {"parent": ReadModelKind.HEALTH_VIEW},
            "health_view has parent health_view, which is itself a sub-model",
        ),
        (
            ReadModelKind.HEALTH_VIEW,
            {"parent": ReadModelKind.MERGE_CONFLICT_VIEW},
            "health_view has parent merge_conflict_view, which is itself a sub-model",
        ),
    ],
)
def test_index_read_models_defective_row_raises_value_error(
    kind: ReadModelKind, changes: dict[str, object], message: str
) -> None:
    table = tuple(dataclasses.replace(s, **changes) if s.kind is kind else s for s in READ_MODELS)
    with pytest.raises(ValueError, match=re.escape(message)):
        index_read_models(table)
