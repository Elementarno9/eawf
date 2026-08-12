"""Close-gate auditor-vs-jury routing under earned authority.

The A4 OR-fold bypass: the config merge ORs ``cross_vendor_jury`` across
enabled profiles, so a bare ``not verify_block.cross_vendor_jury`` check
let an ADVISORY jury displace the blocking single-auditor for every
verdict-always wave (40 of P30's 98 lost their gate). The fix routes a
verdict-always wave to the jury tier ONLY when the jury has EARNED
blocking authority; under ADVISORY authority the blocking single-auditor
still fires.

``_enforce_wave_close_gate`` is driven directly with the verify-block
resolution, authority resolver, verdict producer/gate, and ``run_oracle``
all stubbed at their import sites — no daemon, no subprocess.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.enums import AuditRequirement, CloseAttemptStatus, StoreKind
from eawf.kernel.state.models import CloseAttempt, State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.gate_receipt import GateReceipt
from eawf.kernel.store.paths import store_path
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.platform.profiles.models import VerifyBlock
from eawf.runtime.daemon.limits import (
    cli_mutation_timeout_for,
    configured_juror_wall_clock,
    mutation_hard_limit_for,
)
from eawf.runtime.daemon.methods import state as daemon_state
from eawf.workflow.dispatch.verdict import DurableAuditContext, DurableAuditCriterion
from eawf.workflow.verify.oracle import OracleResult

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
_WAVE = "P30-I23-W07"


def _state_payload() -> dict[str, Any]:
    """A minimal State with one CLAIMED verdict-always (L-bucket) wave."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _T0.isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "ABC",
                "track_id": None,
                "title": "P30",
                "status": "active",
                "iter_ids": ["P30-I23"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I23": {
                "id": "P30-I23",
                "phase_id": "P30",
                "title": "I23",
                "status": "active",
                "wave_ids": [_WAVE],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE: {
                "id": _WAVE,
                "iter_id": "P30-I23",
                "title": "close the jury OR-fold single-auditor bypass",
                "status": "claimed",
                "file_scopes": ["src/eawf/runtime/daemon/methods/state.py"],
                "success_criteria": [],
                "gates": [],
                "effort_bucket": "L",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


#: A wall clock deliberately unequal to the VerifyBlock default, so the
#: assertion proves the configured value is threaded rather than the default
#: coincidentally matching.
_WALL_CLOCK: float = 1234.0


class _Recorder:
    """Records which oracle path the close gate routed through."""

    def __init__(self) -> None:
        self.produced_verdict = False
        self.enforced_verdict_gate = False
        self.oracle_calls = 0
        self.verdict_wall_clock: float | None = None
        self.reuse_existing: bool | None = None
        self.durable_context: DurableAuditContext | None = None
        self.events: list[str] = []


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    authority: BlockAuthority,
) -> _Recorder:
    recorder = _Recorder()
    verify_block = VerifyBlock(
        enforce=True, cross_vendor_jury=True, juror_wall_clock_seconds=_WALL_CLOCK
    )

    monkeypatch.setattr(
        "eawf.workflow.verify.readiness.load_active_verify_block",
        lambda *a, **k: verify_block,
    )
    monkeypatch.setattr(
        "eawf.workflow.verify.readiness.resolve_wave_verify_block",
        lambda block, wave: verify_block,
    )
    monkeypatch.setattr(
        daemon_state,
        "_resolve_jury_block_authority",
        lambda state, *, state_path, verify_block: authority,
    )

    async def _produce(
        state: Any,
        wave: Any,
        *,
        state_path: Path,
        repo_root: Path,
        wall_clock_seconds: float,
        reuse_existing: bool = True,
        durable_context: DurableAuditContext | None = None,
    ) -> str:
        recorder.produced_verdict = True
        recorder.verdict_wall_clock = wall_clock_seconds
        recorder.reuse_existing = reuse_existing
        recorder.durable_context = durable_context
        recorder.events.append("audit")
        return "AR-test"

    def _enforce(wave: Any, *, state_path: Path) -> None:
        recorder.enforced_verdict_gate = True

    monkeypatch.setattr(daemon_state, "_produce_high_risk_verdict", _produce)
    monkeypatch.setattr(daemon_state, "_enforce_wave_verdict_gate", _enforce)
    monkeypatch.setattr(daemon_state, "_jury_spawn_factory", lambda *a, **k: None)

    async def _oracle(criterion: Any, gates: Any, **kwargs: Any) -> OracleResult:
        recorder.oracle_calls += 1
        recorder.events.append("gate")
        return OracleResult(
            criterion_id=criterion.id,
            status="pass",
            tier=4,
            gate_id=None,
            detail="jury tier stub",
        )

    monkeypatch.setattr("eawf.workflow.verify.oracle.run_oracle", _oracle)
    return recorder


def _drive(tmp_path: Path) -> tuple[State, Mutation]:
    state = State.model_validate(_state_payload())
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=_WAVE,
        mutation_id="m" * 32,
        params={"wave_id": _WAVE, "outcome": "ok"},
    )
    return state, mutation


