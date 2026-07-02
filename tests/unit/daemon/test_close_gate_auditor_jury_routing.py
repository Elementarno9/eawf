"""Close-gate auditor-vs-jury routing under earned authority (P30-I23-W07).

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

from eawf.kernel.state.models import State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.platform.profiles.models import VerifyBlock
from eawf.runtime.daemon.methods import state as daemon_state
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


class _Recorder:
    """Records which oracle path the close gate routed through."""

    def __init__(self) -> None:
        self.produced_verdict = False
        self.enforced_verdict_gate = False
        self.oracle_calls = 0


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    authority: BlockAuthority,
) -> _Recorder:
    recorder = _Recorder()
    verify_block = VerifyBlock(enforce=True, cross_vendor_jury=True)

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

    async def _produce(state: Any, wave: Any, *, state_path: Path, repo_root: Path) -> None:
        recorder.produced_verdict = True

    def _enforce(wave: Any, *, state_path: Path) -> None:
        recorder.enforced_verdict_gate = True

    monkeypatch.setattr(daemon_state, "_produce_high_risk_verdict", _produce)
    monkeypatch.setattr(daemon_state, "_enforce_wave_verdict_gate", _enforce)
    monkeypatch.setattr(daemon_state, "_jury_spawn_factory", lambda *a, **k: None)

    async def _oracle(criterion: Any, gates: Any, **kwargs: Any) -> OracleResult:
        recorder.oracle_calls += 1
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
