"""Tests: live wave-executor spawn + AgentSession registration un-idling the report.

Exercises the opt-in live-spawn path of
:func:`eawf.runtime.daemon.methods.agent.dispatch` (``spawn=True``): the daemon
registers an executor :class:`~eawf.kernel.state.models.AgentSession`, renders
the prompt, resolves the runtime adapter, ``await``s its ``spawn_session``
(behind the safety floor), prices the spawn, and drives
:func:`eawf.runtime.daemon.dispatch_runner.run_dispatch` with the registered
session id so the previously-idle ``emit_agent_end_report`` path fires.

The adapter ``spawn_session`` is ALWAYS a monkeypatched stub returning a
canned :class:`~eawf.runtime.runtimes.adapter.SpawnResult` -- no real ``claude``
subprocess, no network, no auth, no cost. The stub fires the ``on_spawn``
callback with a fixed pid so the captured-pid assertion is observable.

The load-bearing assertions, one per wave success criterion:

- an EXECUTOR ``AgentSession`` is registered in ``state.agent_sessions``
  after a live dispatch (ACTIVE, scope = wave_id, role executor);
- ``run_dispatch`` received the registered session id and the
  ``agent_end`` emit fired -- a role-specific executor report envelope
  lands in the ``executor_report`` store;
- the returned plan carries the real captured pid (non-zero) and the
  priced cost flowed into the ``dispatch_cost`` event;
- back-compat: ``spawn=False`` (plan-only + hand-fed-outcome) is unchanged;
- error path: ``spawn=True`` without ``state_path`` fails fast typed.
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
from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus, StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportPayload
from eawf.kernel.store.paths import store_path
from eawf.observability.telemetry.pricing import lookup_pricing
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.dispatch_runner import AGENT_OUTPUT_EVENT_TYPE
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.agent import LiveSpawnError, dispatch
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_ID = "P29-I04-W01"
_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC)
_STUB_PID = 54321


def _executor_report_json(*, wave_id: str = _WAVE_ID, verdict: str = "pass") -> str:
    """A valid ``ExecutorReportBody`` JSON string the stub spawn returns.

    The live-spawn report path now binds the spawned agent's OWN text to a
    validated :class:`~eawf.kernel.store.kinds.agent_report.ExecutorReportBody`
    via the schema-assist re-ask loop, so the stub must emit schema-valid
    JSON (not free prose). The ``wave_id`` matches the dispatched wave so the
    post-execution verify gate passes.
    """
    return json.dumps(
        {
            "role": "executor",
            "verdict": verdict,
            "confidence": "high",
            "summary": "executor implemented the wave",
            "wave_id": wave_id,
            "files_changed": ["src/eawf/runtime/daemon/methods/agent.py"],
            "tests_run": ["uv run pytest tests/daemon -q"],
            "outcome": "bound the spawned executor output to the report body",
        }
    )


# --------------------------------------------------------------------------- #
# Fixtures: a stub adapter spawn + a valid on-disk state with the wave chain.
# --------------------------------------------------------------------------- #


class _StubAdapter:
    """A RuntimeAdapter stand-in whose spawn_session never forks a process.

    Returns a canned :class:`SpawnResult` with a representative token spread
    so the priced cost is non-zero, and fires the ``on_spawn`` callback with
    a fixed pid so the captured-pid path is observable. Records the prompt +
    model it was handed so a test can assert the rendered prompt flowed in.
    """

    id = "claude-code"
    cli_binary = "claude"

    def __init__(self) -> None:
        self.spawn_calls = 0
        self.prompts: list[str] = []
        self.models: list[str] = []

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
    ) -> SpawnResult:
        self.spawn_calls += 1
        self.prompts.append(prompt)
        self.models.append(model)
        if on_spawn is not None:
            on_spawn(_STUB_PID)
        return SpawnResult(
            session_id="sess-live-abc123",
            runtime="claude-code",
            model=model,
            resolved_model="claude-opus-4-8",
            subprocess_pid=_STUB_PID,
            exit_status=0,
            text=_executor_report_json(),
            input_tokens=100,
            output_tokens=42,
            cache_creation_input_tokens=80,
            cache_creation_5m_input_tokens=50,
            cache_creation_1h_input_tokens=30,
            cache_read_input_tokens=200,
            started_at=_T0,
            ended_at=_T1,
        )

    def session_log_handle(self, session_id: str) -> str:
        return f"urn:eawf:v1:session-log:{self.id}:{session_id}"


def _state_payload(*, agent_role: str = "executor", effort_bucket: str = "L") -> dict[str, Any]:
    """A minimal valid State with the full phase -> iter -> wave chain.

    The chain is required because the live path renders the dispatch
    envelope, which walks wave -> iter -> phase -> scope. The wave starts
    CLAIMED so the runner's head transition flips it to IN_PROGRESS, and
    ``agent_sessions`` starts empty so the live path registers the executor
    session itself.
    """
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-01T00:00:00Z",
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
            "phase_id": "P29",
            "iter_id": "P29-I04",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P29": {
                "id": "P29",
                "scope_id": "EAWF",
                "title": "v0.5",
                "status": "active",
                "iter_ids": ["P29-I04"],
                "outcome_ids": [],
                "opened_at": "2026-06-01T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P29-I04": {
                "id": "P29-I04",
                "phase_id": "P29",
                "title": "Spawn spine",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-01T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P29-I04",
                "title": "Live wave-executor spawn",
                "status": "claimed",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/daemon/methods/agent.py"],
                "success_criteria": [
                    {
                        "id": "CR-01",
                        "text": "spawn for real behind the floor",
                        "kind": "legacy",
                        "acceptance_style": "binary",
                        "evidence_kind": "attested",
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": "spawn for real behind the floor",
                    }
                ],
                "agent_role": agent_role,
                "effort_bucket": effort_bucket,
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-01T00:00:00Z",
                "claimed_at": "2026-06-01T00:00:00Z",
                "closed_at": None,
                "runtime_preference": ["claude-code"],
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, **kwargs: Any) -> Path:
    """Serialise a valid :class:`State` to ``<tmp>/.ea/state.json``."""
    from eawf.kernel.state.models import State

    state = State.model_validate(_state_payload(**kwargs))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path | None, *, event_path: Path | None = None) -> MethodContext:
    return MethodContext(
        started_at="2026-06-01T00:00:00+00:00",
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


def _executor_report_rows(state_path: Path) -> list[Envelope]:
    return _read_envelopes(store_path(state_path, StoreKind.EXECUTOR_REPORT))


def _dispatch_cost_payloads(event_path: Path) -> list[dict[str, Any]]:
    return [
        env.payload
        for env in _read_envelopes(event_path)
        if env.payload.get("event_type") == "dispatch_cost"
    ]


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Criterion (a) + (b): live spawn registers an EXECUTOR session, threads the
# session id into run_dispatch, and the agent_end report emit fires.
# --------------------------------------------------------------------------- #


def test_dispatch_spawn_registers_executor_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live dispatch registers an ACTIVE EXECUTOR session scoped to the wave."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    state = load_state(state_path)
    # Exactly one session was registered; it is the executor lane for the wave.
    assert len(state.agent_sessions) == 1
    session = next(iter(state.agent_sessions.values()))
    assert session.role is AgentSessionRole.EXECUTOR
    assert session.scope_id == _WAVE_ID
    assert session.status is AgentSessionStatus.ACTIVE
    # The plan's session id is the registered AgentSession id (not a cosmetic UUID).
    assert result["session_id"] == session.id
    assert session.id in state.current.active_session_ids
    # The adapter spawn was driven exactly once.
    assert adapter.spawn_calls == 1


def test_dispatch_spawn_emits_executor_report_unidling_run_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The threaded session id un-idles emit_agent_end_report: a report row lands.

    This is the keystone assertion -- before W01 ``run_dispatch`` was never
    handed a ``session_id``, so ``emit_agent_end_report`` never fired. The
    live path registers a session and threads its id, so a role-specific
    executor report envelope lands in the executor_report store.
    """
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    rows = _executor_report_rows(state_path)
    assert len(rows) == 1
    payload = AgentReportPayload.model_validate(rows[0].payload)
    # The report is the executor body for the dispatched wave, scoped to it.
    assert payload.header.role is AgentSessionRole.EXECUTOR
    assert payload.header.scope_id == _WAVE_ID
    assert payload.body.role == "executor"
    assert payload.body.wave_id == _WAVE_ID
    # A clean (no-fallback) dispatch reports a pass verdict.
    assert payload.body.verdict.value == "pass"


