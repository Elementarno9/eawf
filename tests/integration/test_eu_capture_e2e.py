"""End-to-end EU capture on the interactive Claude path (P30-I25-W29).

The whole chain, driven from a REAL Claude Code Stop payload and a REAL session
transcript (both scrubbed fixtures, not hand-written statusline shapes -- the
hand-written ones were green for the entire life of the dead capture path):

    Stop hook payload -> transcript aggregation -> runtime.capture params
        -> RuntimeLatest on the wave -> close-time delta -> ActualSummary

and the two failure modes the live run exposed:

- a wave claimed in one session and closed in another must SUM its per-session
  runtimes rather than differencing mismatched origins (which raised); and
- a capture taken before the ``turn_duration`` row lands must still report a
  nonzero duration, because ``elapsed_eu`` derives from duration.

The assertions mirror what the live run recorded on P30-I25-W31: a positive
``elapsed_eu``, ``harness="claude-code"``, a non-null model, and nonzero work
tokens. See ``.ea/artifacts/evidence/2026-07-13-eu-capture/`` for the live
evidence this test encodes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import RuntimeBaseline, RuntimeLatest, Wave
from eawf.observability.telemetry.join import DEFAULT_EU_MINUTES
from eawf.runtime.daemon.methods.state import _rebase_for_session
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.runner import capture_runtime_on_session_end
from eawf.workflow.lifecycle.wave import compute_runtime_delta

_FIXTURES = Path(__file__).resolve().parents[1] / "runtime" / "hooks" / "fixtures"
_STOP_PAYLOAD = _FIXTURES / "claude_session_end_stdin.json"
_TRANSCRIPT = _FIXTURES / "claude_session_transcript.jsonl"

_CLAIMED_AT = datetime(2026, 7, 13, 1, 0, tzinfo=UTC)

_CRITERION: dict[str, Any] = {
    "id": "CR-01",
    "text": "the wave records a captured runtime at close",
    "kind": "legacy",
    "acceptance_style": "binary",
    "evidence_kind": "attested",
    "quality_dimension": "functional_suitability",
    "measurable_signal": "the wave records a captured runtime at close",
}


class _RecordingClient:
    """Daemon stand-in capturing the ``runtime.capture`` params the hook sends."""

    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self._sink = sink

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "runtime.capture"
        self._sink.append(params)
        return {"ok": True}


def _capture_params_from_stop_hook(*, session_id: str = "sess-placeholder-eu27") -> dict[str, Any]:
    """Drive the Stop hook over the real payload and return its capture params."""
    payload = json.loads(_STOP_PAYLOAD.read_text(encoding="utf-8"))
    payload["transcript_path"] = str(_TRANSCRIPT)
    payload["session_id"] = session_id
    sink: list[dict[str, Any]] = []
    result = capture_runtime_on_session_end(
        HookEvent(
            event_type=HookEventType.SESSION_END,
            occurred_at=datetime(2026, 7, 13, 1, 30, tzinfo=UTC),
            payloads={"claude_code": payload},
        ),
        daemon_client_factory=lambda: _RecordingClient(sink),
    )
    assert result.block is False
    assert len(sink) == 1, result.output
    return sink[0]


def _latest_from_params(params: dict[str, Any], *, at: datetime) -> RuntimeLatest:
    """Build the wave snapshot the daemon would persist from these capture params."""
    return RuntimeLatest(
        api_duration_ms=params.get("api_duration_ms"),
        total_duration_ms=params.get("total_duration_ms"),
        cost_usd=float(params["cost_usd"]) if params.get("cost_usd") is not None else None,
        input_tokens=params.get("input_tokens"),
        output_tokens=params.get("output_tokens"),
        cache_creation_input_tokens=params.get("cache_creation_input_tokens"),
        cache_read_input_tokens=params.get("cache_read_input_tokens"),
        harness=params.get("harness"),
        model=params.get("model"),
        session_id=params.get("session_id"),
        captured_at=at,
    )


def _claimed_wave(baseline: RuntimeBaseline | None) -> Wave:
    return Wave.model_validate(
        {
            "id": "P00-I01-W01",
            "iter_id": "P00-I01",
            "title": "Wave one",
            "status": WaveStatus.CLAIMED.value,
            "success_criteria": [_CRITERION],
            "opened_at": _CLAIMED_AT,
            "runtime_baseline": baseline,
        }
    )


def test_stop_payload_carries_no_counters_only_a_transcript() -> None:
    """The premise: production's Stop payload has no cost and no usage block."""
    payload = json.loads(_STOP_PAYLOAD.read_text(encoding="utf-8"))

    assert "cost" not in payload
    assert "usage" not in payload
    assert payload["transcript_path"]


