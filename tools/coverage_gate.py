"""Per-package coverage gate (C09 5.2 nine-layer ratchet + TUI behavioural floor).

``pytest-cov`` has no native per-package threshold, so coverage enforcement is
split: the overall floor is ``[tool.coverage.report] fail_under``, and this gate
parses the combined Cobertura ``coverage.xml`` and asserts each package named in
``[tool.eawf.coverage.gates]`` clears its line / branch ratchet. The thresholds
are a ratchet, not an aspirational target: each sits just below the rate measured
on the current tree, so the gate catches a regression without redding CI today.

The TUI is the deliberate omission from the line/branch ratchet -- Textual
widgets render asynchronously and line-cov misreports them, so the ``tui`` gate
waives both dimensions. Its quality number is the behavioural floor in
``[tool.eawf.coverage.tui_behavioural]``: a counts-based ratchet over the
screen-level golden snapshots + the operator-journey ``tui_flow`` specs, which
fires the moment a screen snapshot or a flow is deleted without replacement.

The gate logic lives here as importable functions (``aggregate``,
``evaluate_package_gates``, ``evaluate_tui_behavioural``) so the negative-control
tests can drive it with injected config + fixture trees without touching the real
coverage.xml. ``main`` is the CLI shim CI invokes.

Invocation:

    python3 tools/coverage_gate.py [--coverage-xml PATH] [--repo-root PATH]

Exit codes:
- ``0`` -- every gated package clears its line/branch ratchet AND the TUI
  behavioural counts clear their floors.
- ``1`` -- at least one package fell below a ratchet, a gate matched no source
  files, or a behavioural count fell below its floor (the failures are named on
  stderr).
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from xml.etree.ElementTree import Element

#: ``condition-coverage="50% (1/2)"`` -> the ``(covered/total)`` branch tally.
_CONDITION = re.compile(r"\((\d+)/(\d+)\)")

#: A canonical ``"flow": "G<N>-..."`` row inside ``FLOW_SPECS`` -- the per-flow
#: countable signal the behavioural gate ratchets on. Scoped to the ``G<digit>``
#: operator-journey names so incidental ``"flow":`` keys in per-test GateSpec
#: args are not double-counted.
_FLOW_ROW = re.compile(r'^\s*"flow":\s*"G\d')


@dataclass(frozen=True)
class PackageRate:
    """Aggregated line + branch coverage for one gated package."""

    line_pct: float
    branch_pct: float
    line_total: int
    branch_total: int


@dataclass(frozen=True)
class GateOutcome:
    """The full gate verdict: per-package failures + behavioural failures."""

    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return whether no failure was recorded."""
        return not self.failures


def load_gates(pyproject_path: Path) -> dict[str, dict[str, object]]:
    """Read ``[tool.eawf.coverage.gates]`` from *pyproject_path*.

    Args:
        pyproject_path: Path to the ``pyproject.toml`` carrying the gate config.

    Returns:
        The gate table mapping each gate key to its ``path`` / ``glob`` +
        ``line`` / ``branch`` (or ``waive_line`` / ``waive_branch``) spec.

    Raises:
        KeyError: When ``[tool.eawf.coverage.gates]`` is absent.
    """
    with pyproject_path.open("rb") as handle:
        config = tomllib.load(handle)
    return cast("dict[str, dict[str, object]]", config["tool"]["eawf"]["coverage"]["gates"])


def load_tui_behavioural(pyproject_path: Path) -> dict[str, object]:
    """Read ``[tool.eawf.coverage.tui_behavioural]`` from *pyproject_path*.

    Args:
        pyproject_path: Path to the ``pyproject.toml`` carrying the gate config.

    Returns:
        The behavioural-floor spec (``golden_glob`` / ``min_goldens`` /
        ``flow_glob`` / ``min_flows``).

    Raises:
        KeyError: When ``[tool.eawf.coverage.tui_behavioural]`` is absent.
    """
    with pyproject_path.open("rb") as handle:
        config = tomllib.load(handle)
    return cast("dict[str, object]", config["tool"]["eawf"]["coverage"]["tui_behavioural"])


