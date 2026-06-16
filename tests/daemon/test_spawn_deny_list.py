"""Tests: per-wave sandbox deny-list reaches the spawn argv (P29-I04-W02).

Exercises the deny-list seam of the opt-in live-spawn path
(:func:`eawf.runtime.daemon.methods.agent.dispatch` with ``spawn=True``):
``_spawn_and_dispatch`` resolves the wave's sandbox deny-list from
``state.sandbox_policies`` via
:func:`eawf.runtime.sandbox.policy.resolve_denied_tools` and passes it as the
``denied_tools`` keyword into the adapter's ``spawn_session`` so a spawned
child CLI is launched with those tools disabled per the wave policy.

The adapter ``spawn_session`` is ALWAYS a monkeypatched stub that records the
``denied_tools`` kwarg it was handed and returns a canned
:class:`~eawf.runtime.runtimes.adapter.SpawnResult` -- no real ``claude``
subprocess, no network, no auth, no cost.

The load-bearing assertions, one per wave success criterion:

- a wave-scoped ``SandboxPolicy`` deny-list reaches the captured
  ``denied_tools`` kwarg;
- a global ``SandboxPolicy`` deny-list reaches it too, and a global + wave
  pair unions (deduped, sorted);
- the empty case (no policy targets the wave) passes an empty deny-list.
"""

from __future__ import annotations

import asyncio
import json
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

pytestmark = pytest.mark.integration

_WAVE_ID = "P29-I04-W02"
_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC)
_STUB_PID = 54321


def _executor_report_json() -> str:
    """A schema-valid ``ExecutorReportBody`` JSON the stub spawn returns.

    The live-spawn report path binds the spawned agent's OWN text to a
    validated executor report body via the schema-assist re-ask loop, so the
    stub must emit schema-valid JSON whose ``wave_id`` matches the dispatched
    wave (the verify gate equality check).
    """
    return json.dumps(
        {
            "role": "executor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "executor implemented the wave",
            "wave_id": _WAVE_ID,
            "files_changed": ["src/eawf/runtime/daemon/methods/agent.py"],
            "tests_run": ["uv run pytest tests/daemon -q"],
            "outcome": "applied the wave-scoped deny-list to the spawn",
        }
    )


class _DenyRecordingAdapter:
    """A RuntimeAdapter stand-in whose spawn_session records ``denied_tools``.

    Never forks a process; fires ``on_spawn`` with a fixed pid so the live
    path completes, and records the ``denied_tools`` kwarg so a test can
    assert the resolved deny-list flowed into the spawn.
    """

    id = "claude-code"
    cli_binary = "claude"

    def __init__(self) -> None:
        self.spawn_calls = 0
        self.denied_tools_seen: list[tuple[str, ...]] = []

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
        self.denied_tools_seen.append(tuple(denied_tools))
        if on_spawn is not None:
            on_spawn(_STUB_PID)
        return SpawnResult(
            session_id="sess-deny-abc123",
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


def _policy(
    *, policy_id: str, scope_kind: str, scope_id: str, denied_tools: list[str]
) -> dict[str, Any]:
    """One ``SandboxPolicy`` JSON row."""
    return {
        "id": policy_id,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "allowed_tools": [],
        "denied_tools": denied_tools,
        "granted_at": "2026-06-01T00:00:00Z",
    }


def _state_payload(*, sandbox_policies: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """A minimal valid State with the phase -> iter -> wave chain + policies.

    The chain is required because the live path renders the dispatch
    envelope, which walks wave -> iter -> phase -> scope. The wave starts
    CLAIMED so the runner's head transition flips it to IN_PROGRESS.
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
                "title": "Thread deny-list into spawn argv",
                "status": "claimed",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/daemon/methods/agent.py"],
                "success_criteria": [
                    {
                        "id": "CR-01",
                        "text": "deny-list reaches the argv",
                        "kind": "legacy",
                        "acceptance_style": "binary",
                        "evidence_kind": "attested",
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": "deny-list reaches the argv",
                    }
                ],
                "agent_role": "executor",
                "effort_bucket": "M",
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
        "sandbox_policies": sandbox_policies,
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


def _ctx(state_path: Path, *, event_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-06-01T00:00:00+00:00",
        pid=4321,
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
    )


def _patch_adapter(monkeypatch: pytest.MonkeyPatch, adapter: _DenyRecordingAdapter) -> None:
    """Make the live path resolve to *adapter* instead of the real claude one."""
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.agent.select_adapter",
        lambda runtime_id: adapter,
    )


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


def test_spawn_passes_wave_scoped_deny_list_to_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wave-scoped policy's deny-list reaches the captured ``denied_tools`` kwarg."""
    policies = {
        "POL-1": _policy(
            policy_id="POL-1",
            scope_kind="wave",
            scope_id=_WAVE_ID,
            denied_tools=["Bash", "Edit"],
        )
    }
    state_path = _write_state(tmp_path, sandbox_policies=policies)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _DenyRecordingAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert adapter.spawn_calls == 1
    # The deny-list resolved for the wave reaches the spawn, sorted.
    assert adapter.denied_tools_seen == [("Bash", "Edit")]


def test_spawn_passes_global_and_wave_deny_list_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Global + wave-scoped deny-lists union (deduped + sorted) into the kwarg."""
    policies = {
        "POL-1": _policy(
            policy_id="POL-1",
            scope_kind="wave",
            scope_id=_WAVE_ID,
            denied_tools=["Edit", "Bash"],
        ),
        "POL-2": _policy(
            policy_id="POL-2",
            scope_kind="global",
            scope_id="global",
            # ``Bash`` overlaps the wave policy -> deduped; ``WebFetch`` is new.
            denied_tools=["WebFetch", "Bash"],
        ),
    }
    state_path = _write_state(tmp_path, sandbox_policies=policies)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _DenyRecordingAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    # Union of {Bash, Edit} and {WebFetch, Bash}, deduped + sorted.
    assert adapter.denied_tools_seen == [("Bash", "Edit", "WebFetch")]


def test_spawn_global_only_deny_list_covers_the_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A global-only policy still reaches the wave's spawn (global covers all)."""
    policies = {
        "POL-1": _policy(
            policy_id="POL-1",
            scope_kind="global",
            scope_id="global",
            denied_tools=["WebFetch"],
        )
    }
    state_path = _write_state(tmp_path, sandbox_policies=policies)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _DenyRecordingAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert adapter.denied_tools_seen == [("WebFetch",)]


def test_spawn_no_policies_passes_empty_deny_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no sandbox policies the spawn receives an empty deny-list."""
    state_path = _write_state(tmp_path, sandbox_policies=None)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _DenyRecordingAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert adapter.denied_tools_seen == [()]


def test_spawn_unrelated_wave_policy_does_not_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deny policy scoped to a DIFFERENT wave does not reach this wave's spawn."""
    policies = {
        "POL-1": _policy(
            policy_id="POL-1",
            scope_kind="wave",
            scope_id="P29-I04-W99",
            denied_tools=["Bash"],
        )
    }
    state_path = _write_state(tmp_path, sandbox_policies=policies)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _DenyRecordingAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    # The other wave's deny policy must not bleed into this wave's spawn.
    assert adapter.denied_tools_seen == [()]
