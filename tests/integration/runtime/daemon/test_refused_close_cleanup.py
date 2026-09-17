"""A refused or cancelled close removes its workspace before its row lands.

Each test drives the real ``_run_attempt`` worker against a Git repository
under ``tmp_path``. A probe wraps ``commit_attempt`` and records whether the
attempt's ``.ea/worktrees/close/<attempt>`` directory still exists at the
moment the row that ends the attempt is written, which is the moment a
``close.status`` watcher can first see it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec, QualityDimension
from eawf.kernel.state.enums import CloseAttemptStatus, CloseFailureKind
from eawf.kernel.state.models import State
from eawf.runtime.daemon import close_workspace
from eawf.runtime.daemon.close_workspace import CloseWorkspaceError, workspace_path
from eawf.runtime.daemon.methods import close as close_module
from eawf.runtime.daemon.methods.close import (
    _CLOSE_TASKS,
    _close_task_key,
    _run_attempt,
    cancel,
    submit,
)
from tests.integration.runtime.daemon.test_close_lock_split import _WAVE
from tests.integration.runtime.daemon.test_durable_close import _repo_with_state

pytestmark = pytest.mark.integration

_TERMINAL_COMMANDS = frozenset({"close.failed", "close.cancelled"})
_REAL_CLEANUP = close_workspace.cleanup_close_workspace


def _patch_cleanup(monkeypatch: pytest.MonkeyPatch, replacement: Callable[..., bool]) -> None:
    """Replace workspace removal at both seams the close worker calls it through."""
    monkeypatch.setattr(close_workspace, "cleanup_close_workspace", replacement)
    monkeypatch.setattr(close_module, "cleanup_close_workspace", replacement)


def _install_failing_gate(repo: Path, state_path: Path) -> None:
    """Bind one enforced gate that the integrated revision cannot satisfy."""
    profile_dir = repo / ".ea" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (repo / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled:\n    - refused-close\n",
        encoding="utf-8",
    )
    (profile_dir / "refused-close.yaml").write_text(
        "name: refused-close\nverify:\n  enforce: true\n  cross_vendor_jury: false\n",
        encoding="utf-8",
    )
    criterion = CriterionSpec(
        id="CR-01",
        text="the integrated revision ships the absent payload",
        kind="contract",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["G-01"],
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="absent.txt exists in the integrated checkout",
    )
    gate = GateSpec(
        id="G-01",
        criterion_id=criterion.id,
        kind="file_exists",
        args={"path": "absent.txt"},
        policy="block",
        cadence="every-wave",
    )
    state = State.model_validate_json(state_path.read_bytes())
    state.waves[_WAVE] = state.waves[_WAVE].model_copy(
        update={"success_criteria": [criterion], "gates": [gate]}
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")


def _probe_terminal_commits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Record ``(command, workspace_exists)`` for every attempt-ending commit."""
    seen: list[tuple[str, bool]] = []
    real_commit = close_module.commit_attempt

    def _probe(
        ctx: Any,
        *,
        repo_root: Path,
        attempt_id: str,
        updates: dict[str, Any],
        command: str,
    ) -> Any:
        if command in _TERMINAL_COMMANDS:
            seen.append((command, workspace_path(repo_root, attempt_id).exists()))
        return real_commit(
            ctx,
            repo_root=repo_root,
            attempt_id=attempt_id,
            updates=updates,
            command=command,
        )

    monkeypatch.setattr(close_module, "commit_attempt", _probe)
    return seen


def _record_workspace_during_mutate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    replacement: Callable[..., Any] | None = None,
) -> list[bool]:
    """Record whether the workspace exists while the close mutation runs."""
    from eawf.runtime.daemon.methods import state as state_methods

    observed: list[bool] = []
    real_mutate = state_methods.mutate

    async def _recording(ctx: Any, params: dict[str, Any]) -> dict[str, Any]:
        verification_root = Path(params["mutation"]["params"]["verification_repo_root"])
        observed.append(verification_root.is_dir())
        if replacement is not None:
            result: dict[str, Any] = await replacement(ctx, params)
            return result
        return await real_mutate(ctx, params)

    monkeypatch.setattr(state_methods, "mutate", _recording)
    return observed


async def _submit(ctx: Any, repo: Path) -> str:
    submitted = await submit(
        ctx,
        {
            "wave_id": _WAVE,
            "outcome": "verified integrated revision",
            "repo_root": str(repo),
            "no_runtime_waiver": True,
        },
    )
    return str(submitted["attempt"]["id"])


def _attempt_row(state_path: Path, attempt_id: str) -> Any:
    return State.model_validate_json(state_path.read_bytes()).close_attempts[attempt_id]