def aggregate(classes: list[Element]) -> PackageRate:
    """Aggregate line + branch coverage across Cobertura ``<class>`` nodes.

    Args:
        classes: The ``<class>`` elements to aggregate (already filtered to one
            package).

    Returns:
        The :class:`PackageRate` over those classes; an empty class list yields
        100% on both dimensions (vacuously covered), matching the CI checker.
    """
    line_total = line_cov = branch_total = branch_cov = 0
    for cls in classes:
        for line in cls.iter("line"):
            line_total += 1
            if int(line.get("hits", "0")) > 0:
                line_cov += 1
            if line.get("branch") == "true":
                match = _CONDITION.search(line.get("condition-coverage", ""))
                if match:
                    branch_cov += int(match.group(1))
                    branch_total += int(match.group(2))
    line_pct = line_cov / line_total * 100 if line_total else 100.0
    branch_pct = branch_cov / branch_total * 100 if branch_total else 100.0
    return PackageRate(line_pct, branch_pct, line_total, branch_total)


def _classes_for_gate(classes: list[Element], spec: dict[str, object]) -> list[Element]:
    """Filter Cobertura classes to the ones a gate *spec* claims.

    A ``glob`` spec matches by ``fnmatch`` over the filename; a ``path`` spec
    matches by filename prefix.
    """
    if "glob" in spec:
        pattern = str(spec["glob"])
        return [c for c in classes if fnmatch.fnmatch(c.get("filename", ""), pattern)]
    prefix = str(spec["path"])
    return [c for c in classes if c.get("filename", "").startswith(prefix)]


def evaluate_package_gates(
    gates: dict[str, dict[str, object]],
    classes: list[Element],
) -> tuple[list[str], list[str]]:
    """Evaluate every per-package line/branch ratchet against *classes*.

    Args:
        gates: The gate table from :func:`load_gates`.
        classes: The Cobertura ``<class>`` elements from the coverage report.

    Returns:
        A ``(report_lines, failures)`` pair: the human-readable per-package
        table rows and the list of failure messages (empty when all pass).
    """
    report: list[str] = []
    failures: list[str] = []
    for name, spec in sorted(gates.items()):
        picked = _classes_for_gate(classes, spec)
        if not picked:
            failures.append(f"{name}: no source files matched")
            continue
        rate = aggregate(picked)
        waive_line = bool(spec.get("waive_line", False))
        waive_branch = bool(spec.get("waive_branch", False))
        line_gate = float(str(spec.get("line", 0)))
        branch_gate = float(str(spec.get("branch", 0)))
        line_ok = waive_line or rate.line_pct >= line_gate
        branch_ok = waive_branch or rate.branch_pct >= branch_gate
        line_label = "waived" if waive_line else str(spec.get("line", 0))
        branch_label = "waived" if waive_branch else str(spec.get("branch", 0))
        status = "ok" if (line_ok and branch_ok) else "FAIL"
        report.append(
            f"{name:16s} {rate.line_pct:8.2f} {line_label:>6s}  "
            f"{rate.branch_pct:8.2f} {branch_label:>6s}  {status}"
        )
        if not line_ok:
            failures.append(f"{name}: line {rate.line_pct:.2f}% < gate {spec.get('line')}%")
        if not branch_ok:
            failures.append(f"{name}: branch {rate.branch_pct:.2f}% < gate {spec.get('branch')}%")
    return report, failures


def count_goldens(repo_root: Path, golden_glob: str) -> int:
    """Count the screen-level golden snapshots under *repo_root*.

    Args:
        repo_root: The repository root the glob is resolved against.
        golden_glob: A repo-relative glob (e.g. ``tests/.../golden/*.txt``).

    Returns:
        The number of files matching *golden_glob*.
    """
    return len(list(repo_root.glob(golden_glob)))


