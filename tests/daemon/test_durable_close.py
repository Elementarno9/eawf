"""Durable asynchronous close-engine tests."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec, QualityDimension
from eawf.kernel.state.enums import (
    CloseAttemptStatus,
    CloseOperatorAction,
    StoreKind,
    WaveIntegrationStatus,
)
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.close_workspace import CloseWorkspaceError
from eawf.runtime.daemon.methods.close import (
    _CLOSE_TASKS,
    _close_task_key,
    _policy_digest,
    _run_attempt,
    _schedule,
    cancel,
    gate_freshness_inputs,
    persist_gate_receipt,
    resume,
    resume_durable_close_attempts,
    reusable_pass_gate_ids,
    status,
    submit,
)
from eawf.runtime.worktree import git
from eawf.workflow.audit_dsl.models import CheckResult
from eawf.workflow.lifecycle.integration import create_wave_integration
from eawf.workflow.lifecycle.wave import close_wave
from tests.daemon.test_close_lock_split import (
    _WAVE,
    _build_ctx,
    _state_payload,
)

pytestmark = pytest.mark.integration


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo_with_state(tmp_path: Path) -> tuple[Path, Path, Any]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "payload.txt").write_text("integrated\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-m", "test: integrated revision")
    commit_sha = git.commit_sha(repo, "HEAD")
    tree_sha = git.tree_sha(repo, commit_sha)

    state = State.model_validate(_state_payload())
    create_wave_integration(
        state,
        wave_id=_WAVE,
        base_sha=commit_sha,
        candidate_sha=commit_sha,
        integrated_sha=commit_sha,
        tree_sha=tree_sha,
        diff_digest=hashlib.sha256(b"").hexdigest(),
        spec_digest="spec-digest",
    )
    state_path = repo / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    ctx = _build_ctx(tmp_path, state_path)
    return repo, state_path, ctx


def test_resume_durable_close_attempts_missing_boot_state_is_noop(tmp_path: Path) -> None:
    """Service boot without a repo anchor keeps daemon RPC available."""
    state_path = tmp_path / "missing" / ".ea" / "state.json"
    ctx = _build_ctx(tmp_path, state_path)

    assert resume_durable_close_attempts(ctx) == 0


async def _wait_terminal(ctx: Any, repo: Path, ref: str) -> dict[str, Any]:
    for _ in range(200):
        result = await status(ctx, {"ref": ref, "repo_root": str(repo)})
        attempt = result["attempt"]
        if attempt["status"] in {
            "closed",
            "blocked",
            "stale",
            "failed",
            "cancelled",
        }:
            return attempt
        await asyncio.sleep(0.02)
    raise AssertionError(f"close attempt did not terminate: {ref}")


def test_submit_returns_immediately_and_close_continues_to_terminal(tmp_path: Path) -> None:
    repo, state_path, ctx = _repo_with_state(tmp_path)

    async def body() -> None:
        result = await submit(
            ctx,
            {
                "wave_id": _WAVE,
                "outcome": "verified integrated revision",
                "repo_root": str(repo),
                "no_runtime_waiver": True,
            },
        )
        assert result["backgrounded"] is True
        attempt_id = result["attempt"]["id"]
        terminal = await _wait_terminal(ctx, repo, attempt_id)
        assert terminal["status"] == CloseAttemptStatus.CLOSED.value
        assert terminal["apply_event_id"]
        assert not (repo / ".ea" / "worktrees" / "close" / attempt_id).exists()

    asyncio.run(body())
    state = State.model_validate_json(state_path.read_bytes())
    assert state.waves[_WAVE].status.value == "closed"
    integration = next(iter(state.wave_integrations.values()))
    assert integration.status is WaveIntegrationStatus.VERIFIED


def test_duplicate_submit_reuses_attempt_and_applies_close_once(tmp_path: Path) -> None:
    repo, state_path, ctx = _repo_with_state(tmp_path)

    async def body() -> None:
        params = {
            "wave_id": _WAVE,
            "outcome": "verified integrated revision",
            "repo_root": str(repo),
            "no_runtime_waiver": True,
        }
        first = await submit(ctx, params)
        second = await submit(ctx, params)
        assert second["attempt"]["id"] == first["attempt"]["id"]
        await _wait_terminal(ctx, repo, first["attempt"]["id"])

    asyncio.run(body())
    event_path = store_path(state_path, StoreKind.EVENT)
    rows = [orjson.loads(line) for line in event_path.read_bytes().splitlines() if line.strip()]
    wave_closed = [row for row in rows if row.get("payload", {}).get("event_kind") == "wave_closed"]
    assert len(wave_closed) == 1


def test_cancel_queued_attempt_is_durable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _state_path, ctx = _repo_with_state(tmp_path)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.close._schedule",
        lambda *args, **kwargs: False,
    )

    async def body() -> None:
        result = await submit(
            ctx,
            {
                "wave_id": _WAVE,
                "outcome": "verified integrated revision",
                "repo_root": str(repo),
                "no_runtime_waiver": True,
            },
        )
        attempt_id = result["attempt"]["id"]
        cancelled = await cancel(
            ctx,
            {
                "ref": attempt_id,
                "repo_root": str(repo),
                "reason": "operator changed release scope",
            },
        )
        assert cancelled["attempt"]["status"] == CloseAttemptStatus.CANCELLED.value
        assert cancelled["attempt"]["failure_detail_ref"]
        assert _close_task_key(repo, attempt_id) not in _CLOSE_TASKS

    asyncio.run(body())


def test_task_registry_scopes_identical_attempt_ids_by_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same attempt id in two repos schedules, reports, and cancels independently."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    repo_a, state_path_a, ctx_a = _repo_with_state(root_a)
    repo_b, state_path_b, ctx_b = _repo_with_state(root_b)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.close._schedule",
        lambda *args, **kwargs: False,
    )

    async def body() -> None:
        submitted = await submit(
            ctx_a,
            {
                "wave_id": _WAVE,
                "outcome": "verified integrated revision",
                "repo_root": str(repo_a),
                "no_runtime_waiver": True,
            },
        )
        attempt_id = str(submitted["attempt"]["id"])
        state_a = State.model_validate_json(state_path_a.read_bytes())
        state_b = State.model_validate_json(state_path_b.read_bytes())
        state_b.close_attempts = dict(state_a.close_attempts)
        state_path_b.write_text(state_b.model_dump_json(), encoding="utf-8")

        async def _hold_worker(
            _ctx: Any,
            *,
            repo_root: Path,
            attempt_id: str,
        ) -> None:
            task_key = _close_task_key(repo_root, attempt_id)
            try:
                await asyncio.Future()
            finally:
                if _CLOSE_TASKS.get(task_key) is asyncio.current_task():
                    _CLOSE_TASKS.pop(task_key, None)

        monkeypatch.setattr(
            "eawf.runtime.daemon.methods.close._run_attempt",
            _hold_worker,
        )
        assert _schedule(
            ctx_a,
            repo_root=repo_a,
            attempt_id=attempt_id,
        )
        assert _schedule(
            ctx_b,
            repo_root=repo_b,
            attempt_id=attempt_id,
        )
        await asyncio.sleep(0)
        key_a = _close_task_key(repo_a, attempt_id)
        key_b = _close_task_key(repo_b, attempt_id)
        assert key_a != key_b
        assert set(_CLOSE_TASKS) >= {key_a, key_b}
        assert (await status(ctx_a, {"ref": attempt_id, "repo_root": str(repo_a)}))["backgrounded"]
        assert (await status(ctx_b, {"ref": attempt_id, "repo_root": str(repo_b)}))["backgrounded"]

        await cancel(ctx_a, {"ref": attempt_id, "repo_root": str(repo_a)})
        assert key_a not in _CLOSE_TASKS
        assert key_b in _CLOSE_TASKS
        assert not _CLOSE_TASKS[key_b].done()
        assert not (await status(ctx_a, {"ref": attempt_id, "repo_root": str(repo_a)}))[
            "backgrounded"
        ]
        assert (await status(ctx_b, {"ref": attempt_id, "repo_root": str(repo_b)}))["backgrounded"]
        await cancel(ctx_b, {"ref": attempt_id, "repo_root": str(repo_b)})
        assert key_b not in _CLOSE_TASKS

    asyncio.run(body())


def test_new_integration_generation_makes_queued_attempt_stale(tmp_path: Path) -> None:
    repo, state_path, ctx = _repo_with_state(tmp_path)

    async def body() -> None:
        result = await submit(
            ctx,
            {
                "wave_id": _WAVE,
                "outcome": "verified integrated revision",
                "repo_root": str(repo),
                "no_runtime_waiver": True,
            },
        )
        attempt_id = result["attempt"]["id"]
        task = _CLOSE_TASKS.pop(_close_task_key(repo, attempt_id), None)
        if task is not None:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        state = State.model_validate_json(state_path.read_bytes())
        head = git.commit_sha(repo, "HEAD")
        tree = git.tree_sha(repo, head)
        create_wave_integration(
            state,
            wave_id=_WAVE,
            base_sha=head,
            candidate_sha="e" * 40,
            integrated_sha="f" * 40,
            tree_sha=tree,
            diff_digest="new-diff",
            spec_digest="new-spec",
        )
        row = state.close_attempts[attempt_id]
        state.close_attempts[attempt_id] = row.model_copy(
            update={
                "status": CloseAttemptStatus.QUEUED,
                "terminal_at": None,
                "failure_kind": None,
            }
        )
        state_path.write_text(state.model_dump_json(), encoding="utf-8")
        await _run_attempt(
            ctx,
            repo_root=repo,
            attempt_id=attempt_id,
        )
        terminal = await status(
            ctx,
            {"ref": attempt_id, "repo_root": str(repo)},
        )
        assert terminal["attempt"]["status"] == CloseAttemptStatus.STALE.value
        assert terminal["attempt"]["invalidation_causes"]

    asyncio.run(body())


def test_policy_change_makes_queued_attempt_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A governing policy edit invalidates proof before any gate runs."""
    repo, _state_path, ctx = _repo_with_state(tmp_path)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.close._schedule",
        lambda *args, **kwargs: False,
    )

    async def body() -> None:
        submitted = await submit(
            ctx,
            {
                "wave_id": _WAVE,
                "outcome": "verified integrated revision",
                "repo_root": str(repo),
                "no_runtime_waiver": True,
            },
        )
        attempt_id = str(submitted["attempt"]["id"])
        config_path = repo / ".ea" / "config.yaml"
        config_path.write_text(
            "verify:\n  juror_wall_clock_seconds: 707\n",
            encoding="utf-8",
        )

        await _run_attempt(ctx, repo_root=repo, attempt_id=attempt_id)
        terminal = await status(
            ctx,
            {"ref": attempt_id, "repo_root": str(repo)},
        )

        assert terminal["attempt"]["status"] == CloseAttemptStatus.STALE.value
        assert "verification policy changed" in terminal["attempt"]["invalidation_causes"][0]

    asyncio.run(body())


