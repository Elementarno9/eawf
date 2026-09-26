"""The agent daemon's state writers refuse a post-mutation state that fails validation.

Every other daemon writer re-validates before ``write_state_unlocked``; the
agent writers (pause/resume, the live-session claim, the attempt persist,
the dispatch-state reassert) route their payload through
``_validated_state_payload`` so an invariant-breaking state never lands.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from eawf import __version__
from eawf.kernel.state.models import State
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.agent import _validated_state_payload
from eawf.runtime.daemon.methods.agent import pause as agent_pause

pytestmark = pytest.mark.unit


def _payload(*, phase_id: str | None = None) -> dict[str, Any]:
    current: dict[str, Any] = {"project_code": "ABC"}
    if phase_id is not None:
        current["phase_id"] = phase_id
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": "2026-07-02T00:00:00Z",
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "Abc",
            "domains": ["infra"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": current,
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, payload: dict[str, Any]) -> Path:
    state_path = tmp_path / "repo" / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(State.model_validate(payload).model_dump_json(), encoding="utf-8")
    return state_path


def _ctx(state_path: Path, tmp_path: Path) -> MethodContext:
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    return MethodContext(
        started_at="2026-07-02T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=None,
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )


def test_validated_state_payload_returns_the_dump_for_a_valid_state() -> None:
    state = State.model_validate(_payload())

    assert _validated_state_payload(state, writer="t") == state.model_dump(mode="json")


def test_validated_state_payload_refuses_an_invariant_violation() -> None:
    state = State.model_validate(_payload(phase_id="P99"))

    with pytest.raises(DaemonValidationError, match="validation_failed: t post-mutation"):
        _validated_state_payload(state, writer="t")


def test_pause_refuses_to_write_an_invalid_state(tmp_path: Path) -> None:
    state_path = _write_state(tmp_path, _payload(phase_id="P99"))
    before = state_path.read_bytes()

    with pytest.raises(DaemonValidationError, match=r"agent\.pause post-mutation state invalid"):
        asyncio.run(agent_pause(_ctx(state_path, tmp_path), {}))

    assert state_path.read_bytes() == before


def test_pause_writes_a_valid_state(tmp_path: Path) -> None:
    state_path = _write_state(tmp_path, _payload())

    result = asyncio.run(agent_pause(_ctx(state_path, tmp_path), {}))

    assert result["paused"] is True
    assert State.model_validate_json(state_path.read_text(encoding="utf-8")).dispatch_paused
