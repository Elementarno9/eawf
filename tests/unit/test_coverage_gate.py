"""Unit tests for the per-package + TUI behavioural coverage gate.

Covers the standalone gate in ``tools/coverage_gate.py``:

- ``aggregate`` computes line/branch percent from Cobertura ``<class>`` nodes,
  including the empty-classes vacuous-100 boundary;
- ``evaluate_package_gates`` PASSES when measured >= gate, FAILS + cites the
  package when measured < gate (the not-idle negative control), honours
  ``waive_line`` / ``waive_branch``, and FAILS when a gate matches no source
  files (a typo'd path);
- ``evaluate_tui_behavioural`` counts golden snapshots + ``"flow":`` rows and
  fires when either count falls below its floor;
- the seven previously-ungated packages named by P30-I10-W06 are all present in
  ``[tool.eawf.coverage.gates]`` and the TUI carries a behavioural floor instead
  of a line/branch ratchet;
- the real ``pyproject.toml`` floors are GREEN against the real ``coverage.xml``
  (when that report is present) -- the ratchet-is-not-aspirational contract.

``tools/`` is excluded from the package, so the gate is loaded via
:mod:`importlib`. The aggregation + evaluation helpers take injected config +
fixture trees so the negative controls never touch the real coverage report.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "tools" / "coverage_gate.py"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_COVERAGE_XML = _REPO_ROOT / "coverage.xml"

#: The seven packages P30-I10-W06 brings under a per-package ratchet.
_W06_PACKAGES = (
    "verify",
    "dispatch",
    "evidence",
    "lifecycle",
    "eval",
    "runtimes",
    "sandbox",
)


def _load_gate() -> ModuleType:
    """Load ``tools/coverage_gate.py`` by path (``tools/`` is not a package)."""
    tool_dir = _GATE_PATH.parent
    if str(tool_dir) not in sys.path:
        sys.path.insert(0, str(tool_dir))
    spec = importlib.util.spec_from_file_location("coverage_gate", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coverage_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


_GATE = _load_gate()


def _class(filename: str, lines: list[tuple[int, str | None]]) -> ET.Element:
    """Build a Cobertura ``<class>`` node.

    Args:
        filename: The ``filename`` attribute the gate matches packages on.
        lines: ``(hits, condition-coverage)`` per line; a non-``None`` condition
            marks the line as a branch.
    """
    cls = ET.Element("class", {"filename": filename})
    container = ET.SubElement(cls, "lines")
    for hits, condition in lines:
        attrs = {"hits": str(hits)}
        if condition is not None:
            attrs["branch"] = "true"
            attrs["condition-coverage"] = condition
        ET.SubElement(container, "line", attrs)
    return cls


def test_aggregate_line_and_branch_percent() -> None:
    # 3 of 4 lines hit (75% line); 3 of 4 branches covered (75% branch).
    classes = [
        _class(
            "src/eawf/workflow/verify/x.py",
            [(1, None), (1, "100% (2/2)"), (0, "50% (1/2)"), (1, None)],
        )
    ]
    rate = _GATE.aggregate(classes)
    assert rate.line_pct == pytest.approx(75.0)
    assert rate.branch_pct == pytest.approx(75.0)
    assert rate.line_total == 4
    assert rate.branch_total == 4


def test_aggregate_empty_classes_is_vacuously_full() -> None:
    # Boundary: no classes -> 100% on both dimensions (matches the CI checker).
    rate = _GATE.aggregate([])
    assert rate.line_pct == pytest.approx(100.0)
    assert rate.branch_pct == pytest.approx(100.0)
    assert rate.line_total == 0
    assert rate.branch_total == 0


def test_package_gate_passes_when_measured_meets_gate() -> None:
    classes = [_class("src/pkg/a.py", [(1, None), (1, None)])]  # 100% line
    gates = {"pkg": {"path": "src/pkg/", "line": 90, "branch": 0}}
    report, failures = _GATE.evaluate_package_gates(gates, classes)
    assert failures == []
    assert any("ok" in row for row in report)


def test_package_gate_fires_and_cites_when_below_threshold() -> None:
    # Negative control: 50% line < 90 gate must FAIL and name the package.
    classes = [_class("src/pkg/a.py", [(1, None), (0, None)])]
    gates = {"pkg": {"path": "src/pkg/", "line": 90, "branch": 0}}
    _report, failures = _GATE.evaluate_package_gates(gates, classes)
    assert any(entry.startswith("pkg: line") and "< gate 90" in entry for entry in failures)


def test_package_gate_branch_dimension_fires() -> None:
    # 50% branch < 80 gate fires on the branch dimension independently.
    classes = [_class("src/pkg/a.py", [(1, "50% (1/2)")])]
    gates = {"pkg": {"path": "src/pkg/", "line": 0, "branch": 80}}
    _report, failures = _GATE.evaluate_package_gates(gates, classes)
    assert any(entry.startswith("pkg: branch") and "< gate 80" in entry for entry in failures)


def test_package_gate_honours_waivers() -> None:
    # A package below both thresholds passes when both dimensions are waived.
    classes = [_class("src/eawf/surfaces/tui/x.py", [(0, "0% (0/2)")])]
    gates = {"tui": {"path": "src/eawf/surfaces/tui/", "waive_line": True, "waive_branch": True}}
    _report, failures = _GATE.evaluate_package_gates(gates, classes)
    assert failures == []


def test_package_gate_fires_on_no_matched_files() -> None:
    # A typo'd path that matches nothing must FAIL, not silently pass.
    classes = [_class("src/other/a.py", [(1, None)])]
    gates = {"pkg": {"path": "src/typo/", "line": 50, "branch": 0}}
    _report, failures = _GATE.evaluate_package_gates(gates, classes)
    assert any("no source files matched" in entry for entry in failures)


def test_package_gate_glob_matching() -> None:
    classes = [
        _class("src/eawf/runtime/runtimes/claude/plugin_install.py", [(1, None)]),
        _class("src/eawf/runtime/runtimes/claude/other.py", [(0, None)]),
    ]
    gates = {
        "pi": {
            "glob": "src/eawf/runtime/runtimes/*/plugin_install.py",
            "line": 90,
            "branch": 0,
        }
    }
    _report, failures = _GATE.evaluate_package_gates(gates, classes)
    # Only the plugin_install file (100%) is picked; the other.py 0% is ignored.
    assert failures == []


def test_classes_for_gate_matches_path_and_glob() -> None:
    classes = [
        _class("src/eawf/runtime/runtimes/claude/plugin_install.py", [(1, None)]),
        _class("src/eawf/runtime/runtimes/claude/other.py", [(1, None)]),
    ]
    by_path = _GATE._classes_for_gate(classes, {"path": "src/eawf/runtime/"})
    by_glob = _GATE._classes_for_gate(
        classes,
        {"glob": "src/eawf/runtime/runtimes/*/plugin_install.py"},
    )
    assert by_path == classes
    assert by_glob == [classes[0]]


def test_run_gate_returns_failures_from_package_and_tui_floors(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[tool.eawf.coverage.gates.pkg]
path = "src/pkg/"
line = 90
branch = 0

[tool.eawf.coverage.tui_behavioural]
golden_glob = "tests/snapshots/tui/golden/*.txt"
min_goldens = 1
flow_glob = "tests/snapshots/tui/test_tui_flow.py"
min_flows = 1
""".lstrip(),
        encoding="utf-8",
    )
    coverage_xml = tmp_path / "coverage.xml"
    root = ET.Element("coverage")
    packages = ET.SubElement(root, "packages")
    package = ET.SubElement(packages, "package")
    classes = ET.SubElement(package, "classes")
    classes.append(_class("src/pkg/a.py", [(1, None), (0, None)]))
    ET.ElementTree(root).write(coverage_xml, encoding="utf-8", xml_declaration=True)

    outcome = _GATE.run_gate(coverage_xml, pyproject, tmp_path)
    assert not outcome.passed
    assert any(entry.startswith("pkg: line") for entry in outcome.failures)
    assert any(entry.startswith("tui.goldens") for entry in outcome.failures)
    assert any(entry.startswith("tui.flows") for entry in outcome.failures)