@pytest.mark.parametrize("layer", ["local", "branch", "env"])
def test_policy_digest_tracks_effective_runtime_layers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
) -> None:
    """Local, branch, and env verification overrides bind exact close policy."""
    repo, state_path, _ctx = _repo_with_state(tmp_path)
    state = State.model_validate_json(state_path.read_bytes())
    before = _policy_digest(state, repo_root=repo, wave_id=_WAVE)

    if layer == "local":
        config_path = repo / ".ea" / "local" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "verify:\n  juror_wall_clock_seconds: 701\n",
            encoding="utf-8",
        )
    elif layer == "branch":
        branch = _git(repo, "branch", "--show-current")
        config_path = repo / ".ea" / "branches" / f"{branch}.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "verify:\n  juror_wall_clock_seconds: 702\n",
            encoding="utf-8",
        )
    else:
        monkeypatch.setenv("EAWF_VERIFY__JUROR_WALL_CLOCK_SECONDS", "703")

    assert _policy_digest(state, repo_root=repo, wave_id=_WAVE) != before


def test_policy_digest_tracks_effective_profile_and_ignores_raw_noise(
    tmp_path: Path,
) -> None:
    """Profile semantics bind close; byte-only config noise does not."""
    repo, state_path, _ctx = _repo_with_state(tmp_path)
    profile_dir = repo / ".ea" / "profiles"
    profile_dir.mkdir(parents=True)
    config_path = repo / ".ea" / "config.yaml"
    config_path.write_text(
        "profiles:\n  enabled:\n    - close-test\n",
        encoding="utf-8",
    )
    profile_path = profile_dir / "close-test.yaml"
    profile_path.write_text(
        "name: close-test\nverify:\n  enforce: true\n  juror_wall_clock_seconds: 704\n",
        encoding="utf-8",
    )
    state = State.model_validate_json(state_path.read_bytes())
    before = _policy_digest(state, repo_root=repo, wave_id=_WAVE)

    config_path.write_text(
        "# byte-only comment must not invalidate proof\nprofiles:\n  enabled:\n    - close-test\n",
        encoding="utf-8",
    )
    assert _policy_digest(state, repo_root=repo, wave_id=_WAVE) == before

    profile_path.write_text(
        "name: close-test\nverify:\n  enforce: true\n  juror_wall_clock_seconds: 705\n",
        encoding="utf-8",
    )
    assert _policy_digest(state, repo_root=repo, wave_id=_WAVE) != before


