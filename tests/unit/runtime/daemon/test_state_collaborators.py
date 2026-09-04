"""Behaviour pins for the collaborators the state-method split introduced.

The split moved most of ``state.py`` verbatim, so the existing daemon suite
already covers the moved bodies. What it does not cover is the handful of
seams the split had to CREATE: the injected apply-registry builder, the apply
step lifted out of ``state.mutate``, the durable close-attempt hook bundle, the
per-kind event extras, and the per-criterion oracle loop. These tests pin those
seams at their boundaries and on their error paths, so a later edit cannot
quietly change the taxonomy the mutator's wire errors depend on.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import State, Wave
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.state_apply import (
    apply_mutation_under_lock,
    build_apply_registry,
)
from eawf.runtime.daemon.methods.state_close import (
    build_close_attempt_hooks,
    enforce_wave_verdict_gate,
    score_required_criteria,
)
from eawf.runtime.daemon.methods.state_events import mutation_event_extras
from eawf.runtime.daemon.methods.state_models import CachedMutation
from eawf.workflow.dispatch.verdict import WaveVerdictGate
from eawf.workflow.lifecycle.transitions import LifecycleError


def _state() -> State:
    """Build a minimal valid :class:`State` with an empty scope tree."""
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": "2026-09-04T00:00:00Z",
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
    return State.model_validate(payload)


def _mutation(kind: MutationKind, **params: Any) -> Mutation:
    return Mutation(
        kind=kind,
        scope_id="ABC",
        mutation_id=uuid.uuid4().hex,
        params=params,
    )


def _wave(**overrides: Any) -> Wave:
    fields: dict[str, Any] = {
        "id": "P31-I01-W22",
        "iter_id": "P31-I01",
        "title": "split the daemon state-method module",
        "status": WaveStatus.CLAIMED,
        "opened_at": datetime(2026, 9, 4, tzinfo=UTC),
    }
    fields.update(overrides)
    return Wave(**fields)


# --- build_apply_registry -------------------------------------------------


def test_build_apply_registry_covers_every_mutation_kind() -> None:
    """The dispatch table is closed: no enumerated kind is unroutable."""

    def _iter_close(state: State, mutation: Mutation) -> None:  # pragma: no cover - unused
        raise AssertionError("not called")

    registry = build_apply_registry(apply_iter_close=_iter_close)

    assert set(registry) == set(MutationKind)


def test_build_apply_registry_binds_the_injected_iter_close() -> None:
    """The injected applier is the one ``ITER_CLOSE`` resolves to.

    The injection exists so the ``ITER_CLOSE`` applier can stay on the facade
    beside the lifecycle transition callers patch; if the table stopped honouring
    it, that patch point would silently detach.
    """
    calls: list[str] = []

    def _iter_close(state: State, mutation: Mutation) -> None:
        calls.append(mutation.kind.value)

    registry = build_apply_registry(apply_iter_close=_iter_close)
    registry[MutationKind.ITER_CLOSE](_state(), _mutation(MutationKind.ITER_CLOSE))

    assert calls == ["iter_close"]


# --- apply_mutation_under_lock --------------------------------------------


def test_apply_mutation_under_lock_runs_the_apply(tmp_path: Path) -> None:
    """Happy path: the resolved applier runs against the supplied state."""
    seen: list[Mutation] = []

    apply_mutation_under_lock(
        _state(),
        _mutation(MutationKind.EVENT_APPEND, event_type="note"),
        apply_func=lambda _state, mutation: seen.append(mutation),
        state_path=tmp_path / "state.json",
        repo_root_override=None,
    )

    assert [m.kind for m in seen] == [MutationKind.EVENT_APPEND]


def test_apply_mutation_under_lock_maps_a_closure_rejection_to_validation(
    tmp_path: Path,
) -> None:
    """A ``*_CLOSE`` lifecycle rejection surfaces as ``validation_failed``.

    The closure kinds are the ones the CLI maps to exit 2, so their rejection
    must not degrade into the generic invalid-params error.
    """

    def _reject(state: State, mutation: Mutation) -> None:
        raise LifecycleError("unknown wave 'P31-I01-W99'")

    with pytest.raises(DaemonValidationError, match="validation_failed"):
        apply_mutation_under_lock(
            _state(),
            _mutation(MutationKind.WAVE_CLOSE, wave_id="P31-I01-W99", outcome="ok"),
            apply_func=_reject,
            state_path=tmp_path / "state.json",
            repo_root_override=None,
        )


def test_apply_mutation_under_lock_maps_a_non_closure_rejection_to_value_error(
    tmp_path: Path,
) -> None:
    """A non-closure lifecycle rejection stays a plain ``ValueError``.

    Off-by-one against the closure set above: the same exception type on a
    different kind must take the other branch, or the daemon and the in-process
    fallback would disagree on the exit code.
    """
    with pytest.raises(ValueError, match="unknown phase") as excinfo:
        apply_mutation_under_lock(
            _state(),
            _mutation(MutationKind.ROADMAP_APPLY, phase_id="P99"),
            apply_func=build_apply_registry(apply_iter_close=lambda *_a: None)[
                MutationKind.ROADMAP_APPLY
            ],
            state_path=tmp_path / "state.json",
            repo_root_override=None,
        )
    assert not isinstance(excinfo.value, DaemonValidationError)


def test_apply_mutation_under_lock_maps_a_missing_param_to_validation(
    tmp_path: Path,
) -> None:
    """A missing required param is a typed rejection, not a raw ``KeyError``."""
    with pytest.raises(DaemonValidationError, match="missing param"):
        apply_mutation_under_lock(
            _state(),
            _mutation(MutationKind.WAVE_FAIL, wave_id="P31-I01-W22"),
            apply_func=build_apply_registry(apply_iter_close=lambda *_a: None)[
                MutationKind.WAVE_FAIL
            ],
            state_path=tmp_path / "state.json",
            repo_root_override=None,
        )


# --- build_close_attempt_hooks --------------------------------------------


def test_build_close_attempt_hooks_is_inert_without_an_attempt_id(tmp_path: Path) -> None:
    """Boundary: an empty attempt id yields no hooks and touches no attempt.

    This is the non-durable close path, which must behave exactly as it did
    before durable attempts existed -- no stage transition, no gate receipts.
    """
    hooks = build_close_attempt_hooks(
        None,  # type: ignore[arg-type]
        state_path=tmp_path / "state.json",
        canonical_repo_root=tmp_path,
        execution_root=tmp_path,
        close_attempt_id="",
    )

    assert hooks.on_auditing is None
    assert hooks.on_audit_result is None
    assert hooks.before_gate_execute is None
    assert hooks.on_gate_result is None
    assert hooks.prevalidated_gate_ids == set()


# --- mutation_event_extras -------------------------------------------------


def test_mutation_event_extras_is_empty_for_an_unmapped_kind() -> None:
    """A kind with nothing to surface gets an empty extras map."""
    assert mutation_event_extras(_state(), _mutation(MutationKind.EVENT_APPEND)) == {}


def test_mutation_event_extras_carries_the_iter_close_audit_id() -> None:
    """``ITER_CLOSE`` surfaces its audit id and acceptance flag on the envelope."""
    extras = mutation_event_extras(
        _state(),
        _mutation(MutationKind.ITER_CLOSE, iter_id="P31-I01", audit_id="AUD-01"),
    )

    assert extras == {"audit_id": "AUD-01", "require_audit_accepted": False}


def test_mutation_event_extras_raises_on_a_missing_audit_id() -> None:
    """Error path: an ``ITER_CLOSE`` with no ``audit_id`` param raises ``KeyError``.

    The mutator's caller maps this onto ``validation_failed``; swallowing it
    here would publish an event row that silently omits the audit binding.
    """
    with pytest.raises(KeyError):
        mutation_event_extras(_state(), _mutation(MutationKind.ITER_CLOSE, iter_id="P31-I01"))


# --- score_required_criteria ----------------------------------------------


def test_score_required_criteria_mints_nothing_for_a_wave_with_no_criteria(
    tmp_path: Path,
) -> None:
    """Boundary: an empty criteria list scores nothing and spawns nothing."""
    evidence = asyncio.run(
        score_required_criteria(
            _wave(success_criteria=[]),
            wave_id="P31-I01-W22",
            state=_state(),
            state_path=tmp_path / "state.json",
            events_path=tmp_path / "event.jsonl",
            repo_root=tmp_path,
            gate_specs=[],
            spawn_factory=None,
            block_authority=None,
            freshness_inputs={},
            tier="all",
            high_risk_single_auditor=False,
            close_attempt_id="",
            reusable_pass_gate_ids=None,
            before_gate_execute=None,
            on_gate_result=None,
            announce_auditing=lambda: None,
        )
    )

    assert evidence == []


# --- enforce_wave_verdict_gate --------------------------------------------


def test_enforce_wave_verdict_gate_returns_on_a_passing_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing verdict gate lets the close proceed."""
    wave = _wave()
    monkeypatch.setattr(
        "eawf.workflow.dispatch.verdict.verify_wave_verdict_gate",
        lambda _wave, *, state_path: WaveVerdictGate(
            wave_id=wave.id, requirement="skip", passed=True, verdict=None, reasons=()
        ),
    )

    assert enforce_wave_verdict_gate(wave, state_path=tmp_path / "state.json") is None


