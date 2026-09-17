"""Receipt-id binding survives a rival commit on the same close attempt.

Both receipt-binding paths used to build the new ``gate_receipt_ids``
list from a state read taken outside the state lock, and the commit
replaced the whole list. A second receipt bound in that window was
therefore dropped. The probes below reproduce the interleaving in one
thread: the attempt snapshot a path reads is captured *before* a rival
receipt is bound, so a whole-list replacement would lose the rival id.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods import close_evidence
from eawf.runtime.daemon.methods.close import (
    commit_attempt,
    gate_freshness_inputs,
    persist_gate_receipt,
    submit,
)
from eawf.workflow.audit_dsl.models import CheckResult
from tests.integration.runtime.daemon.test_close_lock_split import _WAVE
from tests.integration.runtime.daemon.test_durable_close import _repo_with_state

pytestmark = pytest.mark.integration

_FRESHNESS_KEY = "a" * 64
_RECEIPT_ID = f"GR-{'a' * 32}"
_RIVAL_ID = "GR-rival"


@dataclass(frozen=True)
class _Attempt:
    """One queued close attempt plus a complete gate result for it."""

    ctx: Any
    repo: Path
    state_path: Path
    attempt_id: str
    result: CheckResult
    execution_root: Path


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Attempt:
    repo, state_path, ctx = _repo_with_state(tmp_path)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.close.schedule_attempt",
        lambda *args, **kwargs: False,
    )

    async def _submit() -> str:
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

    attempt_id = asyncio.run(_submit())
    state = State.model_validate_json(state_path.read_bytes())
    state.close_attempts[attempt_id] = state.close_attempts[attempt_id].model_copy(
        update={"required_gate_ids": ["G-01"]}
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    freshness = gate_freshness_inputs(state, attempt_id=attempt_id)["G-01"].model_copy(
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
        runner_fingerprint="runner",
        environment_fingerprint="environment",
        full_log_ref=log_ref.as_posix(),
        freshness_key=_FRESHNESS_KEY,
        freshness=freshness,
    )
    return _Attempt(
        ctx=ctx,
        repo=repo,
        state_path=state_path,
        attempt_id=attempt_id,
        result=result,
        execution_root=execution_root,
    )


def _bound_ids(state_path: Path, attempt_id: str) -> list[str]:
    state = State.model_validate_json(state_path.read_bytes())
    return list(state.close_attempts[attempt_id].gate_receipt_ids)


def _arm_rival_bind(
    monkeypatch: pytest.MonkeyPatch,
    *,
    attempt: _Attempt,
    rival_id: str,
) -> None:
    """Bind *rival_id* right after the binding path reads its snapshot.

    The first attempt read of a persist call fetches the attempt itself;
    the read that follows is the one whose result the binding commit is
    built from. Firing once, after that read is captured, leaves the
    caller holding a snapshot the store has already moved past.
    """
    real_load = close_evidence._load_state
    reads = {"count": 0, "fired": False}

    def _load_then_race(ctx: Any, repo_root: Path) -> State:
        reads["count"] += 1
        snapshot = real_load(ctx, repo_root)
        if reads["count"] >= 2 and not reads["fired"]:
            reads["fired"] = True
            commit_attempt(
                attempt.ctx,
                repo_root=attempt.repo,
                attempt_id=attempt.attempt_id,
                updates={},
                command="close.gate_receipt",
                append_gate_receipt_id=rival_id,
            )
        return snapshot

    monkeypatch.setattr(close_evidence, "_load_state", _load_then_race)


def test_persist_gate_receipt_keeps_a_rival_id_bound_mid_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh receipt binding merges with one bound since its own read."""
    attempt = _prepare(tmp_path, monkeypatch)
    _arm_rival_bind(monkeypatch, attempt=attempt, rival_id=_RIVAL_ID)

    receipt_id = persist_gate_receipt(
        attempt.ctx,
        repo_root=attempt.repo,
        execution_root=attempt.execution_root,
        attempt_id=attempt.attempt_id,
        criterion_id="C-01",
        gate_id="G-01",
        result=attempt.result,
    )

    assert receipt_id == _RECEIPT_ID
    assert _bound_ids(attempt.state_path, attempt.attempt_id) == [_RIVAL_ID, _RECEIPT_ID]


def test_reused_gate_receipt_keeps_a_rival_id_bound_mid_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebinding an already-stored receipt merges the same way."""
    attempt = _prepare(tmp_path, monkeypatch)
    first = persist_gate_receipt(
        attempt.ctx,
        repo_root=attempt.repo,
        execution_root=attempt.execution_root,
        attempt_id=attempt.attempt_id,
        criterion_id="C-01",
        gate_id="G-01",
        result=attempt.result,
    )
    assert first == _RECEIPT_ID
    state = State.model_validate_json(attempt.state_path.read_bytes())
    state.close_attempts[attempt.attempt_id] = state.close_attempts[attempt.attempt_id].model_copy(
        update={"gate_receipt_ids": []}
    )
    attempt.state_path.write_text(state.model_dump_json(), encoding="utf-8")
    _arm_rival_bind(monkeypatch, attempt=attempt, rival_id=_RIVAL_ID)

    reused = persist_gate_receipt(
        attempt.ctx,
        repo_root=attempt.repo,
        execution_root=attempt.execution_root,
        attempt_id=attempt.attempt_id,
        criterion_id="C-01",
        gate_id="G-01",
        result=attempt.result,
    )

    assert reused == _RECEIPT_ID
    assert _bound_ids(attempt.state_path, attempt.attempt_id) == [_RIVAL_ID, _RECEIPT_ID]


def test_commit_attempt_binds_one_receipt_id_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binding onto an empty list, then rebinding, yields one entry."""
    attempt = _prepare(tmp_path, monkeypatch)
    assert _bound_ids(attempt.state_path, attempt.attempt_id) == []

    for _ in range(2):
        commit_attempt(
            attempt.ctx,
            repo_root=attempt.repo,
            attempt_id=attempt.attempt_id,
            updates={},
            command="close.gate_receipt",
            append_gate_receipt_id=_RECEIPT_ID,
        )

    assert _bound_ids(attempt.state_path, attempt.attempt_id) == [_RECEIPT_ID]


def test_commit_attempt_binding_ignores_a_stale_caller_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit stale list in the updates never drops the bound id."""
    attempt = _prepare(tmp_path, monkeypatch)
    commit_attempt(
        attempt.ctx,
        repo_root=attempt.repo,
        attempt_id=attempt.attempt_id,
        updates={},
        command="close.gate_receipt",
        append_gate_receipt_id=_RIVAL_ID,
    )

    commit_attempt(
        attempt.ctx,
        repo_root=attempt.repo,
        attempt_id=attempt.attempt_id,
        updates={},
        command="close.gate_receipt",
        append_gate_receipt_id=_RECEIPT_ID,
    )

    assert _bound_ids(attempt.state_path, attempt.attempt_id) == [_RIVAL_ID, _RECEIPT_ID]


def test_commit_attempt_binding_rejects_an_unknown_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No attempt row means no silent receipt binding."""
    attempt = _prepare(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="unknown close attempt"):
        commit_attempt(
            attempt.ctx,
            repo_root=attempt.repo,
            attempt_id="CA-missing",
            updates={},
            command="close.gate_receipt",
            append_gate_receipt_id=_RECEIPT_ID,
        )
