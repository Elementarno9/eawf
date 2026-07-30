"""Unit tests for the M26 estimate-actual variance + bucket calibration (P27-I01-W25).

Covers three deliverables:

1. :func:`eawf.workflow.estimation.metrics.compute_estimate_actual_variance` — the
   C09 §5.9.6 M26 ``eawf_estimate_actual_variance_pct`` gauge.
2. :func:`eawf.workflow.estimation.buckets.calibrate_buckets` — the XS..XL re-fit from
   90-day actuals, including the >25 % drift nudge and its boundary.
3. :class:`eawf.surfaces.tui.widgets.variance_tile.VarianceTile` — the colour-
   banded M26 tile render.

Plus CLI dispatch smoke for ``eawf metrics variance`` and ``eawf calibrate
buckets``. Per AGENTS test discipline: boundary (empty / single / off-by-
one) AND error-path coverage; float aggregates via :func:`pytest.approx`.
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
from eawf.surfaces.cli.app import app
from eawf.surfaces.tui.widgets.variance_tile import (
    EMPTY_STATE,
    VarianceTile,
    band_var,
    render_variance_markup,
    render_variance_plain,
)
from eawf.workflow.estimation.buckets import (
    BUCKET_EU,
    DRIFT_THRESHOLD,
    calibrate_buckets,
)
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


# ---- calibrate_buckets (90-day re-fit + >25 % nudge) -----------------------


def _row_for(state: State, bucket: EffortBucket, *, now: datetime = _T0):
    report = calibrate_buckets(state, now=now)
    return next(row for row in report.buckets if row.bucket == bucket)


def test_calibrate_buckets_empty_state_no_nudges() -> None:
    """Boundary: no actuals -> every bucket reports None fitted, no nudge."""
    report = calibrate_buckets(_empty_state(), now=_T0)
    assert report.window_days == 90
    assert report.drift_threshold_pct == pytest.approx(25.0)
    assert {row.bucket for row in report.buckets} == set(EffortBucket)
    assert all(row.fitted_eu is None and not row.nudge for row in report.buckets)
    assert report.nudged_buckets == []


def test_calibrate_buckets_within_tolerance_no_nudge() -> None:
    """A bucket whose fitted mean is on its configured centroid stays quiet."""
    state = _empty_state()
    # M bucket configured at 1.0 EU; seed actuals averaging exactly 1.0.
    for index, elapsed in enumerate((0.9, 1.0, 1.1)):
        wid = f"P01-I01-W0{index + 1}"
        state.waves[wid] = _wave(wave_id=wid, effort_bucket=EffortBucket.M)
        state.actuals = {**(state.actuals or {}), wid: _actual(wave_id=wid, elapsed_eu=elapsed)}

    row = _row_for(state, EffortBucket.M)
    assert row.sample_count == 3
    assert row.fitted_eu == pytest.approx(1.0)
    assert row.drift_pct == pytest.approx(0.0)
    assert row.nudge is False


def test_calibrate_buckets_drift_over_threshold_nudges() -> None:
    """Synthetic actuals 50 % over the M centroid fire a nudge (>25 % drift)."""
    state = _empty_state()
    # M configured at 1.0 EU; seed actuals averaging 1.5 -> 50 % drift.
    for index, elapsed in enumerate((1.4, 1.5, 1.6)):
        wid = f"P01-I01-W0{index + 1}"
        state.waves[wid] = _wave(wave_id=wid, effort_bucket=EffortBucket.M)
        state.actuals = {**(state.actuals or {}), wid: _actual(wave_id=wid, elapsed_eu=elapsed)}

    row = _row_for(state, EffortBucket.M)
    assert row.fitted_eu == pytest.approx(1.5)
    assert row.drift_pct == pytest.approx(50.0)
    assert row.nudge is True
    assert EffortBucket.M in calibrate_buckets(state, now=_T0).nudged_buckets


def test_calibrate_buckets_at_threshold_boundary_no_nudge() -> None:
    """Boundary: exactly 25 % drift does NOT nudge (strictly-greater gate)."""
    state = _empty_state()
    configured = BUCKET_EU[EffortBucket.M]
    # Drift exactly at the threshold: fitted = configured * (1 + 0.25).
    fitted_target = configured * (1.0 + DRIFT_THRESHOLD)
    wid = "P01-I01-W01"
    state.waves[wid] = _wave(wave_id=wid, effort_bucket=EffortBucket.M)
    state.actuals = {wid: _actual(wave_id=wid, elapsed_eu=fitted_target)}

    row = _row_for(state, EffortBucket.M)
    assert row.fitted_eu == pytest.approx(fitted_target)
    assert row.drift_pct == pytest.approx(25.0)
    assert row.nudge is False


def test_calibrate_buckets_excludes_out_of_window_actuals() -> None:
    """Actuals older than the 90-day window do not inform the re-fit."""
    state = _empty_state()
    stale = _T0 - timedelta(days=120)
    wid = "P01-I01-W01"
    state.waves[wid] = _wave(wave_id=wid, effort_bucket=EffortBucket.L)
    state.actuals = {wid: _actual(wave_id=wid, elapsed_eu=10.0, updated_at=stale)}

    row = _row_for(state, EffortBucket.L)
    assert row.sample_count == 0
    assert row.fitted_eu is None
    assert row.nudge is False


# ---- VarianceTile render ----------------------------------------------------


def test_variance_tile_render_none_is_empty_state() -> None:
    """A None variance renders the muted empty-state sentinel."""
    assert render_variance_plain(None) == EMPTY_STATE
    assert EMPTY_STATE in render_variance_markup(None)


def test_variance_tile_render_signed_value() -> None:
    """A positive variance renders a signed percentage; negative keeps its sign."""
    assert render_variance_plain(12.5) == "+12.5%"
    assert render_variance_plain(-3.0) == "-3.0%"


@pytest.mark.parametrize(
    ("variance_pct", "expected_var"),
    [(10.0, "$ok"), (-20.0, "$ok"), (40.0, "$warn"), (-49.9, "$warn"), (75.0, "$err")],
)
def test_variance_tile_band_var(variance_pct: float, expected_var: str) -> None:
    """The colour band keys off the absolute variance magnitude."""
    assert band_var(variance_pct) == expected_var


def test_variance_tile_set_variance_updates_reactive() -> None:
    """``set_variance`` drives the reactive value the watcher repaints from."""
    tile = VarianceTile()
    tile.set_variance(42.0)
    assert tile.variance_pct == pytest.approx(42.0)
    assert "+42.0%" in render_variance_markup(tile.variance_pct)


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


def test_cli_calibrate_buckets_renders_report(tmp_path: Path) -> None:
    """``--json calibrate buckets`` emits the per-bucket calibration verdict."""
    workspace = _write_state(tmp_path)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "calibrate", "buckets"])
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.stdout)
    assert payload["window_days"] == 90
    assert payload["drift_threshold_pct"] == pytest.approx(25.0)
    assert {row["bucket"] for row in payload["buckets"]} == {b.value for b in EffortBucket}


def test_cli_calibrate_buckets_not_found_exits_one(tmp_path: Path) -> None:
    """Error path: no state.json -> NotFound exit 1."""
    workspace = tmp_path / "no-state"
    workspace.mkdir()
    result = runner.invoke(app, ["-w", str(workspace), "calibrate", "buckets"])
    assert result.exit_code == 1


def test_cli_calibrate_apply_writes_bucket_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``calibrate apply`` routes the fitted centroid through config RPC."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    from eawf.surfaces.cli import _daemon_client as dc
    from eawf.surfaces.cli import _dispatch

    captured: dict[str, Any] = {}

    class _FakeConfigClient:
        def __enter__(self) -> _FakeConfigClient:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def config_set_layer_value(
            self,
            *,
            layer: str,
            key_path: list[str],
            value: Any,
            idempotency_key: str | None = None,
            repo_root: str | None = None,
        ) -> dict[str, Any]:
            captured["layer"] = layer
            captured["key_path"] = list(key_path)
            captured["value"] = value
            captured["repo_root"] = repo_root
            return {
                "layer": layer,
                "layer_path": "fake-path",
                "key_path": list(key_path),
                "value": value,
                "envelope": {"id": "CFG-calibrate"},
                "idempotent_replay": False,
            }

    monkeypatch.setattr(_dispatch, "ensure_daemon", lambda _runtime=None: 4242)
    monkeypatch.setattr(dc, "DaemonClient", _FakeConfigClient)
    workspace = _write_state(tmp_path)
    result = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(workspace),
            "calibrate",
            "apply",
            "--bucket",
            "m",
            "--scope",
            "workspace",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.stdout)
    assert payload["bucket"] == "M"
    assert payload["key"] == "estimation.buckets.overrides.M"
    assert payload["value"]["expected_eu"] == pytest.approx(1.5)
    assert payload["value"]["pessimistic_eu"] == pytest.approx(1.5)
    assert captured == {
        "layer": "workspace",
        "key_path": ["estimation", "buckets", "overrides", "M"],
        "value": {"expected_eu": 1.5, "pessimistic_eu": 1.5},
        "repo_root": str(workspace),
    }
    body = yaml.safe_load((workspace / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert "estimation" not in body


def test_cli_calibrate_apply_no_input_requires_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-input`` refuses config writes unless ``--yes`` is explicit."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    workspace = _write_state(tmp_path)
    result = runner.invoke(
        app,
        [
            "--no-input",
            "-w",
            str(workspace),
            "calibrate",
            "apply",
            "--bucket",
            "M",
            "--scope",
            "workspace",
        ],
    )
    assert result.exit_code != 0
    assert "refusing to apply bucket calibration" in result.output

    body = yaml.safe_load((workspace / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert "estimation" not in body