def test_dispatch_spawn_report_session_id_matches_registered_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emitted report's header session id is the registered session id."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    payload = AgentReportPayload.model_validate(_executor_report_rows(state_path)[0].payload)
    assert payload.header.session_id == result["session_id"]


# --------------------------------------------------------------------------- #
# Criterion (c): real captured pid + priced cost flow through.
# --------------------------------------------------------------------------- #


def test_dispatch_spawn_plan_carries_captured_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The returned plan carries the real (non-zero) pid captured via on_spawn."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert result["pid"] == _STUB_PID
    assert result["pid"] != 0


def test_dispatch_spawn_priced_cost_flows_into_dispatch_cost_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawn is priced and the real cost lands on the dispatch_cost event.

    The stub returns a non-zero token spread; the metering writer prices it
    against ``claude-opus-4-8``, and that exact Decimal cost rides on the
    emitted ``dispatch_cost`` payload rather than a ``$0`` placeholder.
    """
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    pricing = lookup_pricing("claude-opus-4-8")
    assert pricing is not None
    expected = (
        100 * pricing.input_per_token
        + 42 * pricing.output_per_token
        + 50 * pricing.cache_write_5m_per_token
        + 30 * pricing.cache_write_1h_per_token
        + 200 * pricing.cache_read_per_token
    )
    costs = _dispatch_cost_payloads(event_path)
    assert len(costs) == 1
    assert Decimal(costs[0]["cost_usd"]) == expected
    assert Decimal(costs[0]["cost_usd"]) > Decimal("0")
    assert costs[0]["runtime"] == "claude"
    assert costs[0]["wave_id"] == _WAVE_ID


def test_dispatch_spawn_event_ids_reflect_runner_emissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan's event_ids are the runner's emitted ids (non-empty on live)."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    # No fallback in the live W01 path -> two events: the C09 dispatch_cost AND
    # the W08 agent.output fan (the live spawn now threads its captured answer
    # text into run_dispatch(output_text=...), so the stdout producer fires).
    assert len(result["event_ids"]) == 2
    envelopes = _read_envelopes(event_path)
    cost_envelope_ids = [
        env.id for env in envelopes if env.payload.get("event_type") == "dispatch_cost"
    ]
    output_envelope_ids = [
        env.id for env in envelopes if env.payload.get("event_type") == AGENT_OUTPUT_EVENT_TYPE
    ]
    assert len(cost_envelope_ids) == 1
    assert len(output_envelope_ids) == 1
    # The plan's event_ids ARE the runner's emitted ids: the dispatch_cost then
    # the agent.output (emitted in that order).
    assert list(result["event_ids"]) == cost_envelope_ids + output_envelope_ids


def test_dispatch_spawn_model_override_prices_against_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit model override is the model the spawn is driven with."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True, "model": "claude-haiku-4-5"}))

    # The override is the model handed to spawn_session.
    assert adapter.models == ["claude-haiku-4-5"]


def test_dispatch_spawn_resolves_model_from_routing_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no override, the model resolves from the wave's role + effort.

    An executor / L wave routes to the Opus tier per the default routing
    table, so the spawn is driven with that model.
    """
    state_path = _write_state(tmp_path, agent_role="executor", effort_bucket="L")
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert adapter.models == ["claude-opus-4-8"]


