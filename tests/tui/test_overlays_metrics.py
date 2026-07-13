"""Tests for the C06 ``MetricsModal`` 3x2 dashboard overlay (P26-W21).

Two layers: pure :func:`parse_metrics_args` arg parsing + the
:data:`TILE_SPECS` grid-inventory contract (without Textual), and
Pilot-driven mounting of the six-tile grid through the ``/metrics`` palette
verb + the modal-stack cap.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import orjson
from rich.text import Text
from textual.widgets import Static

from eawf.kernel.state.enums import ActualStatus
from eawf.kernel.state.models import ActualSummary, State
from eawf.observability.telemetry.metrics_projection import (
    CacheHealthProjection,
    MetricsProjection,
    RoleCalibrationProjection,
    RuntimeTokensProjection,
    SwitchoverFrequencyProjection,
    VarianceBucketProjection,
    VarianceWaveProjection,
)
from eawf.observability.telemetry.models import TelemetrySession
from eawf.observability.telemetry.store import SqliteMetricsStore
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.palette.verbs import split_verb_args
from eawf.surfaces.tui.screens.overlays.metrics import (
    DEFAULT_WINDOW,
    METRIC_WINDOWS,
    METRICS_CAPTURE_LIVE,
    METRICS_HONEST_NEGATIVE,
    TILE_SPECS,
    CalibrationDrillModal,
    MetricsArgs,
    MetricsModal,
    VarianceDrillModal,
    _eu_capture_landed,
    parse_metrics_args,
    render_projection_tile,
    render_variance_drilldown,
    render_wave_elapsed_tile,
)
from eawf.workflow.estimation.buckets import BucketCalibration, CalibrationReport
from eawf.workflow.estimation.metrics import (
    EstimateActualVarianceMetric,
    WaveElapsedMetric,
    WeeklyBurnMetric,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


def _text(widget: Static) -> str:
    rendered = widget.render()
    return rendered.plain if isinstance(rendered, Text) else str(rendered)


def _load_state() -> State:
    return State.model_validate(orjson.loads(_PHASE_ITER_WAVE.read_bytes()))


# --------------------------------------------------------------------------
# TILE_SPECS — 4x2 grid inventory (D9 + P30-I07-W13 Cost tile)
# --------------------------------------------------------------------------


def test_tile_specs_count_is_seven() -> None:
    # 4x2 grid — six original tiles plus the W13 Cost tile.
    assert len(TILE_SPECS) == 7


def test_tile_specs_ids_are_unique() -> None:
    ids = [spec.tile_id for spec in TILE_SPECS]
    assert len(ids) == len(set(ids))


def test_tile_specs_cover_the_v7_metric_surface() -> None:
    titles = " ".join(spec.title.lower() for spec in TILE_SPECS)
    for needle in ("precision", "burn", "elapsed", "cost", "cache", "switchover", "role"):
        assert needle in titles


def test_tile_specs_carry_the_cost_tile() -> None:
    """The W13 Cost tile lands in the grid inventory with a stable id."""
    cost = next((spec for spec in TILE_SPECS if spec.tile_id == "tile-cost"), None)
    assert cost is not None
    assert cost.title == "Cost"


# --------------------------------------------------------------------------
# parse_metrics_args — window + scope flags
# --------------------------------------------------------------------------


def test_parse_metrics_args_empty_uses_defaults() -> None:
    args = parse_metrics_args("")
    assert args == MetricsArgs(window=DEFAULT_WINDOW, scope_filter=None)


def test_parse_metrics_args_window_flag() -> None:
    assert parse_metrics_args("--window 30d").window == "30d"
    assert parse_metrics_args("--window 90d").window == "90d"


def test_parse_metrics_args_unknown_window_falls_back_to_default() -> None:
    # A bogus window degrades to the default rather than raising.
    assert parse_metrics_args("--window 5y").window == DEFAULT_WINDOW


def test_parse_metrics_args_scope_flag() -> None:
    args = parse_metrics_args("--scope urn:eawf:v1:repo:eawf")
    assert args.scope_filter == "urn:eawf:v1:repo:eawf"


def test_parse_metrics_args_both_flags() -> None:
    args = parse_metrics_args("--window 30d --scope urn:x")
    assert args.window == "30d"
    assert args.scope_filter == "urn:x"


def test_parse_metrics_args_ignores_unknown_flags() -> None:
    # An unrecognised flag is ignored; recognised flags still parse.
    args = parse_metrics_args("--bogus z --window 90d")
    assert args.window == "90d"
    assert args.scope_filter is None


def test_metric_windows_are_the_three_v7_windows() -> None:
    assert METRIC_WINDOWS == ("7d", "30d", "90d")


# --------------------------------------------------------------------------
# tile-elapsed — local state-binding metric (W15)
# --------------------------------------------------------------------------


def test_render_wave_elapsed_tile_uses_compute_wave_elapsed() -> None:
    body = render_wave_elapsed_tile(_load_state())
    assert "median 0.0m" in body
    assert "samples 0" in body


def test_render_wave_elapsed_tile_none_keeps_placeholder() -> None:
    assert render_wave_elapsed_tile(None) == "[$text-muted]awaiting telemetry projection[/]"


def test_render_projection_tile_binds_all_six_metric_tiles() -> None:
    projection = MetricsProjection(
        scope="urn:eawf:v1:state:QR",
        window="7d",
        generated_at=datetime(2026, 5, 22, tzinfo=UTC),
        variance=EstimateActualVarianceMetric(
            sample_count=1,
            planned_eu=1.0,
            actual_eu=1.5,
            variance_pct=50.0,
        ),
        variance_by_bucket=(
            VarianceBucketProjection(
                bucket="M",
                sample_count=1,
                planned_eu=1.0,
                actual_eu=1.5,
                delta_eu=0.5,
                variance_pct=50.0,
                inside_pessimistic_share=1.0,
                waves=(
                    VarianceWaveProjection(
                        wave_id="P01-I01-W01",
                        title="Wave 1",
                        bucket="M",
                        planned_eu=1.0,
                        actual_eu=1.5,
                        delta_eu=0.5,
                        variance_pct=50.0,
                        inside_pessimistic=True,
                    ),
                ),
            ),
        ),
        weekly_burn=WeeklyBurnMetric(consumed_eu=1.5, target_eu=4.0, window_days=7),
        wave_elapsed=WaveElapsedMetric(
            sample_count=1,
            mean_minutes=30.0,
            median_minutes=30.0,
            max_minutes=30.0,
        ),
        cache_health=(
            CacheHealthProjection(
                runtime="claude",
                cache_read_tokens=80,
                cache_create_tokens=20,
                hit_ratio=0.8,
            ),
        ),
        switchover_frequency=(SwitchoverFrequencyProjection(cause="RUNTIME_TIMEOUT", count=1),),
        per_runtime_tokens=(
            RuntimeTokensProjection(
                runtime="claude",
                input_tokens=100,
                output_tokens=50,
                cache_read_tokens=80,
                cache_create_tokens=20,
            ),
        ),
        per_role_calibration=(
            RoleCalibrationProjection(
                agent_role="executor",
                report=CalibrationReport(
                    window_days=90,
                    drift_threshold_pct=25.0,
                    buckets=[
                        BucketCalibration(
                            bucket="M",
                            configured_eu=1.0,
                            fitted_eu=1.5,
                            fitted_pessimistic_eu=1.5,
                            sample_count=1,
                            drift_pct=50.0,
                            nudge=True,
                        )
                    ],
                ),
            ),
        ),
    )

    bodies = {spec.tile_id: render_projection_tile(projection, spec.tile_id) for spec in TILE_SPECS}
    assert "actual 1.50 EU" in bodies["tile-variance"]
    assert "target 4.00 EU" in bodies["tile-burn"]
    assert "median 30.0m" in bodies["tile-elapsed"]
    assert "claude 80%" in bodies["tile-cache"]
    assert "RUNTIME_TIMEOUT 1" in bodies["tile-switchover"]
    assert "executor" in bodies["tile-role-calibration"]
    assert "1.5!" in bodies["tile-role-calibration"]


def test_render_variance_drilldown_lists_buckets_and_waves() -> None:
    projection = MetricsProjection(
        scope="urn:eawf:v1:state:QR",
        window="7d",
        generated_at=datetime(2026, 5, 22, tzinfo=UTC),
        variance=EstimateActualVarianceMetric(
            sample_count=1,
            planned_eu=1.0,
            actual_eu=1.5,
            variance_pct=50.0,
        ),
        variance_by_bucket=(
            VarianceBucketProjection(
                bucket="M",
                sample_count=1,
                planned_eu=1.0,
                actual_eu=1.5,
                delta_eu=0.5,
                variance_pct=50.0,
                inside_pessimistic_share=1.0,
                waves=(
                    VarianceWaveProjection(
                        wave_id="P01-I01-W01",
                        title="Wave 1",
                        bucket="M",
                        planned_eu=1.0,
                        actual_eu=1.5,
                        delta_eu=0.5,
                        variance_pct=50.0,
                        inside_pessimistic=True,
                    ),
                ),
            ),
        ),
        weekly_burn=WeeklyBurnMetric(consumed_eu=1.5, target_eu=4.0, window_days=7),
        wave_elapsed=WaveElapsedMetric(
            sample_count=1,
            mean_minutes=30.0,
            median_minutes=30.0,
            max_minutes=30.0,
        ),
        cache_health=(),
        switchover_frequency=(),
        per_runtime_tokens=(),
        per_role_calibration=(),
    )

    body = render_variance_drilldown(projection)
    assert "M" in body
    assert "P01-I01-W01" in body
    assert "Wave 1" in body


# --------------------------------------------------------------------------
# MetricsModal — mounting + the /metrics verb (Pilot)
# --------------------------------------------------------------------------


def test_metrics_modal_mounts_seven_tiles() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal())
            await pilot.pause()
            assert isinstance(app.screen, MetricsModal)
            tiles = [app.screen.query_one(f"#{spec.tile_id}", Static) for spec in TILE_SPECS]
            assert len(tiles) == 7
            assert tiles[0].border_title == TILE_SPECS[0].title
            assert tiles[-1].border_title == "Role calibration"
            assert "median 0.0m" in _text(app.screen.query_one("#tile-elapsed", Static))
            # The Cost tile mounts and, with no telemetry DB, reads the honest
            # absence line rather than a fabricated dollar figure.
            assert "no metered sessions yet" in _text(app.screen.query_one("#tile-cost", Static))

    asyncio.run(body())


_NOW = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


def _telemetry_session(session_id: str, *, cost: Decimal) -> TelemetrySession:
    """Return one priced telemetry session row for the Cost-tile seed."""
    return TelemetrySession(
        session_id=session_id,
        project_id="urn:eawf:v1:state:QR",
        runtime="claude",
        wave_id="P01-I01-W01",
        attempt_id="a1",
        session_log_path=f"claude/{session_id}.jsonl",
        started_at=_NOW,
        ended_at=_NOW + timedelta(minutes=9),
        duration_ms=540000,
        model_primary="claude-model",
        total_input_tokens=100,
        total_output_tokens=50,
        total_cache_read=80,
        total_cache_write=20,
        total_cost_usd=cost,
        end_marker="clean_stop",
    )


def _seed_state_with_metrics_db(tmp_path: Path, sessions: list[TelemetrySession]) -> Path:
    """Copy the fixture state to *tmp_path* + seed a sibling telemetry DB."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(_PHASE_ITER_WAVE.read_bytes())
    store = SqliteMetricsStore(state_path.parent / "telemetry.db")
    store.init_schema()
    for session in sessions:
        store.upsert("telemetry_sessions", session)
    store.commit()
    store.close()
    return state_path


