"""Unit tests for :class:`eawf.kernel.store.kinds.incident.IncidentPayload`.

Covers the C09 V7 incident-cause taxonomy promotion: ``cause`` is a required
typed :class:`~eawf.kernel.state.enums.IncidentCause` member and the legacy
``root_cause`` free-string field is removed (clean break, ``extra="forbid"``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import IncidentCause, IncidentSeverity, StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds import PAYLOAD_MODELS
from eawf.kernel.store.kinds.incident import IncidentPayload

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "store" / "incident_sample.json"


def _valid_payload() -> dict[str, object]:
    return {
        "severity": IncidentSeverity.HIGH,
        "timeline": [{"at": "2026-01-01T00:00:00+00:00", "entry": "started"}],
        "cause": IncidentCause.RUNTIME_TIMEOUT,
        "corrective_action_ids": [],
    }


def test_incident_payload_valid_round_trip() -> None:
    payload = IncidentPayload.model_validate(_valid_payload())
    assert payload.severity == IncidentSeverity.HIGH
    assert payload.cause is IncidentCause.RUNTIME_TIMEOUT
    assert len(payload.timeline) == 1
    assert payload.corrective_action_ids == []


def test_incident_payload_missing_cause_raises() -> None:
    raw = _valid_payload()
    del raw["cause"]
    with pytest.raises(ValidationError, match="cause"):
        IncidentPayload.model_validate(raw)


def test_incident_payload_invalid_cause_value_raises() -> None:
    raw = _valid_payload()
    raw["cause"] = "not_a_real_cause"
    with pytest.raises(ValidationError):
        IncidentPayload.model_validate(raw)


def test_incident_payload_bad_severity_raises() -> None:
    with pytest.raises(ValidationError):
        IncidentPayload.model_validate(
            {"severity": "ultra", "timeline": [], "cause": IncidentCause.UNKNOWN}
        )


def test_incident_payload_root_cause_rejected_extra_forbid() -> None:
    raw = _valid_payload()
    raw["root_cause"] = "some prose"
    with pytest.raises(ValidationError):
        IncidentPayload.model_validate(raw)


def test_incident_payload_corrective_action_ids_default_empty() -> None:
    raw = _valid_payload()
    del raw["corrective_action_ids"]
    payload = IncidentPayload.model_validate(raw)
    assert payload.corrective_action_ids == []


def test_incident_cause_carries_legacy_and_unknown_sentinels() -> None:
    assert IncidentCause.LEGACY_FREE_TEXT == "legacy_free_text"
    assert IncidentCause.UNKNOWN == "unknown"


def test_incident_cause_taxonomy_membership() -> None:
    members = {c.value for c in IncidentCause}
    expected = {
        "runtime_rate_limit",
        "runtime_server_error",
        "runtime_timeout",
        "runtime_api_error",
        "runtime_auth_error",
        "runtime_unavailable",
        "runtime_oauth_cache_stripped",
        "daemon_wal_recovery",
        "daemon_socket_bind",
        "daemon_version_skew",
        "daemon_subprocess_oom",
        "daemon_subscription_dropped",
        "daemon_lock_timeout",
        "cache_mislayer",
        "cost_budget_breached",
        "session_handle_pruned",
        "session_failover",
        "worktree_cherry_pick_conflict",
        "worktree_branch_stale",
        "git_push_rejected",
        "plugin_drift",
        "spec_validation_failed",
        "state_validation_failed",
        "audit_failed",
        "operator_interrupt",
        "external_api_failure",
        "legacy_free_text",
        "unknown",
    }
    assert members == expected


def test_incident_sample_fixture_validates_through_envelope() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    env = Envelope.model_validate(raw)
    assert env.kind is StoreKind.INCIDENT
    payload = PAYLOAD_MODELS[env.kind].model_validate(env.payload)
    assert isinstance(payload, IncidentPayload)
    assert payload.cause is IncidentCause.CACHE_MISLAYER
    assert payload.corrective_action_ids == ["B-100"]
