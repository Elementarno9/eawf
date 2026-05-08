"""Registry mapping StoreKind -> payload model class."""

from __future__ import annotations

from pydantic import BaseModel

from eawf.state.enums import StoreKind
from eawf.store.kinds.actual import ActualPayload
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
}
