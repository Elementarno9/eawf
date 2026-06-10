"""Tests for the wave-detail ``$`` (cost) tab + the shared cost helpers (W13).

Three layers: the pure cost projection
(:mod:`eawf.surfaces.tui.screens.overlays.detail_cost`) over hand-built
telemetry rollups, the store-backed join that feeds the tab from a seeded
metrics DB, and Pilot-driven painting of the ``$`` tab inside a mounted
:class:`~eawf.surfaces.tui.screens.overlays.detail.DetailModal`.

The success criterion (P30-I07-W13): a wave with metered
:class:`~eawf.kernel.state.models.SessionAttempt` rows renders the ``$`` tab
columns (attempt, model, in/out, cache cr/rd, cost, EU) with the four token
classes and an aggregate cost bar; an attempt with ``priced=false`` (billable
tokens but a zero priced cost) renders the em-dash sentinel plus an inert
un-billed marker; a wave with no metered session renders the honest "no
metered sessions yet" line.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import orjson
from textual.widgets import TabbedContent

from eawf.kernel.state.models import SessionAttempt, State
from eawf.observability.telemetry.join import WaveAttemptRollup, WaveSessionRollup
from eawf.observability.telemetry.models import TelemetrySession
from eawf.observability.telemetry.store import SqliteMetricsStore
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.detail import DetailModal, resolve_detail
from eawf.surfaces.tui.screens.overlays.detail_cost import (
    NO_METERED_SESSIONS,
    UNBILLED_MARKER,
    aggregate_cost_bar,
    aggregate_session_cost,
    attempt_is_priced,
    cost_tab_rows,
    render_cost_tile,
    wave_cost_rollup_for_wave,
    wave_cost_rows,
)
from eawf.surfaces.tui.snapshot import capture_screen_text
from eawf.surfaces.tui.widgets.eu_bar import EMPTY_STATE

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

_NOW = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


def _load(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


def _attempt(
    *,
    attempt: int = 1,
    runtime: str = "claude-code",
    session_id: str = "sess-1",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read_tokens: int = 80,
    cache_write_tokens: int = 20,
    cost_usd: Decimal = Decimal("0.0123"),
    attention_eu: float | None = 0.30,
) -> WaveAttemptRollup:
    """Return one joined per-attempt cost rollup row."""
    return WaveAttemptRollup(
        attempt=attempt,
        runtime=runtime,
        session_id=session_id,
        telemetry_session_id=session_id,
        duration_ms=540000,
        attention_eu=attention_eu,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cost_usd=cost_usd,
    )


def _rollup(*attempts: WaveAttemptRollup, wave_id: str = "P01-I01-W01") -> WaveSessionRollup:
    """Fold *attempts* into a wave rollup with a summed aggregate cost."""
    return WaveSessionRollup(
        wave_id=wave_id,
        attempts=list(attempts),
        cost_usd=sum((a.cost_usd for a in attempts), Decimal("0")),
        input_tokens=sum(a.input_tokens for a in attempts),
        output_tokens=sum(a.output_tokens for a in attempts),
        cache_read_tokens=sum(a.cache_read_tokens for a in attempts),
        cache_write_tokens=sum(a.cache_write_tokens for a in attempts),
    )


# --------------------------------------------------------------------------
# attempt_is_priced — priced vs un-priced classification
# --------------------------------------------------------------------------


def test_attempt_is_priced_true_for_positive_cost() -> None:
    assert attempt_is_priced(_attempt(cost_usd=Decimal("0.0001"))) is True


def test_attempt_is_priced_false_for_zero_cost_with_tokens() -> None:
    # Billable tokens but a zero priced cost — the model was un-priceable.
    assert attempt_is_priced(_attempt(cost_usd=Decimal("0"))) is False


# --------------------------------------------------------------------------
# cost_tab_rows — columns + four token classes + aggregate bar
# --------------------------------------------------------------------------


def test_cost_tab_rows_render_columns_and_four_token_classes() -> None:
    """A metered rollup renders every cost column + all four token classes."""
    rows = dict(cost_tab_rows(_rollup(_attempt())))
    table = rows["attempts"]
    # The eight ordered column headers appear.
    for header in ("att", "model", "in", "out", "cache cr", "cache rd", "cost", "eu"):
        assert header in table
    # The four token classes paint their values: input 100, output 50,
    # cache-create 20, cache-read 80.
    assert "100" in table
    assert "50" in table
    assert "20" in table
    assert "80" in table
    # The priced cost cell carries the four-decimal dollar figure.
    assert "$0.0123" in table
    # The EU cell carries the joined effort-unit value.
    assert "0.30 EU" in table


def test_cost_tab_rows_carry_aggregate_cost_bar_and_total() -> None:
    """The cost tab folds in an aggregate cost bar + the summed total."""
    rows = dict(
        cost_tab_rows(
            _rollup(
                _attempt(cost_usd=Decimal("0.02")),
                _attempt(attempt=2, session_id="sess-2", cost_usd=Decimal("0.06")),
            )
        )
    )
    # The aggregate total sums both attempts ($0.08).
    assert "$0.0800" in rows["total"]
    # The aggregate bar is a real bar (not the empty sentinel): the priciest
    # attempt ($0.06) is 75% of the $0.08 total.
    assert rows["cost"] != EMPTY_STATE
    assert "75%" in rows["cost"]


def test_cost_tab_rows_unpriced_attempt_renders_em_dash_and_inert_marker() -> None:
    """A priced=false attempt renders the em-dash sentinel + inert marker."""
    rows = dict(cost_tab_rows(_rollup(_attempt(cost_usd=Decimal("0")))))
    table = rows["attempts"]
    # The cost cell is the em-dash + the inert un-billed marker, never $0.
    assert "—" in table
    assert UNBILLED_MARKER in table
    assert "$0.00" not in table


def test_cost_tab_rows_empty_rollup_renders_no_metered_sessions_line() -> None:
    """A rollup with no joined attempt renders the honest absence line."""
    rows = cost_tab_rows(_rollup())
    assert rows == (("sessions", NO_METERED_SESSIONS),)


# --------------------------------------------------------------------------
# aggregate_cost_bar — empty-state guard
# --------------------------------------------------------------------------


def test_aggregate_cost_bar_zero_total_is_empty_state() -> None:
    """An all-zero (un-priced) aggregate surfaces the empty-state sentinel."""
    assert aggregate_cost_bar(_rollup(_attempt(cost_usd=Decimal("0")))) == EMPTY_STATE


# --------------------------------------------------------------------------
# wave_cost_rows — wave-shaped honest absence
# --------------------------------------------------------------------------


def _wave_with_sessions(state: State, wave_id: str, count: int) -> State:
    """Return *state* with *count* ended session attempts on *wave_id*."""
    sessions = {
        n: SessionAttempt(
            attempt=n,
            runtime="claude-code",
            session_id=f"sess-{n}",
            session_log_handle=f"urn:eawf:v1:session-log:claude-code:sess-{n}",
            started_at=_NOW,
            ended_at=_NOW + timedelta(minutes=5),
        )
        for n in range(1, count + 1)
    }
    rebuilt = state.waves[wave_id].model_copy(update={"sessions": sessions})
    new_waves = dict(state.waves)
    new_waves[wave_id] = rebuilt
    return state.model_copy(update={"waves": new_waves})


def test_wave_cost_rows_no_session_attempts_yields_empty_group() -> None:
    """A wave with no session attempts builds no cost tab (empty group)."""
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    assert state.waves[wave_id].sessions == {}
    assert wave_cost_rows(state.waves[wave_id], _rollup()) == ()


def test_wave_cost_rows_attempts_but_no_join_render_honest_absence() -> None:
    """A wave with attempts but no joined telemetry shows the absence line."""
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    state = _wave_with_sessions(state, wave_id, count=1)
    # ``None`` rollup (no telemetry DB) against a wave that DOES carry session
    # attempts surfaces the honest "no metered sessions yet" line.
    rows = wave_cost_rows(state.waves[wave_id], None)
    assert rows == (("sessions", NO_METERED_SESSIONS),)


# --------------------------------------------------------------------------
# render_cost_tile + aggregate_session_cost — the /metrics Cost tile
# --------------------------------------------------------------------------


def _telemetry_session(session_id: str, *, cost: Decimal) -> TelemetrySession:
    """Return one priced telemetry session row."""
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


def test_aggregate_session_cost_sums_cost_and_counts_rows() -> None:
    total, count = aggregate_session_cost(
        [
            _telemetry_session("s1", cost=Decimal("0.02")),
            _telemetry_session("s2", cost=Decimal("0.03")),
        ]
    )
    assert total == Decimal("0.05")
    assert count == 2


def test_aggregate_session_cost_empty_is_zero() -> None:
    assert aggregate_session_cost([]) == (Decimal("0"), 0)


def test_render_cost_tile_matches_aggregate() -> None:
    """The Cost tile body carries the summed total + the session count."""
    total, count = aggregate_session_cost(
        [
            _telemetry_session("s1", cost=Decimal("0.04")),
            _telemetry_session("s2", cost=Decimal("0.04")),
        ]
    )
    body = render_cost_tile(total, sample_count=count)
    assert "total $0.0800" in body
    assert "sessions 2" in body


def test_render_cost_tile_no_sessions_is_honest_absence() -> None:
    assert render_cost_tile(Decimal("0"), sample_count=0) == NO_METERED_SESSIONS


# --------------------------------------------------------------------------
# wave_cost_rollup_for_wave — store-backed join from a seeded metrics DB
# --------------------------------------------------------------------------


def _seed_metrics_db(state_path: Path, sessions: list[TelemetrySession]) -> None:
    """Write *sessions* to the project's telemetry DB sibling of *state_path*."""
    store = SqliteMetricsStore(state_path.parent / "telemetry.db")
    store.init_schema()
    for session in sessions:
        store.upsert("telemetry_sessions", session)
    store.commit()
    store.close()