def _no_schedule(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    scheduled: list[str] = []

    def _record(_ctx: Any, *, repo_root: Path, attempt_id: str) -> bool:
        scheduled.append(attempt_id)
        return False

    monkeypatch.setattr(close_module, "schedule_attempt", _record)
    return scheduled


def _gated_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    """Park the first workspace removal until the test releases it."""
    started = threading.Event()
    release = threading.Event()

    def _parked(repo_root: Path, *, attempt_id: str) -> bool:
        if not started.is_set():
            started.set()
            assert release.wait(timeout=30.0)
        return _REAL_CLEANUP(repo_root, attempt_id=attempt_id)

    _patch_cleanup(monkeypatch, _parked)
    return started, release


async def _wait_for(event: threading.Event) -> None:
    for _ in range(1500):
        if event.is_set():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("worker never reached the workspace removal")


def test_run_attempt_refused_close_removes_workspace_before_terminal_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing gate refuses the close and the workspace is gone first."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    _install_failing_gate(repo, state_path)
    _no_schedule(monkeypatch)
    during_mutate = _record_workspace_during_mutate(monkeypatch)
    seen = _probe_terminal_commits(monkeypatch)

    async def body() -> str:
        attempt_id = await _submit(ctx, repo)
        await _run_attempt(ctx, repo_root=repo, attempt_id=attempt_id)
        return attempt_id

    attempt_id = asyncio.run(body())

    assert during_mutate == [True]
    assert seen == [("close.failed", False)]
    row = _attempt_row(state_path, attempt_id)
    assert row.status is CloseAttemptStatus.BLOCKED
    assert row.failure_kind is CloseFailureKind.WORK_REJECTED
    assert row.terminal_at is not None
    assert not workspace_path(repo, attempt_id).exists()


def test_run_attempt_lagging_cleanup_completes_before_terminal_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow removal still finishes before the refused row is written."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    _install_failing_gate(repo, state_path)
    _no_schedule(monkeypatch)
    order: list[str] = []

    def _slow_cleanup(repo_root: Path, *, attempt_id: str) -> bool:
        time.sleep(0.3)
        removed = _REAL_CLEANUP(repo_root, attempt_id=attempt_id)
        order.append("cleanup-done")
        return removed

    _patch_cleanup(monkeypatch, _slow_cleanup)
    seen = _probe_terminal_commits(monkeypatch)
    real_probe = close_module.commit_attempt

    def _ordered(ctx: Any, **kwargs: Any) -> Any:
        if kwargs["command"] in _TERMINAL_COMMANDS:
            order.append(kwargs["command"])
        return real_probe(ctx, **kwargs)

    monkeypatch.setattr(close_module, "commit_attempt", _ordered)

    async def body() -> str:
        attempt_id = await _submit(ctx, repo)
        await _run_attempt(ctx, repo_root=repo, attempt_id=attempt_id)
        return attempt_id

    attempt_id = asyncio.run(body())

    assert order == ["cleanup-done", "close.failed"]
    assert seen == [("close.failed", False)]
    assert _attempt_row(state_path, attempt_id).status is CloseAttemptStatus.BLOCKED
    assert not workspace_path(repo, attempt_id).exists()


def test_run_attempt_cancelled_during_refused_cleanup_commits_terminal_without_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel landing mid-removal waits for it, persists the refusal, then cancels."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    _install_failing_gate(repo, state_path)
    scheduled = _no_schedule(monkeypatch)
    started, release = _gated_cleanup(monkeypatch)
    seen = _probe_terminal_commits(monkeypatch)

    async def body() -> str:
        attempt_id = await _submit(ctx, repo)
        worker = asyncio.create_task(_run_attempt(ctx, repo_root=repo, attempt_id=attempt_id))
        await _wait_for(started)
        worker.cancel()
        await asyncio.sleep(0.05)
        assert not worker.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await worker
        return attempt_id

    attempt_id = asyncio.run(body())

    assert seen == [("close.failed", False)]
    row = _attempt_row(state_path, attempt_id)
    assert row.status is CloseAttemptStatus.BLOCKED
    assert row.terminal_at is not None
    assert not workspace_path(repo, attempt_id).exists()
    # Only submit scheduled the worker; the cancelled worker forked no retry.
    assert scheduled == [attempt_id]


def test_run_attempt_cancelled_at_terminal_commit_leaves_no_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel delivered by the terminal commit itself finds nothing left to remove."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    _install_failing_gate(repo, state_path)
    _no_schedule(monkeypatch)
    seen = _probe_terminal_commits(monkeypatch)
    probe = close_module.commit_attempt

    def _cancel_on_terminal(ctx: Any, **kwargs: Any) -> Any:
        result = probe(ctx, **kwargs)
        if kwargs["command"] == "close.failed":
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
        return result

    monkeypatch.setattr(close_module, "commit_attempt", _cancel_on_terminal)

    async def body() -> str:
        attempt_id = await _submit(ctx, repo)
        worker = asyncio.create_task(_run_attempt(ctx, repo_root=repo, attempt_id=attempt_id))
        with pytest.raises(asyncio.CancelledError):
            await worker
        return attempt_id

    attempt_id = asyncio.run(body())

    assert seen == [("close.failed", False)]
    assert _attempt_row(state_path, attempt_id).status is CloseAttemptStatus.BLOCKED
    assert not workspace_path(repo, attempt_id).exists()


def test_run_attempt_operator_cancel_mid_retry_schedules_no_retry_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An auto-retryable fault cancelled mid-removal ends CANCELLED with no second worker."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    scheduled: list[str] = []
    real_schedule = close_module.schedule_attempt

    def _counting_schedule(ctx: Any, *, repo_root: Path, attempt_id: str) -> bool:
        scheduled.append(attempt_id)
        return real_schedule(ctx, repo_root=repo_root, attempt_id=attempt_id)

    monkeypatch.setattr(close_module, "schedule_attempt", _counting_schedule)

    async def _harness_fault(_ctx: Any, _params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("gate runner lost its child process")

    _record_workspace_during_mutate(monkeypatch, replacement=_harness_fault)
    started, release = _gated_cleanup(monkeypatch)
    seen = _probe_terminal_commits(monkeypatch)

    async def body() -> tuple[str, dict[str, Any]]:
        attempt_id = await _submit(ctx, repo)
        await _wait_for(started)

        async def _cancel() -> dict[str, Any]:
            return await cancel(ctx, {"ref": attempt_id, "repo_root": str(repo)})

        canceller = asyncio.create_task(_cancel())
        await asyncio.sleep(0.05)
        release.set()
        result = await canceller
        await asyncio.sleep(0.05)
        live = _CLOSE_TASKS.get(_close_task_key(repo, attempt_id))
        assert live is None or live.done()
        return attempt_id, result

    attempt_id, result = asyncio.run(body())

    assert scheduled == [attempt_id]
    assert seen == [("close.failed", False)]
    assert result["attempt"]["status"] == CloseAttemptStatus.CANCELLED.value
    row = _attempt_row(state_path, attempt_id)
    assert row.status is CloseAttemptStatus.CANCELLED
    assert not workspace_path(repo, attempt_id).exists()


def test_run_attempt_cancelled_path_removes_workspace_before_cancelled_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a worker mid-gate removes the workspace before its cancelled row."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    in_gate = asyncio.Event()

    async def _hang(_ctx: Any, _params: dict[str, Any]) -> dict[str, Any]:
        in_gate.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    during_mutate = _record_workspace_during_mutate(monkeypatch, replacement=_hang)
    seen = _probe_terminal_commits(monkeypatch)

    async def body() -> str:
        attempt_id = await _submit(ctx, repo)
        await asyncio.wait_for(in_gate.wait(), timeout=30.0)
        cancelled = await cancel(ctx, {"ref": attempt_id, "repo_root": str(repo)})
        assert cancelled["attempt"]["status"] == CloseAttemptStatus.CANCELLED.value
        return attempt_id

    attempt_id = asyncio.run(body())

    assert during_mutate == [True]
    assert seen == [("close.cancelled", False)]
    row = _attempt_row(state_path, attempt_id)
    assert row.status is CloseAttemptStatus.CANCELLED
    assert row.failure_kind is CloseFailureKind.OPERATOR_CANCELLED
    assert not workspace_path(repo, attempt_id).exists()


def test_run_attempt_cleanup_fault_keeps_refusal_and_finally_backstop_removes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed early removal neither masks the refusal nor leaks the workspace."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    _install_failing_gate(repo, state_path)
    _no_schedule(monkeypatch)
    calls: list[str] = []

    def _flaky_cleanup(repo_root: Path, *, attempt_id: str) -> bool:
        calls.append(attempt_id)
        if len(calls) == 1:
            raise CloseWorkspaceError("remove close worktree failed (exit=128): busy")
        return _REAL_CLEANUP(repo_root, attempt_id=attempt_id)

    _patch_cleanup(monkeypatch, _flaky_cleanup)
    seen = _probe_terminal_commits(monkeypatch)

    async def body() -> str:
        attempt_id = await _submit(ctx, repo)
        await _run_attempt(ctx, repo_root=repo, attempt_id=attempt_id)
        return attempt_id

    attempt_id = asyncio.run(body())

    assert len(calls) == 2
    assert seen == [("close.failed", True)]
    row = _attempt_row(state_path, attempt_id)
    assert row.status is CloseAttemptStatus.BLOCKED
    assert row.failure_kind is CloseFailureKind.WORK_REJECTED
    assert not workspace_path(repo, attempt_id).exists()


def test_remove_close_workspace_to_completion_absent_workspace_reports_removed(
    tmp_path: Path,
) -> None:
    repo, _state_path, _ctx = _repo_with_state(tmp_path)

    outcome = asyncio.run(
        close_workspace.remove_close_workspace_to_completion(repo, attempt_id="close-absent")
    )

    assert outcome == (True, False)


def test_remove_close_workspace_to_completion_fault_is_logged_not_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo, _state_path, _ctx = _repo_with_state(tmp_path)

    def _broken(_repo_root: Path, *, attempt_id: str) -> bool:
        raise CloseWorkspaceError(f"remove close worktree failed: {attempt_id}")

    _patch_cleanup(monkeypatch, _broken)

    with caplog.at_level(logging.WARNING, logger=close_workspace.__name__):
        outcome = asyncio.run(
            close_workspace.remove_close_workspace_to_completion(repo, attempt_id="close-broken")
        )

    assert outcome == (False, False)
    assert any(
        "remove_close_workspace_to_completion attempt='close-broken' removed=False" in message
        for message in caplog.messages
    )