def test_tui_behavioural_passes_when_counts_meet_floor(tmp_path: Path) -> None:
    golden_dir = tmp_path / "tests" / "snapshots" / "tui" / "golden"
    golden_dir.mkdir(parents=True)
    for i in range(3):
        (golden_dir / f"screen_{i}.txt").write_text("snap", encoding="utf-8")
    flow_file = tmp_path / "tests" / "snapshots" / "tui" / "test_tui_flow.py"
    flow_file.parent.mkdir(parents=True, exist_ok=True)
    flow_file.write_text('    "flow": "G1",\n    "flow": "G2",\n', encoding="utf-8")
    spec = {
        "golden_glob": "tests/snapshots/tui/golden/*.txt",
        "min_goldens": 3,
        "flow_glob": "tests/snapshots/tui/test_tui_flow.py",
        "min_flows": 2,
    }
    _report, failures = _GATE.evaluate_tui_behavioural(spec, tmp_path)
    assert failures == []


def test_tui_behavioural_fires_on_deleted_golden(tmp_path: Path) -> None:
    # Negative control: only 1 golden but floor is 3 -> FAIL + cite the count.
    golden_dir = tmp_path / "tests" / "snapshots" / "tui" / "golden"
    golden_dir.mkdir(parents=True)
    (golden_dir / "only.txt").write_text("snap", encoding="utf-8")
    flow_file = tmp_path / "tests" / "snapshots" / "tui" / "test_tui_flow.py"
    flow_file.parent.mkdir(parents=True, exist_ok=True)
    flow_file.write_text('    "flow": "G1",\n', encoding="utf-8")
    spec = {
        "golden_glob": "tests/snapshots/tui/golden/*.txt",
        "min_goldens": 3,
        "flow_glob": "tests/snapshots/tui/test_tui_flow.py",
        "min_flows": 1,
    }
    _report, failures = _GATE.evaluate_tui_behavioural(spec, tmp_path)
    assert any(entry.startswith("tui.goldens") and "< floor 3" in entry for entry in failures)


