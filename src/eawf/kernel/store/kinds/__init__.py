"""Registry mapping StoreKind -> payload model class."""

from __future__ import annotations

from pydantic import BaseModel

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.kinds.actual import ActualPayload
from eawf.kernel.store.kinds.agent_report import AgentReportPayload
from eawf.kernel.store.kinds.audit import AuditPayload
from eawf.kernel.store.kinds.config_updated import ConfigUpdatedPayload
from eawf.kernel.store.kinds.decision import DecisionPayload
from eawf.kernel.store.kinds.estimate import EstimatePayload
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.kinds.flow import FlowPayload
from eawf.kernel.store.kinds.gate_receipt import GateReceipt
from eawf.kernel.store.kinds.incident import IncidentPayload
from eawf.kernel.store.kinds.jury_ballot import JuryBallotPayload
from eawf.kernel.store.kinds.memory import MemoryPayload
from eawf.kernel.store.kinds.operator_input import OperatorInputPayload
from eawf.kernel.store.kinds.registry_updated import RegistryUpdatedPayload
from eawf.kernel.store.kinds.research import ResearchPayload
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.kinds.research_round import ResearchRoundPayload
from eawf.kernel.store.kinds.spec_updated import SpecUpdatedPayload
from eawf.kernel.store.kinds.subscription_lag import SubscriptionLagPayload

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
    StoreKind.EVIDENCE: EvidenceRecord,
    StoreKind.RESEARCHER_REPORT: AgentReportPayload,
    StoreKind.PLANNER_REPORT: AgentReportPayload,
    StoreKind.EXECUTOR_REPORT: AgentReportPayload,
    StoreKind.AUDITOR_REPORT: AgentReportPayload,
    StoreKind.REVIEWER_REPORT: AgentReportPayload,
    StoreKind.POLISHER_REPORT: AgentReportPayload,
    StoreKind.OPERATOR_REPORT: AgentReportPayload,
    StoreKind.DOMAIN_SPECIALIST_REPORT: AgentReportPayload,
    StoreKind.SUBSCRIPTION_LAG: SubscriptionLagPayload,
    StoreKind.CONFIG_UPDATED: ConfigUpdatedPayload,
    StoreKind.REGISTRY_UPDATED: RegistryUpdatedPayload,
    StoreKind.SPEC_UPDATED: SpecUpdatedPayload,
    StoreKind.RESEARCH_CAMPAIGN: ResearchCampaignPayload,
    StoreKind.RESEARCH_ROUND: ResearchRoundPayload,
    StoreKind.OPERATOR_INPUT: OperatorInputPayload,
    StoreKind.JURY_BALLOT: JuryBallotPayload,
    StoreKind.GATE_RECEIPT: GateReceipt,
}