def _write_state_with_runtime(tmp_path: Path, *, runtime: str, effort_bucket: str) -> Path:
    """Serialise a state whose only wave pins ``runtime_preference=[runtime]``."""
    from eawf.kernel.state.models import State

    payload = _state_payload(agent_role="executor", effort_bucket=effort_bucket)
    payload["waves"][_WAVE_ID]["runtime_preference"] = [runtime]
    state = State.model_validate(payload)
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


class _VendorStubAdapter(_StubAdapter):
    """A stub adapter bound to a specific runtime id (codex / opencode).

    Inherits the claude stub's spawn machinery but stamps its own ``id`` +
    ``runtime`` so the live path resolves it as the foreign vendor and the
    resulting :class:`SpawnResult` carries that runtime. ``resolved_model`` is
    left ``None`` so the metering writer prices against the requested model the
    routing resolved (the assertion target).
    """

    def __init__(self, runtime_id: str) -> None:
        super().__init__()
        self.id = runtime_id

    async def spawn_session(self, prompt: str, *, model: str, **kwargs: Any) -> SpawnResult:  # type: ignore[override]
        self.spawn_calls += 1
        self.prompts.append(prompt)
        self.models.append(model)
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(_STUB_PID)
        return SpawnResult(
            session_id=f"sess-{self.id}",
            runtime=self.id,
            model=model,
            resolved_model=None,
            subprocess_pid=_STUB_PID,
            exit_status=0,
            text=_executor_report_json(),
            input_tokens=100,
            output_tokens=42,
            cache_creation_input_tokens=80,
            cache_creation_5m_input_tokens=50,
            cache_creation_1h_input_tokens=30,
            cache_read_input_tokens=200,
            started_at=_T0,
            ended_at=_T1,
        )


