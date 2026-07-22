"""Tests: resolve_routing is wired into live dispatch + the idle-contract row.

The ``(agent_role, effort_bucket) -> model`` routing table and its
:func:`eawf.workflow.dispatch.routing.resolve_routing` resolver were built but
only reachable through the per-vendor wrapper, so the live wave-spawn dispatch
measured a constant model. P30-I10-W10 wires ``resolve_routing`` into the live
dispatch path (``agent.py``'s ``_resolve_spawn_model``) so a wave's role + effort
selects its configured model tier, and registers an idle-contract row that reds
if the wiring is dropped.

Two contracts are asserted, one per wave success criterion:

- **C1 (live selection)** -- the live ``dispatch(spawn=True)`` path drives the
  adapter ``spawn_session`` with the model the routing table maps the wave's
  ``(role, effort)`` onto. Two distinct roles at the same effort resolve to
  distinct models (role drives the pick), and a wave that omits ``agent_role``
  falls back to the documented executor / medium-effort default. The adapter is
  a monkeypatched stub -- no real subprocess, no network, no cost.
- **C2 (idle-contract row)** -- :func:`tools.idle_contract_gate.check_resolve_routing_wired`
  passes for the real live-dispatch source AND reds when the
  ``resolve_routing(`` call is removed (an injected idle text), so dropping the
  wiring fails the gate.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf import __version__
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.agent import dispatch
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.dispatch.routing import resolve_routing

pytestmark = pytest.mark.integration

_WAVE_ID = "P30-I10-W10"
_T0 = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 11, 12, 0, 5, tzinfo=UTC)
_STUB_PID = 24680

#: Repo root: this file is ``tests/workflow/...`` so the root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Load the gate module by path (tools/ is not an installed package).
# --------------------------------------------------------------------------- #


def _load_gate_module() -> Any:
    """Import ``tools/idle_contract_gate.py`` by path for the C2 probe.

    The module is registered in ``sys.modules`` before ``exec_module`` so the
    gate's ``@dataclass`` definitions (which resolve ``cls.__module__`` under
    ``from __future__ import annotations``) find their own namespace.
    """
    name = "idle_contract_gate_under_test"
    path = _REPO_ROOT / "tools" / "idle_contract_gate.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


# --------------------------------------------------------------------------- #
# Stub adapter + on-disk state fixtures (mirrors test_live_spawn_dispatch).
# --------------------------------------------------------------------------- #


def _report_json(*, role: str, wave_id: str = _WAVE_ID) -> str:
    """A canonical-invariant-valid role report JSON for the routing probe."""
    import json

    if role == "reviewer":
        body: dict[str, object] = {
            "role": "reviewer",
            "verdict": "pass",
            "confidence": "high",
            "summary": "reviewer verified the routed wave",
            "target_id": wave_id,
            "findings": [],
            "coverage_refs": [
                {
                    "kind": "artifact",
                    "ref": "tests/workflow/test_resolve_routing_wired.py:1",
                    "note": "routing probe",
                }
            ],
        }
    else:
        body = {
            "role": "executor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "executor implemented the wave",
            "wave_id": wave_id,
            "files_changed": ["src/eawf/runtime/daemon/methods/agent.py"],
            "tests_run": ["uv run pytest tests/workflow -q"],
            "commit_sha": "abc1234",
            "outcome": "wired resolve_routing into live dispatch",
        }
    return json.dumps(body)


class _RecordingAdapter:
    """A RuntimeAdapter stub that records the model it is handed, no subprocess.

    ``resolved_model`` is left ``None`` so the metering prices against the
    requested model the routing resolved -- and so the recorded ``models`` list
    is exactly the routing-selected id under test.
    """

    id = "claude-code"
    cli_binary = "claude"

    def __init__(self, *, role: str) -> None:
        self.models: list[str] = []
        self.role = role

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
        self.models.append(model)
        if on_spawn is not None:
            on_spawn(_STUB_PID)
        return SpawnResult(
            session_id="sess-routing-probe",
            runtime="claude-code",
            model=model,
            resolved_model=None,
            subprocess_pid=_STUB_PID,
            exit_status=0,
            text=_report_json(role=self.role),
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


def _state_payload(*, agent_role: str | None, effort_bucket: str | None) -> dict[str, Any]:
    """A minimal valid State with the full phase -> iter -> wave chain.

    ``agent_role`` / ``effort_bucket`` are passed through verbatim so a test can
    omit them (``None``) to exercise the documented-default fallback.
    """
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-11T00:00:00Z",
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
            "iter_id": "P30-I10",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "EAWF",
                "title": "v0.6",
                "status": "active",
                "iter_ids": ["P30-I10"],
                "outcome_ids": [],
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I10": {
                "id": "P30-I10",
                "phase_id": "P30",
                "title": "Substrate",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P30-I10",
                "title": "Wire resolve_routing into live spawn",
                "status": "pending",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/daemon/methods/agent.py"],
                "success_criteria": [
                    {
                        "id": "CR-01",
                        "text": "live dispatch calls resolve_routing",
                        "kind": "legacy",
                        "acceptance_style": "binary",
                        "evidence_kind": "attested",
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": "live dispatch calls resolve_routing",
                    }
                ],
                "agent_role": agent_role,
                "effort_bucket": effort_bucket,
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-11T00:00:00Z",
                "claimed_at": None,
                "closed_at": None,
                "runtime_preference": ["claude-code"],
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, *, agent_role: str | None, effort_bucket: str | None) -> Path:
    """Serialise a valid :class:`State` to ``<tmp>/.ea/state.json``."""
    from eawf.kernel.state.models import State

    state = State.model_validate(_state_payload(agent_role=agent_role, effort_bucket=effort_bucket))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True)
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path, *, event_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=4321,
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
    )


def _patch_adapter(monkeypatch: pytest.MonkeyPatch, adapter: _RecordingAdapter) -> None:
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.agent.select_adapter",
        lambda runtime_id: adapter,
    )


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _dispatch_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_role: str | None,
    effort_bucket: str | None,
) -> str:
    """Drive one live spawn and return the model the routing selected for it."""
    state_path = _write_state(tmp_path, agent_role=agent_role, effort_bucket=effort_bucket)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _RecordingAdapter(role=agent_role or "executor")
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)
    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    assert len(adapter.models) == 1
    return adapter.models[0]


# --------------------------------------------------------------------------- #
# C1: the live dispatch path selects the model via resolve_routing.
# --------------------------------------------------------------------------- #


def test_live_dispatch_selects_role_mapped_model_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An executor / M wave dispatches with the routing-mapped sonnet tier."""
    model = _dispatch_model(tmp_path, monkeypatch, agent_role="executor", effort_bucket="M")
    assert model == "claude-sonnet-4-6"
    # The selected model is exactly what resolve_routing maps the pair onto.
    from eawf.kernel.state.enums import AgentSessionRole, EffortBucket

    assert model == resolve_routing(AgentSessionRole.EXECUTOR, EffortBucket.M).model


