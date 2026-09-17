"""Spawn attempt rows and Codex bindings carry hashed vendor ids and no pid.

The daemon's live spawn writes its session attempt after the child has exited,
so the row records the spawn's vendor session id as a digest and no
``subprocess_pid``. The Codex provider-session binding stores the same digest,
and both the session exit path and a Codex ``runtime.capture`` resolve through
it from the raw id the runtime reports, including a binding that still carries
a raw id.

The live spawn runs against a stub adapter (no process, no network) and every
state file lives under ``tmp_path``. Raw ids are derived at runtime so the file
carries no literal session id.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.state.enums import AgentSessionStatus, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.agent import dispatch, kill
from eawf.runtime.daemon.methods.state import codex_lifecycle, runtime_capture
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.runtime.session.store import stamp_session_end_at_exit
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.workflow.evidence._io import load_state
from tests.integration.runtime.daemon.test_live_spawn_dispatch import (
    _STUB_PID,
    _WAVE_ID,
    _patch_adapter,
    _StubAdapter,
    _write_state,
)
from tests.integration.runtime.daemon.test_live_spawn_dispatch import (
    _ctx as spawn_ctx,
)
from tests.integration.runtime.daemon.test_vendor_session_capture_hash import (
    _T0,
    WAVE_A,
    WAVE_B,
    capture_params,
    method_ctx,
    rpc,
    state_payload,
)

pytestmark = pytest.mark.integration

RAW_SPAWN_SESSION = str(uuid.uuid5(uuid.NAMESPACE_URL, "spawned-session"))
RAW_PROVIDER_SESSION = str(uuid.uuid5(uuid.NAMESPACE_URL, "codex-provider-session"))
RAW_AGENT_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "codex-subagent"))
CODEX_SESSION = "SES-codex-1"


class _VendorSessionAdapter(_StubAdapter):
    """The live-spawn stub, reporting a UUID-shaped vendor session id."""

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
        result = await super().spawn_session(
            prompt,
            model=model,
            cwd=cwd,
            extra_args=extra_args,
            denied_tools=denied_tools,
            timeout=timeout,
            on_spawn=on_spawn,
            on_chunk=on_chunk,
        )
        return result.model_copy(update={"session_id": RAW_SPAWN_SESSION})


def _codex_payload(*, runtime_session_id: str | None) -> dict[str, Any]:
    """Two active waves and one Codex session scoped to ``WAVE_B``."""
    payload = state_payload(active_wave_ids=[WAVE_A, WAVE_B])
    payload["agent_sessions"] = {
        CODEX_SESSION: {
            "id": CODEX_SESSION,
            "role": "executor",
            "runtime": "codex",
            "runtime_session_id": runtime_session_id,
            "scope_id": WAVE_B,
            "status": "active",
            "claimed_wave_ids": [WAVE_B],
            "started_at": _T0.isoformat(),
        }
    }
    payload["current"]["active_session_ids"] = [CODEX_SESSION]
    return payload


def _load(state_path: Path) -> State:
    return State.model_validate(orjson.loads(state_path.read_bytes()))


def test_dispatch_spawn_attempt_row_hashes_session_and_drops_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _VendorSessionAdapter())
    hashed = hash_vendor_session_id(RAW_SPAWN_SESSION)

    result = rpc(
        dispatch(spawn_ctx(state_path, event_path=event_path), {"wave_id": _WAVE_ID, "spawn": True})
    )

    wave = load_state(state_path).waves[_WAVE_ID]
    attempt = wave.sessions[result["attempt"]]
    assert attempt.session_id == hashed
    assert attempt.session_log_handle == f"urn:eawf:v1:session-log:claude-code:{hashed}"
    assert attempt.subprocess_pid is None
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.session_id == hashed
    assert wave.runtime_latest is not None
    assert wave.runtime_latest.session_id == hashed
    written = orjson.loads(state_path.read_bytes())
    assert written["waves"][_WAVE_ID]["sessions"][str(result["attempt"])]["subprocess_pid"] is None
    assert RAW_SPAWN_SESSION not in state_path.read_text(encoding="utf-8")
    # The in-flight pid still reaches the caller; only the persisted row drops it.
    assert result["pid"] == _STUB_PID
    assert result["session_attempt"]["subprocess_pid"] is None
    assert result["session_attempt"]["session_id"] == hashed


def test_kill_pidless_spawn_attempt_signals_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _VendorSessionAdapter())
    ctx = spawn_ctx(state_path, event_path=event_path)
    rpc(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    signalled: list[int] = []
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.fleet.cancel_process_group",
        lambda pgid, *, hard=False: signalled.append(pgid),
    )

    result = rpc(kill(ctx, {"wave_id": _WAVE_ID, "attempt": 1, "signal": "kill"}))

    assert result == {"killed": False, "signal": "kill", "reason": "unkillable-session"}
    assert signalled == []


def test_codex_lifecycle_binding_and_subagent_row_store_hashes(tmp_path: Path) -> None:
    ctx, state_path = method_ctx(tmp_path, _codex_payload(runtime_session_id=None))

    async def body() -> list[dict[str, Any]]:
        results = []
        for event_type, agent_id, minutes in (
            ("session_start", None, 0),
            ("subagent_start", RAW_AGENT_ID, 1),
            ("subagent_stop", RAW_AGENT_ID, 2),
            ("session_end", None, 3),
        ):
            params: dict[str, Any] = {
                "event_type": event_type,
                "provider_session_id": RAW_PROVIDER_SESSION,
                "occurred_at": (_T0 + timedelta(minutes=minutes)).isoformat(),
            }
            if agent_id is not None:
                params["agent_id"] = agent_id
            results.append(await codex_lifecycle(ctx, params))
        return results

    started, sub_started, sub_stopped, ended = asyncio.run(body())

    assert started["correlated"] is True
    assert started["agent_session_id"] == CODEX_SESSION
    assert sub_started["attempt"] == 1
    assert sub_stopped["correlated"] is True
    assert sub_stopped["attempt"] == 1
    assert ended["correlated"] is True
    assert ended["wave_id"] == WAVE_B
    state = _load(state_path)
    session = state.agent_sessions[CODEX_SESSION]
    assert session.runtime_session_id == hash_vendor_session_id(RAW_PROVIDER_SESSION)
    attempts = state.waves[WAVE_B].sessions
    assert set(attempts) == {1}
    assert attempts[1].session_id == hash_vendor_session_id(RAW_AGENT_ID)
    assert attempts[1].ended_at == _T0 + timedelta(minutes=2)
    text = state_path.read_text(encoding="utf-8")
    assert RAW_PROVIDER_SESSION not in text
    assert RAW_AGENT_ID not in text


@pytest.mark.parametrize(
    "stored_binding",
    [hash_vendor_session_id(RAW_PROVIDER_SESSION), RAW_PROVIDER_SESSION],
    ids=["hashed", "legacy-raw"],
)
def test_runtime_capture_codex_resolves_wave_through_binding(
    tmp_path: Path, stored_binding: str
) -> None:
    ctx, state_path = method_ctx(tmp_path, _codex_payload(runtime_session_id=stored_binding))

    result = rpc(
        runtime_capture(ctx, capture_params(harness="codex", session_id=RAW_PROVIDER_SESSION))
    )

    assert result["active_wave_ids"] == [WAVE_B]
    state = _load(state_path)
    assert state.waves[WAVE_A].runtime_latest is None
    latest = state.waves[WAVE_B].runtime_latest
    assert latest is not None
    assert latest.session_id == hash_vendor_session_id(RAW_PROVIDER_SESSION)


def test_runtime_capture_codex_unbound_id_stays_ambiguous(tmp_path: Path) -> None:
    ctx, _state_path = method_ctx(
        tmp_path,
        _codex_payload(runtime_session_id=hash_vendor_session_id(RAW_PROVIDER_SESSION)),
    )

    with pytest.raises(DaemonValidationError, match="correlation ambiguous"):
        rpc(runtime_capture(ctx, capture_params(harness="codex", session_id=RAW_AGENT_ID)))


def test_session_exit_path_resolves_binding_made_by_codex_start(tmp_path: Path) -> None:
    ctx, state_path = method_ctx(tmp_path, _codex_payload(runtime_session_id=None))
    rpc(
        codex_lifecycle(
            ctx,
            {
                "event_type": "session_start",
                "provider_session_id": RAW_PROVIDER_SESSION,
                "occurred_at": _T0.isoformat(),
            },
        )
    )

    stamped = stamp_session_end_at_exit(
        state_path,
        store_path(state_path, StoreKind.EVENT),
        runtime_session_id=RAW_PROVIDER_SESSION,
        now=_T0 + timedelta(minutes=4),
    )

    assert stamped == CODEX_SESSION
    session = _load(state_path).agent_sessions[CODEX_SESSION]
    assert session.status is AgentSessionStatus.CLOSED
    assert session.ended_at == _T0 + timedelta(minutes=4)


@pytest.mark.parametrize(
    "stored_binding",
    [hash_vendor_session_id(RAW_PROVIDER_SESSION), RAW_PROVIDER_SESSION],
    ids=["hashed", "legacy-raw"],
)
def test_stamp_session_end_at_exit_resolves_raw_exit_id(
    tmp_path: Path, stored_binding: str
) -> None:
    payload = _codex_payload(runtime_session_id=stored_binding)
    payload["agent_sessions"]["SES-other"] = payload["agent_sessions"][CODEX_SESSION] | {
        "id": "SES-other",
        "runtime_session_id": None,
        "scope_id": WAVE_A,
        "claimed_wave_ids": [WAVE_A],
    }
    _ctx, state_path = method_ctx(tmp_path, payload)

    stamped = stamp_session_end_at_exit(
        state_path,
        store_path(state_path, StoreKind.EVENT),
        runtime_session_id=RAW_PROVIDER_SESSION,
        now=_T0 + timedelta(minutes=4),
    )

    assert stamped == CODEX_SESSION
    state = _load(state_path)
    assert state.agent_sessions["SES-other"].status is AgentSessionStatus.ACTIVE
    assert state.agent_sessions[CODEX_SESSION].runtime_session_id == stored_binding
