"""Fault-injection matrix for durable exact-revision close attempts."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from pydantic import ValidationError as PydanticValidationError

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.enums import (
    AgentSessionRole,
    CloseAttemptStatus,
    CloseFailureKind,
    DependencyStage,
    EffortBucket,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    CloseAttempt,
    State,
    Wave,
    WaveDependencyBarrier,
    wave_dependency_key,
)
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import gate_execution
from eawf.runtime.daemon.close_workspace import CloseWorkspaceError
from eawf.runtime.daemon.methods import close as close_module
from eawf.runtime.daemon.methods.close import (
    _attempt_invalidation_causes,
    resume_durable_close_attempts,
    shutdown_close_attempts,
    submit,
)
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.runtime.worktree import git
from eawf.workflow.agent_report.rollup import iter_agent_reports
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec
from eawf.workflow.lifecycle import LifecycleGuardError
from eawf.workflow.lifecycle.integration import (
    bind_start_dependencies,
    create_wave_integration,
    latest_wave_integration,
)
from eawf.workflow.verify import oracle
from tests.daemon.test_close_lock_split import _WAVE
from tests.daemon.test_durable_close import _git, _repo_with_state

pytestmark = pytest.mark.integration

_UPSTREAM = "P30-I23-W99"
_MATRIX_CRITERION_TEXT = "the integrated payload exists at the frozen revision"


class _WorkerTerminated(BaseException):
    """Simulated process loss after a real durable stage transition."""


class _MatrixAuditorSpawn:
    """Return exact GateReceipt-grounded auditor bodies and count live spawns."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, prompt: str) -> SpawnResult:
        self.calls += 1
        receipt_refs = re.findall(
            r"urn:eawf:v1:store:[^`\s]+/gate_receipt/GR-[A-Za-z0-9_.-]+",
            prompt,
        )
        assert len(receipt_refs) == 1
        body = {
            "role": "auditor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "verified the frozen close proof",
            "target_id": _WAVE,
            "criteria": [
                {
                    "criterion": _MATRIX_CRITERION_TEXT,
                    "passed": True,
                    "evidence_refs": [
                        {
                            "kind": "store_record",
                            "ref": receipt_refs[0],
                        }
                    ],
                }
            ],
            "refutations": [],
        }
        now = datetime.now(UTC)
        return SpawnResult(
            session_id=f"fault-matrix-auditor-{self.calls}",
            runtime="claude-code",
            model="test-model",
            subprocess_pid=4242,
            exit_status=0,
            text=json.dumps(body),
            started_at=now,
            ended_at=now,
        )


def _configure_real_fault_matrix(
    *,
    repo: Path,
    state_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], _MatrixAuditorSpawn]:
    """Install one real deterministic gate plus one durable single auditor."""
    criterion = CriterionSpec(
        id="CR-MATRIX",
        text=_MATRIX_CRITERION_TEXT,
        kind="contract",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["G-MATRIX"],
        quality_dimension="functional_suitability",
        measurable_signal="payload.txt exists in the integrated checkout",
    )
    gate = GateSpec(
        id="G-MATRIX",
        criterion_id=criterion.id,
        kind="file_exists",
        args={"path": "payload.txt"},
        policy="block",
        cadence="every-wave",
    )
    state = State.model_validate_json(state_path.read_bytes())
    state.waves[_WAVE] = state.waves[_WAVE].model_copy(
        update={
            "effort_bucket": EffortBucket.L,
            "success_criteria": [criterion],
            "gates": [gate],
        }
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    profile_dir = repo / ".ea" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (repo / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled:\n    - close-fault-matrix\n",
        encoding="utf-8",
    )
    profile_dir.joinpath("close-fault-matrix.yaml").write_text(
        "name: close-fault-matrix\n"
        "verify:\n"
        "  enforce: true\n"
        "  cross_vendor_jury: false\n"
        "  argv_allowlist:\n"
        "    - git\n"
        "  floor_checks:\n"
        "    - name: clean-enough\n"
        '      cmd: ["git", "status"]\n'
        "      scope: all\n"
        "      cadence: every-wave\n"
        "      policy: warn\n",
        encoding="utf-8",
    )

    # The gate itself executes in a child interpreter, so the counter sits at
    # the out-of-process seam; an in-process registry patch would never be
    # reached. The child claims its freshness key immediately before executing
    # and reuses a terminal receipt without claiming, so a NEW claim file is
    # the exact signal that the gate really ran rather than replayed.
    gate_executions: list[str] = []
    real_runner = gate_execution.run_gate_out_of_process

    def _counted_runner(
        spec: CheckSpec,
        *,
        cwd: Path,
        context: gate_execution.GateExecutionContext,
        criterion_id: str,
        gate_id: str,
    ) -> CheckResult:
        claims = context.state_path.parent / "local" / "gate-claims"
        before = set(claims.glob("*.json")) if claims.is_dir() else set()
        result = real_runner(
            spec,
            cwd=cwd,
            context=context,
            criterion_id=criterion_id,
            gate_id=gate_id,
        )
        if set(claims.glob("*.json")) - before:
            gate_executions.append(spec.name)
        return result

    monkeypatch.setattr(gate_execution, "run_gate_out_of_process", _counted_runner)
    auditor = _MatrixAuditorSpawn()

    def _spawn_factory(
        _state: State,
        _wave: Wave,
        *,
        repo_root: Path,
        timeout_seconds: float = 600.0,
        events_path: Path | None = None,
    ) -> Any:
        del repo_root, timeout_seconds, events_path
        return lambda _runtime: auditor

    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._jury_spawn_factory",
        _spawn_factory,
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._compute_wave_close_extras",
        lambda *_args, **_kwargs: {},
    )
    return gate_executions, auditor