def test_metrics_cost_tile_matches_dollar_tab_aggregate(tmp_path: Path) -> None:
    """The Cost tile sums the same priced cost the wave-detail $ tab quotes."""
    from eawf.surfaces.tui.screens.overlays.detail_cost import (
        aggregate_session_cost,
        render_cost_tile,
    )

    sessions = [
        _telemetry_session("s1", cost=Decimal("0.02")),
        _telemetry_session("s2", cost=Decimal("0.03")),
    ]
    state_path = _seed_state_with_metrics_db(tmp_path, sessions)
    # The expected tile body is the shared aggregation the $ tab also reads.
    total, count = aggregate_session_cost(sessions)
    expected = render_cost_tile(total, sample_count=count)
    assert "total $0.0500" in expected

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal())
            await pilot.pause()
            cost_tile = _text(app.screen.query_one("#tile-cost", Static))
            assert cost_tile == expected
            assert "total $0.0500" in cost_tile
            assert "sessions 2" in cost_tile

    asyncio.run(body())


def test_metrics_modal_heading_shows_window() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal(MetricsArgs(window="30d", scope_filter=None)))
            await pilot.pause()
            heading = _text(app.screen.query_one(".metrics-title", Static))
            assert "window 30d" in heading

    asyncio.run(body())


def test_metrics_verb_opens_modal_through_cap() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.palette.verbs import VERBS

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            handler = next(v.handler for v in VERBS if v.name == "/metrics")
            handler(app, "--window 90d")
            await pilot.pause()
            assert isinstance(app.screen, MetricsModal)
            assert app.modal_depth() == 1
            assert "window 90d" in _text(app.screen.query_one(".metrics-title", Static))

    asyncio.run(body())