def test_live_dispatch_selects_role_mapped_model_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer / M wave bumps one tier: it dispatches with the opus tier.

    Same effort as the executor case, a different role -- proving role (not just
    effort) drives the live model selection through resolve_routing.
    """
    model = _dispatch_model(tmp_path, monkeypatch, agent_role="reviewer", effort_bucket="M")
    assert model == "claude-opus-4-8"
    from eawf.kernel.state.enums import AgentSessionRole, EffortBucket

    assert model == resolve_routing(AgentSessionRole.REVIEWER, EffortBucket.M).model


def test_two_distinct_roles_dispatch_with_distinct_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two distinct roles at one effort resolve to two distinct live models."""
    executor_model = _dispatch_model(
        tmp_path / "exec", monkeypatch, agent_role="executor", effort_bucket="M"
    )
    reviewer_model = _dispatch_model(
        tmp_path / "rev", monkeypatch, agent_role="reviewer", effort_bucket="M"
    )
    assert executor_model != reviewer_model
    assert {executor_model, reviewer_model} == {"claude-sonnet-4-6", "claude-opus-4-8"}


def test_live_dispatch_falls_back_to_executor_default_when_role_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wave omitting role falls back to executor with its required M bucket.

    Claim lifecycle requires an explicit effort bucket, while routing defaults
    the omitted role to executor; executor/M resolves to the sonnet tier.
    """
    model = _dispatch_model(tmp_path, monkeypatch, agent_role=None, effort_bucket="M")
    from eawf.kernel.state.enums import AgentSessionRole, EffortBucket

    assert model == resolve_routing(AgentSessionRole.EXECUTOR, EffortBucket.M).model
    assert model == "claude-sonnet-4-6"


# --------------------------------------------------------------------------- #
# C2: the idle-contract row asserts resolve_routing has a live dispatch caller.
# --------------------------------------------------------------------------- #


def test_idle_contract_row_passes_for_real_live_dispatch_source() -> None:
    """check_resolve_routing_wired passes against the real agent.py source."""
    gate = _load_gate_module()
    result = gate.check_resolve_routing_wired()
    assert result.passed, result.message
    assert result.failure is None


def test_idle_contract_row_reds_when_wiring_removed() -> None:
    """Removing the resolve_routing call reds the gate (RESOLVE_ROUTING_IDLE).

    The probe scans the live dispatch source for a direct ``resolve_routing(``
    call; an injected text with no such call simulates a refactor that reverted
    to a hardcoded default, and the gate must fail.
    """
    gate = _load_gate_module()
    idle_text = (
        "def _resolve_spawn_model(state_path, *, wave_id, runtime, override):\n"
        "    return override or 'claude-opus-4-8'  # hardcoded default, no routing\n"
    )
    result = gate.check_resolve_routing_wired(module_text=idle_text)
    assert not result.passed
    assert result.failure is gate.GateFailure.RESOLVE_ROUTING_IDLE
    assert "resolve_routing is idle" in result.message


def test_idle_contract_row_passes_for_minimal_wired_text() -> None:
    """A boundary text carrying just the resolve_routing call satisfies the gate."""
    gate = _load_gate_module()
    wired_text = "    decision = resolve_routing(role, effort)\n"
    result = gate.check_resolve_routing_wired(module_text=wired_text)
    assert result.passed
    assert result.failure is None


def test_idle_contract_row_reds_on_docstring_only_reference() -> None:
    """A bare ``resolve_routing`` reference (no call paren) does not satisfy the gate.

    Boundary: a docstring / cross-link mention of the symbol with no trailing
    ``(`` is not a live call, so the gate must still red on it.
    """
    gate = _load_gate_module()
    prose_only = (
        "    # the model is resolved via resolve_routing per the docstring\n"
        "    return 'claude-opus-4-8'\n"
    )
    result = gate.check_resolve_routing_wired(module_text=prose_only)
    assert not result.passed
    assert result.failure is gate.GateFailure.RESOLVE_ROUTING_IDLE
