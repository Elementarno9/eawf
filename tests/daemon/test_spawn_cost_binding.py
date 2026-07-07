"""Binding-proof (auditor): a live spawn's disclosed model + tokens produce a
real priced ``dispatch_cost`` event, and the bare ``opus`` alias never silently
prices to ``Decimal("0")`` (P30-I06-W03 / FLEET-3).

W01 bound the spawn output to a real ``agent_end`` report; this wave proves the
spawn's COST is real, not $0. Two criteria, each ending in a passing test:

1. A recording-stub spawn disclosing a DATED model id ``claude-opus-4-8-*`` plus
   a token spread yields a ``dispatch_cost`` event whose ``cost_usd`` is the
   expected NON-ZERO :class:`~decimal.Decimal` computed against the pinned
   opus-4-8 rates, and whose ``model`` field is the resolved id.
2. A spawn disclosing only the BARE alias ``opus`` resolves to a priced row via
   the longest-prefix / alias path (case a). The negative gate this wave installs
   is a regression lock: the bare alias MUST NOT silently price to ``Decimal("0")``
   -- if the ``opus`` pricing row were removed, the metering writer degrades
   HONESTLY (``priced is False``, a logged WARNING), never a pretend-billed $0.

The audit found the binding already holds: the dated id longest-prefix-resolves
to the ``claude-opus-4-8`` row, and the bare ``opus`` alias has its own priced
row in the pricing snapshot, so neither path can silently bill $0. No production
change was needed -- this file is the proof + the regression lock.

The adapter ``spawn_session`` is ALWAYS a monkeypatched stub returning a canned
:class:`~eawf.runtime.runtimes.adapter.SpawnResult` -- no real ``claude``
subprocess, no network, no auth. The recording-stub pattern mirrors
``tests/daemon/test_live_spawn_dispatch.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf import __version__
from eawf.kernel.store.envelope import Envelope
from eawf.observability.telemetry.pricing import (
    PRICING,
    check_pricing_currency,
    lookup_pricing,
)
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.agent import dispatch
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.runtime.runtimes.metering import price_spawn_result

pytestmark = pytest.mark.integration

_WAVE_ID = "P30-I06-W03"
_T0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 10, 12, 0, 5, tzinfo=UTC)
_STUB_PID = 60603

# A representative token spread so every per-class cost term is non-zero.
_INPUT_TOKENS = 100
_OUTPUT_TOKENS = 42
_CACHE_5M = 50
_CACHE_1H = 30
_CACHE_READ = 200


def _executor_report_json(*, wave_id: str = _WAVE_ID) -> str:
    """A schema-valid ``ExecutorReportBody`` JSON string the stub spawn returns.

    The live-spawn path binds the spawned agent's OWN text to a validated
    :class:`~eawf.kernel.store.kinds.agent_report.ExecutorReportBody` via the
    schema-assist re-ask loop, so the stub emits schema-valid JSON (not prose).
    The ``wave_id`` matches the dispatched wave so the post-execution verify gate
    passes.
    """
    return json.dumps(
        {
            "role": "executor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "executor implemented the wave",
            "wave_id": wave_id,
            "files_changed": ["src/eawf/observability/telemetry/pricing.py"],
            "tests_run": ["uv run pytest tests/daemon -q"],
            "outcome": "proved the spawn cost is real, not $0",
        }
    )


def _opus_4_8_expected_cost() -> Decimal:
    """The exact non-zero Decimal cost for the canned token spread on opus-4-8."""
    pricing = lookup_pricing("claude-opus-4-8")
    assert pricing is not None
    return (
        _INPUT_TOKENS * pricing.input_per_token
        + _OUTPUT_TOKENS * pricing.output_per_token
        + _CACHE_5M * pricing.cache_write_5m_per_token
        + _CACHE_1H * pricing.cache_write_1h_per_token
        + _CACHE_READ * pricing.cache_read_per_token
    )


class _StubAdapter:
    """A RuntimeAdapter stand-in whose spawn_session never forks a process.

    Returns a canned :class:`SpawnResult` whose ``model`` is the requested id and
    whose ``resolved_model`` is the test-supplied disclosed id (a dated opus-4-8
    variant, or ``None`` to fall back to the requested id). The token spread is
    non-zero so the priced cost is observable, and the ``on_spawn`` callback fires
    with a fixed pid.
    """

    id = "claude-code"
    cli_binary = "claude"

    def __init__(self, *, resolved_model: str | None) -> None:
        self.spawn_calls = 0
        self.models: list[str] = []
        self._resolved_model = resolved_model

    async def spawn_session(
        self,
        prompt: str,
        *,
        model: str,
        cwd: str | None = None,
        extra_args: Sequence[str] = (),
        denied_tools: Sequence[str] = (),
        timeout: float | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> SpawnResult:
        self.spawn_calls += 1
        self.models.append(model)
        if on_spawn is not None:
            on_spawn(_STUB_PID)
        return SpawnResult(
            session_id="sess-cost-binding",
            runtime="claude-code",
            model=model,
            resolved_model=self._resolved_model,
            subprocess_pid=_STUB_PID,
            exit_status=0,
            text=_executor_report_json(),
            input_tokens=_INPUT_TOKENS,
            output_tokens=_OUTPUT_TOKENS,
            cache_creation_input_tokens=_CACHE_5M + _CACHE_1H,
            cache_creation_5m_input_tokens=_CACHE_5M,
            cache_creation_1h_input_tokens=_CACHE_1H,
            cache_read_input_tokens=_CACHE_READ,
            started_at=_T0,
            ended_at=_T1,
        )

    def session_log_handle(self, session_id: str) -> str:
        return f"urn:eawf:v1:session-log:{self.id}:{session_id}"


def _state_payload() -> dict[str, Any]:
    """A minimal valid State with the full phase -> iter -> wave chain.

    The chain is required because the live path renders the dispatch envelope,
    which walks wave -> iter -> phase -> scope. The wave starts CLAIMED so the
    runner flips it to IN_PROGRESS, and ``agent_sessions`` starts empty so the
    live path registers the executor session itself.
    """
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-10T00:00:00Z",
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "Eawf",
            "description": "",
            "domains": ["workflow"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {
            "project_code": "EAWF",
            "track_id": None,
            "phase_id": "P30",
            "iter_id": "P30-I06",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "EAWF",
                "title": "v0.6",
                "status": "active",
                "iter_ids": ["P30-I06"],
                "outcome_ids": [],
                "opened_at": "2026-06-10T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I06": {
                "id": "P30-I06",
                "phase_id": "P30",
                "title": "Fleet binding",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-10T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P30-I06",
                "title": "Spawn cost binding proof",
                "status": "claimed",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/observability/telemetry/pricing.py"],
                "success_criteria": [
                    {
                        "id": "CR-01",
                        "text": "the spawn cost is real, not $0",
                        "kind": "legacy",
                        "acceptance_style": "binary",
                        "evidence_kind": "attested",
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": "the spawn cost is real, not $0",
                    }
                ],
                "agent_role": "executor",
                "effort_bucket": "L",
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-10T00:00:00Z",
                "claimed_at": "2026-06-10T00:00:00Z",
                "closed_at": None,
                "runtime_preference": ["claude-code"],
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path) -> Path:
    """Serialise a valid :class:`State` to ``<tmp>/.ea/state.json``."""
    from eawf.kernel.state.models import State

    state = State.model_validate(_state_payload())
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path, *, event_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-06-10T00:00:00+00:00",
        pid=4321,
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
    )


def _patch_adapter(monkeypatch: pytest.MonkeyPatch, adapter: _StubAdapter) -> None:
    """Make the live path resolve to *adapter* instead of the real claude one."""
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.agent.select_adapter",
        lambda runtime_id: adapter,
    )


def _read_envelopes(path: Path) -> list[Envelope]:
    rows: list[Envelope] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(Envelope.model_validate_json(line))
    return rows


def _dispatch_cost_payloads(event_path: Path) -> list[dict[str, Any]]:
    return [
        env.payload
        for env in _read_envelopes(event_path)
        if env.payload.get("event_type") == "dispatch_cost"
    ]


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Criterion 1: dated claude-opus-4-8-* -> non-zero priced dispatch_cost event,
# model field is the resolved id.
# --------------------------------------------------------------------------- #


def test_spawn_dated_opus_4_8_emits_nonzero_priced_dispatch_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spawn disclosing claude-opus-4-8-<date> + tokens prices non-zero.

    The disclosed dated id has no exact pricing key; the longest-prefix resolver
    binds it to the ``claude-opus-4-8`` family row, so the emitted
    ``dispatch_cost`` event carries the exact opus-4-8-rated Decimal cost (not a
    ``$0`` placeholder) and the ``model`` field is the resolved id.
    """
    resolved = "claude-opus-4-8-20260101"
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter(resolved_model=resolved))
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    costs = _dispatch_cost_payloads(event_path)
    assert len(costs) == 1
    cost = Decimal(costs[0]["cost_usd"])
    assert cost == _opus_4_8_expected_cost()
    assert cost > Decimal("0")
    # The dispatch_cost event records the resolved (priced-against) id.
    assert costs[0]["model"] == resolved
    assert costs[0]["pricing_version"] == "2026.05.17"


