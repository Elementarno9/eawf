"""Daemon batch landing submits durable closes after integration commit."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import CloseAttemptStatus, DependencyStage, WaveStatus
from eawf.kernel.state.models import State, Wave, WaveDependencyBarrier, wave_dependency_key
from eawf.runtime.daemon.methods import close as close_methods
from eawf.runtime.daemon.methods import state as state_methods
from eawf.runtime.worktree.create import create_worktree
from tests.daemon.test_close_lock_split import _build_ctx
from tests.integration.test_wave_land import _commit_in
from tests.integration.test_worktree_create import _claimed_state, _make_repo

_NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _two_wave_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Build two claimed worktrees joined by an integrated land barrier."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    upstream_id = "P05-I01-W01"
    downstream_id = "P05-I01-W02"
    state.waves[downstream_id] = Wave(
        id=downstream_id,
        iter_id="P05-I01",
        title="W2",
        status=WaveStatus.CLAIMED,
        deps=[upstream_id],
        file_scopes=["src/eawf/dispatch/"],
        claim_session_id="SES-002",
        opened_at=_NOW,
    )
    state.waves[upstream_id].blocks = [downstream_id]
    state.iters["P05-I01"].wave_ids = [upstream_id, downstream_id]
    state.wave_dependency_barriers[wave_dependency_key(downstream_id, upstream_id)] = (
        WaveDependencyBarrier(
            wave_id=downstream_id,
            dep_wave_id=upstream_id,
            start_after=DependencyStage.INTEGRATED,
            land_after=DependencyStage.INTEGRATED,
            reason="downstream may land after immutable upstream integration",
        )
    )
    upstream = create_worktree(state, repo_root=repo, wave_id=upstream_id)
    _commit_in((repo / upstream.path), name="upstream.txt", content="u\n", msg="upstream")
    downstream = create_worktree(state, repo_root=repo, wave_id=downstream_id)
    _commit_in(
        (repo / downstream.path),
        name="downstream.txt",
        content="d\n",
        msg="downstream",
    )
    state_path = repo / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return repo, state_path


def test_wave_land_batch_submits_durable_attempts_after_integration_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every landed wave stays CLAIMED and returns its queued close attempt."""
    repo, state_path = _two_wave_repo(tmp_path)
    ctx = _build_ctx(tmp_path, state_path)
    submit_in_flight: list[int] = []
    schedule_in_flight: list[int] = []
    real_submit = close_methods.submit

    async def _guarded_submit(
        submit_ctx: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        submit_in_flight.append(submit_ctx.in_flight_mutations)
        return await real_submit(submit_ctx, params)

    def _record_schedule(
        schedule_ctx: Any,
        *,
        repo_root: Path,
        attempt_id: str,
    ) -> bool:
        del repo_root, attempt_id
        schedule_in_flight.append(schedule_ctx.in_flight_mutations)
        return True

    monkeypatch.setattr(close_methods, "submit", _guarded_submit)
    monkeypatch.setattr(close_methods, "_schedule", _record_schedule)

    async def body() -> dict[str, Any]:
        return await state_methods.wave_land_batch_rpc(
            ctx,
            {
                "repo_root": str(repo),
                "iter_id": "P05-I01",
                "ready_only": False,
                "keep_worktree": True,
            },
        )

    result = asyncio.run(body())

    assert result["close_mode"] == "durable_async"
    assert result["failed_wave"] is None
    assert result["barrier_requirements"] == {}
    assert [row["wave"] for row in result["landed"]] == [
        "P05-I01-W01",
        "P05-I01-W02",
    ]
    assert submit_in_flight == [0, 0]
    assert schedule_in_flight == [0, 0]
    assert all(row["closed"] is False for row in result["landed"])
    assert all(row["close_backgrounded"] is True for row in result["landed"])
    assert all(row["close_attempt"]["status"] == "queued" for row in result["landed"])

    persisted = State.model_validate_json(state_path.read_bytes())
    assert persisted.waves["P05-I01-W01"].status is WaveStatus.CLAIMED
    assert persisted.waves["P05-I01-W02"].status is WaveStatus.CLAIMED
    attempt_ids = [row["close_attempt"]["id"] for row in result["landed"]]
    assert set(attempt_ids) <= set(persisted.close_attempts)
    assert all(
        persisted.close_attempts[attempt_id].status is CloseAttemptStatus.QUEUED
        for attempt_id in attempt_ids
    )
