"""Process-isolation tests for the production close scorer.

:func:`eawf.workflow.verify.oracle.run_oracle` is the close scorer the daemon
close path, the fleet auto-drain loop, and the daemonless close lane all reach.
A deterministic gate is routinely a whole test suite, and a suite that
exercises eawf's own RPCs drives whichever runtime directory and state ledger
its process points at -- the LIVE pair when the gate runs in the calling
process. These tests pin the guarantee that NO scorer branch executes a check
in the calling process:

* the advisory branch (no durable claim callback, as the fleet loop drives it)
  runs the batch runner
  :func:`~eawf.workflow.verify.sandboxed_checks.run_checks_out_of_process`;
* the durable branch (a claim callback but no bound gate context, as a
  ``state.mutate`` close carrying a ``close_attempt_id`` drives it) runs
  :func:`~eawf.runtime.daemon.gate_execution.run_gate_out_of_process`, whose
  child re-performs the freshness claim against the live ledger.

Both spawn a real child interpreter -- these are integration tests on purpose,
because a mocked child proves nothing about the process boundary. Every
fixture tree is built under ``tmp_path``; nothing here addresses the
repository's own ``.ea/``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.models import Wave
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec
from eawf.workflow.verify import oracle
from eawf.workflow.verify.oracle import OracleResult, run_oracle

_ATTEMPT_ID = "CA-ISO-01"


def _criterion() -> CriterionSpec:
    """Build the minimal deterministic criterion the scorer escalates."""
    return CriterionSpec(
        id="CR-01",
        text="the close scorer never runs a gate in the calling process",
        kind="contract",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["G-1"],
        quality_dimension="functional_suitability",
        measurable_signal="a measurable signal of at least twenty characters",
    )


def _gate() -> GateSpec:
    """Build a required, blocking ``file_exists`` gate.

    ``file_exists`` needs no subprocess of its own, so the only process the
    scorer creates is the sandbox child under test.
    """
    return GateSpec(
        id="G-1",
        criterion_id="CR-01",
        kind="file_exists",
        args={"path": "payload.txt"},
        policy="block",
        cadence="every-wave",
        required=True,
    )


def _wave() -> Wave:
    """Build a minimal valid wave for the scorer's jury-tier fallthrough."""
    return Wave(
        id="P32-I01-W40",
        iter_id="P32-I01",
        title="close the last in-process gate path in the close scorer",
        status="claimed",
        opened_at="2026-09-08T00:00:00Z",
    )


def _live_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a throwaway stand-in for a live repo + ``.ea/`` pair.

    Args:
        tmp_path: Per-test scratch root the whole fixture lives under.
        monkeypatch: Used to point the runtime-directory resolver away from the
            operator's real daemon directory, so the sandbox snapshot never
            walks it.

    Returns:
        The path of the fixture's ``state.json``. Its parent is the state
        directory the gate child claims and snapshots against.
    """
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scope_kind": "repo",
                "urn": "urn:eawf:v1:state:ABC",
                "updated_at": "2026-09-08T00:00:00Z",
                "project": {
                    "code": "ABC",
                    "slug": "abc",
                    "title": "Abc",
                    "domains": ["infra"],
                    "default_branch": "main",
                    "status": "active",
                    "repo_urn": "urn:eawf:v1:repo:ABC",
                },
                "current": {
                    "project_code": "ABC",
                    "track_id": None,
                    "phase_id": None,
                    "iter_id": None,
                    "active_wave_ids": [],
                    "active_session_ids": [],
                },
                "workspace": None,
                "phases": {},
                "iters": {},
                "waves": {},
                "artifacts": {},
                "agent_sessions": {},
                "plugins": {},
                "indexes": {},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "payload.txt").write_text("gate target\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))
    return state_path


def _score(
    *,
    state_path: Path,
    repo_root: Path,
    before_gate_execute: Any = None,
    close_attempt_id: str = "",
) -> OracleResult:
    """Drive one criterion through the production scorer.

    Args:
        state_path: The fixture's live ``state.json``.
        repo_root: Working directory the gate child runs against.
        before_gate_execute: The durable claim callback, or ``None`` for the
            advisory branch.
        close_attempt_id: The durable attempt the gate child claims under.

    Returns:
        The scorer's :class:`~eawf.workflow.verify.oracle.OracleResult`.
    """
    return asyncio.run(
        run_oracle(
            _criterion(),
            [_gate()],
            wave=_wave(),
            state=object(),  # type: ignore[arg-type]
            state_path=state_path,
            events_path=state_path.parent / "event.jsonl",
            repo_root=repo_root,
            spawn_factory=lambda _runtime: pytest.fail("the jury tier must not be reached"),
            before_gate_execute=before_gate_execute,
            close_attempt_id=close_attempt_id,
        )
    )


def _claim_files(state_path: Path) -> list[Path]:
    """Return the durable gate claims written under the fixture's state dir."""
    claims = state_path.parent / "local" / "gate-claims"
    return sorted(claims.glob("*.json")) if claims.is_dir() else []


