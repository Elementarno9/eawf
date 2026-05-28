"""Trust scorecard metrics for estimation calibration and provenance."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import StoreKind, WaveStatus
from eawf.kernel.state.models import Audit, Decision, Iter, Phase, State, Wave
from eawf.kernel.state.urn import Urn
from eawf.kernel.state.urn import build as build_urn
from eawf.kernel.state.urn import parse as parse_urn
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds import PAYLOAD_MODELS
from eawf.kernel.store.kinds.actual import ActualPayload
from eawf.kernel.store.kinds.audit import AuditPayload
from eawf.kernel.store.kinds.estimate import EstimatePayload
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.workflow.estimation.buckets import calibrate_buckets

SCORECARD_SCHEMA_VERSION: Literal[1] = 1
TrustTier = Literal["verified", "attested", "deferred_outcome", "unavailable"]
WindowKind = Literal["all", "30d", "waves"]
ReliabilityStatus = Literal["computed", "deferred_v0.4.1"]
WhyUrnKind = Literal["phase", "iter", "wave", "decision", "audit"]
_WHY_URN_KINDS: frozenset[str] = frozenset({"phase", "iter", "wave", "decision", "audit"})
_STORE_KINDS: tuple[StoreKind, ...] = (
    StoreKind.ESTIMATE,
    StoreKind.ACTUAL,
    StoreKind.AUDIT,
    StoreKind.EVIDENCE,
)


class EuCalibrationMetric(BaseModel):
    """EU calibration row for the trust scorecard."""

    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(ge=0)
    nudged_bucket_count: int = Field(ge=0)
    max_drift_pct: float | None
    bucket_drift: bool
    drift_badge: Literal["ok", "bucket-drift", "no-data"]


class TrustWindow(BaseModel):
    """Window applied to trust scorecard store projections."""

    model_config = ConfigDict(extra="forbid")

    kind: WindowKind
    wave_count: int | None = Field(default=None, ge=1)

    @classmethod
    def parse(cls, raw: str) -> TrustWindow:
        """Parse ``all``, ``30d``, or ``N-waves`` into a typed window."""
        if raw == "all":
            return cls(kind="all")
        if raw == "30d":
            return cls(kind="30d")
        if raw.endswith("-waves"):
            count_raw = raw.removesuffix("-waves")
            try:
                count = int(count_raw)
            except ValueError as exc:
                raise ValueError(f"invalid scorecard window: {raw!r}") from exc
            if count < 1:
                raise ValueError(f"invalid scorecard window: {raw!r}")
            return cls(kind="waves", wave_count=count)
        raise ValueError(f"invalid scorecard window: {raw!r}")

    def label(self) -> str:
        """Return operator-facing window label."""
        if self.kind == "waves":
            return f"{self.wave_count}-waves"
        return self.kind


class TypedStoreEnvelope(BaseModel):
    """Strictly validated append-only store row plus typed payload."""

    model_config = ConfigDict(extra="forbid")

    envelope: Envelope
    payload: EstimatePayload | ActualPayload | AuditPayload | EvidenceRecord


class StoreProjection(BaseModel):
    """Read-only projection over append-only stores."""

    model_config = ConfigDict(extra="forbid")

    estimates: list[TypedStoreEnvelope] = Field(default_factory=list)
    actuals: list[TypedStoreEnvelope] = Field(default_factory=list)
    audits: list[TypedStoreEnvelope] = Field(default_factory=list)
    evidence: list[TypedStoreEnvelope] = Field(default_factory=list)


class OutputTrustLabel(BaseModel):
    """Trust tier for one scorecard output scope."""

    model_config = ConfigDict(extra="forbid")

    urn: str
    scope_id: str
    tier: TrustTier
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str


class TrustTierCounts(BaseModel):
    """Counts by scorecard trust tier."""

    model_config = ConfigDict(extra="forbid")

    verified: int = 0
    attested: int = 0
    deferred_outcome: int = 0
    unavailable: int = 0


class VerifierReliabilityMetric(BaseModel):
    """Verifier reliability projection."""

    model_config = ConfigDict(extra="forbid")

    status: ReliabilityStatus
    sample_count: int = Field(ge=0)
    pass_rate: float | None = None
    note: str


class TrustScorecard(BaseModel):
    """Top-level trust scorecard payload."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCORECARD_SCHEMA_VERSION
    window: str = "all"
    eu_calibration: EuCalibrationMetric
    store_record_counts: dict[str, int] = Field(default_factory=dict)
    output_labels: list[OutputTrustLabel] = Field(default_factory=list)
    tier_counts: TrustTierCounts = Field(default_factory=TrustTierCounts)
    verifier_reliability: VerifierReliabilityMetric = Field(
        default_factory=lambda: VerifierReliabilityMetric(
            status="deferred_v0.4.1",
            sample_count=0,
            pass_rate=None,
            note="verifier reliability needs outcome-linked verifier rows",
        )
    )