def count_flows(repo_root: Path, flow_glob: str) -> int:
    """Count the operator-journey ``tui_flow`` specs under *repo_root*.

    Each flow is one ``"flow": "G<N>-..."`` row in the ``FLOW_SPECS`` table; the
    count is the number of such rows across the file(s) the glob resolves to.

    Args:
        repo_root: The repository root the glob is resolved against.
        flow_glob: A repo-relative glob naming the flow-spec test file(s).

    Returns:
        The total number of ``"flow":`` rows across the matched files.
    """
    total = 0
    for path in repo_root.glob(flow_glob):
        for line in path.read_text(encoding="utf-8").splitlines():
            if _FLOW_ROW.match(line):
                total += 1
    return total


def evaluate_tui_behavioural(
    spec: dict[str, object],
    repo_root: Path,
) -> tuple[list[str], list[str]]:
    """Evaluate the TUI behavioural-count floors under *repo_root*.

    Args:
        spec: The behavioural-floor spec from :func:`load_tui_behavioural`.
        repo_root: The repository root the globs resolve against.

    Returns:
        A ``(report_lines, failures)`` pair mirroring
        :func:`evaluate_package_gates`.
    """
    report: list[str] = []
    failures: list[str] = []
    goldens = count_goldens(repo_root, str(spec["golden_glob"]))
    min_goldens = int(str(spec["min_goldens"]))
    golden_status = "ok" if goldens >= min_goldens else "FAIL"
    report.append(f"{'tui.goldens':16s} {goldens:8d} {min_goldens:>6d}  {golden_status}")
    if goldens < min_goldens:
        failures.append(f"tui.goldens: {goldens} screen snapshots < floor {min_goldens}")
    flows = count_flows(repo_root, str(spec["flow_glob"]))
    min_flows = int(str(spec["min_flows"]))
    flow_status = "ok" if flows >= min_flows else "FAIL"
    report.append(f"{'tui.flows':16s} {flows:8d} {min_flows:>6d}  {flow_status}")
    if flows < min_flows:
        failures.append(f"tui.flows: {flows} operator-journey flows < floor {min_flows}")
    return report, failures


def run_gate(coverage_xml: Path, pyproject_path: Path, repo_root: Path) -> GateOutcome:
    """Run the full gate: per-package ratchets + TUI behavioural floors.

    Args:
        coverage_xml: Path to the combined Cobertura ``coverage.xml``.
        pyproject_path: Path to the ``pyproject.toml`` carrying the gate config.
        repo_root: The repository root the behavioural globs resolve against.

    Returns:
        The :class:`GateOutcome` with every failure recorded (empty on pass).
    """
    gates = load_gates(pyproject_path)
    classes = ET.parse(coverage_xml).getroot().findall(".//class")
    pkg_report, pkg_failures = evaluate_package_gates(gates, classes)
    print("per-package coverage gates (C09 5.2)")
    print(f"{'package':16s} {'line%':>8s} {'gate':>6s}  {'branch%':>8s} {'gate':>6s}  status")
    for row in pkg_report:
        print(row)
    behavioural = load_tui_behavioural(pyproject_path)
    tui_report, tui_failures = evaluate_tui_behavioural(behavioural, repo_root)
    print("\ntui behavioural gate-coverage (line-cov waived)")
    print(f"{'metric':16s} {'count':>8s} {'floor':>6s}  status")
    for row in tui_report:
        print(row)
    return GateOutcome(failures=pkg_failures + tui_failures)


def main(argv: list[str] | None = None) -> int:
    """Parse args, run the gate, and map the outcome onto an exit code.

    Args:
        argv: Command-line arguments (``--coverage-xml`` / ``--repo-root``).

    Returns:
        ``0`` when every gate passes, ``1`` when at least one failed.
    """
    repo_root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="per-package + TUI behavioural coverage gate")
    parser.add_argument(
        "--coverage-xml",
        type=Path,
        default=repo_root_default / "coverage.xml",
        help="path to the combined Cobertura coverage.xml",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=repo_root_default,
        help="repository root the behavioural globs resolve against",
    )
    args = parser.parse_args(argv)
    pyproject_path = args.repo_root / "pyproject.toml"
    outcome = run_gate(args.coverage_xml, pyproject_path, args.repo_root)
    if outcome.passed:
        print("\nall coverage gates passed")
        return 0
    print("\ncoverage gate FAILED:", file=sys.stderr)
    for entry in outcome.failures:
        print(f"  - {entry}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