def _inject_worker_termination(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: CloseAttemptStatus,
) -> None:
    """Terminate the live task immediately after *target* becomes durable."""
    armed = True

    def _terminate() -> None:
        nonlocal armed
        if armed:
            armed = False
            raise _WorkerTerminated

    if target is CloseAttemptStatus.PREPARING:
        real_commit = close_module._commit_attempt

        def _commit(*args: Any, **kwargs: Any) -> Any:
            result = real_commit(*args, **kwargs)
            if kwargs["updates"].get("status") is target:
                _terminate()
            return result

        monkeypatch.setattr(close_module, "_commit_attempt", _commit)
        return
    if target is CloseAttemptStatus.READY:
        real_ready = close_module.mark_attempt_ready

        def _ready(*args: Any, **kwargs: Any) -> Any:
            result = real_ready(*args, **kwargs)
            _terminate()
            return result

        monkeypatch.setattr(close_module, "mark_attempt_ready", _ready)
        return
    real_transition = close_module.transition_attempt_stage

    def _transition(*args: Any, **kwargs: Any) -> Any:
        result = real_transition(*args, **kwargs)
        if kwargs["status"] is target:
            _terminate()
        return result

    monkeypatch.setattr(close_module, "transition_attempt_stage", _transition)


def test_receipt_miss_executes_once_and_hit_executes_zero_more(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh receipt set is the execution counter boundary for one gate."""
    criterion = CriterionSpec(
        id="CR-01",
        text="the deterministic gate passes",
        kind="contract",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["G-01"],
        quality_dimension="functional_suitability",
        measurable_signal="the deterministic command exits successfully",
    )
    gate = GateSpec(
        id="G-01",
        criterion_id=criterion.id,
        kind="file_exists",
        args={"path": "payload.txt"},
        policy="block",
        cadence="every-wave",
    )
    wave = Wave(
        id="P30-I23-W01",
        iter_id="P30-I23",
        title="receipt execution counter",
        status=WaveStatus.CLAIMED,
        opened_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
    )
    executions = 0

    def _run_checks(
        _specs: list[CheckSpec],
        *,
        cwd: Path | None = None,
    ) -> list[CheckResult]:
        nonlocal executions
        executions += 1
        return [
            CheckResult(
                name=gate.id,
                kind="file_exists",
                passed=True,
                status="pass",
            )
        ]

    monkeypatch.setattr(
        oracle,
        "compile_gate",
        lambda *_args, **_kwargs: CheckSpec(
            kind="file_exists",
            name=gate.id,
            args={"path": "payload.txt"},
        ),
    )
    monkeypatch.setattr(oracle, "run_checks", _run_checks)

    async def _score(reusable: set[str]) -> None:
        result = await oracle.run_oracle(
            criterion,
            [gate],
            wave=wave,
            state=object(),  # type: ignore[arg-type]
            state_path=tmp_path / "state.json",
            events_path=tmp_path / "event.jsonl",
            repo_root=tmp_path,
            spawn_factory=lambda _runtime: pytest.fail("jury must not run"),
            reusable_pass_gate_ids=reusable,
        )
        assert result.status == "pass"

    asyncio.run(_score(set()))
    assert executions == 1
    asyncio.run(_score({gate.id}))
    assert executions == 1


def _submit_without_worker(
    *,
    repo: Path,
    ctx: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    monkeypatch.setattr(close_module, "_schedule", lambda *_args, **_kwargs: False)
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
    return str(submitted["attempt"]["id"])


def _causes(
    *,
    repo: Path,
    state_path: Path,
    attempt_id: str,
) -> list[str]:
    state = State.model_validate_json(state_path.read_bytes())
    return _attempt_invalidation_causes(
        state,
        repo_root=repo,
        attempt=state.close_attempts[attempt_id],
    )


def test_checkout_head_drift_does_not_replace_pinned_integrated_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated checkout advance cannot retarget exact-revision close."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    attempt_id = _submit_without_worker(
        repo=repo,
        ctx=ctx,
        monkeypatch=monkeypatch,
    )
    pinned = (
        State.model_validate_json(state_path.read_bytes()).close_attempts[attempt_id].integrated_sha
    )

    (repo / "unrelated.txt").write_text("new checkout head\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    _git(repo, "commit", "-m", "test: advance unrelated checkout")

    assert git.commit_sha(repo, "HEAD") != pinned
    assert _causes(repo=repo, state_path=state_path, attempt_id=attempt_id) == []


def test_new_integrated_head_invalidates_attempt_with_named_generation_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state_path, ctx = _repo_with_state(tmp_path)
    attempt_id = _submit_without_worker(
        repo=repo,
        ctx=ctx,
        monkeypatch=monkeypatch,
    )
    (repo / "payload.txt").write_text("next integration\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-m", "test: next integrated head")
    head = git.commit_sha(repo, "HEAD")
    state = State.model_validate_json(state_path.read_bytes())
    create_wave_integration(
        state,
        wave_id=_WAVE,
        base_sha=state.close_attempts[attempt_id].integrated_sha,
        candidate_sha=head,
        integrated_sha=head,
        tree_sha=git.tree_sha(repo, head),
        diff_digest="next-diff",
        spec_digest=state.close_attempts[attempt_id].spec_digest,
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    causes = _causes(repo=repo, state_path=state_path, attempt_id=attempt_id)
    assert "integration generation changed" in causes


def test_spec_digest_drift_invalidates_attempt_with_named_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state_path, ctx = _repo_with_state(tmp_path)
    attempt_id = _submit_without_worker(
        repo=repo,
        ctx=ctx,
        monkeypatch=monkeypatch,
    )
    state = State.model_validate_json(state_path.read_bytes())
    active = latest_wave_integration(state, _WAVE)
    assert active is not None
    state.wave_integrations[active.id] = active.model_copy(
        update={"spec_digest": "corrected-spec-digest"}
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    causes = _causes(repo=repo, state_path=state_path, attempt_id=attempt_id)
    assert "spec digest changed" in causes


def test_dependency_binding_drift_invalidates_attempt_with_named_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state_path, ctx = _repo_with_state(tmp_path)
    state = State.model_validate_json(state_path.read_bytes())
    state.waves[_UPSTREAM].status = WaveStatus.IN_PROGRESS
    state.waves[_WAVE].deps = [_UPSTREAM]
    state.waves[_UPSTREAM].blocks = [_WAVE]
    state.wave_dependency_barriers[wave_dependency_key(_WAVE, _UPSTREAM)] = WaveDependencyBarrier(
        wave_id=_WAVE,
        dep_wave_id=_UPSTREAM,
        start_after=DependencyStage.INTEGRATED,
        land_after=DependencyStage.INTEGRATED,
        reason="close binds exact upstream integration",
    )
    head = git.commit_sha(repo, "HEAD")
    create_wave_integration(
        state,
        wave_id=_UPSTREAM,
        base_sha=head,
        candidate_sha=head,
        integrated_sha=head,
        tree_sha=git.tree_sha(repo, head),
        diff_digest="upstream-diff",
        spec_digest="upstream-spec",
    )
    binding = bind_start_dependencies(
        state,
        wave_id=_WAVE,
        now=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
    )[0]
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    attempt_id = _submit_without_worker(
        repo=repo,
        ctx=ctx,
        monkeypatch=monkeypatch,
    )

    state = State.model_validate_json(state_path.read_bytes())
    key = wave_dependency_key(_WAVE, _UPSTREAM)
    state.wave_dependency_bindings[key] = binding.model_copy(
        update={"land_fact_ref": f"integration:{binding.integration_id}"}
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    causes = _causes(repo=repo, state_path=state_path, attempt_id=attempt_id)
    assert "dependency binding changed" in causes


@pytest.mark.parametrize(
    ("drift_kind", "expected_cause"),
    [
        ("policy", "verification policy changed"),
        ("integration", "integration generation changed"),
        ("dependency", "dependency binding changed"),
        ("runner", "runner environment changed"),
        ("receipt", "bound gate/audit proof changed"),
    ],
)
def test_post_ready_governing_drift_fails_final_close_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_kind: str,
    expected_cause: str,
) -> None:
    """READY proof cannot apply after governing inputs move before final lock."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    if drift_kind == "dependency":
        state = State.model_validate_json(state_path.read_bytes())
        state.waves[_UPSTREAM].status = WaveStatus.IN_PROGRESS
        state.waves[_WAVE].deps = [_UPSTREAM]
        state.waves[_UPSTREAM].blocks = [_WAVE]
        state.wave_dependency_barriers[wave_dependency_key(_WAVE, _UPSTREAM)] = (
            WaveDependencyBarrier(
                wave_id=_WAVE,
                dep_wave_id=_UPSTREAM,
                start_after=DependencyStage.INTEGRATED,
                land_after=DependencyStage.INTEGRATED,
                reason="post-ready close CAS binds exact upstream generation",
            )
        )
        head = git.commit_sha(repo, "HEAD")
        create_wave_integration(
            state,
            wave_id=_UPSTREAM,
            base_sha=head,
            candidate_sha=head,
            integrated_sha=head,
            tree_sha=git.tree_sha(repo, head),
            diff_digest="post-ready-upstream-diff",
            spec_digest="post-ready-upstream-spec",
        )
        bind_start_dependencies(
            state,
            wave_id=_WAVE,
            now=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        )
        state_path.write_text(state.model_dump_json(), encoding="utf-8")
    _configure_real_fault_matrix(
        repo=repo,
        state_path=state_path,
        monkeypatch=monkeypatch,
    )
    runner_drifted = False
    real_runner_digest = close_module._runner_environment_digest
    if drift_kind == "runner":

        def _runner_digest() -> str:
            return "0" * 64 if runner_drifted else real_runner_digest()

        monkeypatch.setattr(close_module, "_runner_environment_digest", _runner_digest)
    real_ready = close_module.mark_attempt_ready

    def _ready(*args: Any, **kwargs: Any) -> Any:
        nonlocal runner_drifted
        result = real_ready(*args, **kwargs)
        if drift_kind == "policy":
            config_path = repo / ".ea" / "local" / "config.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                "verify:\n  juror_wall_clock_seconds: 709\n",
                encoding="utf-8",
            )
        elif drift_kind == "integration":
            state = State.model_validate_json(state_path.read_bytes())
            source = latest_wave_integration(state, _WAVE)
            assert source is not None
            create_wave_integration(
                state,
                wave_id=_WAVE,
                base_sha=source.integrated_sha,
                candidate_sha="e" * 40,
                integrated_sha="f" * 40,
                tree_sha=source.tree_sha,
                diff_digest="post-ready-diff",
                spec_digest=source.spec_digest,
            )
            state_path.write_text(state.model_dump_json(), encoding="utf-8")
        elif drift_kind == "dependency":
            state = State.model_validate_json(state_path.read_bytes())
            key = wave_dependency_key(_WAVE, _UPSTREAM)
            binding = state.wave_dependency_bindings[key]
            state.wave_dependency_bindings[key] = binding.model_copy(
                update={"land_fact_ref": f"integration:{binding.integration_id}"}
            )
            state_path.write_text(state.model_dump_json(), encoding="utf-8")
        elif drift_kind == "runner":
            runner_drifted = True
        else:
            receipt_path = store_path(state_path, StoreKind.GATE_RECEIPT)
            rows = [
                orjson.loads(line)
                for line in receipt_path.read_bytes().splitlines()
                if line.strip()
            ]
            assert len(rows) == 1
            rows[0]["payload"]["runner_environment_digest"] = "0" * 64
            receipt_path.write_bytes(orjson.dumps(rows[0]) + b"\n")
        return result

    monkeypatch.setattr(close_module, "mark_attempt_ready", _ready)
    close_module._SHUTTING_DOWN = False

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
        attempt_id = str(submitted["attempt"]["id"])
        await close_module._CLOSE_TASKS[close_module._close_task_key(repo, attempt_id)]
        return attempt_id

    try:
        attempt_id = asyncio.run(body())
    finally:
        close_module._SHUTTING_DOWN = False

    final = State.model_validate_json(state_path.read_bytes())
    attempt = final.close_attempts[attempt_id]
    assert attempt.status is CloseAttemptStatus.STALE
    assert final.waves[_WAVE].status is WaveStatus.CLAIMED
    assert expected_cause in "; ".join(attempt.invalidation_causes)


