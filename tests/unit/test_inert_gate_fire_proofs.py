"""Fire proofs for four gates that once could not fail.

Each gate gets a seeded defect it must red on and a clean input it must pass:

- the dynamic-exercise row predicates in ``tools/idle_contract_gate.py`` (a
  store row that proves nothing ran no longer counts as runtime output);
- ``check_coverage_gate_helpers_wired`` (reds when the coverage ratchet cannot
  fail or ``main`` stops refusing a stale report);
- ``_has_asserting_test`` behind the idle-contract meta-gate (a comment or
  docstring mention in a test file no longer discharges the contract);
- the freshness refusal in ``tools/coverage_gate.py`` ``main`` (a report older
  than a source file it measures is refused).

``tools/`` is not a package, so both gates are loaded by path.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOLS = _REPO_ROOT / "tools"


def _load(name: str) -> ModuleType:
    if str(_TOOLS) not in sys.path:
        sys.path.insert(0, str(_TOOLS))
    spec = importlib.util.spec_from_file_location(name, _TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_COVERAGE = _load("coverage_gate")
_IDLE = _load("idle_contract_gate")


def _store(rows_by_stem: dict[str, list[dict[str, Any]]]) -> Any:
    return lambda stem: rows_by_stem.get(stem, [])


# --------------------------------------------------------------------------- #
# Gate 1: the dynamic-exercise leg counts only rows that prove the contract ran.
# --------------------------------------------------------------------------- #

_EXERCISED = {
    "auditor_report": [{"body": {"verdict": "pass", "report_source": "authored"}}],
    "actual": [{"elapsed_eu": 1.0}],
    "gold_label": [{"wave_id": "P01-I01-W01", "ground_truth": False}],
}


def test_dynamic_leg_passes_rows_that_prove_exercise() -> None:
    findings = _IDLE.check_contract_exercised(
        store_rows_fn=_store(_EXERCISED), jury_convened_fn=lambda: False
    )
    assert findings == []


@pytest.mark.parametrize(
    ("stem", "hollow_row", "contract"),
    [
        (
            "auditor_report",
            {"body": {"verdict": "pass", "report_source": "synthesized"}},
            "verdict_producer",
        ),
        ("auditor_report", {"body": {"verdict": "maybe"}}, "verdict_producer"),
        ("auditor_report", {"header": {"report_id": "AR-1"}}, "verdict_producer"),
        ("gold_label", {"wave_id": "P01-I01-W01", "ground_truth": "yes"}, "calibration"),
        ("gold_label", {"wave_id": "", "ground_truth": True}, "calibration"),
    ],
)
def test_dynamic_leg_reds_on_a_row_that_proves_nothing(
    stem: str, hollow_row: dict[str, Any], contract: str
) -> None:
    rows = {**_EXERCISED, stem: [hollow_row]}
    findings = _IDLE.check_contract_exercised(
        store_rows_fn=_store(rows), jury_convened_fn=lambda: False
    )
    assert [finding.symbol for finding in findings] == [contract]
    assert findings[0].missing is _IDLE.MissingDischarge.NO_RUNTIME_OUTPUT


def test_ballot_contract_reads_the_jury_ballot_store_and_skips_abstentions() -> None:
    abstained = _store({"jury_ballot": [{"wave_id": "W", "verdict": None, "error": "timeout"}]})
    cast = _store({"jury_ballot": [{"wave_id": "W", "verdict": "pass"}]})
    contracts = [_IDLE._BALLOT_CONTRACT]
    red = _IDLE.check_contract_exercised(
        store_rows_fn=abstained, jury_convened_fn=lambda: True, contracts=contracts
    )
    green = _IDLE.check_contract_exercised(
        store_rows_fn=cast, jury_convened_fn=lambda: True, contracts=contracts
    )
    assert [finding.symbol for finding in red] == ["juror_ballot"]
    assert green == []


def test_dynamic_leg_passes_the_real_verdict_and_calibration_stores() -> None:
    rows = {stem: _IDLE._default_store_rows(stem) for stem in ("auditor_report", "gold_label")}
    if not all(rows.values()):
        pytest.skip("the verdict or calibration store is empty on this tree")
    contracts = [c for c in _IDLE._I22_BOUND_CONTRACTS if c.store_stem in rows]
    findings = _IDLE.check_contract_exercised(
        store_rows_fn=_store(rows), jury_convened_fn=lambda: False, contracts=contracts
    )
    assert findings == []


# --------------------------------------------------------------------------- #
# Gate 2: the coverage-gate liveness row reds when the coverage gate cannot fail.
# --------------------------------------------------------------------------- #


def _main_without_freshness(argv: list[str] | None = None) -> int:
    return 0


def _main_with_freshness(argv: list[str] | None = None) -> int:
    return 1 if _COVERAGE.stale_report_reason else 0


def test_coverage_liveness_passes_on_the_real_coverage_gate() -> None:
    result = _IDLE.check_coverage_gate_helpers_wired()
    assert result.passed is True
    assert result.failure is None


def test_coverage_liveness_reds_when_main_skips_the_freshness_refusal() -> None:
    seeded = SimpleNamespace(
        evaluate_package_gates=_COVERAGE.evaluate_package_gates, main=_main_without_freshness
    )
    result = _IDLE.check_coverage_gate_helpers_wired(gate_module=seeded)
    assert result.passed is False
    assert result.failure is _IDLE.GateFailure.COVERAGE_GATE_IDLE
    assert "refuses_stale=False" in result.message


def test_coverage_liveness_reds_when_the_ratchet_never_fails() -> None:
    seeded = SimpleNamespace(
        evaluate_package_gates=lambda _gates, _classes: ([], []), main=_main_with_freshness
    )
    result = _IDLE.check_coverage_gate_helpers_wired(gate_module=seeded)
    assert result.passed is False
    assert "ratchet_fires=False" in result.message


# --------------------------------------------------------------------------- #
# Gate 3: only a test function that uses the contract discharges the meta-gate.
# --------------------------------------------------------------------------- #

_DEFINING = "src/eawf/workflow/verify/widget.py"
_CALLER = "src/eawf/workflow/verify/caller.py"
_TEST = "tests/unit/test_widget.py"
_DIFF = (
    f"diff --git a/{_DEFINING} b/{_DEFINING}\n"
    f"+++ b/{_DEFINING}\n"
    "@@ -0,0 +1,2 @@\n"
    "+def check_widget_parity(node):\n"
    "+    return True\n"
)


def _findings_for_test_file(test_source: str) -> list[Any]:
    files = {
        _DEFINING: "def check_widget_parity(node):\n    return True\n",
        _CALLER: "from widget import check_widget_parity\ncheck_widget_parity(None)\n",
        _TEST: test_source,
    }
    return _IDLE.detect_idle_contracts(
        "--cached",
        diff_fn=lambda _range: _DIFF,
        tree_fn=lambda: sorted(files),
        read_fn=lambda path: files.get(path, ""),
    )


def test_meta_gate_passes_a_test_function_that_calls_the_contract() -> None:
    source = (
        "from widget import check_widget_parity\n\n"
        "def test_it():\n    assert check_widget_parity(None) is True\n"
    )
    assert _findings_for_test_file(source) == []


@pytest.mark.parametrize(
    "hollow_source",
    [
        "# check_widget_parity is covered elsewhere\ndef test_it():\n    assert True\n",
        '"""Covers check_widget_parity."""\n\ndef test_it():\n    assert True\n',
        "def _helper():\n    return check_widget_parity(None)\n\ndef test_it():\n    assert True\n",
        "def test_it(:\n    check_widget_parity(None)\n",
    ],
    ids=["comment", "docstring", "non-test-helper", "unparseable"],
)
def test_meta_gate_reds_when_no_test_function_uses_the_contract(hollow_source: str) -> None:
    findings = _findings_for_test_file(hollow_source)
    assert [finding.symbol for finding in findings] == ["check_widget_parity"]
    assert findings[0].missing is _IDLE.MissingDischarge.NO_ASSERTING_TEST


def test_asserting_test_probe_finds_a_real_test_on_this_tree() -> None:
    assert _IDLE._has_asserting_test(
        "check_coverage_gate_helpers_wired", _IDLE._default_tree(), _IDLE._default_read
    )


# --------------------------------------------------------------------------- #
# Gate 4: the coverage gate refuses a report older than the code it measures.
# --------------------------------------------------------------------------- #

_PYPROJECT = """
[tool.eawf.coverage.gates.pkg]
path = "src/pkg/"
line = 50
branch = 0

