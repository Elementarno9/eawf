"""Unit tests for the M26 estimate-actual variance.

Covers :func:`eawf.workflow.estimation.metrics.compute_estimate_actual_variance`,
the C09 §5.9.6 M26 ``eawf_estimate_actual_variance_pct`` gauge.

Plus CLI dispatch smoke for ``eawf metrics variance``. Per AGENTS test
discipline: boundary (empty / single / off-by-one) AND error-path coverage;
float aggregates via :func:`pytest.approx`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import eawf.kernel.config.layered as layered
from eawf.kernel.state.enums import ActualStatus, Confidence, EffortBucket, WaveStatus
from eawf.kernel.state.models import ActualSummary, EstimateSummary, State, Wave
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain
from eawf.surfaces.cli.commands.metrics import _render_variance
from eawf.workflow.estimation.metrics import (
    EstimateActualVarianceMetric,
    compute_estimate_actual_variance,
    compute_weekly_burn,
)

runner = CliRunner()

_T0 = datetime(2026, 5, 1, tzinfo=UTC)


def _empty_state() -> State:
    """Return a minimal but valid State with no waves/estimates/actuals."""
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
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
    }
    return State.model_validate(payload)


def _wave(
    *,
    wave_id: str,
    status: WaveStatus = WaveStatus.CLOSED,
    effort_bucket: EffortBucket | None = None,
) -> Wave:
    """Return a CLOSED ``Wave`` carrying the fields the calibration relies on."""
    iter_id = "-".join(wave_id.split("-")[:2])
    closed = _T0 + timedelta(minutes=30) if status == WaveStatus.CLOSED else None
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title=f"wave {wave_id}",
        status=status,
        deps=[],
        blocks=[],
        file_scopes=[],
        success_criteria=[],
        effort_bucket=effort_bucket,
        opened_at=_T0,
        closed_at=closed,
    )


def _estimate(*, wave_id: str, expected_eu: float) -> EstimateSummary:
    return EstimateSummary(
        id=f"EST-{wave_id}",
        scope_id=wave_id,
        expected_eu=expected_eu,
        pessimistic_eu=expected_eu * 1.5,
        expected_minutes=expected_eu * 30.0,
        pessimistic_minutes=expected_eu * 45.0,
        display=f"{expected_eu} EU",
        reference_class="core_swe",
        confidence=Confidence.MEDIUM,
        current_store_record_id=f"REC-{wave_id}",
        updated_at=_T0,
    )


def _actual(
    *,
    wave_id: str,
    elapsed_eu: float,
    updated_at: datetime = _T0,
    calibration_excluded: bool = False,
) -> ActualSummary:
    return ActualSummary(
        id=f"ACT-{wave_id}",
        scope_id=wave_id,
        status=ActualStatus.DONE,
        elapsed_eu=elapsed_eu,
        current_store_record_id=f"REC-{wave_id}",
        updated_at=updated_at,
        calibration_excluded=calibration_excluded,
    )


# ---- compute_estimate_actual_variance (M26) --------------------------------


def test_compute_estimate_actual_variance_empty_state_is_none() -> None:
    """Boundary: no contributing wave yields a None variance gauge."""
    result = compute_estimate_actual_variance(_empty_state())
    assert result == EstimateActualVarianceMetric(
        sample_count=0,
        planned_eu=0.0,
        actual_eu=0.0,
        variance_pct=None,
    )


def test_compute_estimate_actual_variance_single_over_run_positive() -> None:
    """One CLOSED wave that ran 50 % over the estimate yields +50 %."""
    state = _empty_state()
    wave = _wave(wave_id="P01-I01-W01")
    state.waves[wave.id] = wave
    state.estimates = {wave.id: _estimate(wave_id=wave.id, expected_eu=1.0)}
    state.actuals = {wave.id: _actual(wave_id=wave.id, elapsed_eu=1.5)}

    result = compute_estimate_actual_variance(state)
    assert result.sample_count == 1
    assert result.planned_eu == pytest.approx(1.0)
    assert result.actual_eu == pytest.approx(1.5)
    assert result.variance_pct == pytest.approx(50.0)


def test_compute_estimate_actual_variance_under_run_negative() -> None:
    """A wave that finished under the estimate yields a negative variance."""
    state = _empty_state()
    wave = _wave(wave_id="P01-I01-W01")
    state.waves[wave.id] = wave
    state.estimates = {wave.id: _estimate(wave_id=wave.id, expected_eu=2.0)}
    state.actuals = {wave.id: _actual(wave_id=wave.id, elapsed_eu=1.0)}

    result = compute_estimate_actual_variance(state)
    assert result.variance_pct == pytest.approx(-50.0)


def test_compute_estimate_actual_variance_aggregates_over_waves() -> None:
    """Variance aggregates summed actual vs summed planned over the waves."""
    state = _empty_state()
    waves = {
        "P01-I01-W01": (1.0, 1.0),  # on estimate
        "P01-I01-W02": (1.0, 3.0),  # 2 EU over
    }
    estimates: dict[str, EstimateSummary] = {}
    actuals: dict[str, ActualSummary] = {}
    for wid, (planned, actual) in waves.items():
        state.waves[wid] = _wave(wave_id=wid)
        estimates[wid] = _estimate(wave_id=wid, expected_eu=planned)
        actuals[wid] = _actual(wave_id=wid, elapsed_eu=actual)
    state.estimates = estimates
    state.actuals = actuals

    result = compute_estimate_actual_variance(state)
    # planned=2.0, actual=4.0 -> (4-2)/2 * 100 = 100 %
    assert result.sample_count == 2
    assert result.variance_pct == pytest.approx(100.0)


def test_compute_estimate_actual_variance_drops_a_calibration_excluded_actual() -> None:
    """An excluded actual cannot move the M26 headline.

    Both waves are CLOSED with an estimate and an actual, so only the
    exclusion flag separates them. The excluded row ran 4 EU over; if the
    filter is removed the aggregate variance stops being 0 %.
    """
    state = _empty_state()
    clean = _wave(wave_id="P01-I01-W01")
    excluded = _wave(wave_id="P01-I01-W02")
    for wave in (clean, excluded):
        state.waves[wave.id] = wave
    state.estimates = {
        clean.id: _estimate(wave_id=clean.id, expected_eu=1.0),
        excluded.id: _estimate(wave_id=excluded.id, expected_eu=1.0),
    }
    state.actuals = {
        clean.id: _actual(wave_id=clean.id, elapsed_eu=1.0),
        excluded.id: _actual(wave_id=excluded.id, elapsed_eu=5.0, calibration_excluded=True),
    }

    result = compute_estimate_actual_variance(state)
    assert result.sample_count == 1
    assert result.planned_eu == pytest.approx(1.0)
    assert result.actual_eu == pytest.approx(1.0)
    assert result.variance_pct == pytest.approx(0.0)


def test_compute_weekly_burn_counts_a_calibration_excluded_actual() -> None:
    """Burn measures spend, so an excluded row still counts toward it.

    The flag disqualifies the figure as a reference class for estimating
    future work, not as a record of work already done — the mirror of the
    two estimate-quality metrics, which drop the same row.
    """
    state = _empty_state()
    wave = _wave(wave_id="P01-I01-W01")
    state.waves[wave.id] = wave
    state.actuals = {wave.id: _actual(wave_id=wave.id, elapsed_eu=3.0, calibration_excluded=True)}

    result = compute_weekly_burn(state, now=_T0)
    assert result.consumed_eu == pytest.approx(3.0)


def test_compute_estimate_actual_variance_excludes_non_closed_and_missing() -> None:
    """Error/boundary: in-progress + estimate-only waves drop out of the gauge."""
    state = _empty_state()
    in_progress = _wave(wave_id="P01-I01-W01", status=WaveStatus.IN_PROGRESS)
    estimate_only = _wave(wave_id="P01-I01-W02")
    counted = _wave(wave_id="P01-I01-W03")
    for wave in (in_progress, estimate_only, counted):
        state.waves[wave.id] = wave
    state.estimates = {
        in_progress.id: _estimate(wave_id=in_progress.id, expected_eu=1.0),
        estimate_only.id: _estimate(wave_id=estimate_only.id, expected_eu=1.0),
        counted.id: _estimate(wave_id=counted.id, expected_eu=1.0),
    }
    # Only the in-progress + counted waves have actuals; estimate_only has none.
    state.actuals = {
        in_progress.id: _actual(wave_id=in_progress.id, elapsed_eu=5.0),
        counted.id: _actual(wave_id=counted.id, elapsed_eu=2.0),
    }

    result = compute_estimate_actual_variance(state)
    # Only "counted" contributes: (2-1)/1 * 100 = 100 %.
    assert result.sample_count == 1
    assert result.variance_pct == pytest.approx(100.0)


# ---- CLI dispatch smoke -----------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the global config layer at an empty tmp file + clear EA_STATE."""
    fake_global = tmp_path / "global-config.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.delenv("EA_STATE", raising=False)


