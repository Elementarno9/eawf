from __future__ import annotations

import pytest

from eawf.kernel.state import enums

# --- Core 4 tests from spec ---


def test_phase_status_values() -> None:
    assert enums.PhaseStatus.PLANNED.value == "planned"
    assert enums.PhaseStatus.ACTIVE.value == "active"
    assert enums.PhaseStatus.CLOSED.value == "closed"
    assert enums.PhaseStatus.ARCHIVED.value == "archived"


def test_wave_status_values() -> None:
    expected = {"pending", "claimed", "in_progress", "closed", "failed", "abandoned"}
    actual = {member.value for member in enums.WaveStatus}
    assert actual == expected


def test_audit_verdict_membership() -> None:
    assert "pass" in {member.value for member in enums.AuditVerdict}
    assert "major" in {member.value for member in enums.AuditVerdict}
    with pytest.raises(ValueError):
        enums.AuditVerdict("not-a-verdict")
    with pytest.raises(ValueError):
        enums.AuditVerdict("fail")


def test_skill_envelope_status_values() -> None:
    expected = {"ok", "needs_user", "blocked", "failed", "partial"}
    actual = {member.value for member in enums.SkillEnvelopeStatus}
    assert actual == expected


# --- project ---


def test_project_status_values() -> None:
    expected = {"active", "archived", "retired"}
    actual = {m.value for m in enums.ProjectStatus}
    assert actual == expected


def test_project_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.ProjectStatus("unknown")


# --- track ---


def test_track_status_values() -> None:
    expected = {"active", "planned", "deferred", "retired"}
    actual = {m.value for m in enums.TrackStatus}
    assert actual == expected


def test_track_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.TrackStatus("bogus")


def test_track_kind_values() -> None:
    expected = {"strategy", "model", "target", "feature", "service"}
    actual = {m.value for m in enums.TrackKind}
    assert actual == expected


def test_track_kind_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.TrackKind("bogus")


# --- goal ---


def test_goal_status_values() -> None:
    expected = {"open", "achieved", "abandoned"}
    actual = {m.value for m in enums.GoalStatus}
    assert actual == expected


def test_goal_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.GoalStatus("nope")


# --- outcome.status ---


def test_outcome_status_values() -> None:
    expected = {"pending", "met", "missed", "waived"}
    actual = {m.value for m in enums.OutcomeStatus}
    assert actual == expected


def test_outcome_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.OutcomeStatus("bad")


# --- outcome.direction ---


def test_outcome_direction_values() -> None:
    expected = {"min", "max", "equal", "range"}
    actual = {m.value for m in enums.OutcomeDirection}
    assert actual == expected


def test_outcome_direction_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.OutcomeDirection("sideways")


# --- iter ---


def test_iter_status_values() -> None:
    expected = {"planned", "active", "closed", "abandoned"}
    actual = {m.value for m in enums.IterStatus}
    assert actual == expected


def test_iter_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.IterStatus("???")


# --- iter.trigger ---


def test_iter_trigger_values() -> None:
    expected = {"reactive", "proactive", "none"}
    actual = {m.value for m in enums.IterTrigger}
    assert actual == expected


def test_iter_trigger_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.IterTrigger("planned")


# --- hypothesis.status ---


def test_hypothesis_status_values() -> None:
    expected = {"pending", "confirmed", "rejected", "inconclusive", "deferred"}
    actual = {m.value for m in enums.HypothesisStatus}
    assert actual == expected


def test_hypothesis_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.HypothesisStatus("maybe")


# --- hypothesis.verdict ---


def test_hypothesis_verdict_values() -> None:
    expected = {"confirmed", "rejected", "inconclusive"}
    actual = {m.value for m in enums.HypothesisVerdict}
    assert actual == expected


def test_hypothesis_verdict_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.HypothesisVerdict("undecided")


# --- audit.kind ---


def test_audit_kind_values() -> None:
    expected = {"evaluation", "ship-gate", "incident", "review"}
    actual = {m.value for m in enums.AuditKind}
    assert actual == expected


def test_audit_kind_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.AuditKind("random")


# --- audit.status ---


def test_audit_status_values() -> None:
    expected = {"pending", "running", "complete", "failed"}
    actual = {m.value for m in enums.AuditStatus}
    assert actual == expected


def test_audit_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.AuditStatus("stalled")


# --- audit.verdict (additional coverage) ---


def test_audit_verdict_full_values() -> None:
    expected = {"pass", "minor", "major"}
    actual = {m.value for m in enums.AuditVerdict}
    assert actual == expected


# --- decision ---


def test_decision_status_values() -> None:
    expected = {"active", "obsolete", "superseded", "reversed"}
    actual = {m.value for m in enums.DecisionStatus}
    assert actual == expected


def test_decision_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.DecisionStatus("pending")


# --- campaign.status ---


def test_campaign_status_values() -> None:
    expected = {"active", "converged", "cancelled"}
    actual = {m.value for m in enums.CampaignStatus}
    assert actual == expected


def test_campaign_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.CampaignStatus("paused")


# --- backlog.priority ---


def test_backlog_priority_values() -> None:
    expected = {"P0", "P1", "P2", "P3"}
    actual = {m.value for m in enums.BacklogPriority}
    assert actual == expected


