"""P30-I21-W29 (G1): headless spawn per-class token capture onto wave runtime.

A headless (sandboxed) spawn parses real per-class token usage but, before this
wave, never landed it on ``wave.tokens_consumed``: a claim-time statusline
sidecar could seed a FOREIGN cumulative baseline (the operator's own interactive
Claude Code session) that blocked the headless snapshot, so ``runtime_latest``
kept null token fields and ``compute_runtime_delta`` collapsed the token tally to
zero -- the runtime tab read "no data" and close recorded ``tokens_consumed=0``.

These tests pin the fix: the headless spawn's own ``SpawnResult`` is
authoritative for the wave's runtime spend, so it stamps the matched
zero-baseline + priced-latest pair unconditionally (real tokens land even when a
claim-time baseline exists), and the ``runtime.capture`` writer merges token
fields rather than null-clobbering a populated per-class count.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.daemon.test_spawn_cost_binding import (
    _CACHE_1H,
    _CACHE_5M,
    _CACHE_READ,
    _INPUT_TOKENS,
    _OUTPUT_TOKENS,
    _T0,
    _WAVE_ID,
    _ctx,
    _patch_adapter,
    _run,
    _StubAdapter,
    _write_state,
)

#: Work tokens exclude the prompt-cache READ class (P30-I25-W31): a cache read
#: re-counts the same context on every request, so its volume tracks how far into
#: a session the wave sits rather than the work done. Cache reads stay billed in
#: ``actual_cost_usd`` and visible per-class on the runtime snapshot.
_WORK_TOKENS = _INPUT_TOKENS + _OUTPUT_TOKENS + _CACHE_5M + _CACHE_1H


def test_headless_spawn_captures_tokens_over_foreign_claim_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A headless spawn stamps real per-class tokens even when a baseline exists.

    Reproduces the smoke scenario: claim resolved the operator's interactive
    statusline sidecar, seeding a FOREIGN cumulative baseline (5M input tokens)
    on the wave before the headless spawn ran. The spawn's own result is
    authoritative, so it replaces the foreign baseline with the matched zero
    baseline and stamps the spawn's real per-class tokens on ``runtime_latest`` --
    ``compute_runtime_delta`` then yields the spawn's own non-zero token tally.
    """
    from eawf.kernel.state.models import RuntimeBaseline, State
    from eawf.workflow.lifecycle.wave import compute_runtime_delta

    resolved = "claude-opus-4-8-20260101"
    state_path = _write_state(tmp_path)
    seeded = State.model_validate_json(state_path.read_text(encoding="utf-8"))
    seeded.waves[_WAVE_ID].runtime_baseline = RuntimeBaseline(
        api_duration_ms=999,
        input_tokens=5_000_000,
        cost_usd=12.0,
        harness="claude-code",
        captured_at=_T0,
    )
    state_path.write_text(seeded.model_dump_json(), encoding="utf-8")
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter(resolved_model=resolved))
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch_spawn(ctx))

    wave = State.model_validate_json(state_path.read_text(encoding="utf-8")).waves[_WAVE_ID]
    assert wave.runtime_latest is not None
    assert wave.runtime_latest.input_tokens == _INPUT_TOKENS
    assert wave.runtime_latest.output_tokens == _OUTPUT_TOKENS
    assert wave.runtime_latest.cache_read_input_tokens == _CACHE_READ
    # The foreign 5M-input baseline is replaced by the spawn's matched zero
    # baseline, so the delta is the spawn's own spend rather than a negative /
    # garbage subtraction against the operator session's cumulative counter.
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.input_tokens == 0
    delta = compute_runtime_delta(wave.runtime_baseline, wave.runtime_latest, eu_minutes=30.0)
    assert delta is not None
    assert delta.actual_tokens == _WORK_TOKENS
    assert delta.cache_read_input_tokens == _CACHE_READ
    assert delta.actual_tokens > 0


def test_headless_close_records_nonzero_tokens_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close-on-behalf writes a non-zero ``tokens_consumed`` off the runtime delta.

    Drives dispatch (stamps the runtime snapshot) then close-on-behalf (records
    the actuals from the delta). The closed wave carries the spawn's real token
    tally -- the runtime tab bar fills instead of reading "no data".
    """
    from eawf.kernel.state.models import FleetRun, FleetRunState, State
    from eawf.runtime.daemon.methods.fleet import _Loop

    resolved = "claude-opus-4-8-20260101"
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    _patch_adapter(monkeypatch, _StubAdapter(resolved_model=resolved))
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch_spawn(ctx))

    loop = _Loop(
        ctx=ctx,
        run=FleetRun(
            run_state=FleetRunState.DRAINING,
            armed_at=datetime(2026, 6, 10, tzinfo=UTC),
        ),
        spawn=lambda *a, **k: None,
        watch=lambda *a, **k: "closed",
    )
    loop._close_wave_on_disk(_WAVE_ID)

    wave = State.model_validate_json(state_path.read_text(encoding="utf-8")).waves[_WAVE_ID]
    assert wave.tokens_consumed == _WORK_TOKENS
    assert wave.tokens_consumed > 0


def dispatch_spawn(ctx: object) -> object:
    """Run the headless dispatch spawn for the fixture wave."""
    from eawf.runtime.daemon.methods.agent import dispatch

    return dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True})


def _latest(**overrides: object):
    """Build a ``RuntimeLatest`` with sensible defaults for merge tests."""
    from eawf.kernel.state.models import RuntimeLatest

    base: dict[str, object] = {
        "api_duration_ms": 1000,
        "total_duration_ms": 1000,
        "cost_usd": 1.0,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 5,
        "harness": "claude-code",
        "model": "claude-opus-4-8",
        "captured_at": _T0,
    }
    base.update(overrides)
    return RuntimeLatest(**base)  # type: ignore[arg-type]


def test_merge_runtime_latest_returns_incoming_when_no_existing() -> None:
    """With no prior snapshot the incoming capture is returned unchanged."""
    from eawf.runtime.daemon.methods.state import _merge_runtime_latest

    incoming = _latest(input_tokens=None)
    assert _merge_runtime_latest(None, incoming) is incoming


def test_merge_runtime_latest_preserves_populated_tokens_over_null_incoming() -> None:
    """A null incoming token field falls back to the existing populated value.

    This is the null-clobber guard: a ``runtime.capture`` payload with a priced
    cost + duration but no ``context_window.current_usage`` block leaves the
    per-class token fields ``None``; merging must keep the tokens a prior
    headless snapshot stamped rather than zeroing the runtime-delta token tally.
    """
    from eawf.runtime.daemon.methods.state import _merge_runtime_latest

    existing = _latest(input_tokens=100, output_tokens=50, cache_read_input_tokens=200)
    incoming = _latest(
        input_tokens=None,
        output_tokens=None,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
        cost_usd=9.0,
        api_duration_ms=7000,
    )
    merged = _merge_runtime_latest(existing, incoming)
    # Token fields fall back to the existing populated counts...
    assert merged.input_tokens == 100
    assert merged.output_tokens == 50
    assert merged.cache_read_input_tokens == 200
    # ...while cost + duration take the fresh capture's values.
    assert merged.cost_usd == pytest.approx(9.0)
    assert merged.api_duration_ms == 7000


def test_merge_runtime_latest_takes_incoming_populated_tokens() -> None:
    """A populated incoming token field overrides the existing value."""
    from eawf.runtime.daemon.methods.state import _merge_runtime_latest

    existing = _latest(input_tokens=100)
    incoming = _latest(input_tokens=999)
    merged = _merge_runtime_latest(existing, incoming)
    assert merged.input_tokens == 999