def test_headless_spawn_stamps_wave_runtime_latest_with_priced_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A headless spawn credits its priced cost onto the wave runtime snapshot.

    A sandboxed runtime fires no ``runtime.capture`` RPC (that writer is the
    Claude Code Stop hook), so without the dispatch stamping the wave runtime
    snapshots the metered cost lands only in the ``dispatch_cost`` event -- the
    wave's ``actual_cost_usd`` and the fleet ``spent_usd`` counter both read
    zero. Assert the dispatch stamps a zero baseline + a priced latest whose
    delta is the real, non-zero spend (USD) and a duration-derived EU.
    """
    from eawf.kernel.state.models import State
    from eawf.workflow.lifecycle.wave import compute_runtime_delta

    resolved = "claude-opus-4-8-20260101"
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter(resolved_model=resolved))
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    wave = State.model_validate_json(state_path.read_text(encoding="utf-8")).waves[_WAVE_ID]
    assert wave.runtime_baseline is not None
    assert wave.runtime_latest is not None
    assert wave.runtime_latest.cost_usd == pytest.approx(float(_opus_4_8_expected_cost()))
    assert wave.runtime_latest.model == resolved
    delta = compute_runtime_delta(wave.runtime_baseline, wave.runtime_latest, eu_minutes=30.0)
    assert delta is not None
    assert delta.actual_cost_usd == pytest.approx(float(_opus_4_8_expected_cost()))
    assert delta.elapsed_eu > 0.0


def test_headless_stamp_overrides_foreign_claim_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headless credit replaces a foreign claim-time baseline (G1).

    A claim-time statusline sidecar can seed a FOREIGN cumulative baseline on
    the wave -- the operator's own interactive Claude Code session, not this
    spawn. Pre-G1 the headless credit deferred to it, so ``runtime_latest``
    stayed ``None`` and the spawn's per-class tokens never landed. The spawn's
    own ``SpawnResult`` is authoritative, so the matched zero-baseline +
    priced-latest pair now replaces the foreign baseline; the delta is the
    spawn's own clean, non-zero spend rather than a subtraction against the
    operator session's counter.
    """
    from eawf.kernel.state.models import RuntimeBaseline, State

    resolved = "claude-opus-4-8-20260101"
    state_path = _write_state(tmp_path)
    seeded = State.model_validate_json(state_path.read_text(encoding="utf-8"))
    seeded.waves[_WAVE_ID].runtime_baseline = RuntimeBaseline(
        api_duration_ms=999, cost_usd=0.0, harness="claude-code", captured_at=_T0
    )
    state_path.write_text(seeded.model_dump_json(), encoding="utf-8")
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter(resolved_model=resolved))
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    wave = State.model_validate_json(state_path.read_text(encoding="utf-8")).waves[_WAVE_ID]
    # The foreign api_duration_ms=999 baseline is replaced by the zero baseline,
    # and the priced latest now carries the spawn's real per-class tokens.
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.api_duration_ms == 0
    assert wave.runtime_latest is not None
    assert wave.runtime_latest.input_tokens == _INPUT_TOKENS
    assert wave.runtime_latest.output_tokens == _OUTPUT_TOKENS