def test_metrics_modal_esc_closes() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal())
            await pilot.pause()
            assert app.modal_depth() == 1
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_metrics_modal_enter_opens_role_calibration_drilldown() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal())
            await pilot.pause()
            assert isinstance(app.screen, MetricsModal)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, CalibrationDrillModal)
            assert app.modal_depth() == 2
            assert "Role calibration" in _text(app.screen.query_one(".calibration-title", Static))

    asyncio.run(body())


def test_metrics_modal_enter_opens_variance_drilldown() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal())
            await pilot.pause()
            assert isinstance(app.screen, MetricsModal)
            app.screen.selected = 0
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, VarianceDrillModal)
            assert app.modal_depth() == 2
            assert "Variance by bucket" in _text(app.screen.query_one(".variance-title", Static))

    asyncio.run(body())


def test_metrics_verb_args_split() -> None:
    name, args = split_verb_args("/metrics --window 30d")
    assert name == "/metrics"
    assert args == "--window 30d"


def test_metrics_honest_line_has_top_margin() -> None:
    # W15 polish (reskinned in W23): the footer block sits flush against the
    # tile grid without a gap; a top margin separates it. The cosmic-terminal
    # reskin pins the frozen honest-negative line directly under the grid, so
    # the gap-separation now rides that line (the hint sits flush under it).
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal())
            await pilot.pause()
            honest = app.screen.query_one(".metrics-honest", Static)
            assert honest.styles.margin.top == 1

    asyncio.run(body())