def test_enforce_wave_verdict_gate_raises_with_the_blocking_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocking gate raises and carries its reasons into the message.

    The reasons are the operator's only account of WHY the close was refused, so
    the message contract is load-bearing rather than cosmetic.
    """
    wave = _wave()
    monkeypatch.setattr(
        "eawf.workflow.dispatch.verdict.verify_wave_verdict_gate",
        lambda _wave, *, state_path: WaveVerdictGate(
            wave_id=wave.id,
            requirement="always",
            passed=False,
            verdict=None,
            reasons=("no fresh auditor verdict",),
        ),
    )

    with pytest.raises(LifecycleError, match="no fresh auditor verdict"):
        enforce_wave_verdict_gate(wave, state_path=tmp_path / "state.json")


def test_enforce_wave_verdict_gate_reports_no_reasons_when_none_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: an empty reason tuple still yields a readable refusal."""
    wave = _wave()
    monkeypatch.setattr(
        "eawf.workflow.dispatch.verdict.verify_wave_verdict_gate",
        lambda _wave, *, state_path: WaveVerdictGate(
            wave_id=wave.id, requirement="always", passed=False, verdict=None, reasons=()
        ),
    )

    with pytest.raises(LifecycleError, match="no reasons recorded"):
        enforce_wave_verdict_gate(wave, state_path=tmp_path / "state.json")


# --- CachedMutation --------------------------------------------------------


def test_cached_mutation_rejects_a_negative_timestamp() -> None:
    """Error path: ``cached_at`` is a monotonic reading, so it cannot be negative."""
    with pytest.raises(ValidationError):
        CachedMutation(result={}, cached_at=-1.0)


def test_cached_mutation_rejects_an_unknown_field() -> None:
    """Error path: the cache row forbids extras, so a typo cannot ride along."""
    with pytest.raises(ValidationError):
        CachedMutation.model_validate({"result": {}, "cached_at": 0.0, "ttl": 60})