@pytest.mark.parametrize(
    ("runtime", "effort_bucket", "expected_model"),
    [
        ("codex", "L", "gpt-5-codex"),
        ("codex", "XS", "gpt-5-mini"),
        ("opencode", "L", "anthropic/claude-opus-4-8"),
        ("opencode", "XS", "anthropic/claude-haiku-4-5"),
    ],
)
def test_dispatch_spawn_resolves_per_vendor_model_for_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
    effort_bucket: str,
    expected_model: str,
) -> None:
    """A codex / opencode spawn is driven with ITS OWN vendor model id.

    The cross-vendor reachability fix: when the wave pins a non-claude runtime,
    the live path resolves that runtime's own per-tier model (a bare OpenAI id
    for codex, a ``provider/model`` id for opencode) rather than a claude id the
    foreign CLI would reject.
    """
    state_path = _write_state_with_runtime(tmp_path, runtime=runtime, effort_bucket=effort_bucket)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _VendorStubAdapter(runtime)
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert adapter.models == [expected_model]


def test_dispatch_spawn_codex_priced_cost_is_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A codex spawn prices its own model to a real non-zero dispatch_cost.

    Proves the pricing lane: the codex per-tier model resolves to a pricing row
    so the emitted ``dispatch_cost`` carries a real cost, not the $0 the
    unpriced fallback would have produced before W15.
    """
    state_path = _write_state_with_runtime(tmp_path, runtime="codex", effort_bucket="L")
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _VendorStubAdapter("codex"))
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    pricing = lookup_pricing("gpt-5-codex")
    assert pricing is not None
    costs = _dispatch_cost_payloads(event_path)
    assert len(costs) == 1
    assert Decimal(costs[0]["cost_usd"]) > Decimal("0")
    assert costs[0]["runtime"] == "codex"
    assert costs[0]["model"] == "gpt-5-codex"


def test_dispatch_spawn_flips_wave_to_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's head transition flips the CLAIMED wave to IN_PROGRESS."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    wave = load_state(state_path).waves[_WAVE_ID]
    assert wave.status.value == "in_progress"


# --------------------------------------------------------------------------- #
# SessionConflict reuse: a second live dispatch reuses the open session.
# --------------------------------------------------------------------------- #


def test_dispatch_spawn_reuses_active_session_on_second_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second live dispatch reuses the already-ACTIVE executor session.

    ``start_session`` raises ``SessionConflict`` for an existing ACTIVE
    ``(scope, runtime)`` pair; the live path catches it and reuses the open
    session rather than failing, so no duplicate session is registered.
    """
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    first: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    second: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    state = load_state(state_path)
    # Still exactly one session; the second dispatch reused the first.
    assert len(state.agent_sessions) == 1
    assert first["session_id"] == second["session_id"]
    # Two attempts of the same wave append two executor report rows.
    assert len(_executor_report_rows(state_path)) == 2


# --------------------------------------------------------------------------- #
# Criterion (d): back-compat -- spawn=False is byte-unchanged.
# --------------------------------------------------------------------------- #


def test_dispatch_plan_only_path_unchanged_when_spawn_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn=False stays plan-only: no session, no spawn, pid 0, no events."""
    state_path = _write_state(tmp_path)
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=None)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "session_policy": "fresh"}))

    assert result["pid"] == 0
    assert result["event_ids"] == []
    # No spawn ran and no session was registered.
    assert adapter.spawn_calls == 0
    assert load_state(state_path).agent_sessions == {}
    assert _executor_report_rows(state_path) == []


def test_dispatch_default_spawn_is_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting ``spawn`` entirely defaults to the plan-only path (no spawn)."""
    state_path = _write_state(tmp_path)
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=None)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID}))

    assert result["pid"] == 0
    assert adapter.spawn_calls == 0


# --------------------------------------------------------------------------- #
# Error paths.
# --------------------------------------------------------------------------- #


def test_dispatch_spawn_without_state_path_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn=True without a state_path fails fast with a typed LiveSpawnError."""
    _patch_adapter(monkeypatch, _StubAdapter())
    # No state_path; an event_path alone is not enough for the live path.
    ctx = _ctx(None, event_path=tmp_path / "store" / "event.jsonl")

    with pytest.raises(LiveSpawnError, match="requires state_path"):
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))


def test_dispatch_spawn_without_event_path_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn=True without an event_path fails fast with a typed LiveSpawnError."""
    state_path = _write_state(tmp_path)
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=None)

    with pytest.raises(LiveSpawnError, match="requires state_path"):
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    # No spawn was attempted -- the guard fires first.
    assert adapter.spawn_calls == 0


def test_dispatch_spawn_and_outcome_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing both spawn=True and an outcome is rejected before any spawn."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    outcome = {
        "model": "claude-opus-4-8",
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": "0.01",
    }
    with pytest.raises(ValueError, match="mutually exclusive"):
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True, "outcome": outcome}))
    assert adapter.spawn_calls == 0


def test_dispatch_spawn_rejects_unknown_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live dispatch for a wave absent from state fails fast."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    with pytest.raises(ValueError, match="unknown wave"):
        _run(dispatch(ctx, {"wave_id": "P29-I04-W99", "spawn": True}))