class WhyReference(BaseModel):
    """One supporting row in an ``eawf why`` response."""

    model_config = ConfigDict(extra="forbid")

    urn: str
    kind: str
    tier: TrustTier
    summary: str


class WhyResult(BaseModel):
    """Typed ``eawf why`` payload."""

    model_config = ConfigDict(extra="forbid")

    urn: str
    kind: WhyUrnKind
    id: str
    title: str | None = None
    tier: TrustTier
    summary: str
    refs: list[WhyReference] = Field(default_factory=list)


def read_store_projection(state_path: Path) -> StoreProjection:
    """Read append-only stores without mutation and validate typed payloads."""
    buckets: dict[StoreKind, list[TypedStoreEnvelope]] = {kind: [] for kind in _STORE_KINDS}
    for kind in _STORE_KINDS:
        path = store_path(state_path, kind)
        if not path.exists():
            continue
        model = PAYLOAD_MODELS[kind]
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            envelope = Envelope.model_validate(orjson.loads(line))
            if envelope.kind != kind:
                raise ValueError(f"store row kind mismatch: expected {kind.value!r}")
            payload = model.model_validate(envelope.payload)
            if not isinstance(
                payload,
                EstimatePayload | ActualPayload | AuditPayload | EvidenceRecord,
            ):
                raise TypeError(f"unsupported scorecard payload: {type(payload)!r}")
            buckets[kind].append(TypedStoreEnvelope(envelope=envelope, payload=payload))
    return StoreProjection(
        estimates=buckets[StoreKind.ESTIMATE],
        actuals=buckets[StoreKind.ACTUAL],
        audits=buckets[StoreKind.AUDIT],
        evidence=buckets[StoreKind.EVIDENCE],
    )


def _project_code(state: State) -> str:
    """Return stable URN owner for scorecard URNs."""
    if state.project is not None:
        return state.project.code
    if state.current.project_code is not None:
        return state.current.project_code
    parsed = parse_urn(state.urn)
    return parsed.owner


def _wave_urn(state: State, wave_id: str) -> str:
    return build_urn("wave", owner=_project_code(state), id=wave_id)


def _entity_urn(state: State, kind: str, entity_id: str) -> str:
    return build_urn(kind, owner=_project_code(state), id=entity_id)


def _closed_waves_for_window(state: State, window: TrustWindow, *, now: datetime) -> set[str]:
    closed = [
        wave
        for wave in state.waves.values()
        if wave.status == WaveStatus.CLOSED and wave.closed_at is not None
    ]
    if window.kind == "30d":
        cutoff = now - timedelta(days=30)
        return {wave.id for wave in closed if wave.closed_at >= cutoff}
    if window.kind == "waves":
        earliest = datetime.min.replace(tzinfo=UTC)
        ordered = sorted(closed, key=lambda wave: wave.closed_at or earliest)
        return {wave.id for wave in ordered[-(window.wave_count or 1) :]}
    return {wave.id for wave in closed}


