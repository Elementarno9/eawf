"""The rc1 checkpoint refuses a coverage grid that does not reconcile with the route registry.

Requirement row proved here, by id:

- ``REL-036``: regenerating the grid at the checkpoint yields no unclassified cell, the
  declared holes match the named set, a registry route absent from the grid fails, and a
  hand-patched grid fails. The grid check itself lives with the console suite that owns
  it; this module asserts only the checkpoint behaviour, and it reads the route registry
  the console declares rather than a copied list.

The checkpoint's verdict is
:func:`~eawf.surfaces.tui.console.coverage_grid.checkpoint_refusals`: empty when the
recorded grid is its own regeneration and agrees with the registry, one line per refusal
otherwise. The tag preflight reads the same verdict off the working copy, so a drifted grid
reds the realization row of the release it would ship in.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.spec.release_config import load_release_config
from eawf.surfaces.tui.console import coverage_grid
from eawf.surfaces.tui.console.coverage_grid import (
    COVERAGE_MANIFEST_PATH,
    CoverageManifest,
    checkpoint_refusals,
    regenerate_grid,
)
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import (
    ReleaseSignalName,
    ReleaseSignalStatus,
    compute_readiness,
)
from tests.tui.surfaces.tui.console.test_coverage_grid_manifest import MANIFEST, load_manifest


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
    monkeypatch.setattr(coverage_grid, "DECLARED_HOLES", frozenset({"track"}), raising=True)
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


# ---------- the tag preflight reads the same verdict off the working copy ----------


_NOW = datetime(2027, 2, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a working copy with no host history, no re-typed rules and a lint config."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text('[tool.eawf.lint]\nenabled = ["EAWF010"]\n')
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    return root


def _record(repo: Path, document: Any) -> None:
    """Write *document* where the working copy records its grid."""
    path = repo / COVERAGE_MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _realization_row(repo: Path) -> Any:
    """Return the realization row the tag preflight computes for *repo*."""
    inputs = TagPreflightInputs(
        repo_root=repo,
        version="0.7.0rc1",
        tag="v0.7.0rc1",
        package_version="0.7.0rc1",
        remote="origin",
        today=date(2027, 2, 1),
    )
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV1_RELEASE_CONFIG_YAML))["release"]
    readiness = compute_readiness(
        load_release_config({"release": body}, train=V07_TRAIN),
        probes={
            ReleaseSignalName.PERFECT_REALIZATION: build_tag_probes(inputs)[
                ReleaseSignalName.PERFECT_REALIZATION
            ]
        },
        observed_revision="deadbee",
        computed_at=_NOW,
    )
    return readiness.row(ReleaseSignalName.PERFECT_REALIZATION)


def test_rel_036_the_tag_preflight_admits_the_recorded_grid(repo: Path) -> None:
    """The committed grid reconciles, so its realization assertion holds."""
    _record(repo, _recorded())

    row = _realization_row(repo)

    assert row.status is ReleaseSignalStatus.PASS
    assert f"coverage_grid:{COVERAGE_MANIFEST_PATH}" in row.evidence_refs


def test_rel_036_the_tag_preflight_reds_on_a_drifted_grid(repo: Path) -> None:
    """Gate fire: a registry route missing from the recorded grid refuses the release."""
    document = _recorded()
    document["routes"] = [row for row in document["routes"] if row["route"] != "release"]
    _record(repo, document)

    row = _realization_row(repo)

    assert row.status is ReleaseSignalStatus.FAIL
    assert "route 'release' is unlisted: the grid is not total" in row.remediation
    assert row.evidence_refs == (f"coverage_grid:{COVERAGE_MANIFEST_PATH}",)


def test_rel_036_the_tag_preflight_blocks_on_an_unclassified_cell(repo: Path) -> None:
    """A grid that does not load blocks the row rather than passing it."""
    document = _recorded()
    document["routes"].append(
        {"route": "spike.hole", "read_model": "search_page", "binding": "hole"}
    )
    _record(repo, document)

    assert _realization_row(repo).status is ReleaseSignalStatus.BLOCKED


def test_rel_036_a_working_copy_without_a_grid_has_none_to_check(repo: Path) -> None:
    """Another project's tree records no console grid, so it has none to reconcile."""
    row = _realization_row(repo)

    assert row.status is ReleaseSignalStatus.PASS
    assert "coverage_grid:no-grid" in row.evidence_refs
