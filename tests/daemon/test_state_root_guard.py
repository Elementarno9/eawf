"""EP3 state-root guard: mutations refuse a mismatched root (P30-I23-W11).

On 2026-07-01 a machine-global daemon bound to a smoke-fixture state
served ``dispatch resume`` aimed at the real repo and mutated the
FIXTURE (A2 EP3): the per-request ``repo_root`` fell back silently.
Now every mutation-bearing method refuses a caller whose intended state
root differs from the daemon-bound root, with a typed error naming both
paths and ZERO state write; read-only methods keep the fallback.
"""

from __future__ import annotations

import asyncio
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
    DaemonValidationError,
    MethodContext,
    require_bound_state_root,
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


def _run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(body())


# ---- the guard primitive -----------------------------------------------------


def test_guard_refuses_mismatched_root(tmp_path: Path) -> None:
    bound = tmp_path / "bound-repo" / ".ea" / "state.json"
    bound.parent.mkdir(parents=True)
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    ctx = _ctx(bound, tmp_path)
    with pytest.raises(DaemonValidationError, match="wrong-state-root") as excinfo:
        require_bound_state_root(ctx, repo_root=str(other_repo), command="probe")
    # The typed error names BOTH paths so the operator sees the mismatch.
    message = str(excinfo.value)
    assert "other-repo" in message
    assert "bound-repo" in message


def test_guard_passes_matching_and_omitted_root(tmp_path: Path) -> None:
    bound_repo = tmp_path / "bound-repo"
    bound = bound_repo / ".ea" / "state.json"
    bound.parent.mkdir(parents=True)
    ctx = _ctx(bound, tmp_path)
    require_bound_state_root(ctx, repo_root=str(bound_repo), command="probe")
    require_bound_state_root(ctx, repo_root=None, command="probe")


# ---- CR-01: a mismatched mutation RPC is refused with zero state write -------


def test_mutation_rpc_with_mismatched_root_refused_no_write(tmp_path: Path) -> None:
    bound_repo = tmp_path / "bound-repo"
    bound = bound_repo / ".ea" / "state.json"
    bound.parent.mkdir(parents=True)
    bound.write_text("{}", encoding="utf-8")
    other_repo = tmp_path / "other-repo"
    (other_repo / ".ea").mkdir(parents=True)
    ctx = _ctx(bound, tmp_path)
    mutation = {
        "mutation": Mutation(
            kind=MutationKind.ROADMAP_REVISE,
            scope_id="P01",
            mutation_id=uuid.uuid4().hex,
            params={"op": "retitle", "wave_id": "P01-I01-W01", "title": "x"},
        ).model_dump(mode="json"),
        "repo_root": str(other_repo),
    }
    before = bound.read_bytes()

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="wrong-state-root"):
            await mutate(ctx, mutation)

    _run(body)
    assert bound.read_bytes() == before
    assert not (other_repo / ".ea" / "state.json").exists()


# ---- CR-02: the EP3 reproduction — dispatch resume refused -------------------


def test_dispatch_resume_with_mismatched_root_refused(tmp_path: Path) -> None:
    bound_repo = tmp_path / "fixture-repo"
    bound = bound_repo / ".ea" / "state.json"
    bound.parent.mkdir(parents=True)
    bound.write_text("{}", encoding="utf-8")
    real_repo = tmp_path / "real-repo"
    real_repo.mkdir()
    ctx = _ctx(bound, tmp_path)
    before = bound.read_bytes()

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="wrong-state-root"):
            await agent_resume(ctx, {"repo_root": str(real_repo)})

    _run(body)
    # The fixture state the daemon is bound to was never touched.
    assert bound.read_bytes() == before


def test_dispatch_resume_matching_root_proceeds(tmp_path: Path) -> None:
    """The happy path: a matching root toggles the flag as before."""
    from eawf.kernel.state.models import State

    bound_repo = tmp_path / "bound-repo"
    bound = bound_repo / ".ea" / "state.json"
    bound.parent.mkdir(parents=True)
    payload: dict[str, Any] = {
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
    bound.write_text(State.model_validate(payload).model_dump_json(), encoding="utf-8")
    ctx = _ctx(bound, tmp_path)

    async def body() -> None:
        result = await agent_resume(ctx, {"repo_root": str(bound_repo)})
        assert result["paused"] is False

    _run(body)