def test_worker_shutdown_from_preparing_is_durable_and_restart_reschedules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel a real worker at PREPARING; recovery consumes its durable row."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    entered_prepare = threading.Event()
    release_prepare = threading.Event()

    def _blocking_prepare(*_args: Any, **_kwargs: Any) -> object:
        entered_prepare.set()
        if not release_prepare.wait(timeout=5):
            raise AssertionError("test did not release blocked prepare seam")
        return object()

    monkeypatch.setattr(close_module, "prepare_close_workspace", _blocking_prepare)
    close_module._SHUTTING_DOWN = False

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
        attempt_id = str(submitted["attempt"]["id"])
        for _ in range(200):
            if entered_prepare.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered_prepare.is_set()
        preparing = State.model_validate_json(state_path.read_bytes())
        assert preparing.close_attempts[attempt_id].status is CloseAttemptStatus.PREPARING

        await shutdown_close_attempts()
        queued = State.model_validate_json(state_path.read_bytes())
        assert queued.close_attempts[attempt_id].status is CloseAttemptStatus.QUEUED
        assert queued.close_attempts[attempt_id].failure_kind == "daemon_shutdown"
        release_prepare.set()
        await asyncio.sleep(0)
        return attempt_id

    try:
        attempt_id = asyncio.run(body())
    finally:
        release_prepare.set()
        close_module._SHUTTING_DOWN = False

    scheduled: list[str] = []

    def _record_schedule(*_args: Any, attempt_id: str, **_kwargs: Any) -> bool:
        scheduled.append(attempt_id)
        return True

    monkeypatch.setattr(close_module, "_schedule", _record_schedule)
    assert resume_durable_close_attempts(ctx) == 1
    assert scheduled == [attempt_id]
    recovered = State.model_validate_json(state_path.read_bytes())
    assert recovered.close_attempts[attempt_id].status is CloseAttemptStatus.QUEUED