def test_close_gate_advisory_jury_routes_to_blocking_auditor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-01: ADVISORY authority keeps the blocking single-auditor in charge.

    Under merged ``cross_vendor_jury=True`` an uncalibrated (ADVISORY) jury
    must NOT displace the single-auditor: the verdict-always wave routes
    through ``_produce_high_risk_verdict`` + ``_enforce_wave_verdict_gate``
    and never reaches the jury fallthrough.
    """
    recorder = _wire(monkeypatch, authority=BlockAuthority.ADVISORY)
    state, mutation = _drive(tmp_path)

    evidence = asyncio.run(
        daemon_state._enforce_wave_close_gate(
            state,
            mutation,
            state_path=tmp_path / ".ea" / "state.json",
            repo_root=tmp_path,
        )
    )

    assert recorder.produced_verdict is True
    assert recorder.enforced_verdict_gate is True
    assert recorder.oracle_calls == 0
    assert evidence == []
    # The auditor spawn takes the CONFIGURED wall clock, not the factory's 600s
    # default: a killed auditor writes no verdict, and the gate reads "no
    # verdict" as a refusal -- so a too-short ceiling makes the wave unclosable
    # no matter how often the operator retries.
    assert recorder.verdict_wall_clock == _WALL_CLOCK
    assert recorder.reuse_existing is True
    assert VerifyBlock().juror_wall_clock_seconds != _WALL_CLOCK


def test_durable_high_risk_close_runs_deterministic_gates_before_fresh_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Durable close consumes deterministic proof before spawning its auditor."""
    recorder = _wire(monkeypatch, authority=BlockAuthority.ADVISORY)
    state, mutation = _drive(tmp_path)
    criterion = CriterionSpec(
        id="CR-01",
        text="the deterministic close gate passes before the fresh audit",
        kind="contract",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["G-01"],
        quality_dimension="functional_suitability",
        measurable_signal="the required file exists in the exact verification tree",
    )
    state.waves[_WAVE].success_criteria = [criterion]
    state.waves[_WAVE].gates = [
        GateSpec(
            id="G-01",
            criterion_id=criterion.id,
            kind="file_exists",
            args={"path": "sentinel"},
            policy="block",
            cadence="every-wave",
        )
    ]
    mutation.params["close_attempt_id"] = "CA-01"
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.close.gate_freshness_inputs",
        lambda state, *, attempt_id: {},
    )
    context = DurableAuditContext(
        wave_id=_WAVE,
        close_attempt_id="CA-01",
        integration_id="WI-01",
        integrated_sha="a" * 40,
        tree_sha="b" * 40,
        spec_digest="c" * 64,
        criteria_digest="d" * 64,
        gate_manifest_digest="e" * 64,
        policy_digest="f" * 64,
        runner_digest="1" * 64,
        dependency_binding_digest="2" * 64,
        criteria=(
            DurableAuditCriterion(
                criterion_id="CR-01",
                text=criterion.text,
                deterministic=True,
                gate_receipt_urns=(f"urn:eawf:v1:store:{_WAVE}/gate_receipt/GR-proof",),
            ),
        ),
    )

    def _build_context(**kwargs: Any) -> DurableAuditContext:
        assert recorder.events == ["gate"]
        recorder.events.append("context")
        return context

    monkeypatch.setattr(daemon_state, "_build_durable_audit_context", _build_context)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.close.reusable_bound_audit_report_id",
        lambda *_args, **_kwargs: None,
    )

    asyncio.run(
        daemon_state._enforce_wave_close_gate(
            state,
            mutation,
            state_path=tmp_path / ".ea" / "state.json",
            repo_root=tmp_path,
        )
    )

    assert recorder.events == ["gate", "context", "audit"]
    assert recorder.oracle_calls == 1
    assert recorder.reuse_existing is False
    assert recorder.durable_context == context