# --------------------------------------------------------------------------
# W29: the honest-negative footer is conditional on captured EU
# --------------------------------------------------------------------------


def _state_with_captured_eu() -> State:
    """Return the fixture state with one wave carrying positive elapsed EU."""
    base = _load_state()
    wave_id = next(iter(base.waves))
    actual = ActualSummary(
        id="ACT-cap",
        scope_id=wave_id,
        status=ActualStatus.DONE,
        elapsed_eu=2.0,
        current_store_record_id="ACT-REC-cap",
        updated_at=_NOW,
    )
    return base.model_copy(update={"actuals": {wave_id: actual}})


def test_eu_capture_landed_false_for_uncaptured_state() -> None:
    # The fixture has no captured runtime, so the dashboard stays honestly dark.
    assert _eu_capture_landed(_load_state()) is False
    assert _eu_capture_landed(None) is False


def test_eu_capture_landed_true_with_positive_elapsed_eu() -> None:
    assert _eu_capture_landed(_state_with_captured_eu()) is True


def _state_with_stale_capture_only(*, recent_zero_eu: int) -> State:
    """Return a state whose only captured EU is older than *recent_zero_eu* closes.

    The historical wave carries real EU; every wave closed since captured none --
    the exact shape of a capture path that died silently.
    """
    base = _load_state()
    wave_id = next(iter(base.waves))
    actuals = {
        wave_id: ActualSummary(
            id="ACT-old",
            scope_id=wave_id,
            status=ActualStatus.DONE,
            elapsed_eu=2.0,
            current_store_record_id="ACT-REC-old",
            updated_at=_NOW - timedelta(days=90),
        )
    }
    for index in range(recent_zero_eu):
        scope_id = f"P00-I01-W{index + 50:02d}"
        actuals[scope_id] = ActualSummary(
            id=f"ACT-new-{index:02d}",
            scope_id=scope_id,
            status=ActualStatus.DONE,
            elapsed_eu=0.0,
            current_store_record_id=f"ACT-REC-new-{index:02d}",
            updated_at=_NOW - timedelta(minutes=index),
        )
    return base.model_copy(update={"actuals": actuals})


def test_eu_capture_landed_false_when_only_stale_actuals_carry_eu() -> None:
    # Regression guard: the banner must go dark when new waves stop capturing,
    # rather than being pinned "live" forever by a few historical waves.
    assert _eu_capture_landed(_state_with_stale_capture_only(recent_zero_eu=12)) is False


def test_eu_capture_landed_true_while_the_captured_wave_is_still_recent() -> None:
    # ... and stay live while the captured wave is still inside the window.
    assert _eu_capture_landed(_state_with_stale_capture_only(recent_zero_eu=3)) is True


def test_metrics_footer_is_honest_negative_until_capture() -> None:
    # Default reality: nothing captured, so the pinned honest-negative banner.
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal())
            await pilot.pause()
            footer = _text(app.screen.query_one("#metrics-honest", Static))
            assert METRICS_HONEST_NEGATIVE in footer
            assert METRICS_CAPTURE_LIVE not in footer

    asyncio.run(body())


def test_metrics_footer_flips_to_capture_live(tmp_path: Path) -> None:
    # Once a wave captures runtime EU, the footer drops the dark banner and
    # affirms the tiles are measured -- no more pinned honest-negative.
    state_path = tmp_path / "state.json"
    state_path.write_text(_state_with_captured_eu().model_dump_json(), encoding="utf-8")

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(MetricsModal())
            await pilot.pause()
            footer = _text(app.screen.query_one("#metrics-honest", Static))
            assert METRICS_CAPTURE_LIVE in footer
            assert METRICS_HONEST_NEGATIVE not in footer

    asyncio.run(body())
