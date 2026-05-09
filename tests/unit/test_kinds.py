"""Tests for store.kinds payload models (2 per kind)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.state.enums import (
    AuditKind,
    AuditVerdict,
    Confidence,
    IncidentSeverity,
    StoreKind,
)
from eawf.store.envelope import Envelope
from eawf.store.kinds import PAYLOAD_MODELS
from eawf.store.kinds.actual import ActualPayload
from eawf.store.kinds.audit import AuditPayload
from eawf.store.kinds.decision import DecisionPayload
from eawf.store.kinds.estimate import EstimatePayload
from eawf.store.kinds.event import EventPayload
from eawf.store.kinds.flow import FlowPayload
from eawf.store.kinds.incident import IncidentPayload
from eawf.store.kinds.memory import MemoryPayload
from eawf.store.kinds.research import ResearchPayload


def _make_envelope(kind: StoreKind, payload: dict[str, object]) -> Envelope:
    return Envelope(
        id=f"env-{kind.value}",
        kind=kind,
        scope_id=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        summary="test envelope",
        payload=payload,
    )


# ---------------------------------------------------------------------------
# ResearchPayload
# ---------------------------------------------------------------------------


def test_research_valid_round_trip() -> None:
    raw = {"topic": "market structure", "findings": ["f1", "f2"], "sources": ["s1"]}
    env = _make_envelope(StoreKind.RESEARCH, raw)
    loaded = Envelope.model_validate_json(env.model_dump_json())
    payload = PAYLOAD_MODELS[StoreKind.RESEARCH].model_validate(loaded.payload)
    assert isinstance(payload, ResearchPayload)
    assert payload.topic == "market structure"


def test_research_invalid_payload() -> None:
    with pytest.raises(ValidationError):
        ResearchPayload.model_validate({"topic": 123, "findings": "not_a_list", "sources": []})


# ---------------------------------------------------------------------------
# AuditPayload
# ---------------------------------------------------------------------------


def test_audit_valid_round_trip() -> None:
    raw = {
        "audit_kind": AuditKind.EVALUATION,
        "verdict": AuditVerdict.PASS,
        "check_results": [{"name": "lint", "passed": True, "details": None}],
        "report_artifact_id": None,
    }
    env = _make_envelope(StoreKind.AUDIT, raw)
    loaded = Envelope.model_validate_json(env.model_dump_json())
    payload = AuditPayload.model_validate(loaded.payload)
    assert payload.verdict == AuditVerdict.PASS
    assert payload.check_results[0].name == "lint"


def test_audit_invalid_payload_missing_required() -> None:
    with pytest.raises(ValidationError):
        # audit_kind is required
        AuditPayload.model_validate({"check_results": []})


# ---------------------------------------------------------------------------
# IncidentPayload
# ---------------------------------------------------------------------------


def test_incident_valid_round_trip() -> None:
    raw = {
        "severity": IncidentSeverity.HIGH,
        "timeline": [{"at": "2026-01-01T00:00:00+00:00", "entry": "started"}],
        "root_cause": None,
        "corrective_action_ids": [],
    }
    env = _make_envelope(StoreKind.INCIDENT, raw)
    loaded = Envelope.model_validate_json(env.model_dump_json())
    payload = IncidentPayload.model_validate(loaded.payload)
    assert payload.severity == IncidentSeverity.HIGH
    assert len(payload.timeline) == 1


def test_incident_invalid_payload_bad_severity() -> None:
    with pytest.raises(ValidationError):
        IncidentPayload.model_validate({"severity": "ultra", "timeline": []})


# ---------------------------------------------------------------------------
# MemoryPayload
# ---------------------------------------------------------------------------


def test_memory_valid_round_trip() -> None:
    raw = {"body": "The sky is blue.", "confidence": Confidence.HIGH, "review_due": None}
    env = _make_envelope(StoreKind.MEMORY, raw)
    loaded = Envelope.model_validate_json(env.model_dump_json())
    payload = MemoryPayload.model_validate(loaded.payload)
    assert payload.confidence == Confidence.HIGH


def test_memory_invalid_payload_missing_body() -> None:
    with pytest.raises(ValidationError):
        MemoryPayload.model_validate({"confidence": Confidence.LOW})


# ---------------------------------------------------------------------------
# DecisionPayload
# ---------------------------------------------------------------------------


def test_decision_valid_round_trip() -> None:
    raw = {"summary": "Use pydantic v2", "rationale": "Better perf", "alternatives": ["attrs"]}
    env = _make_envelope(StoreKind.DECISION, raw)
    loaded = Envelope.model_validate_json(env.model_dump_json())
    payload = DecisionPayload.model_validate(loaded.payload)
    assert payload.alternatives == ["attrs"]


def test_decision_invalid_payload_missing_rationale() -> None:
    with pytest.raises(ValidationError):
        DecisionPayload.model_validate({"summary": "Something"})


# ---------------------------------------------------------------------------
# EstimatePayload
# ---------------------------------------------------------------------------


def test_estimate_valid_round_trip() -> None:
    raw = {
        "scope_type": "wave",
        "source": "agent",
        "grain": "wave",
        "expected_eu": 8.0,
        "pessimistic_eu": 16.0,
        "expected_minutes": 480.0,
        "pessimistic_minutes": 960.0,
        "display": "8h",
        "display_category": "hours",
        "reference_class": "feature-implementation",
        "confidence": Confidence.MEDIUM,
        "basis": ["velocity data"],
        "coefficients_profile": "standard",
    }
    env = _make_envelope(StoreKind.ESTIMATE, raw)
    loaded = Envelope.model_validate_json(env.model_dump_json())
    payload = EstimatePayload.model_validate(loaded.payload)
    assert payload.expected_eu == pytest.approx(8.0)
    assert payload.expected_minutes == pytest.approx(480.0)
    assert payload.display == "8h"


def test_estimate_invalid_payload_wrong_type() -> None:
    with pytest.raises(ValidationError):
        EstimatePayload.model_validate(
            {
                "scope_type": "wave",
                "source": "agent",
                "grain": "wave",
                "expected_eu": "not_a_float",
                "pessimistic_eu": 10.0,
                "expected_minutes": 480.0,
                "pessimistic_minutes": 600.0,
                "display": "8h",
                "display_category": "hours",
                "confidence": Confidence.LOW,
                "coefficients_profile": "default",
            }
        )


def test_estimate_reference_class_optional() -> None:
    payload = EstimatePayload.model_validate(
        {
            "scope_type": "wave",
            "source": "agent",
            "grain": "wave",
            "expected_eu": 1.0,
            "pessimistic_eu": 2.0,
            "expected_minutes": 60.0,
            "pessimistic_minutes": 120.0,
            "display": "1h",
            "display_category": "hours",
            "confidence": Confidence.HIGH,
            "coefficients_profile": "standard",
        }
    )
    assert payload.reference_class is None


# ---------------------------------------------------------------------------
# ActualPayload
# ---------------------------------------------------------------------------


def test_actual_valid_round_trip() -> None:
    raw = {
        "segments": [
            {
                "session_id": "S-001",
                "started_at": "2026-01-01T09:00:00+00:00",
                "ended_at": "2026-01-01T11:00:00+00:00",
                "eu": 2.0,
                "active_minutes": 100.0,
                "idle_excluded_minutes": 10.0,
                "external_wait_minutes": 5.0,
                "agent_runtime_minutes": 90.0,
                "status": "done",
            }
        ],
        "elapsed_eu": 2.0,
        "attention_eu": 1.8,
        "agent_runtime_eu": 1.5,
        "ratio_actual_over_estimate": 1.1,
        "inside_pessimistic": True,
        "calibration_eligible": True,
        "outcome": "shipped",
        "idle_policy": "none",
    }
    env = _make_envelope(StoreKind.ACTUAL, raw)
    loaded = Envelope.model_validate_json(env.model_dump_json())
    payload = ActualPayload.model_validate(loaded.payload)
    assert payload.elapsed_eu == pytest.approx(2.0)
    assert payload.segments[0].eu == pytest.approx(2.0)
    assert payload.segments[0].session_id == "S-001"
    assert payload.segments[0].status == "done"
    assert payload.calibration_eligible is True
    assert payload.inside_pessimistic is True


def test_actual_invalid_payload_missing_outcome() -> None:
    with pytest.raises(ValidationError):
        ActualPayload.model_validate({"segments": [], "elapsed_eu": 0.0, "idle_policy": "none"})


def test_actual_segment_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        ActualPayload.model_validate(
            {
                "segments": [
                    {
                        "session_id": "S-001",
                        "started_at": "2026-01-01T09:00:00+00:00",
                        "ended_at": "2026-01-01T11:00:00+00:00",
                        "eu": 2.0,
                        "active_minutes": 100.0,
                        "idle_excluded_minutes": 0.0,
                        "external_wait_minutes": 0.0,
                        "agent_runtime_minutes": 100.0,
                        "status": "weird",
                    }
                ],
                "elapsed_eu": 2.0,
                "outcome": "shipped",
                "idle_policy": "none",
            }
        )


def test_actual_optional_fields_default() -> None:
    payload = ActualPayload.model_validate(
        {
            "segments": [],
            "elapsed_eu": 0.0,
            "outcome": "in-flight",
            "idle_policy": "none",
        }
    )
    assert payload.attention_eu is None
    assert payload.agent_runtime_eu is None
    assert payload.ratio_actual_over_estimate is None
    assert payload.inside_pessimistic is None
    assert payload.calibration_eligible is False


# ---------------------------------------------------------------------------
# FlowPayload
# ---------------------------------------------------------------------------


def test_flow_valid_round_trip() -> None:
    raw = {
        "kind": "flow_record",
        "flow_id": "FL-0123456789ab",
        "goal": "ship wave 05",
        "policy": {"mode": "auto"},
        "last_safe_checkpoint": None,
        "next_action": None,
    }
    env = _make_envelope(StoreKind.FLOW, raw)
    loaded = Envelope.model_validate_json(env.model_dump_json())
    payload = FlowPayload.model_validate(loaded.payload)
    assert payload.goal == "ship wave 05"
    assert payload.flow_id == "FL-0123456789ab"
    assert payload.kind == "flow_record"


def test_flow_invalid_payload_missing_goal() -> None:
    with pytest.raises(ValidationError):
        FlowPayload.model_validate({"flow_id": "FL-0123456789ab", "policy": {}})


def test_flow_invalid_payload_bad_flow_id_pattern() -> None:
    with pytest.raises(ValidationError):
        FlowPayload.model_validate(
            {
                "flow_id": "FL-not-hex",
                "goal": "x",
                "policy": {},
            }
        )


# ---------------------------------------------------------------------------
# EventPayload
# ---------------------------------------------------------------------------


def test_event_valid_round_trip() -> None:
    raw = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "event_type": "commit",
        "actor": "agent-01",
        "command": "git commit",
        "args_hash": "abc123",
        "before_state_version": None,
        "after_state_version": "v2",
        "status": "ok",
        "message": "all good",
    }
    env = _make_envelope(StoreKind.EVENT, raw)
    loaded = Envelope.model_validate_json(env.model_dump_json())
    payload = EventPayload.model_validate(loaded.payload)
    assert payload.event_type == "commit"
    assert payload.timestamp == datetime(2026, 1, 1, tzinfo=UTC)


def test_event_invalid_payload_missing_actor() -> None:
    with pytest.raises(ValidationError):
        EventPayload.model_validate(
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "event_type": "commit",
                "command": "git",
                "args_hash": "abc",
                "status": "ok",
                "message": "x",
            }
        )


def test_event_invalid_payload_missing_timestamp() -> None:
    with pytest.raises(ValidationError):
        EventPayload.model_validate(
            {
                "event_type": "commit",
                "actor": "agent-01",
                "command": "git",
                "args_hash": "abc",
                "status": "ok",
                "message": "x",
            }
        )


# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------


def test_registry_covers_all_store_kinds() -> None:
    for kind in StoreKind:
        assert kind in PAYLOAD_MODELS, f"StoreKind.{kind} missing from PAYLOAD_MODELS"
