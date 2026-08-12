"""Tests for the ``runtime.capture`` daemon RPC."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state import codex_lifecycle, runtime_capture
from eawf.runtime.daemon.session import resolve_session_log
from eawf.surfaces.tui.screens.overlays.detail_cost import (
    NO_METERED_SESSIONS,
    cost_tab_rows,
    wave_cost_rollup_for_wave,
)
from eawf.workflow.lifecycle.wave import compute_runtime_delta

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def _state_payload(*, active_wave_ids: list[str]) -> dict[str, Any]:
    iter_id = "P30-I05"
    phase_id = "P30"
    waves: dict[str, Any] = {}
    for wave_id in active_wave_ids or ["P30-I05-W04"]:
        waves[wave_id] = {
            "id": wave_id,
            "iter_id": iter_id,
            "title": "capture runtime",
            "status": "claimed",
            "claim_session_id": "session-1",
            "opened_at": _now().isoformat(),
            "claimed_at": _now().isoformat(),
            # Every real claim stamps a baseline, and the
            # interactive attempt row is the wave's delta AGAINST that baseline
            # (P30-I25-W31) -- a baseline-less wave has no wave-scoped truth to
            # record, so it mints no attempt. Claim-time zero origin here.
            "runtime_baseline": {
                "api_duration_ms": 0,
                "total_duration_ms": 0,
                "cost_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "captured_at": _now().isoformat(),
            },
            "sessions": {},
        }
    return {
        "schema_version": "1.9",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {
            "project_code": "ABC",
            "phase_id": phase_id,
            "iter_id": iter_id,
            "active_wave_ids": active_wave_ids,
        },
        "workspace": None,
        "phases": {
            phase_id: {
                "id": phase_id,
                "scope_id": "ABC",
                "track_id": None,
                "title": "Runtime capture",
                "status": "active",
                "iter_ids": [iter_id],
                "outcome_ids": [],
                "opened_at": _now().isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            iter_id: {
                "id": iter_id,
                "phase_id": phase_id,
                "title": "Harness EU",
                "status": "active",
                "wave_ids": list(waves),
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _now().isoformat(),
                "closed_at": None,
            }
        },
        "waves": waves,
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _ctx(tmp_path: Path, payload: dict[str, Any]) -> tuple[MethodContext, Path]:
    state_path = tmp_path / "state.json"
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    ctx = MethodContext(
        started_at="2026-06-10T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=store_path(state_path, StoreKind.EVENT),
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )
    return ctx, state_path


def _run(body: Any) -> None:
    asyncio.run(body())


def _capture_params(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "api_duration_ms": 17000,
        "total_duration_ms": 21000,
        "cost_usd": "0.42",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 7,
        "session_id": "session-1",
        "captured_at": _now().isoformat(),
    }
    payload.update(overrides)
    return payload


def test_runtime_capture_updates_one_active_wave(tmp_path: Path) -> None:
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=["P30-I05-W04"]))

    async def body() -> None:
        result = await runtime_capture(ctx, _capture_params())

        assert result["active_wave_ids"] == ["P30-I05-W04"]
        assert result["active_count"] == 1
        payload = orjson.loads(state_path.read_bytes())
        latest = payload["waves"]["P30-I05-W04"]["runtime_latest"]
        assert latest["api_duration_ms"] == 17000
        assert latest["cost_usd"] == 0.42
        assert latest["captured_at"] == "2026-06-10T12:00:00Z"

    _run(body)


def test_runtime_capture_refuses_ambiguous_active_waves(tmp_path: Path) -> None:
    active_wave_ids = ["P30-I05-W03", "P30-I05-W04"]
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=active_wave_ids))
    before = state_path.read_bytes()

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="correlation ambiguous"):
            await runtime_capture(ctx, _capture_params())
        assert state_path.read_bytes() == before

    _run(body)


def test_runtime_capture_threads_harness_and_model_attribution(tmp_path: Path) -> None:
    """W25: the runtime-capture writer threads harness+model onto RuntimeLatest.

    W19 added the parser-stamped ``harness`` + ``model`` attribution to the
    capture params, but the daemon writer dropped them when persisting. With the
    wiring in place the persisted ``runtime_latest`` carries NON-NULL attribution
    so a recorded actual derived from it is calibratable by harness+model.
    """
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=["P30-I05-W04"]))

    async def body() -> None:
        await runtime_capture(
            ctx,
            _capture_params(harness="claude-code", model="claude-opus-4-1"),
        )

        payload = orjson.loads(state_path.read_bytes())
        latest = payload["waves"]["P30-I05-W04"]["runtime_latest"]
        assert latest["harness"] == "claude-code"
        assert latest["model"] == "claude-opus-4-1"

    _run(body)


def test_runtime_capture_attribution_defaults_null_when_absent(tmp_path: Path) -> None:
    """Boundary: a capture with no harness/model persists null attribution.

    The fields stay nullable -- a payload that supplies no attribution does not
    fabricate a value, so the persisted snapshot carries ``None`` for both.
    """
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=["P30-I05-W04"]))

    async def body() -> None:
        await runtime_capture(ctx, _capture_params())

        payload = orjson.loads(state_path.read_bytes())
        latest = payload["waves"]["P30-I05-W04"]["runtime_latest"]
        assert latest["harness"] is None
        assert latest["model"] is None

    _run(body)


def test_explicit_wave_correlation_never_fans_capture(tmp_path: Path) -> None:
    active_wave_ids = ["P30-I05-W03", "P30-I05-W04"]
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=active_wave_ids))

    async def body() -> None:
        await runtime_capture(ctx, _capture_params(wave_id="P30-I05-W04"))

        state = State.model_validate(orjson.loads(state_path.read_bytes()))
        assert state.waves["P30-I05-W03"].runtime_latest is None
        wave = state.waves["P30-I05-W04"]
        assert wave.runtime_latest is not None
        assert wave.runtime_latest.shared_wave_count == 1

    _run(body)


def test_codex_provider_session_binding_correlates_one_wave(tmp_path: Path) -> None:
    active_wave_ids = ["P30-I05-W03", "P30-I05-W04"]
    payload = _state_payload(active_wave_ids=active_wave_ids)
    payload["agent_sessions"] = {
        "SES-01": {
            "id": "SES-01",
            "role": "executor",
            "runtime": "codex",
            "runtime_session_id": "provider-session-1",
            "scope_id": "P30-I05-W04",
            "status": "active",
            "claimed_wave_ids": ["P30-I05-W04"],
            "started_at": _now().isoformat(),
        }
    }
    ctx, state_path = _ctx(tmp_path, payload)

    async def body() -> None:
        await runtime_capture(
            ctx,
            _capture_params(
                harness="codex",
                session_id="provider-session-1",
            ),
        )
        state = State.model_validate(orjson.loads(state_path.read_bytes()))
        assert state.waves["P30-I05-W03"].runtime_latest is None
        assert state.waves["P30-I05-W04"].runtime_latest is not None

    _run(body)


def test_one_wave_alone_in_its_session_records_all_of_it(tmp_path: Path) -> None:
    """Boundary: the divisor is 1 for an unshared session -- no runtime is lost."""
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=["P30-I05-W04"]))

    async def body() -> None:
        await runtime_capture(ctx, _capture_params())

        state = State.model_validate(orjson.loads(state_path.read_bytes()))
        wave = state.waves["P30-I05-W04"]
        assert wave.runtime_latest is not None
        assert wave.runtime_latest.shared_wave_count == 1

        delta = compute_runtime_delta(
            wave.runtime_baseline,
            wave.runtime_latest,
            carry=wave.runtime_carry,
            eu_minutes=30.0,
        )
        assert delta is not None
        assert delta.shared_wave_count == 1
        assert delta.api_duration_ms == 17000
        assert delta.actual_cost_usd == pytest.approx(0.42)

    _run(body)


def test_explicit_wave_capture_stays_single_when_sibling_closes(tmp_path: Path) -> None:
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=["P30-I05-W03", "P30-I05-W04"]))

    async def body() -> None:
        await runtime_capture(ctx, _capture_params(wave_id="P30-I05-W04"))
        # The sibling closes; the next capture sees this wave on its own.
        payload = orjson.loads(state_path.read_bytes())
        payload["current"]["active_wave_ids"] = ["P30-I05-W04"]
        state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))

        await runtime_capture(ctx, _capture_params(api_duration_ms=25000))

        state = State.model_validate(orjson.loads(state_path.read_bytes()))
        latest = state.waves["P30-I05-W04"].runtime_latest
        assert latest is not None
        assert latest.shared_wave_count == 1

    _run(body)


def test_runtime_capture_refuses_without_active_waves(tmp_path: Path) -> None:
    ctx, _state_path = _ctx(tmp_path, _state_payload(active_wave_ids=[]))

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="requires active waves"):
            await runtime_capture(ctx, _capture_params())

    _run(body)


def test_runtime_capture_params_forbid_extra_keys(tmp_path: Path) -> None:
    ctx, _state_path = _ctx(tmp_path, _state_payload(active_wave_ids=["P30-I05-W04"]))

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="extra"):
            await runtime_capture(ctx, _capture_params(unexpected=True))

    _run(body)


def _codex_agent_session_payload(*, ambiguous: bool = False) -> dict[str, Any]:
    session = {
        "id": "SES-CODEX-01",
        "role": "executor",
        "runtime": "codex",
        "scope_id": "P30-I05-W04",
        "status": "active",
        "claimed_wave_ids": ["P30-I05-W04"],
        "started_at": _now().isoformat(),
    }
    sessions = {"SES-CODEX-01": session}
    if ambiguous:
        sessions["SES-CODEX-02"] = session | {
            "id": "SES-CODEX-02",
            "scope_id": "P30-I05-W03",
            "claimed_wave_ids": ["P30-I05-W03"],
        }
    return sessions


def test_codex_lifecycle_correlates_timed_subagent_attempt(tmp_path: Path) -> None:
    payload = _state_payload(active_wave_ids=["P30-I05-W03", "P30-I05-W04"])
    payload["agent_sessions"] = _codex_agent_session_payload()
    ctx, state_path = _ctx(tmp_path, payload)
    transcript = tmp_path / "agent-rollout.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"type":"session_meta","payload":{"id":"agent-01"}}',
                (
                    '{"type":"event_msg","payload":{"type":"token_count","info":'
                    '{"total_token_usage":{"input_tokens":120,'
                    '"cached_input_tokens":20,"output_tokens":30,'
                    '"reasoning_output_tokens":5}}}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    async def body() -> None:
        bound = await codex_lifecycle(
            ctx,
            {
                "event_type": "session_start",
                "provider_session_id": "provider-session-01",
                "occurred_at": _now().isoformat(),
            },
        )
        assert bound["correlated"] is True
        started_at = _now() + timedelta(minutes=1)
        started = await codex_lifecycle(
            ctx,
            {
                "event_type": "subagent_start",
                "provider_session_id": "provider-session-01",
                "agent_id": "agent-01",
                "occurred_at": started_at.isoformat(),
            },
        )
        assert started["wave_id"] == "P30-I05-W04"
        assert started["attempt"] == 1
        stopped_at = _now() + timedelta(minutes=3)
        stopped = await codex_lifecycle(
            ctx,
            {
                "event_type": "subagent_stop",
                "provider_session_id": "provider-session-01",
                "agent_id": "agent-01",
                "agent_transcript_path": str(transcript),
                "occurred_at": stopped_at.isoformat(),
                "counters": {
                    "input_tokens": 100,
                    "output_tokens": 35,
                    "cache_read_input_tokens": 20,
                    "harness": "codex",
                    "measure_version": 201,
                },
                "measurement_quality": "exact",
                "measurement_status": "usage_observed",
            },
        )
        assert stopped["correlated"] is True

        state = State.model_validate(orjson.loads(state_path.read_bytes()))
        agent_session = state.agent_sessions["SES-CODEX-01"]
        assert agent_session.runtime_session_id == "provider-session-01"
        attempt = state.waves["P30-I05-W04"].sessions[1]
        assert attempt.started_at == started_at
        assert attempt.ended_at == stopped_at
        assert attempt.input_tokens == 100
        assert attempt.output_tokens == 35
        assert attempt.cache_read_input_tokens == 20
        assert attempt.cache_creation_input_tokens is None
        assert attempt.cost_usd is None
        assert attempt.measurement_quality.value == "exact"
        assert attempt.measurement_status.value == "usage_observed"
        assert str(transcript) not in attempt.session_log_handle
        assert resolve_session_log(attempt.session_log_handle) == transcript

    _run(body)


def test_codex_lifecycle_refuses_ambiguous_session_binding(tmp_path: Path) -> None:
    payload = _state_payload(active_wave_ids=["P30-I05-W03", "P30-I05-W04"])
    payload["agent_sessions"] = _codex_agent_session_payload(ambiguous=True)
    ctx, state_path = _ctx(tmp_path, payload)

    async def body() -> None:
        result = await codex_lifecycle(
            ctx,
            {
                "event_type": "session_start",
                "provider_session_id": "provider-session-01",
                "occurred_at": _now().isoformat(),
            },
        )
        assert result["correlated"] is False
        assert result["reason"] == "provider_session_target_ambiguous"
        state = State.model_validate(orjson.loads(state_path.read_bytes()))
        assert all(session.runtime_session_id is None for session in state.agent_sessions.values())
        assert all(not wave.sessions for wave in state.waves.values())

    _run(body)


def test_codex_lifecycle_stop_without_start_does_not_invent_attempt(
    tmp_path: Path,
) -> None:
    payload = _state_payload(active_wave_ids=["P30-I05-W04"])
    payload["agent_sessions"] = _codex_agent_session_payload()
    payload["agent_sessions"]["SES-CODEX-01"]["runtime_session_id"] = "provider-session-01"
    ctx, state_path = _ctx(tmp_path, payload)

    async def body() -> None:
        result = await codex_lifecycle(
            ctx,
            {
                "event_type": "subagent_stop",
                "provider_session_id": "provider-session-01",
                "agent_id": "agent-01",
                "occurred_at": _now().isoformat(),
                "measurement_quality": "unavailable",
                "measurement_status": "usage_unavailable",
                "measurement_reason": "missing_transcript_path",
            },
        )
        assert result["correlated"] is False
        assert result["reason"] == "subagent_start_missing"
        state = State.model_validate(orjson.loads(state_path.read_bytes()))
        assert state.waves["P30-I05-W04"].sessions == {}

    _run(body)


# --------------------------------------------------------------------------- #
# Per-attempt-cost parity across the headless / interactive-claude axis.
#
# The headless spawn stamps ``SessionAttempt.cost_usd`` directly; the
# interactive-claude lifecycle (claude CLI claim/close + the Stop hook) mints
# its attempt HERE, off the Stop-hook ``runtime.capture``. All three wave
# flavours must surface a per-attempt cost row through the same cost-tab rollup.
# --------------------------------------------------------------------------- #


def _headless_wave_dict(wave_id: str, iter_id: str, *, runtime: str, cost: float) -> dict[str, Any]:
    """A claimed wave carrying one already-minted headless ``SessionAttempt``.

    Mirrors the attempt shape a headless spawn stamps in
    ``_persist_live_session_attempt``: a single attempt (number 1) whose own
    ``cost_usd`` is priced onto the attempt row -- the headless half of the
    parity the interactive path must match.
    """
    return {
        "id": wave_id,
        "iter_id": iter_id,
        "title": "headless spawn",
        "status": "claimed",
        "claim_session_id": f"claim-{runtime}",
        "opened_at": _now().isoformat(),
        "claimed_at": _now().isoformat(),
        "sessions": {
            "1": {
                "attempt": 1,
                "runtime": runtime,
                "session_id": f"sess-{runtime}",
                "session_log_handle": f"urn:eawf:v1:session-log:{runtime}:sess",
                "started_at": _now().isoformat(),
                "ended_at": _now().isoformat(),
                "exit_status": 0,
                "input_tokens": 100,
                "output_tokens": 50,
                "cost_usd": cost,
            }
        },
    }


def test_per_attempt_cost_parity_headless_codex_claude_and_interactive(
    tmp_path: Path,
) -> None:
    """Parity: codex-headless, claude-headless, and interactive-claude waves each
    surface a per-attempt cost row.

    The headless half stamps ``SessionAttempt.cost_usd`` in the spawn path; the
    interactive-claude half mints it here off the Stop-hook ``runtime.capture``.
    All three flow through the SAME cost-tab rollup entry point
    (``wave_cost_rollup_for_wave`` -> ``cost_tab_rows``), so each renders a
    per-attempt row carrying its priced cost rather than the honest-absence line.
    """
    iter_id = "P30-I05"
    interactive_id = "P30-I05-W04"
    codex_id = "P30-I05-W02"
    claude_id = "P30-I05-W03"
    codex_cost = 0.1234
    claude_cost = 0.2345
    interactive_cost = 0.42  # matches _capture_params default cost_usd "0.42"

    payload = _state_payload(active_wave_ids=[interactive_id])
    payload["waves"][codex_id] = _headless_wave_dict(
        codex_id, iter_id, runtime="codex", cost=codex_cost
    )
    payload["waves"][claude_id] = _headless_wave_dict(
        claude_id, iter_id, runtime="claude-code", cost=claude_cost
    )
    payload["iters"][iter_id]["wave_ids"] = [interactive_id, codex_id, claude_id]
    ctx, state_path = _ctx(tmp_path, payload)

    async def body() -> None:
        await runtime_capture(ctx, _capture_params(session_id="interactive-sess"))
        state = State.model_validate(orjson.loads(state_path.read_bytes()))

        # The interactive wave gained exactly one minted attempt carrying cost.
        interactive = state.waves[interactive_id]
        assert len(interactive.sessions) == 1
        assert interactive.sessions[1].cost_usd == pytest.approx(interactive_cost)
        assert interactive.sessions[1].runtime == "claude-code"

        # Every flavour surfaces >=1 per-attempt cost row through the shared
        # rollup + cost-tab entry points -- identical shape, honest figure.
        expected = {
            codex_id: codex_cost,
            claude_id: claude_cost,
            interactive_id: interactive_cost,
        }
        for wave_id, cost in expected.items():
            rollup = wave_cost_rollup_for_wave(state, wave_id, state_path)
            assert rollup is not None
            assert len(rollup.attempts) >= 1
            assert float(rollup.attempts[0].cost_usd) == pytest.approx(cost)
            rows = cost_tab_rows(rollup)
            labels = [label for label, _ in rows]
            assert "attempts" in labels
            assert all(str(value) != NO_METERED_SESSIONS for _label, value in rows)

    _run(body)


def test_interactive_capture_idempotent_updates_not_appends(tmp_path: Path) -> None:
    """A repeated Stop-hook capture for the same session UPDATES its attempt.

    Idempotency parity with the headless attempt counter: two captures for the
    same interactive session id leave exactly ONE attempt on the wave (the cost
    updated to the latest figure), never a second duplicate attempt row.
    """
    interactive_id = "P30-I05-W04"
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=[interactive_id]))

    async def body() -> None:
        await runtime_capture(ctx, _capture_params(session_id="sess-x", cost_usd="0.10"))
        await runtime_capture(ctx, _capture_params(session_id="sess-x", cost_usd="0.30"))
        state = State.model_validate(orjson.loads(state_path.read_bytes()))
        wave = state.waves[interactive_id]
        assert len(wave.sessions) == 1
        assert wave.sessions[1].session_id == "sess-x"
        assert wave.sessions[1].cost_usd == pytest.approx(0.30)

    _run(body)


def test_interactive_capture_without_cost_mints_no_attempt(tmp_path: Path) -> None:
    """Boundary: a capture carrying no priced cost mints no attempt.

    Nothing to surface as a per-attempt cost row, so the wave-level runtime
    snapshot stays the only record and ``sessions`` remains empty (unchanged
    pre-parity behaviour for a cost-less capture).
    """
    interactive_id = "P30-I05-W04"
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=[interactive_id]))

    async def body() -> None:
        params = _capture_params(session_id="sess-x")
        del params["cost_usd"]
        await runtime_capture(ctx, params)
        state = State.model_validate(orjson.loads(state_path.read_bytes()))
        assert state.waves[interactive_id].sessions == {}

    _run(body)