def test_wave_cost_rollup_for_wave_joins_seeded_sessions(tmp_path: Path) -> None:
    """The store-backed join prices the wave's attempts from the metrics DB."""
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    state = _wave_with_sessions(state, wave_id, count=1)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))
    # Seed a priced telemetry session whose id matches the wave's attempt.
    _seed_metrics_db(state_path, [_telemetry_session("sess-1", cost=Decimal("0.05"))])
    rollup = wave_cost_rollup_for_wave(state, wave_id, state_path)
    assert rollup is not None
    assert len(rollup.attempts) == 1
    assert rollup.attempts[0].cost_usd == Decimal("0.05")
    assert rollup.cost_usd == Decimal("0.05")


def test_wave_cost_rollup_for_wave_missing_db_returns_none(tmp_path: Path) -> None:
    """No telemetry DB yields ``None`` so the cost tab folds the absence."""
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    state = _wave_with_sessions(state, wave_id, count=1)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    assert wave_cost_rollup_for_wave(state, wave_id, state_path) is None


# --------------------------------------------------------------------------
# DetailModal — the $ tab paints the columns + bar (Pilot)
# --------------------------------------------------------------------------


def test_detail_modal_cost_tab_paints_columns_and_bar() -> None:
    """The ``$`` tab paints the per-attempt cost columns + aggregate bar."""

    async def body() -> None:
        state = _load(_PHASE_ITER_WAVE)
        wave_id = next(iter(state.waves))
        state = _wave_with_sessions(state, wave_id, count=1)
        rollup = _rollup(_attempt(session_id="sess-1", cost_usd=Decimal("0.0123")))
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            card = resolve_detail(state, wave_id, cost_rollup=rollup)
            modal = DetailModal(card)
            app.push_screen(modal)
            await pilot.pause()
            await app.workers.wait_for_complete()
            tabs = modal.query_one(TabbedContent)
            tabs.active = "detail-tab-cost"
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = capture_screen_text(app)
            # The priced cost figure + the joined EU value paint.
            assert "$0.0123" in rendered
            assert "0.30 EU" in rendered

    asyncio.run(body())