def test_backlog_priority_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.BacklogPriority("P9")


# --- backlog.status ---


def test_backlog_status_values() -> None:
    expected = {"open", "in_progress", "closed", "deferred"}
    actual = {m.value for m in enums.BacklogStatus}
    assert actual == expected


def test_backlog_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.BacklogStatus("abandoned")


# --- incident.severity ---


def test_incident_severity_values() -> None:
    expected = {"low", "medium", "high", "critical"}
    actual = {m.value for m in enums.IncidentSeverity}
    assert actual == expected


def test_incident_severity_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.IncidentSeverity("extreme")


# --- incident.status ---


def test_incident_status_values() -> None:
    expected = {"open", "mitigated", "resolved", "wont-fix"}
    actual = {m.value for m in enums.IncidentStatus}
    assert actual == expected


def test_incident_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.IncidentStatus("closed")


# --- flow ---


def test_flow_status_values() -> None:
    expected = {"pending", "in_progress", "paused", "blocked", "done", "abandoned", "superseded"}
    actual = {m.value for m in enums.FlowStatus}
    assert actual == expected


def test_flow_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.FlowStatus("idle")


# --- agent_session.role ---


def test_agent_session_role_values() -> None:
    expected = {
        "researcher",
        "planner",
        "executor",
        "auditor",
        "reviewer",
        "polisher",
        "operator",
        "domain-specialist",
    }
    actual = {m.value for m in enums.AgentSessionRole}
    assert actual == expected


def test_agent_session_role_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.AgentSessionRole("hacker")


# --- agent_session.status ---


def test_agent_session_status_values() -> None:
    expected = {"active", "checkpointed", "closed", "stale", "failed"}
    actual = {m.value for m in enums.AgentSessionStatus}
    assert actual == expected


def test_agent_session_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.AgentSessionStatus("running")


# --- worktree ---


def test_worktree_status_values() -> None:
    expected = {"active", "conflicted", "merged", "abandoned"}
    actual = {m.value for m in enums.WorktreeStatus}
    assert actual == expected


def test_worktree_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.WorktreeStatus("stale")


# --- mcp_server.risk ---


def test_mcp_risk_values() -> None:
    expected = {"read", "read-write", "admin"}
    actual = {m.value for m in enums.McpRisk}
    assert actual == expected


def test_mcp_risk_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.McpRisk("write")


# --- mcp_server.status ---


def test_mcp_status_values() -> None:
    expected = {"not_configured", "configured", "installed", "degraded", "disabled"}
    actual = {m.value for m in enums.McpStatus}
    assert actual == expected


def test_mcp_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.McpStatus("active")


# --- plugin_install ---


def test_plugin_install_status_values() -> None:
    expected = {"installed", "drifted", "conflicted", "disabled"}
    actual = {m.value for m in enums.PluginInstallStatus}
    assert actual == expected


def test_plugin_install_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.PluginInstallStatus("pending")


# --- memory_summary.status ---


def test_memory_status_values() -> None:
    expected = {"active", "stale", "superseded", "pruned"}
    actual = {m.value for m in enums.MemoryStatus}
    assert actual == expected


def test_memory_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.MemoryStatus("archived")


# --- memory_summary.confidence / estimate.confidence ---


def test_confidence_values() -> None:
    expected = {"high", "medium", "low"}
    actual = {m.value for m in enums.Confidence}
    assert actual == expected


def test_confidence_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.Confidence("certain")


# --- health ---


def test_health_values() -> None:
    expected = {"ok", "needs_setup", "degraded"}
    actual = {m.value for m in enums.Health}
    assert actual == expected


def test_health_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.Health("broken")


# --- scope ---


def test_scope_kind_values() -> None:
    expected = {"repo", "workspace"}
    actual = {m.value for m in enums.ScopeKind}
    assert actual == expected


def test_scope_kind_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.ScopeKind("global")


# --- actual ---


def test_actual_status_values() -> None:
    expected = {
        "planned",
        "active",
        "done",
        "interrupted",
        "blocked",
        "abandoned",
        "failed",
        "superseded",
    }
    actual = {m.value for m in enums.ActualStatus}
    assert actual == expected


def test_actual_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.ActualStatus("weird")


# --- store ---


def test_store_kind_values() -> None:
    expected = {
        "research",
        "audit",
        "incident",
        "estimate",
        "actual",
        "memory",
        "decision",
        "event",
        # P28-I01-W04 — verify-spine attestation store (daemon-owned append).
        "evidence",
        "flow",
        "researcher_report",
        "planner_report",
        "executor_report",
        "auditor_report",
        "reviewer_report",
        "polisher_report",
        "operator_report",
        "domain_specialist_report",
        "subscription_lag",
        # P24-W10 — config + registry updates fan out through the bus.
        "config_updated",
        "registry_updated",
        # P25-W03 — spec writer / cache lifecycle events.
        "spec_updated",
        "research_campaign",
        # P30-I18-W03/W04 — campaign run rounds + operator-channel inputs.
        "research_round",
        "operator_input",
        # P30-I23-W17 — persisted per-juror ballots (calibration substrate).
        "jury_ballot",
    }
    actual = {m.value for m in enums.StoreKind}
    assert actual == expected


def test_store_kind_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        enums.StoreKind("log")
