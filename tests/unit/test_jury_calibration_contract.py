"""Jury calibration contract and telemetry contract census.

The close jury may block only under a calibration an active Decision
ratified; the plan jury never blocks. Every metric family the exporter emits
carries a declared contract, and a family without one fails the export.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.observability.telemetry import exporter
from eawf.observability.telemetry.contracts import (
    TELEMETRY_CONTRACTS,
    RetentionClassId,
    TelemetryContract,
    UndeclaredMetricError,
    check_metric_contracts,
)
from eawf.observability.telemetry.exporter import MetricFamily, MetricType, build_snapshot
from eawf.observability.telemetry.store import SqliteMetricsStore
from eawf.platform.profiles.models import JuryCalibration

# --- JuryCalibration contract -------------------------------------------------


def test_jury_calibration_default_close_site_is_advisory() -> None:
    calibration = JuryCalibration()
    assert calibration.close_authority == "advisory"
    assert calibration.calibration_decision is None


def test_jury_calibration_blocking_close_without_decision_refused() -> None:
    with pytest.raises(ValidationError, match="ratified calibration"):
        JuryCalibration(close_authority="blocking")


def test_jury_calibration_plan_site_cannot_block() -> None:
    with pytest.raises(ValidationError, match="plan_authority"):
        JuryCalibration.model_validate({"plan_authority": "blocking"})


@pytest.mark.parametrize("floor", [0.5, 1.0])
def test_jury_calibration_precision_floor_bounds_accepted(floor: float) -> None:
    assert JuryCalibration(precision_floor=floor).precision_floor == floor


@pytest.mark.parametrize("floor", [0.4999, 1.0001])
def test_jury_calibration_precision_floor_out_of_range_refused(floor: float) -> None:
    with pytest.raises(ValidationError, match="precision_floor"):
        JuryCalibration(precision_floor=floor)


def test_jury_calibration_malformed_decision_id_refused() -> None:
    with pytest.raises(ValidationError, match="calibration_decision"):
        JuryCalibration(close_authority="blocking", calibration_decision="calib-1")


def test_jury_calibration_unknown_key_refused() -> None:
    with pytest.raises(ValidationError, match="extra"):
        JuryCalibration.model_validate({"close_authority": "advisory", "autopass": True})


def test_jury_calibration_wrong_type_refused() -> None:
    with pytest.raises(ValidationError):
        JuryCalibration.model_validate({"precision_floor": "high"})


# --- telemetry contract census --------------------------------------------------


def test_telemetry_contracts_cover_every_exported_family(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "telemetry.db")
    store.init_schema()
    snapshot = build_snapshot(store, scope="repo/abc")
    emitted = {family.name for family in snapshot.families}
    assert emitted == set(TELEMETRY_CONTRACTS)
    assert {"eawf_run_tokens_total", "eawf_run_cost_usd_total"} <= emitted


def test_telemetry_contracts_declare_producer_retention_consumer() -> None:
    for name, contract in TELEMETRY_CONTRACTS.items():
        assert contract.metric == name
        assert contract.producer
        assert contract.consumer
        assert contract.retention_class in RetentionClassId


def test_check_metric_contracts_empty_emission_passes() -> None:
    check_metric_contracts([])


def test_check_metric_contracts_single_declared_passes() -> None:
    check_metric_contracts(["eawf_run_cost_usd_total"])


def test_check_metric_contracts_undeclared_metric_reds() -> None:
    with pytest.raises(UndeclaredMetricError, match="eawf_orphan_total"):
        check_metric_contracts(["eawf_tokens_total", "eawf_orphan_total"])


def test_check_metric_contracts_lists_every_undeclared_sorted() -> None:
    with pytest.raises(UndeclaredMetricError, match="eawf_a_total, eawf_b_total"):
        check_metric_contracts(["eawf_b_total", "eawf_a_total"], contracts={})


def test_build_snapshot_undeclared_family_reds_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def orphan(runs: object, *, scope: str) -> MetricFamily:
        return MetricFamily(
            name="eawf_orphan_total",
            help_text="An undeclared family.",
            metric_type=MetricType.COUNTER,
            samples=(),
        )

    monkeypatch.setattr(exporter, "_run_cost_family", orphan)
    store = SqliteMetricsStore(tmp_path / "telemetry.db")
    store.init_schema()
    with pytest.raises(UndeclaredMetricError, match="eawf_orphan_total"):
        build_snapshot(store, scope="repo/abc")


def test_telemetry_contract_unknown_retention_class_refused() -> None:
    with pytest.raises(ValidationError, match="retention_class"):
        TelemetryContract.model_validate(
            {
                "metric": "eawf_x_total",
                "producer": "t",
                "retention_class": "forever",
                "consumer": "c",
            }
        )


def test_telemetry_contract_empty_producer_refused() -> None:
    with pytest.raises(ValidationError, match="producer"):
        TelemetryContract(
            metric="eawf_x_total",
            producer="",
            retention_class=RetentionClassId.TELEMETRY,
            consumer="c",
        )


def test_telemetry_contract_missing_consumer_refused() -> None:
    with pytest.raises(ValidationError, match="consumer"):
        TelemetryContract.model_validate(
            {"metric": "eawf_x_total", "producer": "t", "retention_class": "telemetry"}
        )


def test_telemetry_contract_non_eawf_metric_name_refused() -> None:
    with pytest.raises(ValidationError, match="metric"):
        TelemetryContract(
            metric="tokens_total",
            producer="t",
            retention_class=RetentionClassId.TELEMETRY,
            consumer="c",
        )


def test_retention_class_id_is_closed_at_sixteen() -> None:
    assert len(RetentionClassId) == 16