@pytest.mark.parametrize(
    "interrupted_status",
    [
        CloseAttemptStatus.PREPARING,
        CloseAttemptStatus.CHECKING,
        CloseAttemptStatus.AUDITING,
        CloseAttemptStatus.READY,
        CloseAttemptStatus.APPLYING,
    ],
)
def test_real_worker_termination_recovers_exactly_once_from_each_durable_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted_status: CloseAttemptStatus,
) -> None:
    """Crash the live worker at each stage; restart reuses all durable proof."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    gate_executions, auditor = _configure_real_fault_matrix(
        repo=repo,
        state_path=state_path,
        monkeypatch=monkeypatch,
    )
    _inject_worker_termination(
        monkeypatch,
        target=interrupted_status,
    )
    close_module._SHUTTING_DOWN = False

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
        attempt_id = str(submitted["attempt"]["id"])
        task_key = close_module._close_task_key(repo, attempt_id)
        first_worker = close_module._CLOSE_TASKS[task_key]
        with pytest.raises(_WorkerTerminated):
            await first_worker

        interrupted = State.model_validate_json(state_path.read_bytes())
        assert interrupted.close_attempts[attempt_id].status is interrupted_status
        assert set(interrupted.close_attempts) == {attempt_id}

        assert resume_durable_close_attempts(ctx) == 1
        restarted_worker = close_module._CLOSE_TASKS[task_key]
        assert restarted_worker is not first_worker
        await restarted_worker
        return attempt_id

    try:
        attempt_id = asyncio.run(body())
    finally:
        close_module._SHUTTING_DOWN = False

    final = State.model_validate_json(state_path.read_bytes())
    attempt = final.close_attempts[attempt_id]
    assert attempt.status is CloseAttemptStatus.CLOSED
    assert final.waves[_WAVE].status is WaveStatus.CLOSED
    assert set(final.close_attempts) == {attempt_id}
    assert gate_executions == ["G-MATRIX"]

    receipt_path = store_path(state_path, StoreKind.GATE_RECEIPT)
    receipt_rows = [
        orjson.loads(line) for line in receipt_path.read_bytes().splitlines() if line.strip()
    ]
    assert len(receipt_rows) == 1
    assert attempt.gate_receipt_ids == [receipt_rows[0]["id"]]

    reports = iter_agent_reports(
        state_path,
        role=AgentSessionRole.AUDITOR,
        base_id=_WAVE,
    )
    assert auditor.calls == 1
    assert len(reports) == 1
    assert attempt.audit_report_id == reports[0].envelope.id

    event_rows = [
        orjson.loads(line)
        for line in store_path(state_path, StoreKind.EVENT).read_bytes().splitlines()
        if line.strip()
    ]
    wave_closed = [
        row for row in event_rows if row.get("payload", {}).get("event_kind") == "wave_closed"
    ]
    assert len(wave_closed) == 1


@pytest.mark.parametrize(
    "proof_fault",
    ["missing-report", "stale-report-receipt", "stale-receipt-context"],
)
def test_ready_restart_fails_closed_on_invalid_bound_auditor_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proof_fault: str,
) -> None:
    """READY recovery never respawns around missing or stale bound proof."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    gate_executions, auditor = _configure_real_fault_matrix(
        repo=repo,
        state_path=state_path,
        monkeypatch=monkeypatch,
    )
    _inject_worker_termination(
        monkeypatch,
        target=CloseAttemptStatus.READY,
    )
    close_module._SHUTTING_DOWN = False

    async def interrupt() -> str:
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
        with pytest.raises(_WorkerTerminated):
            await close_module._CLOSE_TASKS[close_module._close_task_key(repo, attempt_id)]
        return attempt_id

    attempt_id = asyncio.run(interrupt())
    report_path = store_path(state_path, StoreKind.AUDITOR_REPORT)
    if proof_fault == "missing-report":
        report_path.write_text("", encoding="utf-8")
    elif proof_fault == "stale-report-receipt":
        rows = [
            orjson.loads(line) for line in report_path.read_bytes().splitlines() if line.strip()
        ]
        assert len(rows) == 1
        rows[0]["payload"]["body"]["criteria"][0]["evidence_refs"][0]["ref"] = (
            f"urn:eawf:v1:store:{_WAVE}/gate_receipt/GR-stale"
        )
        report_path.write_bytes(orjson.dumps(rows[0]) + b"\n")
    else:
        receipt_path = store_path(state_path, StoreKind.GATE_RECEIPT)
        rows = [
            orjson.loads(line) for line in receipt_path.read_bytes().splitlines() if line.strip()
        ]
        assert len(rows) == 1
        rows[0]["payload"]["runner_environment_digest"] = "0" * 64
        receipt_path.write_bytes(orjson.dumps(rows[0]) + b"\n")

    async def restart() -> None:
        assert resume_durable_close_attempts(ctx) == 1
        await close_module._CLOSE_TASKS[close_module._close_task_key(repo, attempt_id)]

    asyncio.run(restart())
    final = State.model_validate_json(state_path.read_bytes())
    assert final.close_attempts[attempt_id].status is CloseAttemptStatus.BLOCKED
    assert final.waves[_WAVE].status is WaveStatus.CLAIMED
    assert set(final.close_attempts) == {attempt_id}
    assert gate_executions == ["G-MATRIX"]
    assert auditor.calls == 1


