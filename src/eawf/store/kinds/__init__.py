"""Registry mapping StoreKind -> payload model class."""

from __future__ import annotations

from pydantic import BaseModel

from eawf.state.enums import StoreKind
from eawf.store.kinds.actual import ActualPayload
from eawf.store.kinds.agent_report import AgentReportPayload
from eawf.store.kinds.audit import AuditPayload
from eawf.store.kinds.decision import DecisionPayload
from eawf.store.kinds.estimate import EstimatePayload
from eawf.store.kinds.event import EventPayload
from eawf.store.kinds.flow import FlowPayload
from eawf.store.kinds.incident import IncidentPayload
from eawf.store.kinds.memory import MemoryPayload
from eawf.store.kinds.research import ResearchPayload

PAYLOAD_MODELS: dict[StoreKind, type[BaseModel]] = {
    StoreKind.RESEARCH: ResearchPayload,
    StoreKind.AUDIT: AuditPayload,
    StoreKind.INCIDENT: IncidentPayload,
    StoreKind.MEMORY: MemoryPayload,
    StoreKind.DECISION: DecisionPayload,
    StoreKind.ESTIMATE: EstimatePayload,
    StoreKind.ACTUAL: ActualPayload,
    StoreKind.FLOW: FlowPayload,
    StoreKind.EVENT: EventPayload,
    StoreKind.RESEARCHER_REPORT: AgentReportPayload,
    StoreKind.PLANNER_REPORT: AgentReportPayload,
    StoreKind.EXECUTOR_REPORT: AgentReportPayload,
    StoreKind.AUDITOR_REPORT: AgentReportPayload,
    StoreKind.REVIEWER_REPORT: AgentReportPayload,
    StoreKind.POLISHER_REPORT: AgentReportPayload,
    StoreKind.OPERATOR_REPORT: AgentReportPayload,
    StoreKind.DOMAIN_SPECIALIST_REPORT: AgentReportPayload,
}
