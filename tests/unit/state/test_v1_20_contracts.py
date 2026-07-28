"""Schema-1.20 sparse operational contracts and migration."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.migrations import DEFAULT_REGISTRY, MigrationError, build_migration_chain
from eawf.kernel.migrations.v1_19_to_v1_20 import MigrationV119ToV120
from eawf.kernel.state.enums import (
    AuditRequirement,
    CloseAttemptStatus,
    CloseOperatorAction,
    DependencyStage,
    MeasurementQuality,
    MeasurementStatus,
    WaveIntegrationKind,
    WaveIntegrationStatus,
    WaveStatus,
)
from eawf.kernel.state.models import (
    AgentSession,
    CloseAttempt,
    SessionAttempt,
    State,
    Wave,
    WaveDependencyBarrier,
    WaveDependencyBinding,
    WaveIntegration,
    wave_dependency_key,
    wave_dependency_stages,
)

_ROOT = Path(__file__).resolve().parents[3]
_TS = "2026-07-28T12:00:00Z"
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40
_SHA_D = "d" * 40
_DIGEST = "sha256:" + "e" * 64


def _state_payload() -> dict[str, object]:
    payload = json.loads(
        (_ROOT / "tests/fixtures/states/valid/01-empty-repo.json").read_text(encoding="utf-8")
    )
    payload["schema_version"] = "1.20"
    return payload


def _dependency_waves_payload() -> dict[str, dict[str, object]]:
    upstream_id = "P01-I01-W01"
    downstream_id = "P01-I01-W02"
    return {
        upstream_id: Wave(
            id=upstream_id,
            iter_id="P01-I01",
            title="Upstream",
            status="pending",
            opened_at=_TS,
        ).model_dump(mode="json"),
        downstream_id: Wave(
            id=downstream_id,
            iter_id="P01-I01",
            title="Downstream",
            status="pending",
            deps=[upstream_id],
            opened_at=_TS,
        ).model_dump(mode="json"),
    }


def _integration_payload() -> dict[str, object]:
    return {
        "id": "WI-01",
        "wave_id": "P01-I01-W01",
        "generation": 1,
        "status": "integrated",
        "base_sha": _SHA_A,
        "candidate_sha": _SHA_B,
        "integrated_sha": _SHA_C,
        "tree_sha": _SHA_D,
        "diff_digest": _DIGEST,
        "spec_digest": _DIGEST,
        "created_at": _TS,
        "supersedes_id": None,
        "kind": "land",
    }


def _close_attempt_payload() -> dict[str, object]:
    return {
        "id": "CA-01",
        "wave_id": "P01-I01-W01",
        "outcome": "verified exact integrated revision",
        "tokens_consumed": None,
        "generation": 1,
        "supersedes_id": None,
        "status": "queued",
        "integration_id": "WI-01",
        "candidate_sha": _SHA_B,
        "integrated_sha": _SHA_C,
        "tree_sha": _SHA_D,
        "wave_revision_digest": _DIGEST,
        "spec_digest": _DIGEST,
        "criteria_digest": _DIGEST,
        "gate_manifest_digest": _DIGEST,
        "policy_digest": _DIGEST,
        "runner_environment_digest": _DIGEST,
        "dependency_binding_digest": _DIGEST,
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
        "requested_at": _TS,
        "started_at": None,
        "updated_at": _TS,
        "terminal_at": None,
        "idempotency_key": "close:P01-I01-W01:1",
        "apply_event_id": None,
    }


def test_wave_status_contract_is_unchanged() -> None:
    assert [status.value for status in WaveStatus] == [
        "pending",
        "claimed",
        "in_progress",
        "closed",
        "failed",
        "abandoned",
    ]


def test_wave_integration_is_strict_frozen_and_typed() -> None:
    integration = WaveIntegration.model_validate(_integration_payload())
    assert integration.status is WaveIntegrationStatus.INTEGRATED
    assert integration.kind is WaveIntegrationKind.LAND
    with pytest.raises(ValidationError, match="extra"):
        WaveIntegration.model_validate({**_integration_payload(), "extra": True})
    with pytest.raises(ValidationError, match="frozen"):
        integration.status = WaveIntegrationStatus.VERIFIED


def test_close_attempt_is_strict_frozen_and_separate_from_wave_status() -> None:
    attempt = CloseAttempt.model_validate(_close_attempt_payload())
    assert attempt.status is CloseAttemptStatus.QUEUED
    assert attempt.audit_requirement is AuditRequirement.REQUIRED
    with pytest.raises(ValidationError, match="extra"):
        CloseAttempt.model_validate({**_close_attempt_payload(), "extra": True})
    with pytest.raises(ValidationError, match="frozen"):
        attempt.status = CloseAttemptStatus.CHECKING


def test_exhausted_blocked_close_requires_typed_operator_actions() -> None:
    payload = {
        **_close_attempt_payload(),
        "status": "blocked",
        "repair_generation": 1,
        "repair_budget_remaining": 0,
        "terminal_at": _TS,
    }
    with pytest.raises(ValidationError, match="split/defer/abort"):
        CloseAttempt.model_validate(payload)
    attempt = CloseAttempt.model_validate(
        {
            **payload,
            "required_operator_actions": ["split", "defer", "abort"],
        }
    )
    assert attempt.required_operator_actions == [
        CloseOperatorAction.SPLIT,
        CloseOperatorAction.DEFER,
        CloseOperatorAction.ABORT,
    ]


def test_dependency_barrier_defaults_legacy_edge_to_closed_closed() -> None:
    assert wave_dependency_stages({}, wave_id="P01-I01-W02", dep_wave_id="P01-I01-W01") == (
        DependencyStage.CLOSED,
        DependencyStage.CLOSED,
    )


def test_adopted_integration_requires_persisted_reason() -> None:
    payload = {
        **_integration_payload(),
        "kind": "adopt",
    }
    with pytest.raises(ValidationError, match="requires a reason"):
        WaveIntegration.model_validate(payload)
    adopted = WaveIntegration.model_validate(
        {
            **payload,
            "reason": "explicitly attest an already-integrated revision",
        }
    )
    assert adopted.reason == "explicitly attest an already-integrated revision"


def test_dependency_barrier_and_binding_use_canonical_edge_key() -> None:
    barrier = WaveDependencyBarrier(
        wave_id="P01-I01-W02",
        dep_wave_id="P01-I01-W01",
        start_after="integrated",
        land_after="verified",
        reason="consume integrated code, then wait for proof",
    )
    binding = WaveDependencyBinding(
        wave_id="P01-I01-W02",
        dep_wave_id="P01-I01-W01",
        integration_id="WI-01",
        generation=1,
        integrated_sha=_SHA_C,
        tree_sha=_SHA_D,
        bound_at=_TS,
    )
    key = wave_dependency_key(barrier.wave_id, barrier.dep_wave_id)
    payload = _state_payload()
    payload["waves"] = _dependency_waves_payload()
    payload["wave_dependency_barriers"] = {key: barrier.model_dump(mode="json")}
    payload["wave_dependency_bindings"] = {key: binding.model_dump(mode="json")}
    state = State.model_validate(payload)
    assert wave_dependency_stages(
        state.wave_dependency_barriers,
        wave_id=barrier.wave_id,
        dep_wave_id=barrier.dep_wave_id,
    ) == (DependencyStage.INTEGRATED, DependencyStage.VERIFIED)


def test_dependency_barrier_rejects_self_edge_and_weaker_land_stage() -> None:
    with pytest.raises(ValidationError, match="cannot reference itself"):
        WaveDependencyBarrier(
            wave_id="P01-I01-W01",
            dep_wave_id="P01-I01-W01",
            start_after="closed",
            land_after="closed",
            reason="invalid self edge",
        )
    with pytest.raises(ValidationError, match="cannot be weaker"):
        WaveDependencyBarrier(
            wave_id="P01-I01-W02",
            dep_wave_id="P01-I01-W01",
            start_after="verified",
            land_after="integrated",
            reason="invalid weakening",
        )


def test_state_operational_maps_are_sparse_and_key_checked() -> None:
    state = State.model_validate(_state_payload())
    assert state.wave_integrations == {}
    assert state.close_attempts == {}
    assert state.wave_dependency_barriers == {}
    assert state.wave_dependency_bindings == {}

    payload = _state_payload()
    payload["wave_integrations"] = {"wrong": _integration_payload()}
    with pytest.raises(ValidationError, match="does not match id"):
        State.model_validate(payload)


@pytest.mark.parametrize(
    ("map_name", "record_kind"),
    [
        ("wave_dependency_barriers", "barrier"),
        ("wave_dependency_bindings", "binding"),
    ],
)
@pytest.mark.parametrize(
    ("orphan_kind", "message"),
    [
        ("wave", "missing wave"),
        ("dep", "missing dep wave"),
        ("edge", "not declared in wave deps"),
    ],
)
def test_state_rejects_orphan_dependency_metadata(
    map_name: str,
    record_kind: str,
    orphan_kind: str,
    message: str,
) -> None:
    payload = _state_payload()
    waves = _dependency_waves_payload()
    wave_id = "P01-I01-W02"
    dep_wave_id = "P01-I01-W01"
    if orphan_kind == "wave":
        wave_id = "P01-I01-W03"
    elif orphan_kind == "dep":
        dep_wave_id = "P01-I01-W03"
    else:
        downstream = dict(waves[wave_id])
        downstream["deps"] = []
        waves[wave_id] = downstream
    payload["waves"] = waves
    if record_kind == "barrier":
        record = WaveDependencyBarrier(
            wave_id=wave_id,
            dep_wave_id=dep_wave_id,
            start_after="integrated",
            land_after="verified",
            reason="dependency metadata must resolve to a declared graph edge",
        )
    else:
        record = WaveDependencyBinding(
            wave_id=wave_id,
            dep_wave_id=dep_wave_id,
            integration_id="WI-01",
            generation=1,
            integrated_sha=_SHA_C,
            tree_sha=_SHA_D,
            bound_at=_TS,
        )
    key = wave_dependency_key(wave_id, dep_wave_id)
    payload[map_name] = {key: record.model_dump(mode="json")}

    with pytest.raises(ValidationError, match=message):
        State.model_validate(payload)


def test_session_measurement_defaults_are_unavailable_not_zero() -> None:
    attempt = SessionAttempt(
        attempt=1,
        runtime="codex",
        session_id="agent-1",
        session_log_handle="urn:eawf:v1:session-log:codex:opaque",
        started_at=_TS,
    )
    assert attempt.measurement_quality is MeasurementQuality.UNAVAILABLE
    assert attempt.measurement_status is MeasurementStatus.USAGE_UNAVAILABLE
    assert attempt.measurement_reason is None
    assert attempt.input_tokens is None
    assert attempt.output_tokens is None


def test_agent_session_runtime_binding_defaults_none() -> None:
    session = AgentSession(
        id="SES-01",
        role="executor",
        runtime="codex",
        scope_id="P01-I01-W01",
        status="active",
        started_at=_TS,
    )
    assert session.runtime_session_id is None


def test_v1_19_to_v1_20_migration_matches_sparse_golden() -> None:
    source = {
        "schema_version": "1.19",
        "waves": {
            "P01-I01-W01": {
                "status": "closed",
                "sessions": {"1": {"input_tokens": 7}},
            }
        },
        "agent_sessions": {"SES-legacy": {"runtime": "codex"}},
    }
    migrated = MigrationV119ToV120().apply(source)
    golden = json.loads(
        (_ROOT / "tests/golden/migrations/v1_19_to_v1_20.json").read_text(encoding="utf-8")
    )
    assert migrated == golden
    assert source["schema_version"] == "1.19"


def test_v1_19_to_v1_20_migration_is_pure_and_idempotent() -> None:
    source = _state_payload()
    source["schema_version"] = "1.19"
    before = copy.deepcopy(source)
    once = MigrationV119ToV120().apply(source)
    assert source == before
    once["schema_version"] = "1.19"
    twice = MigrationV119ToV120().apply(once)
    once["schema_version"] = "1.20"
    assert twice == once


def test_v1_19_to_v1_20_migration_invents_no_wave_or_usage_facts() -> None:
    source = {
        "schema_version": "1.19",
        "waves": {
            "P01-I01-W01": {
                "status": "closed",
                "sessions": {"1": {"input_tokens": 7}},
            }
        },
    }
    migrated = MigrationV119ToV120().apply(source)
    assert migrated["waves"] == source["waves"]
    assert migrated["wave_integrations"] == {}
    assert migrated["close_attempts"] == {}
    assert migrated["wave_dependency_bindings"] == {}
    assert "gate_receipts" not in migrated
    assert "usage_receipts" not in migrated


def test_v1_20_downgrade_is_refused_without_backup_restore() -> None:
    with pytest.raises(MigrationError, match="no migration"):
        build_migration_chain(DEFAULT_REGISTRY, from_version="1.20", to_version="1.19")


def test_v1_19_to_v1_20_pre_post_guards_refuse_wrong_direction() -> None:
    step = MigrationV119ToV120()
    step.check_pre({"schema_version": "1.19"})
    step.check_post({"schema_version": "1.20"})
    with pytest.raises(ValidationError, match="schema_version"):
        step.check_pre({"schema_version": "1.20"})