@pytest.mark.parametrize(
    "interrupted_status",
    [
        CloseAttemptStatus.QUEUED,
        CloseAttemptStatus.PREPARING,
        CloseAttemptStatus.CHECKING,
        CloseAttemptStatus.AUDITING,
        CloseAttemptStatus.READY,
        CloseAttemptStatus.APPLYING,
    ],
)
def test_restart_reschedules_every_resumable_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted_status: CloseAttemptStatus,
) -> None:
    """Daemon startup schedules every durable nonterminal/retry stage."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.close._schedule",
        lambda *args, **kwargs: False,
    )

    submitted = asyncio.run(
        submit(
            ctx,
            {
                "wave_id": _WAVE,
                "outcome": "verified integrated revision",
                "repo_root": str(repo),
                "no_runtime_waiver": True,
            },
        )
    )
    attempt_id = str(submitted["attempt"]["id"])
    state = State.model_validate_json(state_path.read_bytes())
    state.close_attempts[attempt_id] = state.close_attempts[attempt_id].model_copy(
        update={"status": interrupted_status}
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    scheduled: list[str] = []

    def _record_schedule(*_args: Any, attempt_id: str, **_kwargs: Any) -> bool:
        scheduled.append(attempt_id)
        return True

    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.close._schedule",
        _record_schedule,
    )

    assert resume_durable_close_attempts(ctx) == 1
    assert scheduled == [attempt_id]
    restarted = State.model_validate_json(state_path.read_bytes())
    assert restarted.close_attempts[attempt_id].status is CloseAttemptStatus.QUEUED


def test_cleanup_failure_remains_retryable_until_cleanup_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed Wave does not hide failed close-workspace cleanup."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.close._schedule",
        lambda *args, **kwargs: False,
    )

    async def body() -> None:
        submitted = await submit(
            ctx,
            {
                "wave_id": _WAVE,
                "outcome": "verified integrated revision",
                "repo_root": str(repo),
                "no_runtime_waiver": True,
            },
        )
        attempt_id = str(submitted["attempt"]["id"])
        state = State.model_validate_json(state_path.read_bytes())
        close_wave(state, wave_id=_WAVE, outcome="closed before cleanup")
        state.current.active_wave_ids = []
        state.close_attempts[attempt_id] = state.close_attempts[attempt_id].model_copy(
            update={"infrastructure_retry_budget_remaining": 0}
        )
        state_path.write_text(state.model_dump_json(), encoding="utf-8")

        def _fail_cleanup(*_args: Any, **_kwargs: Any) -> None:
            raise CloseWorkspaceError("cleanup failed")

        monkeypatch.setattr(
            "eawf.runtime.daemon.methods.close.cleanup_close_workspace",
            _fail_cleanup,
        )
        await _run_attempt(ctx, repo_root=repo, attempt_id=attempt_id)
        failed = await status(ctx, {"ref": attempt_id, "repo_root": str(repo)})
        assert failed["attempt"]["status"] == CloseAttemptStatus.FAILED.value

        await resume(ctx, {"ref": attempt_id, "repo_root": str(repo)})
        monkeypatch.setattr(
            "eawf.runtime.daemon.methods.close.cleanup_close_workspace",
            lambda *_args, **_kwargs: None,
        )
        await _run_attempt(ctx, repo_root=repo, attempt_id=attempt_id)
        closed = await status(ctx, {"ref": attempt_id, "repo_root": str(repo)})
        assert closed["attempt"]["status"] == CloseAttemptStatus.CLOSED.value

    asyncio.run(body())


def test_gate_receipt_is_durable_idempotent_and_binds_full_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete result appends once, binds once, and preserves exact output."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.close._schedule",
        lambda *args, **kwargs: False,
    )

    async def body() -> str:
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

    attempt_id = asyncio.run(body())
    state = State.model_validate_json(state_path.read_bytes())
    state.close_attempts[attempt_id] = state.close_attempts[attempt_id].model_copy(
        update={"required_gate_ids": ["G-01"]}
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    exact_freshness = gate_freshness_inputs(state, attempt_id=attempt_id)["G-01"].model_copy(
        update={"criterion_id": "C-01"}
    )
    execution_root = tmp_path / "verification"
    log_ref = Path(".ea") / "local" / "close-logs" / attempt_id / "gate.log"
    source = execution_root / log_ref
    source.parent.mkdir(parents=True)
    source.write_text("full diagnostic output\n", encoding="utf-8")
    now = datetime.now(UTC)
    result = CheckResult(
        name="G-01",
        kind="command_exit_zero",
        passed=True,
        status="pass",
        details="returncode=0",
        started_at=now,
        ended_at=now,
        duration_ms=1,
        resolved_timeout_seconds=30,
        exit_status=0,
        argv=["uv", "run", "pytest"],
        stdout_tail="failed",
        stderr_tail="trace",
        selected_file_digest="files",
        runner_fingerprint="runner",
        environment_fingerprint="environment",
        full_log_ref=log_ref.as_posix(),
        freshness_key="a" * 64,
        freshness=exact_freshness,
    )
    first = persist_gate_receipt(
        ctx,
        repo_root=repo,
        execution_root=execution_root,
        attempt_id=attempt_id,
        criterion_id="C-01",
        gate_id="G-01",
        result=result,
    )
    source.unlink()
    second = persist_gate_receipt(
        ctx,
        repo_root=repo,
        execution_root=execution_root,
        attempt_id=attempt_id,
        criterion_id="C-01",
        gate_id="G-01",
        result=result,
    )

    assert first == second == f"GR-{'a' * 32}"
    receipt_path = store_path(state_path, StoreKind.GATE_RECEIPT)
    assert len(receipt_path.read_bytes().splitlines()) == 1
    state = State.model_validate_json(state_path.read_bytes())
    assert state.close_attempts[attempt_id].gate_receipt_ids == [first]
    assert (repo / log_ref).read_text(encoding="utf-8") == "full diagnostic output\n"
    assert reusable_pass_gate_ids(
        ctx,
        repo_root=repo,
        attempt_id=attempt_id,
    ) == {"G-01"}
    state = State.model_validate_json(state_path.read_bytes())
    frozen = state.close_attempts[attempt_id]
    bound_freshness = gate_freshness_inputs(state, attempt_id=attempt_id)["G-01"]
    assert bound_freshness.criteria_digest == frozen.criteria_digest
    assert bound_freshness.gate_manifest_digest == frozen.gate_manifest_digest
    assert bound_freshness.policy_digest == frozen.policy_digest
    assert bound_freshness.dependency_binding_digest == frozen.dependency_binding_digest
    assert bound_freshness.runner_environment_digest == frozen.runner_environment_digest
    for field in (
        "policy_digest",
        "dependency_binding_digest",
        "runner_environment_digest",
    ):
        state.close_attempts[attempt_id] = frozen.model_copy(update={field: f"changed-{field}"})
        state_path.write_text(state.model_dump_json(), encoding="utf-8")
        assert (
            reusable_pass_gate_ids(
                ctx,
                repo_root=repo,
                attempt_id=attempt_id,
            )
            == set()
        )


def test_file_exists_result_persists_terminal_receipt_without_command_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-command deterministic proof receives the same durable receipt."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.close._schedule",
        lambda *args, **kwargs: False,
    )

    async def body() -> str:
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

    attempt_id = asyncio.run(body())
    state = State.model_validate_json(state_path.read_bytes())
    state.close_attempts[attempt_id] = state.close_attempts[attempt_id].model_copy(
        update={"required_gate_ids": ["G-FILE"]}
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    exact_freshness = gate_freshness_inputs(state, attempt_id=attempt_id)["G-FILE"].model_copy(
        update={"criterion_id": "C-01"}
    )
    execution_root = tmp_path / "verification"
    proof_ref = Path(".ea") / "local" / "close-logs" / attempt_id / "file-proof.log"
    proof = execution_root / proof_ref
    proof.parent.mkdir(parents=True)
    proof.write_text("path=payload.txt exists=True\n", encoding="utf-8")
    now = datetime.now(UTC)
    result = CheckResult(
        name="G-FILE",
        kind="file_exists",
        passed=True,
        details="path=payload.txt exists=True",
        started_at=now,
        ended_at=now,
        duration_ms=0,
        runner_fingerprint="runner",
        environment_fingerprint="environment",
        full_log_ref=proof_ref.as_posix(),
        freshness_key="b" * 64,
        freshness=exact_freshness,
    )

    receipt_id = persist_gate_receipt(
        ctx,
        repo_root=repo,
        execution_root=execution_root,
        attempt_id=attempt_id,
        criterion_id="C-01",
        gate_id="G-FILE",
        result=result,
    )

    assert receipt_id == f"GR-{'b' * 32}"
    rows = [
        orjson.loads(line)
        for line in store_path(state_path, StoreKind.GATE_RECEIPT).read_bytes().splitlines()
        if line
    ]
    receipt = rows[0]["payload"]
    assert receipt["gate_id"] == "G-FILE"
    assert receipt["argv"] is None
    assert receipt["argv_digest"] is None
    assert receipt["resolved_timeout_seconds"] is None
    assert receipt["started_at"]
    assert receipt["ended_at"]
    assert receipt["runner_digest"] == "runner"
    assert receipt["environment_digest"] == "environment"
    assert receipt["full_log_ref"] == proof_ref.as_posix()
    assert (repo / proof_ref).is_file()


def test_blocked_attempt_has_one_bounded_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair needs new integration, rebuilds proof, then exhausts once."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.close._schedule",
        lambda *args, **kwargs: False,
    )

    async def body() -> None:
        submitted = await submit(
            ctx,
            {
                "wave_id": _WAVE,
                "outcome": "verified integrated revision",
                "repo_root": str(repo),
                "no_runtime_waiver": True,
            },
        )
        attempt_id = str(submitted["attempt"]["id"])
        state = State.model_validate_json(state_path.read_bytes())
        state.close_attempts[attempt_id] = state.close_attempts[attempt_id].model_copy(
            update={
                "status": CloseAttemptStatus.BLOCKED,
                "gate_receipt_ids": ["GR-parent"],
                "audit_report_id": "AR-parent",
                "waiver_decision_ids": ["D-parent"],
                "usage_receipt_ids": ["UR-parent"],
                "artifact_refs": ["urn:eawf:v1:artifact:parent"],
                "terminal_at": datetime.now(UTC),
            }
        )
        state_path.write_text(state.model_dump_json(), encoding="utf-8")

        with pytest.raises(ValueError, match="land a repair integration first"):
            await resume(
                ctx,
                {"ref": attempt_id, "repo_root": str(repo)},
            )
        refused = State.model_validate_json(state_path.read_bytes())
        assert len(refused.close_attempts) == 1
        assert refused.close_attempts[attempt_id].repair_budget_remaining == 1

        criterion = CriterionSpec(
            id="CR-01",
            text="the repaired integration contains the required payload",
            kind="contract",
            acceptance_style="binary",
            evidence_kind="deterministic",
            gate_ids=["G-01"],
            quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
            measurable_signal="payload.txt exists in the repaired integration",
        )
        gate = GateSpec(
            id="G-01",
            criterion_id=criterion.id,
            kind="file_exists",
            args={"path": "payload.txt"},
            policy="block",
            cadence="every-wave",
        )
        (repo / "payload.txt").write_text("repaired\n", encoding="utf-8")
        _git(repo, "add", "payload.txt")
        _git(repo, "commit", "-m", "test: repair integration")
        repair_sha = git.commit_sha(repo, "HEAD")
        repair_tree = git.tree_sha(repo, repair_sha)
        state = State.model_validate_json(state_path.read_bytes())
        source_integration = state.wave_integrations[
            state.close_attempts[attempt_id].integration_id
        ]
        state.waves[_WAVE] = state.waves[_WAVE].model_copy(
            update={
                "title": "repaired exact close inputs",
                "success_criteria": [criterion],
                "gates": [gate],
            }
        )
        repair_integration = create_wave_integration(
            state,
            wave_id=_WAVE,
            base_sha=source_integration.integrated_sha,
            candidate_sha=repair_sha,
            integrated_sha=repair_sha,
            tree_sha=repair_tree,
            diff_digest=hashlib.sha256(b"repair").hexdigest(),
            spec_digest=hashlib.sha256(b"repair-spec").hexdigest(),
        )
        state_path.write_text(state.model_dump_json(), encoding="utf-8")
        monkeypatch.setenv("EAWF_VERIFY__JUROR_WALL_CLOCK_SECONDS", "706")
        monkeypatch.setattr(
            "eawf.runtime.daemon.methods.close._runner_environment_digest",
            lambda: "e" * 64,
        )
        monkeypatch.setattr(
            "eawf.runtime.daemon.methods.close._dependency_binding_digest",
            lambda _state, *, wave_id: "d" * 64,
        )

        resumed = await resume(
            ctx,
            {"ref": attempt_id, "repo_root": str(repo)},
        )
        repair_id = str(resumed["attempt"]["id"])
        assert repair_id != attempt_id
        assert resumed["attempt"]["status"] == CloseAttemptStatus.QUEUED.value
        assert resumed["attempt"]["generation"] == 2
        assert resumed["attempt"]["supersedes_id"] == attempt_id
        assert resumed["attempt"]["repair_wave_id"] == _WAVE
        assert resumed["attempt"]["repair_generation"] == 1
        assert resumed["attempt"]["repair_budget_remaining"] == 0
        replayed = await resume(
            ctx,
            {"ref": attempt_id, "repo_root": str(repo)},
        )
        assert replayed["attempt"]["id"] == repair_id

        state = State.model_validate_json(state_path.read_bytes())
        blocked_parent = state.close_attempts[attempt_id]
        repair = state.close_attempts[repair_id]
        assert blocked_parent.status is CloseAttemptStatus.BLOCKED
        assert blocked_parent.terminal_at is not None
        assert len(state.close_attempts) == 2
        assert repair.integration_id == repair_integration.id
        assert repair.integration_id != blocked_parent.integration_id
        assert repair.candidate_sha == repair_sha
        assert repair.integrated_sha == repair_sha
        assert repair.tree_sha == repair_tree
        assert repair.spec_digest == hashlib.sha256(b"repair-spec").hexdigest()
        assert repair.wave_revision_digest != blocked_parent.wave_revision_digest
        assert repair.criteria_digest != blocked_parent.criteria_digest
        assert repair.gate_manifest_digest != blocked_parent.gate_manifest_digest
        assert repair.policy_digest != blocked_parent.policy_digest
        assert repair.runner_environment_digest == "e" * 64
        assert repair.dependency_binding_digest == "d" * 64
        assert repair.required_gate_ids == ["G-01"]
        assert repair.gate_receipt_ids == []
        assert repair.audit_report_id is None
        assert repair.waiver_decision_ids == []
        assert repair.usage_receipt_ids == []
        assert repair.artifact_refs == []
        assert repair.no_runtime_waiver is blocked_parent.no_runtime_waiver

        latest = await status(ctx, {"ref": _WAVE, "repo_root": str(repo)})
        assert latest["attempt"]["id"] == repair_id
        parent = await status(ctx, {"ref": attempt_id, "repo_root": str(repo)})
        assert parent["attempt"]["status"] == CloseAttemptStatus.BLOCKED.value

        async def _blocked_reaudit(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise ValueError("validation_failed: second audit blocked close")

        monkeypatch.setattr(
            "eawf.runtime.daemon.methods.state.mutate",
            _blocked_reaudit,
        )
        await _run_attempt(ctx, repo_root=repo, attempt_id=repair_id)
        state = State.model_validate_json(state_path.read_bytes())
        exhausted = state.close_attempts[repair_id]
        assert exhausted.status is CloseAttemptStatus.BLOCKED
        assert exhausted.required_operator_actions == [
            CloseOperatorAction.SPLIT,
            CloseOperatorAction.DEFER,
            CloseOperatorAction.ABORT,
        ]
        replayed_parent = await resume(
            ctx,
            {"ref": attempt_id, "repo_root": str(repo)},
        )
        assert replayed_parent["attempt"]["id"] == repair_id
        assert replayed_parent["attempt"]["status"] == CloseAttemptStatus.BLOCKED.value
        assert replayed_parent["backgrounded"] is False

        with pytest.raises(
            ValueError,
            match="operator action required: split, defer, abort",
        ):
            await resume(
                ctx,
                {"ref": repair_id, "repo_root": str(repo)},
            )

    asyncio.run(body())