def test_tui_behavioural_fires_on_deleted_flow(tmp_path: Path) -> None:
    golden_dir = tmp_path / "tests" / "snapshots" / "tui" / "golden"
    golden_dir.mkdir(parents=True)
    (golden_dir / "s.txt").write_text("snap", encoding="utf-8")
    flow_file = tmp_path / "tests" / "snapshots" / "tui" / "test_tui_flow.py"
    flow_file.parent.mkdir(parents=True, exist_ok=True)
    # Only one flow row but floor is 7.
    flow_file.write_text('    "flow": "G1-only",\n', encoding="utf-8")
    spec = {
        "golden_glob": "tests/snapshots/tui/golden/*.txt",
        "min_goldens": 1,
        "flow_glob": "tests/snapshots/tui/test_tui_flow.py",
        "min_flows": 7,
    }
    _report, failures = _GATE.evaluate_tui_behavioural(spec, tmp_path)
    assert any(entry.startswith("tui.flows") and "< floor 7" in entry for entry in failures)


def test_load_gates_missing_section_raises(tmp_path: Path) -> None:
    bad = tmp_path / "pyproject.toml"
    bad.write_text("[tool.other]\nx = 1\n", encoding="utf-8")
    with pytest.raises(KeyError):
        _GATE.load_gates(bad)


def _real_gates() -> dict[str, dict[str, object]]:
    with _PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["tool"]["eawf"]["coverage"]["gates"]


def test_all_w06_packages_carry_a_gate() -> None:
    # Each previously-ungated package named by the wave now carries a ratchet.
    gates = _real_gates()
    for pkg in _W06_PACKAGES:
        assert pkg in gates, f"missing coverage gate for {pkg!r}"
        spec = gates[pkg]
        assert "line" in spec and "branch" in spec, f"{pkg!r} missing line/branch"
        assert "waive_line" not in spec, f"{pkg!r} must not waive line-cov"


def test_tui_line_cov_stays_waived_but_carries_behavioural_floor() -> None:
    gates = _real_gates()
    assert gates["tui"].get("waive_line") is True
    assert gates["tui"].get("waive_branch") is True
    with _PYPROJECT.open("rb") as handle:
        config = tomllib.load(handle)
    behavioural = config["tool"]["eawf"]["coverage"]["tui_behavioural"]
    assert int(behavioural["min_goldens"]) > 0
    assert int(behavioural["min_flows"]) > 0


@pytest.mark.skipif(not _COVERAGE_XML.exists(), reason="no coverage.xml on this tree")
def test_real_floors_are_green_against_real_coverage() -> None:
    # The ratchet-is-not-aspirational contract: the floors committed in
    # pyproject pass against the measured coverage report present on the tree.
    gates = _real_gates()
    classes = ET.parse(_COVERAGE_XML).getroot().findall(".//class")
    # Restrict to the W06 packages this wave authored (other gates may predate
    # the coverage.xml on the tree and are out of this wave's scope).
    w06_gates = {k: v for k, v in gates.items() if k in _W06_PACKAGES}
    _report, failures = _GATE.evaluate_package_gates(w06_gates, classes)
    assert failures == [], f"W06 floors red against real coverage: {failures}"


@pytest.mark.skipif(not _COVERAGE_XML.exists(), reason="no coverage.xml on this tree")
def test_real_tui_behavioural_floor_is_green() -> None:
    with _PYPROJECT.open("rb") as handle:
        behavioural = tomllib.load(handle)["tool"]["eawf"]["coverage"]["tui_behavioural"]
    _report, failures = _GATE.evaluate_tui_behavioural(behavioural, _REPO_ROOT)
    assert failures == [], f"TUI behavioural floor red against real tree: {failures}"


def test_main_passes_with_real_tree() -> None:
    if not _COVERAGE_XML.exists():
        pytest.skip("no coverage.xml on this tree")
    rc = _GATE.main(["--coverage-xml", str(_COVERAGE_XML), "--repo-root", str(_REPO_ROOT)])
    assert rc == 0
