"""Tests for the test-kind taxonomy and its four projections.

The load-bearing test is ``test_kind_enum_unit_holds_on_the_real_repo``:
it feeds the live ``pyproject.toml`` markers, the live ``tests/``
directory listing, the live ``[tool.eawf.quality.kind_gates]`` table and
the in-code proof-locus mapping to :func:`taxonomy_drift` as ONE unit, so
a kind added to any single surface without the enum reds here. The
surrounding tests inject drift into each surface in turn to prove the
check has teeth, plus the boundary and error paths of the taxonomy's
public helpers.

``TestKind`` is imported under the alias ``Kind`` because pytest treats a
module-level ``Test*`` name in a test module as a candidate test class.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from eawf.kernel.spec.common import ProofLocus
from eawf.platform.lint.kind_taxonomy import (
    GRANDFATHERED_MARKER_CONFLICTS,
    KIND_MARKERS,
    KIND_PROOF_LOCI,
    NON_KIND_MARKERS,
    NON_KIND_TEST_DIRS,
    QUALITY_LANES,
    declared_markers,
    kind_directory,
    kind_for_test_path,
    load_kind_gates,
    marker_conflict,
    repo_relative_test_path,
    suite_directories,
    taxonomy_drift,
)
from eawf.platform.lint.kind_taxonomy import TestKind as Kind

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PYPROJECT = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

_LIVE_MARKERS = declared_markers(_PYPROJECT)
_LIVE_DIRECTORIES = suite_directories(_REPO_ROOT / "tests")
_LIVE_GATES = load_kind_gates(_PYPROJECT)


def _drift(
    *,
    markers: frozenset[str] | None = None,
    directories: frozenset[str] | None = None,
    kind_gates: Mapping[str, str] | None = None,
    proof_loci: Mapping[Kind, ProofLocus] | None = None,
    non_kind_dirs: frozenset[str] = NON_KIND_TEST_DIRS,
) -> list[str]:
    """Run the one-unit check over the live surfaces with one substituted."""
    return taxonomy_drift(
        markers=_LIVE_MARKERS if markers is None else markers,
        directories=_LIVE_DIRECTORIES if directories is None else directories,
        kind_gates=_LIVE_GATES if kind_gates is None else kind_gates,
        proof_loci=KIND_PROOF_LOCI if proof_loci is None else proof_loci,
        non_kind_dirs=non_kind_dirs,
    )


# --- the one-unit taxonomy check -------------------------------------------


def test_kind_enum_unit_holds_on_the_real_repo() -> None:
    assert _drift() == []


def test_kind_enum_unit_flags_an_injected_extra_marker() -> None:
    drift = _drift(markers=_LIVE_MARKERS | {"smoke"})
    assert any("'smoke'" in row and "neither a TestKind" in row for row in drift)


def test_kind_enum_unit_flags_a_kind_with_no_registered_marker() -> None:
    drift = _drift(markers=_LIVE_MARKERS - {"golden"})
    assert any("'golden'" in row and "--strict-markers" in row for row in drift)


def test_kind_enum_unit_flags_an_injected_extra_directory() -> None:
    assert any("tests/fuzz/" in row for row in _drift(directories=_LIVE_DIRECTORIES | {"fuzz"}))


def test_kind_enum_unit_flags_an_extra_gate_row() -> None:
    drift = _drift(kind_gates={**_LIVE_GATES, "fuzz": "default"})
    assert any("quality gate config declares kind 'fuzz'" in row for row in drift)


def test_kind_enum_unit_flags_a_missing_gate_row() -> None:
    gates = {name: lane for name, lane in _LIVE_GATES.items() if name != "perf"}
    assert "kind 'perf' has no quality gate lane" in _drift(kind_gates=gates)


def test_kind_enum_unit_flags_an_unknown_quality_lane() -> None:
    drift = _drift(kind_gates={**_LIVE_GATES, "unit": "nightly"})
    assert any("unknown quality lane 'nightly'" in row for row in drift)


def test_kind_enum_unit_flags_a_missing_proof_locus() -> None:
    loci = {kind: locus for kind, locus in KIND_PROOF_LOCI.items() if kind is not Kind.TUI}
    assert "kind 'tui' has no proof locus" in _drift(proof_loci=loci)


def test_kind_enum_unit_flags_a_non_prooflocus_value() -> None:
    loci: dict[Kind, ProofLocus] = {**KIND_PROOF_LOCI}
    loci[Kind.UNIT] = "pytest"  # type: ignore[assignment]
    assert any("not a ProofLocus" in row for row in _drift(proof_loci=loci))


def test_kind_enum_unit_flags_a_registry_claiming_a_kind_directory() -> None:
    drift = _drift(non_kind_dirs=NON_KIND_TEST_DIRS | {"unit"})
    assert any("registered as a non-kind directory" in row for row in drift)


def test_kind_enum_unit_on_empty_surfaces_reports_every_kind() -> None:
    # Boundary: nothing declared anywhere. Markers, gates and proof loci
    # each report the whole enum as missing; the directory surface has
    # nothing to complain about because an absent directory is legal.
    drift = taxonomy_drift(
        markers=frozenset(),
        directories=frozenset(),
        kind_gates={},
        proof_loci={},
    )
    assert len(drift) == 3 * len(Kind)


# --- projection helpers: boundary + error paths ----------------------------


def test_declared_markers_strips_the_help_text() -> None:
    assert KIND_MARKERS <= _LIVE_MARKERS
    assert NON_KIND_MARKERS <= _LIVE_MARKERS


def test_declared_markers_raises_when_the_table_is_absent() -> None:
    with pytest.raises(KeyError):
        declared_markers("[tool.pytest.ini_options]\ntestpaths = ['tests']\n")


def test_load_kind_gates_covers_every_kind_with_a_known_lane() -> None:
    assert set(_LIVE_GATES) == set(KIND_MARKERS)
    assert set(_LIVE_GATES.values()) <= QUALITY_LANES


def test_load_kind_gates_raises_when_the_table_is_absent() -> None:
    with pytest.raises(KeyError):
        load_kind_gates("[tool.eawf.lint]\nenabled = []\n")


def test_load_kind_gates_raises_on_a_non_string_lane() -> None:
    with pytest.raises(TypeError, match="must be a string"):
        load_kind_gates("[tool.eawf.quality.kind_gates]\nunit = 3\n")


def test_suite_directories_skips_dunder_and_dot_entries(tmp_path: Path) -> None:
    (tmp_path / "unit").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "loose.py").write_text("", encoding="utf-8")
    assert suite_directories(tmp_path) == {"unit"}


def test_suite_directories_on_a_missing_root_is_empty(tmp_path: Path) -> None:
    assert suite_directories(tmp_path / "absent") == frozenset()


def test_kind_directory_is_the_enum_value() -> None:
    assert kind_directory(Kind.METAMORPHIC) == "tests/metamorphic/"


# --- path classification: boundary + error paths ---------------------------


def test_kind_for_test_path_resolves_a_repo_relative_path() -> None:
    assert kind_for_test_path("tests/unit/kernel/test_x.py") is Kind.UNIT


def test_kind_for_test_path_resolves_an_absolute_path() -> None:
    assert kind_for_test_path("/tmp/repo/tests/golden/test_x.py") is Kind.GOLDEN


def test_kind_for_test_path_folds_backslashes() -> None:
    assert kind_for_test_path("tests\\property\\test_x.py") is Kind.PROPERTY


def test_kind_for_test_path_ignores_a_nested_kind_name() -> None:
    # ``tui`` nested under ``perf`` is a perf test, not a TUI test: only
    # the first segment under tests/ carries the kind.
    assert kind_for_test_path("tests/perf/tui/test_x.py") is Kind.PERF


def test_kind_for_test_path_returns_none_outside_the_taxonomy() -> None:
    assert kind_for_test_path("tests/lint/test_x.py") is None
    assert kind_for_test_path("tests/conftest.py") is None
    assert kind_for_test_path("src/eawf/kernel/state/models.py") is None


def test_kind_for_test_path_returns_none_for_a_non_python_file() -> None:
    assert kind_for_test_path("tests/unit/data.json") is None


def test_kind_for_test_path_on_empty_string_is_none() -> None:
    # Boundary: the empty path.
    assert kind_for_test_path("") is None


def test_repo_relative_test_path_ignores_a_lookalike_segment() -> None:
    assert repo_relative_test_path("/repo/mytests/unit/x.py") == "/repo/mytests/unit/x.py"


def test_repo_relative_test_path_passes_through_a_pathless_string() -> None:
    assert repo_relative_test_path("x.py") == "x.py"


# --- marker conflicts: boundary + error paths ------------------------------


def test_marker_conflict_flags_a_contradicting_marker() -> None:
    conflict = marker_conflict(
        path="tests/unit/kernel/test_x.py",
        marker_names=frozenset({"integration"}),
    )
    assert conflict is not None
    assert conflict.directory_kind is Kind.UNIT
    assert conflict.declared == ("integration",)
    assert "auto-marked 'unit'" in conflict.render()


def test_marker_conflict_sorts_multiple_declared_kinds() -> None:
    conflict = marker_conflict(
        path="tests/unit/test_x.py",
        marker_names=frozenset({"tui", "integration", "slow"}),
    )
    assert conflict is not None
    assert conflict.declared == ("integration", "tui")


def test_marker_conflict_allows_the_matching_marker() -> None:
    assert marker_conflict(path="tests/unit/test_x.py", marker_names=frozenset({"unit"})) is None


def test_marker_conflict_allows_a_non_kind_marker() -> None:
    assert marker_conflict(path="tests/unit/test_x.py", marker_names=frozenset({"slow"})) is None


def test_marker_conflict_on_no_markers_is_none() -> None:
    # Boundary: an item declaring nothing at all.
    assert marker_conflict(path="tests/unit/test_x.py", marker_names=frozenset()) is None


def test_marker_conflict_outside_the_taxonomy_is_none() -> None:
    assert marker_conflict(path="tests/lint/test_x.py", marker_names=frozenset({"tui"})) is None


def test_marker_conflict_honours_the_grandfather_baseline() -> None:
    grandfathered = sorted(GRANDFATHERED_MARKER_CONFLICTS)[0]
    assert marker_conflict(path=grandfathered, marker_names=frozenset({"unit", "property"})) is None


def test_marker_conflict_grandfather_matches_on_the_repo_relative_path() -> None:
    grandfathered = sorted(GRANDFATHERED_MARKER_CONFLICTS)[0]
    assert (
        marker_conflict(path=f"/tmp/checkout/{grandfathered}", marker_names=frozenset({"unit"}))
        is None
    )


def test_grandfathered_conflicts_all_live_under_a_kind_directory() -> None:
    assert all(kind_for_test_path(path) is not None for path in GRANDFATHERED_MARKER_CONFLICTS)


def test_every_kind_proof_locus_is_a_prooflocus_member() -> None:
    assert all(isinstance(locus, ProofLocus) for locus in KIND_PROOF_LOCI.values())