def _write_state(tmp_path: Path) -> Path:
    """Persist a state.json with one over-run CLOSED wave + an M-bucket actual."""
    workspace = tmp_path / "ws"
    ea_dir = workspace / ".ea"
    ea_dir.mkdir(parents=True)
    (ea_dir / "config.yaml").write_text(yaml.safe_dump({"schema_version": "1.0"}), encoding="utf-8")

    state = _empty_state()
    wid = "P01-I01-W01"
    state.waves[wid] = _wave(wave_id=wid, effort_bucket=EffortBucket.M)
    state.estimates = {wid: _estimate(wave_id=wid, expected_eu=1.0)}
    state.actuals = {
        wid: _actual(
            wave_id=wid,
            elapsed_eu=1.5,
            updated_at=datetime.now(UTC),
        )
    }
    (ea_dir / "state.json").write_text(state.model_dump_json(), encoding="utf-8")
    return workspace


def test_cli_metrics_variance_emits_gauge(tmp_path: Path) -> None:
    """``--json metrics variance`` emits the M26 gauge payload from state.json."""
    workspace = _write_state(tmp_path)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics", "variance"])
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.stdout)
    assert payload["sample_count"] == 1
    assert payload["variance_pct"] == pytest.approx(50.0)


def test_cli_metrics_variance_not_found_exits_one(tmp_path: Path) -> None:
    """Error path: no state.json -> NotFound exit 1."""
    workspace = tmp_path / "no-state"
    workspace.mkdir()
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "variance"])
    assert result.exit_code == 1