def test_stop_hook_to_actual_records_positive_eu() -> None:
    """The full chain: real Stop payload -> capture -> close-time actual."""
    params = _capture_params_from_stop_hook()

    # The hook read the transcript, not the payload.
    assert params["harness"] == "claude-code"
    assert params["model"] == "claude-opus-4-8"
    assert params["api_duration_ms"] > 0

    # The wave was claimed earlier in the same session: its baseline is that
    # session's counters at claim time.
    baseline = RuntimeBaseline(
        api_duration_ms=params["api_duration_ms"] // 2,
        total_duration_ms=params["total_duration_ms"] // 2,
        cost_usd=0.1,
        input_tokens=1,
        output_tokens=100,
        cache_creation_input_tokens=1_000,
        cache_read_input_tokens=10_000,
        harness="claude-code",
        model="claude-opus-4-8",
        session_id=params["session_id"],
        captured_at=_CLAIMED_AT,
    )
    wave = _claimed_wave(baseline)
    wave.runtime_latest = _latest_from_params(params, at=_CLAIMED_AT + timedelta(minutes=30))

    delta = compute_runtime_delta(
        wave.runtime_baseline,
        wave.runtime_latest,
        carry=wave.runtime_carry,
        eu_minutes=DEFAULT_EU_MINUTES,
    )

    assert delta is not None
    # What the live run proved on P30-I25-W31, pinned here.
    assert delta.elapsed_eu > 0.0
    assert delta.actual_tokens > 0
    assert delta.actual_cost_usd > 0.0
    assert wave.runtime_latest.harness == "claude-code"
    assert wave.runtime_latest.model == "claude-opus-4-8"
    # Cache reads are billed but are not work.
    assert delta.cache_read_input_tokens > 0
    assert delta.actual_tokens == (
        delta.input_tokens + delta.output_tokens + delta.cache_creation_input_tokens
    )


def test_capture_without_a_turn_duration_row_still_reports_duration(tmp_path: Path) -> None:
    """The W30 defect: the turn_duration row lands after the Stop hook reads the file."""
    rows = [
        json.loads(line)
        for line in _TRANSCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not isinstance(json.loads(line).get("durationMs"), int)
    ]
    live = tmp_path / "live.jsonl"
    live.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    payload = json.loads(_STOP_PAYLOAD.read_text(encoding="utf-8"))
    payload["transcript_path"] = str(live)
    sink: list[dict[str, Any]] = []
    capture_runtime_on_session_end(
        HookEvent(
            event_type=HookEventType.SESSION_END,
            occurred_at=datetime(2026, 7, 13, 1, 30, tzinfo=UTC),
            payloads={"claude_code": payload},
        ),
        daemon_client_factory=lambda: _RecordingClient(sink),
    )

    assert len(sink) == 1
    # elapsed_eu derives from duration, so a zero here means zero EU -- the exact
    # failure the first live Stop-boundary run hit.
    assert sink[0]["api_duration_ms"] > 0


def test_wave_spanning_two_sessions_sums_both_runtimes() -> None:
    """A wave claimed in one session and closed in another: sum, never raise."""
    first = _capture_params_from_stop_hook(session_id="sess-a")
    baseline = RuntimeBaseline(
        api_duration_ms=0,
        total_duration_ms=0,
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        harness="claude-code",
        model="claude-opus-4-8",
        session_id="sess-a",
        captured_at=_CLAIMED_AT,
    )
    wave = _claimed_wave(baseline)
    wave.runtime_latest = _latest_from_params(first, at=_CLAIMED_AT + timedelta(minutes=30))
    session_a_duration = first["api_duration_ms"]

    # The operator quits and resumes tomorrow: session B's counters start over.
    second = _capture_params_from_stop_hook(session_id="sess-b")
    _rebase_for_session(wave, "sess-b")
    wave.runtime_latest = _latest_from_params(second, at=_CLAIMED_AT + timedelta(days=1))

    delta = compute_runtime_delta(
        wave.runtime_baseline,
        wave.runtime_latest,
        carry=wave.runtime_carry,
        eu_minutes=DEFAULT_EU_MINUTES,
    )

    assert delta is not None
    assert wave.runtime_carry is not None
    assert wave.runtime_carry.sessions_folded == 1
    # Both sessions counted, and the close did not raise on the backwards counter.
    assert delta.api_duration_ms == session_a_duration + second["api_duration_ms"]
    assert delta.elapsed_eu == pytest.approx(
        delta.api_duration_ms / (DEFAULT_EU_MINUTES * 60_000.0)
    )