def _envelope_in_window(
    row: TypedStoreEnvelope,
    *,
    window: TrustWindow,
    now: datetime,
    wave_ids: set[str],
) -> bool:
    if window.kind == "all":
        return True
    if window.kind == "30d":
        return row.envelope.created_at >= now - timedelta(days=30)
    scope_id = row.envelope.scope_id
    if scope_id in wave_ids:
        return True
    if isinstance(row.payload, EvidenceRecord):
        return row.payload.scope_id in wave_ids
    return False


def _window_projection(
    projection: StoreProjection,
    *,
    window: TrustWindow,
    now: datetime,
    wave_ids: set[str],
) -> StoreProjection:
    return StoreProjection(
        estimates=[
            row
            for row in projection.estimates
            if _envelope_in_window(row, window=window, now=now, wave_ids=wave_ids)
        ],
        actuals=[
            row
            for row in projection.actuals
            if _envelope_in_window(row, window=window, now=now, wave_ids=wave_ids)
        ],
        audits=[
            row
            for row in projection.audits
            if _envelope_in_window(row, window=window, now=now, wave_ids=wave_ids)
        ],
        evidence=[
            row
            for row in projection.evidence
            if _envelope_in_window(row, window=window, now=now, wave_ids=wave_ids)
        ],
    )


def _evidence_for_scope(
    projection: StoreProjection,
    scope_id: str,
) -> list[EvidenceRecord]:
    rows: list[EvidenceRecord] = []
    for row in projection.evidence:
        if isinstance(row.payload, EvidenceRecord) and row.payload.scope_id == scope_id:
            rows.append(row.payload)
    return rows


def _actual_scopes(state: State, projection: StoreProjection) -> set[str]:
    store_scopes = {
        row.envelope.scope_id
        for row in projection.actuals
        if row.envelope.scope_id is not None and isinstance(row.payload, ActualPayload)
    }
    state_scopes = {
        actual.scope_id for actual in (state.actuals or {}).values() if actual.scope_id is not None
    }
    return store_scopes | state_scopes


def _tier_from_evidence(evidence: Iterable[EvidenceRecord]) -> tuple[TrustTier | None, list[str]]:
    refs: list[str] = []
    has_attestation = False
    for record in evidence:
        refs.append(record.id)
        if record.evidence_kind == "deterministic" and record.status == "pass":
            return "verified", refs
        if record.evidence_kind in {"attested", "jury"} and record.status in {"pass", "waived"}:
            has_attestation = True
    if has_attestation:
        return "attested", refs
    return None, refs


def _label_wave(state: State, wave: Wave, projection: StoreProjection) -> OutputTrustLabel:
    evidence = _evidence_for_scope(projection, wave.id)
    tier, refs = _tier_from_evidence(evidence)
    if tier is not None:
        reason = f"{tier} by evidence record"
        return OutputTrustLabel(
            urn=_wave_urn(state, wave.id),
            scope_id=wave.id,
            tier=tier,
            evidence_refs=refs,
            reason=reason,
        )
    if wave.status != WaveStatus.CLOSED or wave.id not in _actual_scopes(state, projection):
        return OutputTrustLabel(
            urn=_wave_urn(state, wave.id),
            scope_id=wave.id,
            tier="deferred_outcome",
            evidence_refs=refs,
            reason="outcome or actual store row not available yet",
        )
    return OutputTrustLabel(
        urn=_wave_urn(state, wave.id),
        scope_id=wave.id,
        tier="unavailable",
        evidence_refs=refs,
        reason="no verifier or attestation evidence",
    )


def _tier_counts(labels: Iterable[OutputTrustLabel]) -> TrustTierCounts:
    counts = TrustTierCounts()
    for label in labels:
        current = getattr(counts, label.tier)
        setattr(counts, label.tier, current + 1)
    return counts


