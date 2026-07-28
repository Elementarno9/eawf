"""Fault-injection matrix for durable exact-revision close attempts."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.enums import (
    AgentSessionRole,
    CloseAttemptStatus,
    DependencyStage,
    EffortBucket,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    State,
    Wave,
    WaveDependencyBarrier,
    wave_dependency_key,
)
from eawf.kernel.store.paths import store_path
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
from eawf.workflow.audit_dsl import registry
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec
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

    gate_executions: list[str] = []
    real_gate = registry.CHECK_REGISTRY["file_exists"]

    def _counted_gate(spec: CheckSpec, cwd: Path) -> CheckResult:
        gate_executions.append(spec.name)
        return real_gate(spec, cwd)

    monkeypatch.setitem(registry.CHECK_REGISTRY, "file_exists", _counted_gate)
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