# --- REL-003: one typed close failure vocabulary ---------------------------


_REQUIRED_FAILURE_KINDS = {
    "timed_out",
    "harness_fault",
    "work_rejected",
    "operator_cancelled",
    "policy_blocked",
}


def _close_attempt_row() -> dict[str, Any]:
    """One minimal schema-valid ``CloseAttempt`` payload."""
    sha = "a" * 40
    digest = "b" * 64
    stamp = "2026-07-28T12:00:00Z"
    return {
        "id": "CA-01",
        "wave_id": "P01-I01-W01",
        "outcome": "verified exact integrated revision",
        "tokens_consumed": None,
        "generation": 1,
        "supersedes_id": None,
        "status": "queued",
        "integration_id": "WI-01",
        "candidate_sha": sha,
        "integrated_sha": sha,
        "tree_sha": sha,
        "wave_revision_digest": digest,
        "spec_digest": digest,
        "criteria_digest": digest,
        "gate_manifest_digest": digest,
        "policy_digest": digest,
        "runner_environment_digest": digest,
        "dependency_binding_digest": digest,
        "required_gate_ids": ["G-01"],
        "gate_receipt_ids": [],
        "audit_requirement": "required",
        "audit_report_id": None,
        "no_runtime_waiver": False,
        "repair_wave_id": None,
        "repair_generation": None,
        "repair_budget_remaining": 1,
        "infrastructure_retry_budget_remaining": 1,
        "required_operator_actions": [],
        "waiver_decision_ids": [],
        "usage_receipt_ids": [],
        "artifact_refs": [],
        "failure_kind": None,
        "failure_detail_ref": None,
        "invalidation_causes": [],
        "requested_at": stamp,
        "started_at": None,
        "updated_at": stamp,
        "terminal_at": None,
        "idempotency_key": "close:P01-I01-W01:1",
        "apply_event_id": None,
    }