def test_meas_018_variance_labels_the_mapping_revisions_it_spans() -> None:
    """A roll-up over estimates of two revisions names both instead of blending silently."""
    state = _empty_state()
    for index, revision in enumerate((2, None), 1):
        wave = _wave(wave_id=f"P01-I01-W0{index}")
        state.waves[wave.id] = wave
        estimate = _estimate(wave_id=wave.id, expected_eu=1.0)
        state.estimates = {
            **(state.estimates or {}),
            wave.id: estimate.model_copy(update={"mapping_revision": revision}),
        }
        state.actuals = {
            **(state.actuals or {}),
            wave.id: _actual(wave_id=wave.id, elapsed_eu=1.0),
        }

    result = compute_estimate_actual_variance(state)

    assert result.mapping_revisions == ["2", "unrecorded"]
    assert "across mapping revisions 2, unrecorded" in _render_variance(
        result.variance_pct, result.sample_count, result.mapping_revisions
    )
    assert "across" not in _render_variance(0.0, 1, ["2"])


def _refit_answer(result: str, **fields: Any) -> dict[str, Any]:
    """Return a daemon re-fit answer: the shipped revision, not due, one exclusion."""
    return {
        "result": result,
        "revision": 2,
        "digest": "sha256:" + "0" * 64,
        "outcome": {
            "disposition": "not_due",
            "current_revision": 2,
            "current_digest": "sha256:" + "0" * 64,
            "eligible_count": 1,
            "excluded": [{"reason": "flagged_excluded", "count": 1}],
            "not_due": ["sample_below_minimum"],
        },
        **fields,
    }


def test_meas_020_023_cli_metrics_refit_forwards_to_the_daemon_verb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json metrics refit`` sends the actor and key, and prints what the daemon did."""
    sent: list[tuple[str, dict[str, Any]]] = []

    def answer(method: str, params: dict[str, Any], **_options: Any) -> dict[str, Any]:
        sent.append((method, params))
        return _refit_answer("not_due")

    monkeypatch.setattr(domain, "_native_answer", answer)
    result = runner.invoke(
        app,
        ["--json", "metrics", "refit", "--actor", "OP-0001", "--idempotency-key", "refit-7"],
    )

    assert result.exit_code == 0, result.output
    assert sent == [
        ("runtime.estimation.refit", {"actor": "OP-0001", "idempotency_key": "refit-7"})
    ]
    import json

    payload = json.loads(result.stdout)
    assert payload["result"] == "not_due"
    assert payload["outcome"]["excluded"] == [{"reason": "flagged_excluded", "count": 1}]


def test_cli_metrics_refit_renders_plain_text_and_keys_by_the_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The text form names the result, the revision and the exclusions."""
    sent: list[dict[str, Any]] = []

    def answer(method: str, params: dict[str, Any], **_options: Any) -> dict[str, Any]:
        sent.append(params)
        return _refit_answer("decision_opened", action_ref="eawf://W/P/R/pending-action/ACT-0001")

    monkeypatch.setattr(domain, "_native_answer", answer)
    result = runner.invoke(app, ["metrics", "refit", "--actor", "OP-0001"])

    assert result.exit_code == 0, result.output
    assert sent[0]["idempotency_key"] == f"refit-{datetime.now(UTC):%Y-%m-%d}"
    assert "effort mapping: decision_opened, revision 2 in force" in result.stdout
    assert "operator decision: eawf://W/P/R/pending-action/ACT-0001" in result.stdout
    assert "excluded: flagged_excluded=1" in result.stdout


def test_cli_metrics_refit_without_an_actor_exits_one() -> None:
    """Error path: a re-fit may file an operator decision, so it names who asks."""
    result = runner.invoke(app, ["metrics", "refit"])
    assert result.exit_code == 1


def test_cli_metrics_refit_maps_a_daemon_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Error path: the daemon's refusal becomes the CLI's validation exit."""

    def refuse(method: str, params: dict[str, Any], **_options: Any) -> dict[str, Any]:
        raise DaemonRpcError(cli_errors.RPC_VALIDATION_FAILED, "validation_failed: refit_raced")

    monkeypatch.setattr(domain, "_native_answer", refuse)
    result = runner.invoke(app, ["metrics", "refit", "--actor", "OP-0001"])
    assert result.exit_code == 2
