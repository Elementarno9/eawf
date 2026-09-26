"""Jury calibration contract and telemetry contract census.

The close jury may block only under a calibration an active Decision
ratified; the plan jury never blocks. Every metric family the exporter emits
carries a declared contract, and a family without one fails the export.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.models import State
from eawf.observability.eval.jury_validation import BlockAuthority
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
from eawf.platform.profiles.models import JuryAuthorityConfig, JuryCalibration, VerifyBlock
from eawf.runtime.daemon.methods.state import _resolve_jury_block_authority
from eawf.runtime.daemon.methods.state_jury import (
    JuryCalibrationRefusedError,
    ratified_calibration_decision,
)
from eawf.workflow.lifecycle.transitions import LifecycleError

_RATIFYING_TITLE = "Ratify the jury calibration for close blocking"


def _decision(
    decision_id: str,
    *,
    title: str = _RATIFYING_TITLE,
    status: str = "active",
    rationale: str = "Scored cohort cleared every floor.",
) -> dict[str, Any]:
    return {
        "id": decision_id,
        "scope_id": "P01",
        "title": title,
        "rationale": rationale,
        "status": status,
        "created_at": "2026-09-01T00:00:00Z",
    }


def _state(decisions: dict[str, dict[str, Any]] | None = None) -> State:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": "2026-06-11T00:00:00Z",
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "Abc",
            "domains": ["infra"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {
            "project_code": "ABC",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
        "decisions": decisions or {},
    }
    return State.model_validate(payload)


def _blocking_block(
    decision_id: str = "D07", *, precision_floor: float = 0.8, lb_floor: float = 0.8
) -> VerifyBlock:
    return VerifyBlock(
        enforce=True,
        cross_vendor_jury=True,
        jury_authority=JuryAuthorityConfig(
            known_bad_catch_lb_floor=lb_floor,
            calibration=JuryCalibration(
                close_authority="blocking",
                calibration_decision=decision_id,
                precision_floor=precision_floor,
            ),
        ),
    )


def _resolve(state: State, block: VerifyBlock | None, tmp_path: Path) -> BlockAuthority:
    return _resolve_jury_block_authority(
        state, state_path=tmp_path / "state.json", verify_block=block
    )


# --- JuryCalibration contract -------------------------------------------------


def test_jury_calibration_default_is_advisory_at_both_sites() -> None:
    calibration = JuryCalibration()
    assert calibration.plan_authority == "advisory"
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


# --- ratified_calibration_decision ---------------------------------------------


def test_ratified_calibration_decision_returns_active_naming_decision() -> None:
    state = _state({"D07": _decision("D07")})
    calibration = JuryCalibration(close_authority="blocking", calibration_decision="D07")
    assert ratified_calibration_decision(calibration, state).id == "D07"


def test_ratified_calibration_decision_marker_in_rationale_accepted() -> None:
    state = _state(
        {"D07": _decision("D07", title="Allow close vetoes", rationale="Jury Calibration scored.")}
    )
    calibration = JuryCalibration(close_authority="blocking", calibration_decision="D07")
    assert ratified_calibration_decision(calibration, state).id == "D07"


def test_ratified_calibration_decision_missing_decision_refused() -> None:
    calibration = JuryCalibration(close_authority="blocking", calibration_decision="D07")
    with pytest.raises(JuryCalibrationRefusedError, match="not a Decision in state"):
        ratified_calibration_decision(calibration, _state())


@pytest.mark.parametrize("status", ["superseded", "reversed", "obsolete"])
def test_ratified_calibration_decision_inactive_decision_refused(status: str) -> None:
    state = _state({"D07": _decision("D07", status=status)})
    calibration = JuryCalibration(close_authority="blocking", calibration_decision="D07")
    with pytest.raises(JuryCalibrationRefusedError, match="not active"):
        ratified_calibration_decision(calibration, state)


def test_ratified_calibration_decision_unrelated_decision_refused() -> None:
    state = _state({"D07": _decision("D07", title="Adopt uv for Python installs")})
    calibration = JuryCalibration(close_authority="blocking", calibration_decision="D07")
    with pytest.raises(JuryCalibrationRefusedError, match="does not name"):
        ratified_calibration_decision(calibration, state)


def test_ratified_calibration_decision_advisory_calibration_refused() -> None:
    with pytest.raises(JuryCalibrationRefusedError, match="only a blocking close jury"):
        ratified_calibration_decision(JuryCalibration(), _state())


def test_ratified_calibration_decision_refusal_is_a_lifecycle_refusal() -> None:
    assert issubclass(JuryCalibrationRefusedError, LifecycleError)


# --- close-gate resolver --------------------------------------------------------


def test_resolve_jury_block_authority_blocking_without_decision_refused(tmp_path: Path) -> None:
    with pytest.raises(JuryCalibrationRefusedError, match="not a Decision in state"):
        _resolve(_state(), _blocking_block(), tmp_path)


def test_resolve_jury_block_authority_ratified_blocking_on_empty_substrate_advisory(
    tmp_path: Path,
) -> None:
    state = _state({"D07": _decision("D07")})
    assert _resolve(state, _blocking_block(), tmp_path) is BlockAuthority.ADVISORY


def _stub_scored_substrate(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any], *, earned: BlockAuthority
) -> None:
    import eawf.observability.eval.jury_validation as jv
    import eawf.runtime.daemon.methods.state_jury as sj

    monkeypatch.setattr(
        jv, "build_jury_validation_cohort", lambda state, state_path: _nonempty_cohort()
    )
    monkeypatch.setattr(sj, "_load_recorded_ballots", lambda state_path: {"W": ()})
    monkeypatch.setattr(jv, "validate_jury", lambda cohort, ballots_by_wave: "report")

    def fake_gate(report: object, verbosity: object, config: object) -> BlockAuthority:
        captured["config"] = config
        return earned

    monkeypatch.setattr(jv, "jury_block_authority", fake_gate)


def _nonempty_cohort() -> Any:
    from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole
    from eawf.observability.eval.jury_validation import (
        LabeledVerdict,
        LabelSource,
        ValidationCohort,
    )
    from eawf.observability.eval.reputation import VerdictOutcome

    labelled = LabeledVerdict(
        outcome=VerdictOutcome(
            base_id="P01-I01-W01",
            agent_role=AgentSessionRole.AUDITOR,
            runtime="claude-code",
            verdict=AgentReportVerdict.PASS,
            confidence=0.8,
            held=True,
            outcome_source="clean-close",
        ),
        ground_truth=True,
        label_source=LabelSource.SILVER,
    )
    return ValidationCohort(silver=[labelled], gold=[])


def test_resolve_jury_block_authority_advisory_calibration_never_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    _stub_scored_substrate(monkeypatch, captured, earned=BlockAuthority.BLOCKING)
    block = VerifyBlock(enforce=True, cross_vendor_jury=True)

    assert _resolve(_state(), block, tmp_path) is BlockAuthority.ADVISORY
    assert "config" not in captured


def test_resolve_jury_block_authority_ratified_calibration_earns_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    _stub_scored_substrate(monkeypatch, captured, earned=BlockAuthority.BLOCKING)
    state = _state({"D07": _decision("D07")})

    assert _resolve(state, _blocking_block(), tmp_path) is BlockAuthority.BLOCKING


@pytest.mark.parametrize(
    ("precision_floor", "lb_floor", "expected"),
    [(0.95, 0.8, 0.95), (0.5, 0.9, 0.9)],
)
def test_resolve_jury_block_authority_precision_floor_tightens_catch_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    precision_floor: float,
    lb_floor: float,
    expected: float,
) -> None:
    captured: dict[str, Any] = {}
    _stub_scored_substrate(monkeypatch, captured, earned=BlockAuthority.ADVISORY)
    state = _state({"D07": _decision("D07")})
    block = _blocking_block(precision_floor=precision_floor, lb_floor=lb_floor)

    _resolve(state, block, tmp_path)

    assert captured["config"].known_bad_catch_lb_floor == pytest.approx(expected)


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