def test_build_durable_audit_context_reads_bound_persisted_receipt(
    tmp_path: Path,
) -> None:
    """Exact audit context comes from canonical attempt plus receipt store."""
    state = State.model_validate(_state_payload())
    criterion = CriterionSpec(
        id="CR-01",
        text="the exact deterministic receipt grounds this required criterion",
        kind="contract",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["G-01"],
        quality_dimension="functional_suitability",
        measurable_signal="a passing persisted receipt names this exact integrated tree",
    )
    state.waves[_WAVE].success_criteria = [criterion]
    attempt = CloseAttempt(
        id="CA-01",
        wave_id=_WAVE,
        outcome="close exact integrated revision",
        tokens_consumed=None,
        generation=1,
        supersedes_id=None,
        status=CloseAttemptStatus.CHECKING,
        integration_id="WI-01",
        candidate_sha="0" * 40,
        integrated_sha="a" * 40,
        tree_sha="b" * 40,
        wave_revision_digest="3" * 64,
        spec_digest="c" * 64,
        criteria_digest="d" * 64,
        gate_manifest_digest="e" * 64,
        policy_digest="f" * 64,
        runner_environment_digest="1" * 64,
        dependency_binding_digest="2" * 64,
        required_gate_ids=["G-01"],
        gate_receipt_ids=["GR-proof"],
        audit_requirement=AuditRequirement.REQUIRED,
        no_runtime_waiver=False,
        repair_budget_remaining=1,
        infrastructure_retry_budget_remaining=1,
        requested_at=_T0,
        updated_at=_T0,
        idempotency_key="close-exact-audit",
    )
    state.close_attempts[attempt.id] = attempt
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    receipt = GateReceipt(
        id="GR-proof",
        scope_id=_WAVE,
        criterion_id=criterion.id,
        gate_id="G-01",
        integration_id=attempt.integration_id,
        integrated_sha=attempt.integrated_sha,
        tree_sha=attempt.tree_sha,
        contract_digest=attempt.spec_digest,
        criteria_digest=attempt.criteria_digest,
        gate_manifest_digest=attempt.gate_manifest_digest,
        policy_digest=attempt.policy_digest,
        dependency_binding_digest=attempt.dependency_binding_digest,
        runner_environment_digest=attempt.runner_environment_digest,
        runner_digest="4" * 64,
        environment_digest="5" * 64,
        freshness_key="7" * 64,
        argv_digest="6" * 64,
        timeout_class="quick",
        resolved_timeout_seconds=30.0,
        started_at=_T0,
        ended_at=_T0,
        duration_ms=0,
        result="pass",
        exit_status=0,
    )
    append_envelope(
        store_path(state_path, StoreKind.GATE_RECEIPT),
        Envelope(
            id=receipt.id,
            kind=StoreKind.GATE_RECEIPT,
            scope_id=_WAVE,
            created_at=_T0,
            summary="persisted exact gate proof",
            payload=receipt.model_dump(mode="json"),
        ),
    )

    context = daemon_state._build_durable_audit_context(
        state_path=state_path,
        close_attempt_id=attempt.id,
        wave=state.waves[_WAVE],
    )

    assert context.close_attempt_id == attempt.id
    assert context.integration_id == attempt.integration_id
    assert context.integrated_sha == attempt.integrated_sha
    assert context.tree_sha == attempt.tree_sha
    assert context.spec_digest == attempt.spec_digest
    assert context.criteria_digest == attempt.criteria_digest
    assert context.gate_manifest_digest == attempt.gate_manifest_digest
    assert context.policy_digest == attempt.policy_digest
    assert context.runner_digest == attempt.runner_environment_digest
    assert context.dependency_binding_digest == attempt.dependency_binding_digest
    assert context.criteria[0].gate_receipt_urns == (
        f"urn:eawf:v1:store:{_WAVE}/gate_receipt/GR-proof",
    )