def test_detail_modal_cost_tab_unpriced_paints_em_dash_and_marker() -> None:
    """A priced=false attempt paints the em-dash + inert un-billed marker."""

    async def body() -> None:
        state = _load(_PHASE_ITER_WAVE)
        wave_id = next(iter(state.waves))
        state = _wave_with_sessions(state, wave_id, count=1)
        rollup = _rollup(_attempt(session_id="sess-1", cost_usd=Decimal("0")))
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            card = resolve_detail(state, wave_id, cost_rollup=rollup)
            modal = DetailModal(card)
            app.push_screen(modal)
            await pilot.pause()
            await app.workers.wait_for_complete()
            modal.query_one(TabbedContent).active = "detail-tab-cost"
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = capture_screen_text(app)
            assert UNBILLED_MARKER in rendered

    asyncio.run(body())


def test_detail_modal_no_metered_sessions_paints_absence_line() -> None:
    """A wave with attempts but no join paints the honest absence line."""

    async def body() -> None:
        state = _load(_PHASE_ITER_WAVE)
        wave_id = next(iter(state.waves))
        state = _wave_with_sessions(state, wave_id, count=1)
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # ``None`` rollup against a wave that carries session attempts.
            card = resolve_detail(state, wave_id, cost_rollup=None)
            modal = DetailModal(card)
            app.push_screen(modal)
            await pilot.pause()
            await app.workers.wait_for_complete()
            modal.query_one(TabbedContent).active = "detail-tab-cost"
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = capture_screen_text(app)
            assert NO_METERED_SESSIONS in rendered

    asyncio.run(body())