def test_spawn_dated_opus_4_8_priced_against_family_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dated id resolves to the same row as the bare claude-opus-4-8 key.

    Pins WHY the cost is non-zero: the dated id is not its own key, so the
    longest-prefix fallback binds it to the family row -- if that resolution
    regressed to a miss, the cost would silently fall to ``$0`` / ``priced=False``.
    """
    resolved = "claude-opus-4-8-20260101"
    assert lookup_pricing(resolved) is PRICING["claude-opus-4-8"]
    # And the metered cost is flagged priced (a real billed figure, not a fallback).
    result = SpawnResult(
        session_id="s",
        runtime="claude",
        model="claude-opus-4-8",
        resolved_model=resolved,
        subprocess_pid=1,
        exit_status=0,
        text="x",
        input_tokens=_INPUT_TOKENS,
        output_tokens=_OUTPUT_TOKENS,
        cache_creation_input_tokens=_CACHE_5M + _CACHE_1H,
        cache_creation_5m_input_tokens=_CACHE_5M,
        cache_creation_1h_input_tokens=_CACHE_1H,
        cache_read_input_tokens=_CACHE_READ,
        started_at=_T0,
        ended_at=_T1,
    )
    metered = price_spawn_result(result)
    assert metered.priced is True
    assert metered.model == resolved
    assert metered.cost_usd == _opus_4_8_expected_cost()
    assert metered.cost_usd > Decimal("0")


# --------------------------------------------------------------------------- #
# Criterion 2: the bare `opus` alias resolves to a priced (>0) row -- it MUST
# NOT silently price to Decimal("0").
# --------------------------------------------------------------------------- #


def test_spawn_bare_opus_alias_emits_nonzero_priced_dispatch_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spawn disclosing only the bare ``opus`` alias prices to a non-zero row.

    The bare alias has no dated suffix; it resolves via its own alias key in the
    pricing snapshot to the 4.x family rate, so the emitted ``dispatch_cost``
    event carries a real non-zero cost -- never a silent ``Decimal("0")``.
    """
    # resolved_model None -> the metering writer prices against the requested
    # (bare alias) model the override pins.
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter(resolved_model=None))
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True, "model": "opus"}))

    costs = _dispatch_cost_payloads(event_path)
    assert len(costs) == 1
    cost = Decimal(costs[0]["cost_usd"])
    # The bare alias prices at the 4.x family rate -> identical to the opus-4-8
    # expected cost for the same token spread.
    assert cost == _opus_4_8_expected_cost()
    assert cost > Decimal("0")
    # NEVER a silent $0 -- this is the binding the wave exists to lock.
    assert cost != Decimal("0")
    assert costs[0]["model"] == "opus"


