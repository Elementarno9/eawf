"""Multi-root serve: mutations honour an explicit per-request state root.

History: on 2026-07-01 a machine-global daemon bound to a smoke-fixture
state served ``dispatch resume`` aimed at the real repo and mutated the
FIXTURE (A2 EP3) — the OMITTED ``repo_root`` fell back silently to the
boot anchor. P30-I23-W11 answered with a refusal of every mismatched
root; the multi-root hotfix replaces the refusal with correct routing:
an explicit ``repo_root`` is served against ITS OWN state/event/WAL
paths, and the boot root is never touched by a cross-root request.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from eawf import __version__
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import (
    MethodContext,
    note_cross_root_serve,
)
from eawf.runtime.daemon.methods.agent import resume as agent_resume
from eawf.runtime.daemon.methods.state import mutate

pytestmark = pytest.mark.unit


def _ctx(state_path: Path, tmp_path: Path) -> MethodContext:
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
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


def _valid_state_payload() -> dict[str, Any]:
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
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_valid_state(repo: Path) -> Path:
    from eawf.kernel.state.models import State

    state_path = repo / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        State.model_validate(_valid_state_payload()).model_dump_json(), encoding="utf-8"
    )
    return state_path


def _run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(body())


# ---- the note primitive ------------------------------------------------------


def test_note_allows_mismatched_root(tmp_path: Path) -> None:
    bound = tmp_path / "bound-repo" / ".ea" / "state.json"
    bound.parent.mkdir(parents=True)
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    ctx = _ctx(bound, tmp_path)
    note_cross_root_serve(ctx, repo_root=str(other_repo), command="probe")


def test_note_passes_matching_and_omitted_root(tmp_path: Path) -> None:
    bound_repo = tmp_path / "bound-repo"
    bound = bound_repo / ".ea" / "state.json"
    bound.parent.mkdir(parents=True)
    ctx = _ctx(bound, tmp_path)
    note_cross_root_serve(ctx, repo_root=str(bound_repo), command="probe")
    note_cross_root_serve(ctx, repo_root=None, command="probe")


# ---- CR-01 flipped: a cross-root mutation is served against ITS root ---------


def test_mutation_rpc_cross_root_served_against_target(tmp_path: Path) -> None:
    bound_repo = tmp_path / "bound-repo"
    bound = bound_repo / ".ea" / "state.json"
    bound.parent.mkdir(parents=True)
    bound.write_text("{}", encoding="utf-8")
    other_repo = tmp_path / "other-repo"
    other_state = _write_valid_state(other_repo)
    other_before = other_state.read_bytes()
    ctx = _ctx(bound, tmp_path)
    params = {
        "mutation": Mutation(
            kind=MutationKind.EVENT_APPEND,
            scope_id="ABC",
            mutation_id=uuid.uuid4().hex,
            params={"event_type": "probe"},
        ).model_dump(mode="json"),
        "repo_root": str(other_repo),
    }
    bound_before = bound.read_bytes()

    async def body() -> None:
        result = await mutate(ctx, params)
        assert result["idempotent_replay"] is False

    _run(body)
    # The boot root the daemon is bound to was never touched.
    assert bound.read_bytes() == bound_before
    # The TARGET repo's state advanced (updated_at bump) and its own
    # event log carries the appended envelope.
    assert other_state.read_bytes() != other_before
    other_events = other_repo / ".ea" / "store" / "event.jsonl"
    assert other_events.exists()
    # The WAL record is stamped with the target state path so crash
    # replay routes the envelope back to the target repo.
    fsynced = list((tmp_path / "wal").glob("*.fsynced.json"))
    assert len(fsynced) == 1
    record = json.loads(fsynced[0].read_text(encoding="utf-8"))
    assert record["state_path"] == str(other_state)


# ---- CR-02 flipped: the EP3 reproduction now routes to the real repo ---------


def test_dispatch_resume_cross_root_served(tmp_path: Path) -> None:
    fixture_repo = tmp_path / "fixture-repo"
    fixture = fixture_repo / ".ea" / "state.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("{}", encoding="utf-8")
    real_repo = tmp_path / "real-repo"
    _write_valid_state(real_repo)
    ctx = _ctx(fixture, tmp_path)
    before = fixture.read_bytes()

    async def body() -> None:
        result = await agent_resume(ctx, {"repo_root": str(real_repo)})
        assert result["paused"] is False

    _run(body)
    # The fixture state the daemon is bound to was never touched.
    assert fixture.read_bytes() == before


def test_dispatch_resume_matching_root_proceeds(tmp_path: Path) -> None:
    """The happy path: a matching root toggles the flag as before."""
    bound_repo = tmp_path / "bound-repo"
    bound = _write_valid_state(bound_repo)
    ctx = _ctx(bound, tmp_path)

    async def body() -> None:
        result = await agent_resume(ctx, {"repo_root": str(bound_repo)})
        assert result["paused"] is False

    _run(body)
