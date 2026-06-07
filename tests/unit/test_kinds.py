"""Tests for store.kinds payload models (2 per kind)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    AuditKind,
    AuditVerdict,
    Confidence,
    IncidentCause,
    IncidentSeverity,
    StoreKind,
)
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds import PAYLOAD_MODELS
from eawf.kernel.store.kinds.actual import ActualPayload
from eawf.kernel.store.kinds.agent_report import (
    AgentReportPayload,
    ExecutorReportBody,
    report_store_urn,
    role_for_store_kind,
    store_kind_for_role,
)
from eawf.kernel.store.kinds.audit import AuditPayload
from eawf.kernel.store.kinds.decision import DecisionPayload
from eawf.kernel.store.kinds.estimate import EstimatePayload
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.kinds.flow import FlowPayload
from eawf.kernel.store.kinds.incident import IncidentPayload
from eawf.kernel.store.kinds.memory import MemoryPayload
from eawf.kernel.store.kinds.research import ResearchPayload
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload


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
    assert payload.references[0].ref == "s1"
    assert payload.sources == ["s1"]


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
        "cause": IncidentCause.RUNTIME_TIMEOUT,
        "corrective_action_ids": [],
    }
    env = _make_envelope(StoreKind.INCIDENT, raw)
    loaded = Envelope.model_validate_json(env.model_dump_json())
    payload = IncidentPayload.model_validate(loaded.payload)
    assert payload.severity == IncidentSeverity.HIGH
    assert payload.cause is IncidentCause.RUNTIME_TIMEOUT
    assert len(payload.timeline) == 1


def test_incident_invalid_payload_bad_severity() -> None:
    with pytest.raises(ValidationError):
        IncidentPayload.model_validate(
            {"severity": "ultra", "timeline": [], "cause": IncidentCause.UNKNOWN}
        )


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


def test_event_actor_principal_id_optional_default_none() -> None:
    """C01-IMPL W02 placeholder: actor_principal_id defaults to None.

    Per c01-foundations §5.3.19 + Q3 2026-05-18 the field is optional so
    every existing row stays valid without backfill; v0.5+ governance owns
    the populated path.
    """
    payload = EventPayload.model_validate(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "event_type": "commit",
            "actor": "cli",
            "command": "git",
            "args_hash": "abc",
            "status": "ok",
            "message": "x",
        }
    )
    assert payload.actor_principal_id is None


def test_event_actor_principal_id_accepts_string() -> None:
    payload = EventPayload.model_validate(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "event_type": "commit",
            "actor": "cli",
            "actor_principal_id": "u-abc12345",
            "command": "git",
            "args_hash": "abc",
            "status": "ok",
            "message": "x",
        }
    )
    assert payload.actor_principal_id == "u-abc12345"


# ---------------------------------------------------------------------------
# AgentReportPayload
# ---------------------------------------------------------------------------


def test_agent_report_valid_round_trip() -> None:
    raw = {
        "header": {
            "report_id": "AR-executor-P18-I01-W01-01",
            "role": "executor",
            "session_id": "SES-001",
            "scope_id": "P18-I01-W01",
            "base_id": "P18-I01-W01",
            "attempt": 1,
            "runtime": "codex",
            "generated_at": "2026-05-14T00:00:00+00:00",
            "summary": "executor report",
        },
        "body": {
            "role": "executor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "implemented",
            "wave_id": "P18-I01-W01",
            "outcome": "done",
        },
    }
    env = _make_envelope(StoreKind.EXECUTOR_REPORT, raw)
    loaded = Envelope.model_validate_json(env.model_dump_json())
    payload = PAYLOAD_MODELS[StoreKind.EXECUTOR_REPORT].model_validate(loaded.payload)
    assert isinstance(payload, AgentReportPayload)
    assert isinstance(payload.body, ExecutorReportBody)
    assert payload.header.role is AgentSessionRole.EXECUTOR
    assert payload.body.verdict is AgentReportVerdict.PASS


def test_agent_report_invalid_role_mismatch() -> None:
    with pytest.raises(ValidationError, match="does not match header role"):
        AgentReportPayload.model_validate(
            {
                "header": {
                    "report_id": "AR-reviewer-P18-I01-W01-01",
                    "role": "reviewer",
                    "session_id": "SES-001",
                    "scope_id": "P18-I01-W01",
                    "base_id": "P18-I01-W01",
                    "attempt": 1,
                    "runtime": "codex",
                    "generated_at": "2026-05-14T00:00:00+00:00",
                    "summary": "reviewer report",
                },
                "body": {
                    "role": "executor",
                    "verdict": "pass",
                    "confidence": "high",
                    "summary": "implemented",
                    "wave_id": "P18-I01-W01",
                    "outcome": "done",
                },
            }
        )


def test_agent_report_store_kind_helpers() -> None:
    assert store_kind_for_role(AgentSessionRole.EXECUTOR) is StoreKind.EXECUTOR_REPORT
    assert role_for_store_kind(StoreKind.EXECUTOR_REPORT) is AgentSessionRole.EXECUTOR
    assert (
        report_store_urn(
            scope_id="P18",
            role=AgentSessionRole.EXECUTOR,
            report_id="AR-executor-P18-I01-W01-01",
        )
        == "urn:eawf:v1:store:P18/executor_report/AR-executor-P18-I01-W01-01"
    )
    with pytest.raises(ValueError, match="not an agent report kind"):
        role_for_store_kind(StoreKind.EVENT)


# ---------------------------------------------------------------------------
# ResearchCampaignPayload
# ---------------------------------------------------------------------------


def _campaign_raw(domains: dict[str, object]) -> dict[str, object]:
    return {
        "campaign_id": "RC-001",
        "config": {"default_depth": "medium", "domains": domains},
        "campaign": {
            "topic": "market structure",
            "spawned": False,
            "dispatches": [
                {
                    "domain": domain,
                    "agent_role": "researcher",
                    "depth": "medium",
                    "prompt": "market structure",
                    "read_only": True,
                }
                for domain in sorted(domains)
            ],
        },
    }


def test_research_campaign_valid_round_trip() -> None:
    raw = _campaign_raw({"a": {}, "b": {"depth": "deep"}})
    env = _make_envelope(StoreKind.RESEARCH_CAMPAIGN, raw)
    loaded = Envelope.model_validate_json(env.model_dump_json())
    payload = PAYLOAD_MODELS[StoreKind.RESEARCH_CAMPAIGN].model_validate(loaded.payload)
    assert isinstance(payload, ResearchCampaignPayload)
    assert payload.campaign.spawned is False
    assert payload.campaign_id == "RC-001"
    assert {d.domain for d in payload.campaign.dispatches} == {"a", "b"}


def test_research_campaign_invalid_missing_campaign_id() -> None:
    raw = _campaign_raw({"a": {}})
    del raw["campaign_id"]
    with pytest.raises(ValidationError):
        ResearchCampaignPayload.model_validate(raw)


def test_research_campaign_rejects_spawned_true() -> None:
    raw = _campaign_raw({"a": {}})
    raw["campaign"]["spawned"] = True  # type: ignore[index]
    with pytest.raises(ValidationError):
        ResearchCampaignPayload.model_validate(raw)


def test_research_campaign_rejects_over_max_dispatches() -> None:
    from eawf.kernel.spec.research_campaign import MAX_STAGED_DISPATCHES

    too_many = {f"d{i:03d}": {} for i in range(MAX_STAGED_DISPATCHES + 1)}
    with pytest.raises(ValidationError, match="exceeds max"):
        ResearchCampaignPayload.model_validate(_campaign_raw(too_many))


def test_research_campaign_defaults_to_active_status() -> None:
    """A campaign with no status field defaults to ACTIVE with no tombstone."""
    from eawf.kernel.state.enums import CampaignStatus

    payload = ResearchCampaignPayload.model_validate(_campaign_raw({"a": {}}))
    assert payload.status is CampaignStatus.ACTIVE
    assert payload.tombstone is None


def test_research_campaign_cancelled_round_trip() -> None:
    """A cancelled campaign carries its tombstone (cancel time + reason)."""
    from eawf.kernel.state.enums import CampaignStatus

    raw = _campaign_raw({"a": {}})
    raw["status"] = "cancelled"
    raw["tombstone"] = {
        "cancelled_at": "2026-06-07T00:00:00+00:00",
        "reason": "superseded by RC-002",
    }
    payload = ResearchCampaignPayload.model_validate(raw)
    assert payload.status is CampaignStatus.CANCELLED
    assert payload.tombstone is not None
    assert payload.tombstone.cancelled_at == datetime(2026, 6, 7, tzinfo=UTC)
    assert payload.tombstone.reason == "superseded by RC-002"


def test_research_campaign_cancelled_without_tombstone_rejected() -> None:
    """A cancelled campaign with no tombstone violates the status invariant."""
    raw = _campaign_raw({"a": {}})
    raw["status"] = "cancelled"
    with pytest.raises(ValidationError, match="requires a tombstone"):
        ResearchCampaignPayload.model_validate(raw)


def test_research_campaign_active_with_tombstone_rejected() -> None:
    """An active campaign carrying a tombstone violates the status invariant."""
    raw = _campaign_raw({"a": {}})
    raw["tombstone"] = {"cancelled_at": "2026-06-07T00:00:00+00:00", "reason": None}
    with pytest.raises(ValidationError, match="must not carry a tombstone"):
        ResearchCampaignPayload.model_validate(raw)


def test_research_campaign_rejects_unknown_status() -> None:
    """An out-of-vocabulary status is rejected by the closed enum."""
    raw = _campaign_raw({"a": {}})
    raw["status"] = "paused"
    with pytest.raises(ValidationError):
        ResearchCampaignPayload.model_validate(raw)


def test_campaign_tombstone_rejects_over_long_reason() -> None:
    """A cancel reason past the 280-char bound is rejected."""
    from eawf.kernel.store.kinds.research_campaign import CampaignTombstone

    with pytest.raises(ValidationError):
        CampaignTombstone.model_validate(
            {"cancelled_at": "2026-06-07T00:00:00+00:00", "reason": "x" * 281}
        )


# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------


def test_registry_covers_all_store_kinds() -> None:
    for kind in StoreKind:
        assert kind in PAYLOAD_MODELS, f"StoreKind.{kind} missing from PAYLOAD_MODELS"
