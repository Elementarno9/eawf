"""Fixture builders for driving the production wave-close gate.

:func:`eawf.runtime.daemon.methods.state._enforce_wave_close_gate` is the one
production call site the daemon close pipeline awaits before applying a
wave-close mutation. Two test modules here drive it directly -- the receipt
floor and the gate-execution stability pins -- because the properties under
test are about what that function does and does not run, which a stub of it
could not show.

Driving it needs a tree: a ``state.json`` with the closing wave (and, for a
durable close, its :class:`~eawf.kernel.state.models.CloseAttempt`), an
enforcing verify block, and a runtime directory pointed away from the
operator's own daemon. Everything here builds that under ``tmp_path``; nothing
reads or writes the repository's ``.ea/``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.models import CloseAttempt, State, Wave
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.platform.profiles.models import VerifyBlock

#: The durable attempt id every fixture close claims under.
ATTEMPT_ID = "CA-FLOOR-01"

#: A 40-hex git object id. The fixture never resolves it -- the close gate
#: reads the attempt's digests, not the repository's history.
FIXTURE_SHA = "0" * 40

#: A digest-shaped value for every frozen close-attempt input.
FIXTURE_DIGEST = f"sha256:{'1' * 64}"


def criterion(
    criterion_id: str,
    *,
    gate_ids: list[str],
    required: bool = True,
    evidence_kind: str = "deterministic",
) -> CriterionSpec:
    """Build one typed success criterion bound to *gate_ids*.

    Args:
        criterion_id: The ``CR-<NN>`` id.
        gate_ids: Gate ids the criterion is falsified by.
        required: Whether the close gate must score it.
        evidence_kind: ``deterministic`` compiles a runnable gate; the other
            flavours deliberately do not.

    Returns:
        The validated criterion row.
    """
    return CriterionSpec(
        id=criterion_id,
        text=f"criterion {criterion_id} is observed by a gate that can red",
        kind="contract",
        acceptance_style="binary",
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        required=required,
        gate_ids=gate_ids,
        quality_dimension="functional_suitability",  # type: ignore[arg-type]
        measurable_signal="a measurable signal of at least twenty characters",
    )


def file_exists_gate(
    gate_id: str,
    *,
    criterion_id: str,
    path: str = "payload.txt",
    required: bool = True,
    policy: str = "block",
) -> GateSpec:
    """Build a ``file_exists`` gate.

    ``file_exists`` spawns no subprocess of its own, so the only child a close
    creates for it is the sandbox gate child under test.

    Args:
        gate_id: The ``G-<NN>`` id.
        criterion_id: The criterion the gate falsifies.
        path: Repo-relative path the gate asserts.
        required: Whether the gate is required.
        policy: ``block`` makes a non-pass decisive.

    Returns:
        The validated gate row.
    """
    return GateSpec(
        id=gate_id,
        criterion_id=criterion_id,
        kind="file_exists",
        args={"path": path},
        policy=policy,  # type: ignore[arg-type]
        cadence="every-wave",
        required=required,
    )


def wave(
    *,
    criteria: list[CriterionSpec],
    gates: list[GateSpec],
    effort_bucket: str,
    wave_id: str = "P32-I01-W43",
) -> Wave:
    """Build the closing wave.

    Args:
        criteria: The wave's typed success criteria.
        gates: The wave's typed gate rows.
        effort_bucket: Drives the risk-weighted verdict requirement: ``L`` /
            ``XL`` classify ``always``, anything smaller is mechanical.
        wave_id: The wave id.

    Returns:
        The validated wave row.
    """
    return Wave(
        id=wave_id,
        iter_id=wave_id.rsplit("-", 1)[0],
        title="refuse a close that cannot prove its required gates ran",
        status="claimed",  # type: ignore[arg-type]
        opened_at="2026-09-10T00:00:00Z",  # type: ignore[arg-type]
        agent_role="executor",  # type: ignore[arg-type]
        effort_bucket=effort_bucket,  # type: ignore[arg-type]
        success_criteria=criteria,
        gates=gates,
    )


def close_attempt(*, wave_id: str, required_gate_ids: list[str]) -> CloseAttempt:
    """Build the durable close attempt the gate child claims under.

    Args:
        wave_id: The wave being closed.
        required_gate_ids: The gate manifest the attempt froze, exactly as
            ``close.submit`` records it.

    Returns:
        The validated attempt row, with no receipts bound yet.
    """
    return CloseAttempt.model_validate(
        {
            "id": ATTEMPT_ID,
            "wave_id": wave_id,
            "outcome": "the close gate refuses a wave whose gates left no receipt",
            "tokens_consumed": 1,
            "generation": 1,
            "supersedes_id": None,
            "status": "applying",
            "integration_id": "WI-FLOOR-01",
            "candidate_sha": FIXTURE_SHA,
            "integrated_sha": FIXTURE_SHA,
            "tree_sha": FIXTURE_SHA,
            "wave_revision_digest": FIXTURE_DIGEST,
            "spec_digest": FIXTURE_DIGEST,
            "criteria_digest": FIXTURE_DIGEST,
            "gate_manifest_digest": FIXTURE_DIGEST,
            "policy_digest": FIXTURE_DIGEST,
            "runner_environment_digest": FIXTURE_DIGEST,
            "dependency_binding_digest": FIXTURE_DIGEST,
            "required_gate_ids": required_gate_ids,
            "gate_receipt_ids": [],
            "audit_requirement": "none",
            "no_runtime_waiver": True,
            "repair_budget_remaining": 1,
            "infrastructure_retry_budget_remaining": 1,
            "requested_at": "2026-09-10T00:00:00Z",
            "updated_at": "2026-09-10T00:00:00Z",
            "idempotency_key": "floor-fixture",
        }
    )


def _state_payload(closing: Wave, attempt: CloseAttempt | None) -> dict[str, Any]:
    """Return the minimal valid state payload carrying *closing*."""
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": "2026-09-10T00:00:00Z",
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
        "waves": {closing.id: closing.model_dump(mode="json")},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    if attempt is not None:
        payload["close_attempts"] = {attempt.id: attempt.model_dump(mode="json")}
    return payload


def live_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    closing: Wave,
    attempt: CloseAttempt | None = None,
) -> tuple[State, Path]:
    """Materialise a throwaway repo + ``.ea/`` pair carrying *closing*.

    Args:
        tmp_path: Per-test scratch root the whole fixture lives under.
        monkeypatch: Used to point the runtime-directory resolver away from the
            operator's real daemon directory.
        closing: The wave the close gate scores.
        attempt: The durable close attempt, or ``None`` for an advisory close.

    Returns:
        The validated state model and the path of its ``state.json``.
    """
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    payload = _state_payload(closing, attempt)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "payload.txt").write_text("gate target\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))
    return State.model_validate(payload), state_path


def enforce_verify_block(monkeypatch: pytest.MonkeyPatch, *, uiux_bands: list[str]) -> None:
    """Make the close gate enforcing, as an opted-in profile does.

    The loader is patched at its defining module so the function-local import
    inside the close gate resolves to the stub at call time.

    Args:
        monkeypatch: Fixture used to install the stub loader.
        uiux_bands: Band tokens; empty models the whole-fleet profile this
            repository runs, non-empty models a band-scoped one.
    """
    block = VerifyBlock(enforce=True, uiux_bands=uiux_bands)
    monkeypatch.setattr(
        "eawf.workflow.verify.readiness.load_active_verify_block",
        lambda *_args, **_kwargs: block,
    )


def score_close(
    state: State,
    *,
    state_path: Path,
    repo_root: Path,
    close_attempt_id: str = "",
    on_gate_result: Callable[[str, str, Any], None] | None = None,
    before_gate_execute: Callable[..., Any] | None = None,
) -> list[EvidenceRecord]:
    """Drive the production close gate over *state*'s single wave.

    Args:
        state: The loaded state carrying the closing wave.
        state_path: Path to the fixture's ``state.json``.
        repo_root: Working directory the gate child runs against.
        close_attempt_id: The durable attempt, or ``""`` for advisory scoring.
        on_gate_result: The durable receipt-persisting hook.
        before_gate_execute: The durable claim callback. Supplying it is what
            makes the scorer take its durable branch.

    Returns:
        The deterministic-pass evidence rows the gate minted.
    """
    from eawf.runtime.daemon.methods.state import _enforce_wave_close_gate

    closing = next(iter(state.waves.values()))
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=closing.id,
        mutation_id="b" * 32,
        params={
            "wave_id": closing.id,
            "outcome": "the close gate scored this wave",
            "close_attempt_id": close_attempt_id,
        },
    )
    return asyncio.run(
        _enforce_wave_close_gate(
            state,
            mutation,
            state_path=state_path,
            repo_root=repo_root,
            before_gate_execute=before_gate_execute,
            on_gate_result=on_gate_result,
        )
    )