def _compute_verifier_reliability(projection: StoreProjection) -> VerifierReliabilityMetric:
    deterministic = [
        row.payload
        for row in projection.evidence
        if isinstance(row.payload, EvidenceRecord) and row.payload.evidence_kind == "deterministic"
    ]
    if not deterministic:
        return VerifierReliabilityMetric(
            status="deferred_v0.4.1",
            sample_count=0,
            pass_rate=None,
            note="no deterministic verifier evidence in window",
        )
    passed = sum(1 for record in deterministic if record.status == "pass")
    return VerifierReliabilityMetric(
        status="computed",
        sample_count=len(deterministic),
        pass_rate=passed / len(deterministic),
        note="pass-rate over deterministic evidence rows; outcome correlation deferred to v0.4.1",
    )


def compute_eu_calibration_metric(
    state: State,
    *,
    now: datetime | None = None,
) -> EuCalibrationMetric:
    """Return the bucket-drift verdict from ``calibrate_buckets``."""
    report = calibrate_buckets(state, now=now)
    populated = [row for row in report.buckets if row.sample_count > 0]
    nudged = [row for row in populated if row.nudge]
    max_drift = max((row.drift_pct or 0.0 for row in populated), default=None)
    bucket_drift = bool(nudged)
    if bucket_drift:
        badge: Literal["ok", "bucket-drift", "no-data"] = "bucket-drift"
    elif populated:
        badge = "ok"
    else:
        badge = "no-data"
    return EuCalibrationMetric(
        sample_count=sum(row.sample_count for row in populated),
        nudged_bucket_count=len(nudged),
        max_drift_pct=max_drift,
        bucket_drift=bucket_drift,
        drift_badge=badge,
    )


def compute_trust_scorecard(
    state: State,
    *,
    store_projection: StoreProjection | None = None,
    state_path: Path | None = None,
    window: TrustWindow | str = "all",
    now: datetime | None = None,
) -> TrustScorecard:
    """Compute the estimation trust scorecard from state plus append-only stores."""
    anchor = now or datetime.now(UTC)
    parsed_window = TrustWindow.parse(window) if isinstance(window, str) else window
    projection = store_projection
    if projection is None and state_path is not None:
        projection = read_store_projection(state_path)
    if projection is None:
        projection = StoreProjection()
    wave_ids = _closed_waves_for_window(state, parsed_window, now=anchor)
    scoped_projection = _window_projection(
        projection,
        window=parsed_window,
        now=anchor,
        wave_ids=wave_ids,
    )
    labels = [
        _label_wave(state, wave, scoped_projection)
        for wave_id, wave in sorted(state.waves.items())
        if parsed_window.kind == "all" or wave_id in wave_ids
    ]
    return TrustScorecard(
        schema_version=SCORECARD_SCHEMA_VERSION,
        window=parsed_window.label(),
        eu_calibration=compute_eu_calibration_metric(state, now=now),
        store_record_counts={
            StoreKind.ESTIMATE.value: len(scoped_projection.estimates),
            StoreKind.ACTUAL.value: len(scoped_projection.actuals),
            StoreKind.AUDIT.value: len(scoped_projection.audits),
            StoreKind.EVIDENCE.value: len(scoped_projection.evidence),
        },
        output_labels=labels,
        tier_counts=_tier_counts(labels),
        verifier_reliability=_compute_verifier_reliability(scoped_projection),
    )


def _target_id(parsed: Urn) -> str:
    if parsed.id:
        return parsed.id
    return parsed.owner


def _aggregate_child_tier(labels: Iterable[OutputTrustLabel]) -> TrustTier:
    tiers = {label.tier for label in labels}
    if not tiers:
        return "unavailable"
    if "deferred_outcome" in tiers:
        return "deferred_outcome"
    if "unavailable" in tiers:
        return "unavailable"
    if "attested" in tiers:
        return "attested"
    return "verified"