def test_bare_opus_alias_resolves_to_priced_row_not_silent_zero() -> None:
    """The bare ``opus`` alias resolves to a priced (>0) row (case a).

    The disjunction the wave gates on: a bare ``opus`` spawn either resolves to a
    priced row OR fails the pricing-currency check -- it MUST NOT silently price
    to ``Decimal("0")``. The code implements case (a): ``opus`` is its own pricing
    key, so ``lookup_pricing`` returns a priced row and the metering writer flags
    ``priced is True`` with a non-zero cost.
    """
    row = lookup_pricing("opus")
    assert row is not None, "bare 'opus' alias must resolve to a pricing row"
    assert row.input_per_token > Decimal("0")
    assert row.output_per_token > Decimal("0")

    result = SpawnResult(
        session_id="s",
        runtime="claude",
        model="opus",
        resolved_model=None,
        subprocess_pid=1,
        exit_status=0,
        text="x",
        input_tokens=_INPUT_TOKENS,
        output_tokens=_OUTPUT_TOKENS,
        cache_creation_input_tokens=_CACHE_5M + _CACHE_1H,
        cache_creation_5m_input_tokens=_CACHE_5M,
        cache_creation_1h_input_tokens=_CACHE_1H,
        cache_read_input_tokens=_CACHE_READ,
        started_at=_T0,
        ended_at=_T1,
    )
    metered = price_spawn_result(result)
    assert metered.priced is True
    assert metered.model == "opus"
    assert metered.cost_usd > Decimal("0")
    # The negative gate: a bare-alias spawn never silently bills $0.
    assert metered.cost_usd != Decimal("0")


def test_bare_opus_alias_row_keeps_pricing_currency_green() -> None:
    """The ``opus`` alias row is internally currency-consistent (no drift).

    The disjunction's second leg is the pricing-currency check: the bare-alias row
    must not fail it. This pins that the row backing the no-silent-$0 guarantee is
    itself a real, currency-consistent row -- so the alias resolves to a *valid*
    priced row, not a malformed one that would later be scrubbed.
    """
    row = lookup_pricing("opus")
    assert row is not None
    # The alias row obeys the same cache-multiplier currency the check enforces.
    assert row.cache_read_per_token == row.input_per_token * Decimal("0.1")
    assert row.cache_write_5m_per_token == row.input_per_token * Decimal("1.25")
    assert row.cache_write_1h_per_token == row.input_per_token * Decimal("2")
    report = check_pricing_currency()
    assert report.is_current is True
    assert report.findings == []