[tool.eawf.coverage.tui_behavioural]
golden_glob = "tests/snapshots/tui/golden/*.txt"
min_goldens = 0
flow_glob = "tests/snapshots/tui/test_tui_flow.py"
min_flows = 0
""".lstrip()


def _seed(root: Path, *, source_mtime: float, report_mtime: float) -> Path:
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    source = root / "src" / "pkg" / "a.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n", encoding="utf-8")
    os.utime(source, (source_mtime, source_mtime))
    report = ET.Element("coverage")
    classes = ET.SubElement(ET.SubElement(ET.SubElement(report, "packages"), "package"), "classes")
    cls = ET.SubElement(classes, "class", {"filename": "src/pkg/a.py"})
    ET.SubElement(ET.SubElement(cls, "lines"), "line", {"number": "1", "hits": "1"})
    coverage_xml = root / "coverage.xml"
    ET.ElementTree(report).write(coverage_xml, encoding="utf-8", xml_declaration=True)
    os.utime(coverage_xml, (report_mtime, report_mtime))
    return coverage_xml


@pytest.fixture()
def _old_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_COVERAGE, "head_commit_time", lambda _root: 0)


@pytest.mark.usefixtures("_old_head")
def test_coverage_gate_passes_a_fresh_report(tmp_path: Path) -> None:
    coverage_xml = _seed(tmp_path, source_mtime=1_000.0, report_mtime=2_000.0)
    argv = ["--coverage-xml", str(coverage_xml), "--repo-root", str(tmp_path)]
    assert _COVERAGE.main(argv) == 0


@pytest.mark.usefixtures("_old_head")
def test_coverage_gate_reds_on_a_report_older_than_its_source(tmp_path: Path) -> None:
    coverage_xml = _seed(tmp_path, source_mtime=2_000.0, report_mtime=1_000.0)
    argv = ["--coverage-xml", str(coverage_xml), "--repo-root", str(tmp_path)]
    assert _COVERAGE.main(argv) == 1
