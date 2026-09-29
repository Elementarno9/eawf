"""The rc1 checkpoint refuses a coverage grid that does not reconcile with the route registry.

Requirement row proved here, by id:

- ``REL-036``: regenerating the grid at the checkpoint yields no unclassified cell, the
  declared holes match the named set, a registry route absent from the grid fails, and a
  hand-patched grid fails. The grid check itself lives with the console suite that owns
  it; this module asserts only the checkpoint behaviour, and it reads the route registry
  the console declares rather than a copied list.

The checkpoint's verdict is :func:`checkpoint_refusals`: empty when the recorded grid is
its own regeneration and agrees with the registry, one line per refusal otherwise.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.surfaces.tui.console.registry import REGISTRY
from tests.tui.surfaces.tui.console.test_coverage_grid_manifest import (
    DECLARED_HOLES,
    MANIFEST,
    CoverageManifest,
    coverage_defects,
    load_manifest,
    regenerate_grid,
)


def checkpoint_refusals(document: Any) -> tuple[str, ...]:
    """Return why the checkpoint refuses the grid ``document``, or nothing when it passes.

    Raises:
        pydantic.ValidationError: The document is not a grid, or holds a cell that is
            neither bound, served off document, unprojectable with a reason, nor a hole
            naming its wave -- an unclassified cell.
    """
    recorded = load_manifest(document)
    refusals = list(coverage_defects(recorded))
    regenerated = regenerate_grid(recorded)
    if regenerated != recorded:
        refusals.append("the recorded grid is not its regeneration: it was patched by hand")
    holes = {row.route for row in regenerated.routes if row.binding == "hole"}
    if holes != DECLARED_HOLES:
        refusals.append(f"holes {sorted(holes)} do not match the declared {sorted(DECLARED_HOLES)}")
    return tuple(refusals)


def _recorded() -> dict[str, Any]:
    """Return the recorded grid document."""
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_rel_036_the_recorded_grid_passes_the_checkpoint() -> None:
    """The grid as committed reconciles, so the checkpoint admits it."""
    assert checkpoint_refusals(_recorded()) == ()


def test_rel_036_the_regenerated_grid_is_total_over_the_registry() -> None:
    """Every registry route has exactly one row, and no row is unclassified."""
    regenerated = regenerate_grid(load_manifest())
    assert [row.route for row in regenerated.routes] == sorted(REGISTRY.ids)
    assert all(
        row.binding in {"bound", "served_off_document", "unprojectable", "hole"}
        for row in regenerated.routes
    )


def test_rel_036_a_registry_route_absent_from_the_grid_is_refused() -> None:
    """Dropping a row the registry holds refuses the checkpoint by name."""
    document = _recorded()
    document["routes"] = [row for row in document["routes"] if row["route"] != "release"]
    refusals = checkpoint_refusals(document)
    assert "route 'release' is unlisted: the grid is not total" in refusals
    assert "the recorded grid is not its regeneration: it was patched by hand" in refusals


def test_rel_036_a_hand_patched_grid_is_refused() -> None:
    """A binding edited by hand no longer matches what the tables regenerate."""
    document = _recorded()
    row = next(row for row in document["routes"] if row["route"] == "trust")
    row.update({"binding": "hole", "bound_by": "P99-I99-W99"})
    refusals = checkpoint_refusals(document)
    assert "the recorded grid is not its regeneration: it was patched by hand" in refusals
    assert "route 'trust' is served by a projection but listed hole" in refusals


def test_rel_036_a_hole_for_no_registry_route_is_refused() -> None:
    """A hole naming a wave still refuses the checkpoint when the registry holds no such route."""
    document = _recorded()
    document["routes"].append(
        {
            "route": "spike.hole",
            "read_model": "search_page",
            "binding": "hole",
            "bound_by": "P99-I99-W99",
        }
    )
    refusals = checkpoint_refusals(document)
    assert "route 'spike.hole' is listed but the registry does not hold it" in refusals


def test_rel_036_an_unclassified_cell_is_refused_at_load() -> None:
    """A hole naming no wave is no classification at all, so the grid does not load."""
    document = _recorded()
    document["routes"].append(
        {"route": "spike.hole", "read_model": "search_page", "binding": "hole"}
    )
    with pytest.raises(ValidationError, match="undeclared hole"):
        checkpoint_refusals(document)


def test_rel_036_the_declared_hole_set_mismatch_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """A named hole the grid no longer carries is a stale declaration, and is refused."""
    monkeypatch.setattr(f"{__name__}.DECLARED_HOLES", frozenset({"track"}), raising=True)
    assert checkpoint_refusals(_recorded()) == ("holes [] do not match the declared ['track']",)


@pytest.mark.parametrize(
    "document", [[], {}, {"schema_version": "coverage-grid/1.0", "routes": []}]
)
def test_rel_036_a_document_that_is_not_a_grid_is_refused(document: Any) -> None:
    """The wrong-type, missing-key and empty boundaries are each refused at load."""
    with pytest.raises(ValidationError):
        checkpoint_refusals(document)


def test_rel_036_the_grid_validates_against_its_closed_model() -> None:
    """The checkpoint reads the grid through the closed model, never as a raw mapping."""
    assert isinstance(load_manifest(), CoverageManifest)