def test_bare_opus_alias_degrades_honestly_if_row_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression lock: with the ``opus`` row removed, the alias degrades HONESTLY.

    This is the negative gate the wave installs as a regression lock. The
    no-silent-$0 guarantee for the bare alias depends on the ``opus`` key existing
    in the pricing snapshot. If a future edit deletes it (and the alias no longer
    longest-prefix-matches any dated row, since ``opus`` is not a prefix of
    ``claude-opus-4-8``), the metering writer MUST NOT pretend a billed ``$0``:
    it returns ``priced is False`` with a logged WARNING, so the gap is observable
    rather than a silent zero hiding in the ledger.

    Removing the row is the only way a bare ``opus`` spawn could reach
    ``Decimal("0")``, and even then the ``priced is False`` flag distinguishes the
    unpriced fallback from a real billed zero -- so the alias never SILENTLY prices
    to $0.
    """
    # Snapshot the keys to restore, then remove every key that could resolve the
    # bare alias (`opus` itself; no other key is a prefix of the bare token).
    monkeypatch.delitem(PRICING, "opus", raising=True)
    assert lookup_pricing("opus") is None

    result = SpawnResult(
        session_id="s",
        runtime="claude",
        model="opus",
        resolved_model=None,
        subprocess_pid=1,
        exit_status=0,
        text="x",
        input_tokens=_INPUT_TOKENS,
        output_tokens=_OUTPUT_TOKENS,
        cache_creation_input_tokens=_CACHE_5M + _CACHE_1H,
        cache_creation_5m_input_tokens=_CACHE_5M,
        cache_creation_1h_input_tokens=_CACHE_1H,
        cache_read_input_tokens=_CACHE_READ,
        started_at=_T0,
        ended_at=_T1,
    )
    metered = price_spawn_result(result)
    # The cost is $0 ONLY with priced=False -- the honest-degrade observable, not
    # a silent billed zero.
    assert metered.cost_usd == Decimal("0")
    assert metered.priced is False


# --------------------------------------------------------------------------- #
# E2E live-chain (incident keystone): a headless dispatch's metered cost must
# walk the WHOLE chain -- runtime snapshot -> close-on-behalf -> wave actuals ->
# the rendered narrative surface -- not just each joint in isolation. This is
# the test class the StubAdapter-only coverage hid three binding bugs behind
# (W50 / W54 / W56): the stub returns a real SpawnResult, but here it flows
# through the real dispatch, the real close-on-behalf, and the real renderer.
# --------------------------------------------------------------------------- #


def test_headless_dispatch_chains_to_surfaced_nonzero_actual_eu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A headless spawn's spend reaches the rendered wave narrative as real EU.

    Drives the full path: dispatch (stamps the wave runtime snapshot off the
    priced spawn) -> close-on-behalf (records the actuals from the runtime
    delta) -> build_narrative (renders the actual). Asserts the narrative shows
    a non-zero elapsed EU and the wave's recorded cost/model -- never the
    "No rollup yet." empty state that a zero-EU close would surface.
    """
    from datetime import UTC, datetime

    from eawf.kernel.state.models import FleetRun, FleetRunState, State
    from eawf.runtime.daemon.methods.fleet import _Loop
    from eawf.surfaces.render.narrative import build_narrative

    resolved = "claude-opus-4-8-20260101"
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    _patch_adapter(monkeypatch, _StubAdapter(resolved_model=resolved))
    ctx = _ctx(state_path, event_path=event_path)

    # 1. Dispatch the headless spawn -> the wave's runtime snapshot is stamped.
    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    # 2. Close on behalf of the (sandboxed) agent -> the actuals are recorded.
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

    # 3. The rendered surface carries the real spend, not the empty state.
    state = State.model_validate_json(state_path.read_text(encoding="utf-8"))
    actual = state.actuals[_WAVE_ID]
    assert actual.elapsed_eu > 0.0
    assert actual.actual_cost_usd == pytest.approx(float(_opus_4_8_expected_cost()))
    assert actual.model == resolved

    validation = build_narrative(state, _WAVE_ID).validation
    body = "\n".join(validation)
    assert "No rollup yet." not in body
    assert f"elapsed EU: {actual.elapsed_eu:.2f}." in body