def test_close_gate_blocking_jury_replaces_auditor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-02: BLOCKING authority routes the same wave to the jury tier.

    With a calibrated cohort granting BLOCKING authority the jury replaces
    the blocking single-auditor: the verdict producer/gate never run and
    the per-criterion oracle loop (the jury tier's entry) takes over.
    """
    recorder = _wire(monkeypatch, authority=BlockAuthority.BLOCKING)
    state, mutation = _drive(tmp_path)
    # One required criterion so the oracle loop demonstrably runs.
    from eawf.kernel.spec.common import grandfather_criterion

    wave = state.waves[_WAVE]
    wave.success_criteria = [
        grandfather_criterion("prove the jury tier is reachable under blocking", index=1)
    ]

    asyncio.run(
        daemon_state._enforce_wave_close_gate(
            state,
            mutation,
            state_path=tmp_path / ".ea" / "state.json",
            repo_root=tmp_path,
        )
    )

    assert recorder.produced_verdict is False
    assert recorder.enforced_verdict_gate is False
    assert recorder.oracle_calls == 1


# --- P30-I25-W35: the configured ceiling must survive the config->block path ---


def test_repo_config_file_ceiling_reaches_the_close_path_loader(tmp_path: Path) -> None:
    """A ceiling in `.ea/config.yaml` must reach the block THE CLOSE PATH resolves.

    Drives `load_active_verify_block` -- the function the close gate itself calls
    (`daemon/methods/state.py` -> `_load_active_verify_block` -> the overlay). Not
    the overlay helper, and not a monkeypatched block.

    This is the THIRD attempt at this test, and the first two were both green over
    a dead path -- the exact defect class the phase exists to kill:

    - W32's asserted the ceiling on a synthetic block it had monkeypatched in,
      while the overlay silently dropped the leaf. The auditor kept spawning at
      600s, was killed mid-audit, wrote no verdict, and the wave could not close.
    - W35's called `_overlay_repo_verify_leaves` directly, reaching PAST the loader
      whose call site is the thing that could go missing. Deleting that call site
      left it green.

    So: delete the overlay call site in the loader and this test fails.
    """
    from eawf.workflow.verify.readiness import load_active_verify_block

    ea = tmp_path / ".ea"
    ea.mkdir()
    (ea / "config.yaml").write_text(
        "schema_version: '1.0'\n"
        "profiles:\n  enabled:\n    - core\n    - python\n"
        "verify:\n  juror_wall_clock_seconds: 1800.0\n",
        encoding="utf-8",
    )
    state = State.model_validate(_state_payload())
    wave_id = next(iter(state.waves))

    resolved = load_active_verify_block(wave_id, state, repo_root=tmp_path, config_root=tmp_path)

    assert resolved is not None
    # Off the FILE, through the loader the close path calls, onto the block.
    assert resolved.juror_wall_clock_seconds == 1800.0
    assert resolved.juror_wall_clock_seconds != VerifyBlock().juror_wall_clock_seconds


def test_repo_verify_overlay_ignores_a_junk_wall_clock() -> None:
    from eawf.workflow.verify.readiness import _overlay_repo_verify_leaves

    for junk in ("1800", True, 0, -5):
        resolved = _overlay_repo_verify_leaves(
            VerifyBlock(), {"verify": {"juror_wall_clock_seconds": junk}}
        )
        assert resolved is not None
        assert resolved.juror_wall_clock_seconds == VerifyBlock().juror_wall_clock_seconds


def test_watchdog_hard_limit_accommodates_a_raised_juror_ceiling() -> None:
    """An auditor allowed 1800s inside a mutation watched at 900s is killed anyway.

    The watchdog exists to catch a HUNG mutation, not to cancel a long one that
    is working. Raising the juror ceiling without raising the watchdog would make
    the close strictly worse than the timeout it was raised to escape.
    """
    from eawf.runtime.daemon.main import (
        _MUTATION_HARD_LIMIT_SECONDS,
        mutation_hard_limit_for,
    )

    assert mutation_hard_limit_for(1800.0) > 1800.0
    assert mutation_hard_limit_for(1800.0) == 2100.0
    # The default ceiling leaves the historical limit untouched.
    assert mutation_hard_limit_for(600.0) == _MUTATION_HARD_LIMIT_SECONDS
    assert mutation_hard_limit_for(None) == _MUTATION_HARD_LIMIT_SECONDS


def test_cli_wire_timeout_outlives_the_daemon_hard_limit(tmp_path: Path) -> None:
    """W48: the CLI must not give up before the daemon does.

    A gated close spawns a fresh-context auditor INSIDE the watched mutation, so
    the daemon may legitimately hold the request for the juror ceiling plus the
    commit margin. The CLI's wire timeout was a 30s constant -- less than a
    THIRTIETH of the daemon's own floor -- so every gated close raised
    ``DaemonMutationIndeterminate`` ("the write may or may not have applied")
    while the daemon went on to apply it. It misreported every close in P30-I25.

    The ceiling is read off the repo's real config file, through the same helper
    the CLI calls, so deleting the wiring cannot leave this green.
    """
    from eawf.surfaces.cli._daemon_client import DEFAULT_CALL_TIMEOUT_SECONDS

    ea = tmp_path / ".ea"
    ea.mkdir()
    (ea / "config.yaml").write_text(
        "schema_version: '1.0'\nverify:\n  juror_wall_clock_seconds: 1800.0\n",
        encoding="utf-8",
    )

    ceiling = configured_juror_wall_clock(tmp_path)

    assert ceiling == 1800.0
    # The old constant could not even cover the daemon's DEFAULT limit, let alone
    # a raised ceiling -- that is the whole bug, in one line.
    assert mutation_hard_limit_for(None) > DEFAULT_CALL_TIMEOUT_SECONDS
    assert cli_mutation_timeout_for(ceiling) > mutation_hard_limit_for(ceiling)
    assert cli_mutation_timeout_for(None) > mutation_hard_limit_for(None)