def _forbid_in_process_runner(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Trip-wire the in-process check runner and return its call log.

    The runner is patched at its DEFINING module, so the trip-wire covers any
    import path the scorer might reach it by. The child interpreter is a
    separate process and never sees the patch, so a correctly routed gate still
    executes for real.

    Args:
        monkeypatch: Fixture used to install the trip-wire.

    Returns:
        A list that stays empty unless the calling process ran a check.
    """
    calls: list[str] = []

    def _tripped(specs: list[CheckSpec], **_kwargs: Any) -> list[CheckResult]:
        calls.append(",".join(spec.name for spec in specs))
        raise AssertionError("run_checks must never execute in the scorer's own process")

    monkeypatch.setattr("eawf.workflow.audit_dsl.runner.run_checks", _tripped)
    return calls


def test_close_scorer_runs_every_gate_out_of_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both scorer branches spawn a sandboxed child; neither runs a check here."""
    state_path = _live_tree(tmp_path, monkeypatch)
    in_process_calls = _forbid_in_process_runner(monkeypatch)
    spawned: list[str] = []

    from eawf.runtime.daemon import gate_execution
    from eawf.workflow.verify import sandboxed_checks

    real_batch = sandboxed_checks.run_checks_out_of_process
    real_gate = gate_execution.run_gate_out_of_process

    def _spy_batch(specs: Any, **kwargs: Any) -> list[CheckResult]:
        spawned.append("run_checks_out_of_process")
        return real_batch(specs, **kwargs)

    def _spy_gate(spec: CheckSpec, **kwargs: Any) -> CheckResult:
        spawned.append("run_gate_out_of_process")
        return real_gate(spec, **kwargs)

    monkeypatch.setattr(oracle, "run_checks_out_of_process", _spy_batch)
    monkeypatch.setattr(gate_execution, "run_gate_out_of_process", _spy_gate)

    advisory = _score(state_path=state_path, repo_root=tmp_path)
    durable = _score(
        state_path=state_path,
        repo_root=tmp_path,
        before_gate_execute=lambda *_args: pytest.fail(
            "the parent must not claim: the gate child owns the claim"
        ),
        close_attempt_id=_ATTEMPT_ID,
    )

    assert advisory.status == "pass"
    assert durable.status == "pass"
    assert spawned == ["run_checks_out_of_process", "run_gate_out_of_process"]
    assert in_process_calls == []


def test_close_scorer_advisory_branch_survives_a_crashed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that proves nothing blocks the criterion instead of passing it."""
    state_path = _live_tree(tmp_path, monkeypatch)
    _forbid_in_process_runner(monkeypatch)
    monkeypatch.setattr(
        oracle,
        "run_checks_out_of_process",
        lambda specs, **_kwargs: [
            CheckResult(
                name=specs[0].name,
                kind=specs[0].kind,
                passed=False,
                status="blocked",
                details="readiness check child crashed without a terminal result",
            )
        ],
    )

    result = _score(state_path=state_path, repo_root=tmp_path)

    assert result.status == "blocked"
    assert result.gate_id == "G-1"


def test_close_scorer_durable_branch_without_attempt_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable close naming no attempt is refused, never run in process."""
    state_path = _live_tree(tmp_path, monkeypatch)
    in_process_calls = _forbid_in_process_runner(monkeypatch)

    result = _score(
        state_path=state_path,
        repo_root=tmp_path,
        before_gate_execute=lambda *_args: None,
        close_attempt_id="",
    )

    assert result.status == "blocked"
    assert "no claimable attempt identity" in result.detail
    assert in_process_calls == []
    assert _claim_files(state_path) == []


def test_stale_freshness_claim_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim left without a terminal receipt still refuses through the child.

    The first score lets the gate child write its own claim and deliberately
    binds no terminal-result callback, so the claim is left exactly as a
    crashed child would leave it. The second score must then read that stale
    claim through a NEW child and refuse with the indeterminate-execution
    outcome -- the same ``blocked`` status and the same receipt-id-bearing
    detail the claim check produces in process. Routing the claim across the
    process boundary must not turn that refusal into a pass.
    """
    state_path = _live_tree(tmp_path, monkeypatch)
    in_process_calls = _forbid_in_process_runner(monkeypatch)

    first = _score(
        state_path=state_path,
        repo_root=tmp_path,
        before_gate_execute=lambda *_args: None,
        close_attempt_id=_ATTEMPT_ID,
    )
    assert first.status == "pass"

    claims = _claim_files(state_path)
    assert len(claims) == 1
    freshness_key = claims[0].stem
    claim_payload = json.loads(claims[0].read_text(encoding="utf-8"))
    assert claim_payload["attempt_id"] == _ATTEMPT_ID
    assert claim_payload["gate_id"] == "G-1"
    assert claim_payload["completed_at"] is None

    second = _score(
        state_path=state_path,
        repo_root=tmp_path,
        before_gate_execute=lambda *_args: None,
        close_attempt_id=_ATTEMPT_ID,
    )

    assert second.status == "blocked"
    assert second.gate_id == "G-1"
    assert second.check_result is not None
    assert second.check_result.status == "blocked"
    assert second.check_result.freshness_key == freshness_key
    assert "indeterminate gate execution" in second.detail
    assert f"GR-{freshness_key[:32]}" in second.detail
    assert in_process_calls == []