@pytest.mark.unit
def test_close_failure_kind_is_the_closed_persisted_vocabulary() -> None:
    """``CloseAttempt.failure_kind`` validates against the closed enum."""
    values = {member.value for member in CloseFailureKind}
    assert values >= _REQUIRED_FAILURE_KINDS

    row = _close_attempt_row()
    assert CloseAttempt.model_validate(row).failure_kind is None
    for member in CloseFailureKind:
        parsed = CloseAttempt.model_validate({**row, "failure_kind": member.value})
        assert parsed.failure_kind is member
        assert parsed.model_dump(mode="json")["failure_kind"] == member.value


@pytest.mark.unit
@pytest.mark.parametrize("rejected", ["", "infrastructure", "TIMED_OUT", "timed out", 7])
def test_close_attempt_rejects_failure_kind_outside_the_vocabulary(rejected: object) -> None:
    """Empty, near-miss, wrong-case, and wrong-type kinds all fail closed."""
    with pytest.raises(PydanticValidationError):
        CloseAttempt.model_validate({**_close_attempt_row(), "failure_kind": rejected})


@pytest.mark.unit
def test_failure_status_classifies_by_exception_class_not_message() -> None:
    """No lowered-substring branch survives in ``_failure_status``."""
    source = inspect.getsource(close_module._failure_status)
    assert "lowered" not in source
    assert ".lower()" not in source
    assert " in detail" not in source
    assert source.count("isinstance") >= 3

    # Same prose, opposite classes: only the class may decide the kind.
    prose = "close attempt stale: validation_failed refused"
    assert close_module._failure_status(close_module.CloseHarnessError(prose)) == (
        CloseAttemptStatus.FAILED,
        CloseFailureKind.HARNESS_FAULT,
    )
    assert close_module._failure_status(close_module.CloseWorkRejectedError(prose)) == (
        CloseAttemptStatus.BLOCKED,
        CloseFailureKind.WORK_REJECTED,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("factory", "expected_status", "expected_kind"),
    [
        (
            lambda: close_module.CloseTimedOutError("gate wall clock exceeded"),
            CloseAttemptStatus.FAILED,
            CloseFailureKind.TIMED_OUT,
        ),
        (
            lambda: TimeoutError("close stage exceeded its budget"),
            CloseAttemptStatus.FAILED,
            CloseFailureKind.TIMED_OUT,
        ),
        (
            lambda: close_module.CloseHarnessError("worktree harness broke"),
            CloseAttemptStatus.FAILED,
            CloseFailureKind.HARNESS_FAULT,
        ),
        (
            lambda: RuntimeError("unclassified harness breakage"),
            CloseAttemptStatus.FAILED,
            CloseFailureKind.HARNESS_FAULT,
        ),
        (
            lambda: close_module.CloseWorkRejectedError("gate G-MATRIX failed"),
            CloseAttemptStatus.BLOCKED,
            CloseFailureKind.WORK_REJECTED,
        ),
        (
            lambda: close_module.ClosePolicyBlockedError("waivers are disabled"),
            CloseAttemptStatus.BLOCKED,
            CloseFailureKind.POLICY_BLOCKED,
        ),
        (
            lambda: LifecycleGuardError("waiver_mode_disabled", _WAVE, "waivers are disabled"),
            CloseAttemptStatus.BLOCKED,
            CloseFailureKind.POLICY_BLOCKED,
        ),
        (
            lambda: close_module.CloseStaleInputError("close attempt stale: drift"),
            CloseAttemptStatus.STALE,
            CloseFailureKind.STALE_INPUT,
        ),
    ],
)
def test_failure_status_routes_each_typed_exception(
    factory: Any,
    expected_status: CloseAttemptStatus,
    expected_kind: CloseFailureKind,
) -> None:
    """Every routed exception class lands on exactly one durable outcome."""
    assert close_module._failure_status(factory()) == (expected_status, expected_kind)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cause", "expected_kind"),
    [
        (subprocess.TimeoutExpired(cmd="git", timeout=1.0), CloseFailureKind.TIMED_OUT),
        (OSError("git is not installed"), CloseFailureKind.HARNESS_FAULT),
        (None, CloseFailureKind.STALE_INPUT),
    ],
)
def test_workspace_fault_types_from_the_chained_cause(
    cause: BaseException | None,
    expected_kind: CloseFailureKind,
) -> None:
    """One workspace error type splits by cause, never by its message."""
    error = CloseWorkspaceError("close tree mismatch: expected 'a', resolved 'b'")
    error.__cause__ = cause
    assert close_module._workspace_fault(error).failure_kind is expected_kind