def _label_for_scope(state: State, scope_id: str, projection: StoreProjection) -> TrustTier:
    evidence = _evidence_for_scope(projection, scope_id)
    tier, _refs = _tier_from_evidence(evidence)
    if tier is not None:
        return tier
    wave = state.waves.get(scope_id)
    if wave is not None:
        return _label_wave(state, wave, projection).tier
    return "unavailable"


def _record_refs_for_scope(
    state: State,
    scope_id: str,
    projection: StoreProjection,
) -> list[WhyReference]:
    refs: list[WhyReference] = []
    for record in _evidence_for_scope(projection, scope_id):
        tier: TrustTier = (
            "verified"
            if record.evidence_kind == "deterministic" and record.status == "pass"
            else "attested"
        )
        refs.append(
            WhyReference(
                urn=_entity_urn(state, "store", record.id),
                kind="evidence",
                tier=tier,
                summary=record.summary,
            )
        )
    for audit in (state.audits or {}).values():
        if audit.scope_id == scope_id:
            refs.append(
                WhyReference(
                    urn=_entity_urn(state, "audit", audit.id),
                    kind="audit",
                    tier=_label_for_scope(state, audit.id, projection),
                    summary=f"{audit.kind.value} {audit.status.value}",
                )
            )
    return refs


def _phase_result(state: State, phase: Phase, urn: str, projection: StoreProjection) -> WhyResult:
    wave_ids = [
        wave_id
        for iter_id in phase.iter_ids
        for wave_id in state.iters.get(iter_id, Iter.model_construct(wave_ids=[])).wave_ids
    ]
    labels = [
        _label_wave(state, state.waves[wave_id], projection)
        for wave_id in wave_ids
        if wave_id in state.waves
    ]
    refs = [
        WhyReference(
            urn=_entity_urn(state, "iter", iter_id),
            kind="iter",
            tier=_aggregate_child_tier(
                _label_wave(state, state.waves[wave_id], projection)
                for wave_id in state.iters.get(iter_id, Iter.model_construct(wave_ids=[])).wave_ids
                if wave_id in state.waves
            ),
            summary=state.iters[iter_id].title if iter_id in state.iters else iter_id,
        )
        for iter_id in phase.iter_ids
    ]
    return WhyResult(
        urn=urn,
        kind="phase",
        id=phase.id,
        title=phase.title,
        tier=_aggregate_child_tier(labels),
        summary=f"phase {phase.status.value} with {len(wave_ids)} waves",
        refs=refs + _record_refs_for_scope(state, phase.id, projection),
    )


def _iter_result(state: State, it: Iter, urn: str, projection: StoreProjection) -> WhyResult:
    labels = [
        _label_wave(state, state.waves[wave_id], projection)
        for wave_id in it.wave_ids
        if wave_id in state.waves
    ]
    refs = [
        WhyReference(
            urn=_wave_urn(state, label.scope_id),
            kind="wave",
            tier=label.tier,
            summary=state.waves[label.scope_id].title,
        )
        for label in labels
    ]
    return WhyResult(
        urn=urn,
        kind="iter",
        id=it.id,
        title=it.title,
        tier=_aggregate_child_tier(labels),
        summary=f"iter {it.status.value} under {it.phase_id}",
        refs=refs + _record_refs_for_scope(state, it.id, projection),
    )


def _wave_result(state: State, wave: Wave, urn: str, projection: StoreProjection) -> WhyResult:
    label = _label_wave(state, wave, projection)
    refs = _record_refs_for_scope(state, wave.id, projection)
    for row in projection.estimates:
        if row.envelope.scope_id == wave.id:
            refs.append(
                WhyReference(
                    urn=_entity_urn(state, "store", row.envelope.id),
                    kind="estimate",
                    tier="attested",
                    summary=row.envelope.summary,
                )
            )
    for row in projection.actuals:
        if row.envelope.scope_id == wave.id:
            refs.append(
                WhyReference(
                    urn=_entity_urn(state, "store", row.envelope.id),
                    kind="actual",
                    tier="verified",
                    summary=row.envelope.summary,
                )
            )
    return WhyResult(
        urn=urn,
        kind="wave",
        id=wave.id,
        title=wave.title,
        tier=label.tier,
        summary=label.reason,
        refs=refs,
    )


