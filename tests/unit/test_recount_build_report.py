"""Unit tests for the EvalReport schema + the build-report recount tool.

Two surfaces under test:

- :class:`eawf.kernel.spec.eval_report.EvalReport` — the typed,
  ``extra="forbid"`` pydantic model that validates the pre-registered
  build-report metric set. A well-formed metric set validates; malformed
  inputs (unknown key, bad metric id, fractional count, out-of-range ratio,
  duplicate id / recount_key) raise :class:`pydantic.ValidationError`.

- ``tools/recount_build_report.py`` — the recount tool that re-derives each
  metric from ``state.json`` and exits ``0`` only when every figure
  reproduces. The key acceptance: it exits ``0`` on a report whose declared
  values match the recount of a fixture state, and NONZERO when a declared
  value is wrong OR when a metric's ``recount_key`` has no registered
  recomputer (an unrecountable figure).

The tool lives under ``tools/`` (excluded from the package), so it is loaded
via :mod:`importlib` rather than imported by name — mirroring the
sigil-totality gate test.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.eval_report import (
    EvalMetric,
    EvalReport,
    MetricSource,
    MetricUnit,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_PATH = _REPO_ROOT / "tools" / "recount_build_report.py"
_TOOL_DIR = _TOOL_PATH.parent


def _load_tool_module() -> Any:
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("recount_build_report", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["recount_build_report"] = mod
    spec.loader.exec_module(mod)
    return mod


# --- fixture state: a small, hand-built state.json the recount can reproduce ---


def _fixture_state() -> dict[str, Any]:
    """A minimal state mapping the recount functions read deterministically.

    Two closed waves (one M, one L bucket), one pending wave (excluded), one
    closed + one active phase. The closed-M wave has two criteria — one
    gate-cited (evidenced), one bare (unevidenced) — so the coverage ratio is
    a non-trivial fraction the test can pin.
    """
    return {
        "waves": {
            "P01-I01-W01": {
                "status": "closed",
                "effort_bucket": "M",
                "commit": "abc1234",
                "success_criteria": [
                    {"id": "CR-01", "gate_ids": ["G-01"], "evidence_kind": "gate"},
                    {"id": "CR-02", "gate_ids": [], "evidence_kind": "legacy"},
                ],
            },
            "P01-I01-W02": {
                "status": "closed",
                "effort_bucket": "L",
                "commit": "def5678",
                "success_criteria": [
                    {"id": "CR-01", "gate_ids": [], "evidence_kind": "attested"},
                ],
            },
            "P01-I01-W03": {
                "status": "pending",
                "effort_bucket": "XL",
                "commit": None,
                "success_criteria": [],
            },
        },
        "phases": {
            "P01": {"status": "closed"},
            "P02": {"status": "active"},
        },
    }


# Recount of the fixture above:
#   closed_wave_count  = 2
#   eu_delivered       = 1.0 (M) + 2.0 (L) = 3.0
#   phases_closed      = 1
#   evidence_coverage  = 2 evidenced / 3 total = 0.6666...
#   commit_pinned_waves= 2
_FIXTURE_COVERAGE = 2 / 3


def _good_report() -> EvalReport:
    return EvalReport(
        report_id="EVAL-FIXTURE",
        title="Fixture build report",
        metrics=[
            EvalMetric(
                id="M1",
                label="closed waves",
                source=MetricSource.STATE,
                unit=MetricUnit.COUNT,
                recount_key="closed_wave_count",
                declared_value=2.0,
            ),
            EvalMetric(
                id="M2",
                label="EU delivered",
                source=MetricSource.STATE,
                unit=MetricUnit.EU,
                recount_key="eu_delivered",
                declared_value=3.0,
                tolerance=0.01,
            ),
            EvalMetric(
                id="M3",
                label="phases closed",
                source=MetricSource.STATE,
                unit=MetricUnit.COUNT,
                recount_key="phases_closed",
                declared_value=1.0,
            ),
            EvalMetric(
                id="M4",
                label="evidence coverage",
                source=MetricSource.STATE,
                unit=MetricUnit.RATIO,
                recount_key="evidence_coverage",
                declared_value=round(_FIXTURE_COVERAGE, 4),
                tolerance=0.001,
            ),
            EvalMetric(
                id="M5",
                label="commit-pinned waves",
                source=MetricSource.STATE,
                unit=MetricUnit.COUNT,
                recount_key="commit_pinned_waves",
                declared_value=2.0,
            ),
        ],
    )


def _write(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# EvalReport schema validation
# --------------------------------------------------------------------------- #


def test_evalreport_validates_a_good_metric_set() -> None:
    report = _good_report()
    assert report.kind == "EvalReport"
    assert report.schema_version == "1.0"
    assert len(report.metrics) == 5
    assert report.metrics[0].source is MetricSource.STATE


def test_evalreport_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        EvalReport.model_validate(
            {
                "report_id": "EVAL-X",
                "title": "x",
                "metrics": [
                    {
                        "id": "M1",
                        "label": "x",
                        "source": "state",
                        "unit": "count",
                        "recount_key": "closed_wave_count",
                        "declared_value": 1.0,
                    }
                ],
                "bogus_key": True,
            }
        )


def test_evalreport_rejects_empty_metric_list() -> None:
    with pytest.raises(ValidationError):
        EvalReport(report_id="EVAL-X", title="x", metrics=[])


def test_evalmetric_rejects_malformed_id() -> None:
    with pytest.raises(ValidationError):
        EvalMetric(
            id="metric-1",  # must match ^M\d+$
            label="x",
            source=MetricSource.STATE,
            unit=MetricUnit.COUNT,
            recount_key="closed_wave_count",
            declared_value=1.0,
        )


def test_evalmetric_rejects_fractional_count() -> None:
    with pytest.raises(ValidationError, match="not whole"):
        EvalMetric(
            id="M1",
            label="x",
            source=MetricSource.STATE,
            unit=MetricUnit.COUNT,
            recount_key="closed_wave_count",
            declared_value=2.5,
        )


def test_evalmetric_rejects_ratio_above_one() -> None:
    with pytest.raises(ValidationError, match="out of"):
        EvalMetric(
            id="M1",
            label="x",
            source=MetricSource.STATE,
            unit=MetricUnit.RATIO,
            recount_key="evidence_coverage",
            declared_value=1.5,
        )


def test_evalmetric_rejects_bad_recount_key() -> None:
    with pytest.raises(ValidationError):
        EvalMetric(
            id="M1",
            label="x",
            source=MetricSource.STATE,
            unit=MetricUnit.COUNT,
            recount_key="Bad-Key",  # must match ^[a-z][a-z0-9_]*$
            declared_value=1.0,
        )


def test_evalreport_rejects_duplicate_metric_id() -> None:
    with pytest.raises(ValidationError, match="duplicate metric id"):
        EvalReport(
            report_id="EVAL-X",
            title="x",
            metrics=[
                EvalMetric(
                    id="M1",
                    label="a",
                    source=MetricSource.STATE,
                    unit=MetricUnit.COUNT,
                    recount_key="closed_wave_count",
                    declared_value=1.0,
                ),
                EvalMetric(
                    id="M1",
                    label="b",
                    source=MetricSource.STATE,
                    unit=MetricUnit.COUNT,
                    recount_key="phases_closed",
                    declared_value=1.0,
                ),
            ],
        )


def test_evalreport_rejects_duplicate_recount_key() -> None:
    with pytest.raises(ValidationError, match="duplicate recount_key"):
        EvalReport(
            report_id="EVAL-X",
            title="x",
            metrics=[
                EvalMetric(
                    id="M1",
                    label="a",
                    source=MetricSource.STATE,
                    unit=MetricUnit.COUNT,
                    recount_key="closed_wave_count",
                    declared_value=1.0,
                ),
                EvalMetric(
                    id="M2",
                    label="b",
                    source=MetricSource.STATE,
                    unit=MetricUnit.COUNT,
                    recount_key="closed_wave_count",
                    declared_value=2.0,
                ),
            ],
        )


# --------------------------------------------------------------------------- #
# recount pure functions
# --------------------------------------------------------------------------- #


def test_recount_functions_reproduce_fixture() -> None:
    mod = _load_tool_module()
    state = _fixture_state()
    assert mod.recount_closed_wave_count(state) == 2.0
    assert mod.recount_eu_delivered(state) == pytest.approx(3.0)
    assert mod.recount_phases_closed(state) == 1.0
    assert mod.recount_evidence_coverage(state) == pytest.approx(_FIXTURE_COVERAGE)
    assert mod.recount_commit_pinned_waves(state) == 2.0


def test_recount_evidence_coverage_empty_corpus_is_zero() -> None:
    mod = _load_tool_module()
    assert mod.recount_evidence_coverage({"waves": {}}) == 0.0


# --------------------------------------------------------------------------- #
# recount tool exit codes — the load-bearing acceptance
# --------------------------------------------------------------------------- #


def test_tool_exits_zero_when_all_metrics_reproduce(tmp_path: Path) -> None:
    mod = _load_tool_module()
    report_path = _write(tmp_path, "report.json", _good_report().model_dump(mode="json"))
    state_path = _write(tmp_path, "state.json", _fixture_state())
    rc = mod.main([str(report_path), "--state", str(state_path)])
    assert rc == 0


def test_tool_exits_nonzero_on_a_wrong_declared_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_tool_module()
    report = _good_report()
    # Tamper with the closed-wave count: declare 99 where the recount is 2.
    payload = report.model_dump(mode="json")
    payload["metrics"][0]["declared_value"] = 99.0
    report_path = _write(tmp_path, "report.json", payload)
    state_path = _write(tmp_path, "state.json", _fixture_state())
    rc = mod.main([str(report_path), "--state", str(state_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "M1" in err
    assert "did not reproduce" in err


def test_tool_exits_nonzero_on_unrecountable_metric(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_tool_module()
    # A metric whose recount_key has no registered recomputer is unrecountable.
    report = EvalReport(
        report_id="EVAL-UNRECOUNTABLE",
        title="x",
        metrics=[
            EvalMetric(
                id="M1",
                label="api-equivalent build cost",
                source=MetricSource.TELEMETRY,
                unit=MetricUnit.USD,
                recount_key="api_equivalent_cost_usd",
                declared_value=8281.0,
            )
        ],
    )
    report_path = _write(tmp_path, "report.json", report.model_dump(mode="json"))
    state_path = _write(tmp_path, "state.json", _fixture_state())
    rc = mod.main([str(report_path), "--state", str(state_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no recomputer registered" in err
    assert "api_equivalent_cost_usd" in err


def test_tool_exits_nonzero_on_invalid_report(tmp_path: Path) -> None:
    mod = _load_tool_module()
    bad_report = _write(tmp_path, "report.json", {"report_id": "EVAL-X", "metrics": []})
    state_path = _write(tmp_path, "state.json", _fixture_state())
    rc = mod.main([str(bad_report), "--state", str(state_path)])
    assert rc == 1


def test_tool_exits_nonzero_on_missing_report(tmp_path: Path) -> None:
    mod = _load_tool_module()
    state_path = _write(tmp_path, "state.json", _fixture_state())
    rc = mod.main([str(tmp_path / "nope.json"), "--state", str(state_path)])
    assert rc == 1


def test_tool_recounts_against_the_real_state_json() -> None:
    """The recount registry runs end-to-end over the live committed state.json.

    Not an exact-value pin (state moves every wave close); this proves the
    recount functions execute over the real document without error and yield
    plausible, in-range figures — the figures a build-report would declare.
    """
    mod = _load_tool_module()
    state = json.loads((_REPO_ROOT / ".ea" / "state.json").read_text(encoding="utf-8"))
    closed = mod.recount_closed_wave_count(state)
    eu = mod.recount_eu_delivered(state)
    coverage = mod.recount_evidence_coverage(state)
    pinned = mod.recount_commit_pinned_waves(state)
    assert closed > 0
    assert eu > 0
    assert 0.0 <= coverage <= 1.0
    assert 0 <= pinned <= closed