def _submit_unscheduled(
    ctx: Any,
    *,
    repo: Path,
    state_path: Path,
    retry_budget: int,
) -> str:
    """Create one durable attempt without letting the worker start."""

    async def _body() -> str:
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

    attempt_id = asyncio.run(_body())
    state = State.model_validate_json(state_path.read_bytes())
    state.close_attempts[attempt_id] = state.close_attempts[attempt_id].model_copy(
        update={"infrastructure_retry_budget_remaining": retry_budget}
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return attempt_id


def _persisted_attempt_row(state_path: Path, attempt_id: str) -> dict[str, Any]:
    """Read one close-attempt row straight out of ``state.json``."""
    payload = orjson.loads(state_path.read_bytes())
    return dict(payload["close_attempts"][attempt_id])


@pytest.mark.parametrize(
    ("factory", "expected_status", "expected_kind"),
    [
        (
            lambda: close_module.CloseTimedOutError("close prepare exceeded its budget"),
            CloseAttemptStatus.FAILED,
            CloseFailureKind.TIMED_OUT,
        ),
        (
            lambda: RuntimeError("close harness broke before any gate ran"),
            CloseAttemptStatus.FAILED,
            CloseFailureKind.HARNESS_FAULT,
        ),
        (
            lambda: close_module.CloseWorkRejectedError("gate G-MATRIX failed"),
            CloseAttemptStatus.BLOCKED,
            CloseFailureKind.WORK_REJECTED,
        ),
        (
            lambda: LifecycleGuardError("waiver_mode_disabled", _WAVE, "waivers are disabled"),
            CloseAttemptStatus.BLOCKED,
            CloseFailureKind.POLICY_BLOCKED,
        ),
        (
            lambda: close_module.CloseStaleInputError(
                "close attempt stale: drift",
                causes=["integration generation changed"],
            ),
            CloseAttemptStatus.STALE,
            CloseFailureKind.STALE_INPUT,
        ),
    ],
)
def test_typed_worker_fault_persists_its_exact_failure_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory: Any,
    expected_status: CloseAttemptStatus,
    expected_kind: CloseFailureKind,
) -> None:
    """Each typed exception reaches ``state.json`` as its own failure kind."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    monkeypatch.setattr(close_module, "_schedule", lambda *_args, **_kwargs: False)
    close_module._SHUTTING_DOWN = False
    attempt_id = _submit_unscheduled(ctx, repo=repo, state_path=state_path, retry_budget=0)

    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise factory()

    monkeypatch.setattr(close_module, "prepare_close_workspace", _raise)
    asyncio.run(close_module._run_attempt(ctx, repo_root=repo, attempt_id=attempt_id))

    row = _persisted_attempt_row(state_path, attempt_id)
    assert row["status"] == expected_status.value
    assert row["failure_kind"] == expected_kind.value
    assert row["failure_detail_ref"]
    if expected_status is CloseAttemptStatus.STALE:
        assert row["invalidation_causes"] == ["integration generation changed"]
    else:
        assert row["invalidation_causes"] == []
    final = State.model_validate_json(state_path.read_bytes())
    assert final.waves[_WAVE].status is WaveStatus.CLAIMED


def test_operator_cancel_persists_operator_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancel RPC is the only producer of ``operator_cancelled``."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    monkeypatch.setattr(close_module, "_schedule", lambda *_args, **_kwargs: False)
    close_module._SHUTTING_DOWN = False
    attempt_id = _submit_unscheduled(ctx, repo=repo, state_path=state_path, retry_budget=1)

    async def _cancel() -> dict[str, Any]:
        return await close_module.cancel(
            ctx,
            {
                "ref": attempt_id,
                "repo_root": str(repo),
                "reason": "operator aborted the close",
            },
        )

    result = asyncio.run(_cancel())

    assert result["attempt"]["status"] == CloseAttemptStatus.CANCELLED.value
    row = _persisted_attempt_row(state_path, attempt_id)
    assert row["failure_kind"] == CloseFailureKind.OPERATOR_CANCELLED.value
    assert row["terminal_at"] is not None


def test_harness_fault_still_spends_the_infrastructure_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary: a retryable harness fault re-queues instead of going terminal."""
    repo, state_path, ctx = _repo_with_state(tmp_path)
    monkeypatch.setattr(close_module, "_schedule", lambda *_args, **_kwargs: False)
    close_module._SHUTTING_DOWN = False
    attempt_id = _submit_unscheduled(ctx, repo=repo, state_path=state_path, retry_budget=1)

    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("close harness broke")

    monkeypatch.setattr(close_module, "prepare_close_workspace", _raise)
    asyncio.run(close_module._run_attempt(ctx, repo_root=repo, attempt_id=attempt_id))

    row = _persisted_attempt_row(state_path, attempt_id)
    assert row["status"] == CloseAttemptStatus.QUEUED.value
    assert row["failure_kind"] == CloseFailureKind.INFRASTRUCTURE_RETRY.value
    assert row["infrastructure_retry_budget_remaining"] == 0
    assert row["terminal_at"] is None