def _decision_result(
    state: State,
    decision: Decision,
    urn: str,
    projection: StoreProjection,
) -> WhyResult:
    refs = _record_refs_for_scope(state, decision.id, projection)
    decision_urn = _entity_urn(state, "decision", decision.id)
    for row in projection.evidence:
        if isinstance(row.payload, EvidenceRecord) and decision_urn in row.payload.refs:
            refs.append(
                WhyReference(
                    urn=_entity_urn(state, "store", row.payload.id),
                    kind="evidence",
                    tier=_label_for_scope(state, row.payload.scope_id, projection),
                    summary=row.payload.summary,
                )
            )
    return WhyResult(
        urn=urn,
        kind="decision",
        id=decision.id,
        title=decision.title,
        tier=_label_for_scope(state, decision.id, projection),
        summary=f"decision {decision.status.value}: {decision.rationale}",
        refs=refs,
    )


def _audit_result(state: State, audit: Audit, urn: str, projection: StoreProjection) -> WhyResult:
    refs = _record_refs_for_scope(state, audit.id, projection)
    for artifact_id in [audit.report_artifact_id] if audit.report_artifact_id else []:
        artifact = state.artifacts.get(artifact_id)
        artifact_urn = (
            artifact.urn if artifact is not None else _entity_urn(state, "artifact", artifact_id)
        )
        refs.append(
            WhyReference(
                urn=artifact_urn,
                kind="artifact",
                tier="attested",
                summary=artifact.uri if artifact is not None else artifact_id,
            )
        )
    verdict = audit.verdict.value if audit.verdict else "none"
    return WhyResult(
        urn=urn,
        kind="audit",
        id=audit.id,
        title=audit.kind.value,
        tier=_label_for_scope(state, audit.id, projection),
        summary=f"audit {audit.status.value} verdict={verdict}",
        refs=refs,
    )


def assemble_why(
    state: State,
    urn: str,
    *,
    store_projection: StoreProjection | None = None,
    state_path: Path | None = None,
) -> WhyResult:
    """Assemble a provenance explanation for supported eawf URN kinds."""
    parsed = parse_urn(urn)
    if parsed.kind not in _WHY_URN_KINDS:
        raise ValueError(f"unsupported why URN kind: {parsed.kind!r}")
    projection = store_projection
    if projection is None and state_path is not None:
        projection = read_store_projection(state_path)
    if projection is None:
        projection = StoreProjection()
    entity_id = _target_id(parsed)
    if parsed.kind == "phase" and entity_id in state.phases:
        return _phase_result(state, state.phases[entity_id], urn, projection)
    if parsed.kind == "iter" and entity_id in state.iters:
        return _iter_result(state, state.iters[entity_id], urn, projection)
    if parsed.kind == "wave" and entity_id in state.waves:
        return _wave_result(state, state.waves[entity_id], urn, projection)
    if parsed.kind == "decision" and entity_id in state.decisions:
        return _decision_result(state, state.decisions[entity_id], urn, projection)
    if parsed.kind == "audit" and entity_id in (state.audits or {}):
        return _audit_result(state, (state.audits or {})[entity_id], urn, projection)
    raise KeyError(f"URN target not found: {urn!r}")


__all__ = [
    "SCORECARD_SCHEMA_VERSION",
    "EuCalibrationMetric",
    "OutputTrustLabel",
    "StoreProjection",
    "TrustScorecard",
    "TrustTierCounts",
    "TrustWindow",
    "TypedStoreEnvelope",
    "VerifierReliabilityMetric",
    "WhyReference",
    "WhyResult",
    "assemble_why",
    "compute_eu_calibration_metric",
    "compute_trust_scorecard",
    "read_store_projection",
]
