"""Trust scorecard metrics for estimation provenance.

The scorecard is advisory: its one consumer is ``eawf why``, which renders
it beside the provenance of a phase, iter or wave. No dispatch, admission,
integration or acceptance gate reads it, and none may until a calibration
threshold is ratified against a labelled dataset.

Every rate it states is a :class:`ScorecardValue` that names its input
sample and measurement quality, and a rate over an empty sample is
unavailable, never zero. The scorecard also declares its
:class:`~eawf.observability.measurement.coverage.Coverage`: how many waves
it labelled, how many had evidence, and whether the window was a bounded
sample.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

import orjson
from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.enums import QualityLadder, StoreKind, WaveStatus
from eawf.kernel.state.ids import (
    RE_HYPOTHESIS,
    RE_HYPOTHESIS_SCOPED,
    RE_ITER,
    RE_PHASE,
    RE_WAVE,
)
from eawf.kernel.state.models import Audit, Decision, Hypothesis, Iter, Phase, State, Wave
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
from eawf.observability.measurement.coverage import Coverage

SCORECARD_SCHEMA_VERSION: Literal[2] = 2

#: The one surface that reads the scorecard. It renders; it never gates.
SCORECARD_CONSUMER: Literal["eawf why"] = "eawf why"
TrustTier = Literal["verified", "attested", "deferred_outcome", "unavailable"]
WindowKind = Literal["all", "30d", "waves"]
ReliabilityStatus = Literal["computed", "deferred_v0.4.1"]
WhyUrnKind = Literal["phase", "iter", "wave", "hypothesis", "decision", "audit"]
_WHY_URN_KINDS: frozenset[str] = frozenset(
    {"phase", "iter", "wave", "hypothesis", "decision", "audit"}
)
_STORE_KINDS: tuple[StoreKind, ...] = (
    StoreKind.ESTIMATE,
    StoreKind.ACTUAL,
    StoreKind.AUDIT,
    StoreKind.EVIDENCE,
)


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


class ScorecardValue(BaseModel):
    """One scorecard rate, with the sample it was taken over and its quality.

    Attributes:
        value: The rate, or ``None`` when it is undefined.
        sample_size: How many rows the rate was taken over.
        sample: What those rows are, in words.
        measurement_quality: How far the rate can be trusted;
            ``unavailable`` exactly when it is undefined.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float | None = Field(default=None, ge=0.0, le=1.0)
    sample_size: int = Field(ge=0)
    sample: str = Field(min_length=1)
    measurement_quality: QualityLadder

    @model_validator(mode="after")
    def _undefined_is_never_zero(self) -> Self:
        """Tie an undefined rate to an empty sample and unavailable quality.

        Raises:
            ValueError: A rate is stated over no rows, a sample yields no
                rate, or the quality disagrees with whether there is one.
        """
        if (self.value is None) != (self.sample_size == 0):
            raise ValueError("a rate is undefined exactly when its sample is empty")
        if (self.value is None) != (self.measurement_quality == "unavailable"):
            raise ValueError("an undefined rate is unavailable, and only an undefined one")
        return self


def _rate(part: int, sample_size: int, *, sample: str) -> ScorecardValue:
    """Return ``part / sample_size`` as a measured value, unavailable over nothing."""
    if not sample_size:
        return ScorecardValue(sample_size=0, sample=sample, measurement_quality="unavailable")
    return ScorecardValue(
        value=part / sample_size,
        sample_size=sample_size,
        sample=sample,
        measurement_quality="measured",
    )


class VerifierReliabilityMetric(BaseModel):
    """Verifier reliability projection."""

    model_config = ConfigDict(extra="forbid")

    status: ReliabilityStatus
    pass_rate: ScorecardValue
    note: str


class TrustScorecard(BaseModel):
    """Top-level trust scorecard payload.

    Attributes:
        advisory: Always ``True``: nothing gates on the scorecard.
        consumer: The one surface that reads it.
        verified_share: The share of closed labelled waves whose tier is
            ``verified``.
        coverage: The labelled waves, those with evidence, and whether the
            window is a bounded sample.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = SCORECARD_SCHEMA_VERSION
    advisory: Literal[True] = True
    consumer: Literal["eawf why"] = SCORECARD_CONSUMER
    window: str = "all"
    store_record_counts: dict[str, int] = Field(default_factory=dict)
    output_labels: list[OutputTrustLabel] = Field(default_factory=list)
    tier_counts: TrustTierCounts = Field(default_factory=TrustTierCounts)
    verified_share: ScorecardValue
    verifier_reliability: VerifierReliabilityMetric
    coverage: Coverage


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
    scorecard: TrustScorecard | None = None


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
        recent: set[str] = set()
        for wave in closed:
            closed_at = wave.closed_at
            if closed_at is not None and closed_at >= cutoff:
                recent.add(wave.id)
        return recent
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
    # A wave that has not closed yet is genuinely "outcome pending" -- the
    # actual store row may still arrive. A CLOSED wave with no evidence is
    # terminal: its outcome will not improve, so it is `unavailable`, NOT
    # `deferred_outcome`. The old condition collapsed both into
    # `deferred_outcome`, which mislabelled every pre-P28 closed wave (those
    # predate ActualSummary auto-creation and can never gain an actual) as
    # "store row not available yet" -- implying data was still coming.
    if wave.status != WaveStatus.CLOSED:
        return OutputTrustLabel(
            urn=_wave_urn(state, wave.id),
            scope_id=wave.id,
            tier="deferred_outcome",
            evidence_refs=refs,
            reason="wave not yet closed",
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
    passed = sum(1 for record in deterministic if record.status == "pass")
    pass_rate = _rate(passed, len(deterministic), sample="deterministic evidence rows in window")
    if not deterministic:
        return VerifierReliabilityMetric(
            status="deferred_v0.4.1",
            pass_rate=pass_rate,
            note="no deterministic verifier evidence in window",
        )
    return VerifierReliabilityMetric(
        status="computed",
        pass_rate=pass_rate,
        note="pass-rate over deterministic evidence rows; outcome correlation deferred to v0.4.1",
    )


def _verified_share(labels: Iterable[OutputTrustLabel], *, state: State) -> ScorecardValue:
    """Return the verified share of the labelled waves that have closed.

    An open wave can already carry passing evidence, but its outcome is not in,
    so it stays out of both sides of the share.
    """
    settled = [
        label
        for label in labels
        if label.scope_id in state.waves and state.waves[label.scope_id].status == WaveStatus.CLOSED
    ]
    verified = sum(1 for label in settled if label.tier == "verified")
    return _rate(verified, len(settled), sample="closed waves labelled in window")


def _coverage(labels: list[OutputTrustLabel], *, window: TrustWindow) -> Coverage:
    """Return what the labels covered: every wave, those with evidence, the window."""
    return Coverage(
        subjects_total=len(labels),
        subjects_contributing=sum(1 for label in labels if label.evidence_refs),
        sampled=window.kind != "all",
    )


def compute_trust_scorecard(
    state: State,
    *,
    store_projection: StoreProjection | None = None,
    state_path: Path | None = None,
    window: TrustWindow | str = "all",
    now: datetime | None = None,
    scope_wave_ids: frozenset[str] | None = None,
) -> TrustScorecard:
    """Compute the estimation trust scorecard from state plus append-only stores.

    Args:
        state: The state whose waves are labelled.
        store_projection: The append-only stores, already read.
        state_path: Where to read the stores from when no projection is given.
        window: The window the stores and waves are restricted to.
        now: The instant a time window is anchored at.
        scope_wave_ids: The waves of the entity ``eawf why`` explains, or
            ``None`` for every wave.

    Returns:
        The scorecard.
    """
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
        if (parsed_window.kind == "all" or wave_id in wave_ids)
        and (scope_wave_ids is None or wave_id in scope_wave_ids)
    ]
    return TrustScorecard(
        schema_version=SCORECARD_SCHEMA_VERSION,
        window=parsed_window.label(),
        store_record_counts={
            StoreKind.ESTIMATE.value: len(scoped_projection.estimates),
            StoreKind.ACTUAL.value: len(scoped_projection.actuals),
            StoreKind.AUDIT.value: len(scoped_projection.audits),
            StoreKind.EVIDENCE.value: len(scoped_projection.evidence),
        },
        output_labels=labels,
        tier_counts=_tier_counts(labels),
        verified_share=_verified_share(labels, state=state),
        verifier_reliability=_compute_verifier_reliability(scoped_projection),
        coverage=_coverage(labels, window=parsed_window),
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


def _hypothesis_result(
    state: State,
    hypothesis: Hypothesis,
    urn: str,
    projection: StoreProjection,
) -> WhyResult:
    refs = _record_refs_for_scope(state, hypothesis.id, projection)
    if hypothesis.audit_id and (state.audits or {}).get(hypothesis.audit_id) is not None:
        audit = (state.audits or {})[hypothesis.audit_id]
        verdict = audit.verdict.value if audit.verdict else "none"
        refs.append(
            WhyReference(
                urn=_entity_urn(state, "audit", audit.id),
                kind="audit",
                tier=_label_for_scope(state, audit.id, projection),
                summary=f"{audit.kind.value} {audit.status.value} verdict={verdict}",
            )
        )
    if hypothesis.source_artifact_id:
        artifact = state.artifacts.get(hypothesis.source_artifact_id)
        artifact_urn = (
            artifact.urn
            if artifact is not None
            else _entity_urn(state, "artifact", hypothesis.source_artifact_id)
        )
        refs.append(
            WhyReference(
                urn=artifact_urn,
                kind="artifact",
                tier="attested",
                summary=artifact.uri if artifact is not None else hypothesis.source_artifact_id,
            )
        )
    verdict = hypothesis.verdict.value if hypothesis.verdict else "none"
    return WhyResult(
        urn=urn,
        kind="hypothesis",
        id=hypothesis.id,
        title=hypothesis.title,
        tier=_label_for_scope(state, hypothesis.id, projection),
        summary=f"hypothesis {hypothesis.status.value} verdict={verdict}",
        refs=refs,
    )


def _phase_wave_ids(state: State, phase: Phase) -> frozenset[str]:
    """Return every wave id under *phase*'s iters."""
    return frozenset(
        wave_id
        for iter_id in phase.iter_ids
        if iter_id in state.iters
        for wave_id in state.iters[iter_id].wave_ids
    )


def _with_scorecard(
    state: State, result: WhyResult, projection: StoreProjection, wave_ids: frozenset[str]
) -> WhyResult:
    """Attach the advisory scorecard over *wave_ids* to a why result."""
    scorecard = compute_trust_scorecard(state, store_projection=projection, scope_wave_ids=wave_ids)
    return result.model_copy(update={"scorecard": scorecard})


def _kind_from_bare_id(entity_id: str) -> str | None:
    """Map a bare entity id to its why URN kind by id-shape, or ``None``.

    The id grammars are mutually exclusive (a wave id never matches the
    hypothesis pattern), so the first match is unambiguous. Returns ``None``
    when the id matches no recognised shape, letting the caller raise a
    malformed-input error.
    """
    if RE_WAVE.fullmatch(entity_id):
        return "wave"
    if RE_ITER.fullmatch(entity_id):
        return "iter"
    if RE_PHASE.fullmatch(entity_id):
        return "phase"
    if RE_HYPOTHESIS.fullmatch(entity_id) or RE_HYPOTHESIS_SCOPED.fullmatch(entity_id):
        return "hypothesis"
    return None


def _resolve_why_target(state: State, raw: str) -> tuple[str, str, str]:
    """Resolve ``raw`` into a ``(kind, entity_id, urn)`` triple.

    A full ``urn:eawf:v1:*`` string keeps its parsed kind; a bare id is
    routed by id-shape (``H<NN>-<NN>`` -> hypothesis, ``P##-I##-W##`` ->
    wave, and so on) and the canonical URN is rebuilt for the response.

    Raises:
        ValueError: ``raw`` is neither a parseable URN of a supported why
            kind nor a bare id of a recognised lifecycle / hypothesis shape.
    """
    if raw.startswith("urn:"):
        parsed = parse_urn(raw)
        if parsed.kind not in _WHY_URN_KINDS:
            raise ValueError(f"unsupported why URN kind: {parsed.kind!r}")
        return parsed.kind, _target_id(parsed), raw
    kind = _kind_from_bare_id(raw)
    if kind is None:
        raise ValueError(f"unrecognised why target: {raw!r}")
    return kind, raw, _entity_urn(state, kind, raw)


def assemble_why(
    state: State,
    urn: str,
    *,
    store_projection: StoreProjection | None = None,
    state_path: Path | None = None,
) -> WhyResult:
    """Assemble a provenance explanation for a supported eawf URN or bare id.

    Accepts either a full ``urn:eawf:v1:<kind>:<owner>/<id>`` string or a
    bare lifecycle / hypothesis id (``P01``, ``P01-I01``, ``P01-I01-W01``,
    ``H03-12``); the bare id is routed to its kind by id-shape.

    Raises:
        ValueError: The input is malformed or names an unsupported why kind.
        KeyError: The resolved kind + id has no matching state record.
    """
    kind, entity_id, resolved_urn = _resolve_why_target(state, urn)
    projection = store_projection
    if projection is None and state_path is not None:
        projection = read_store_projection(state_path)
    if projection is None:
        projection = StoreProjection()
    if kind == "phase" and entity_id in state.phases:
        phase = state.phases[entity_id]
        result = _phase_result(state, phase, resolved_urn, projection)
        return _with_scorecard(state, result, projection, _phase_wave_ids(state, phase))
    if kind == "iter" and entity_id in state.iters:
        it = state.iters[entity_id]
        result = _iter_result(state, it, resolved_urn, projection)
        return _with_scorecard(state, result, projection, frozenset(it.wave_ids))
    if kind == "wave" and entity_id in state.waves:
        result = _wave_result(state, state.waves[entity_id], resolved_urn, projection)
        return _with_scorecard(state, result, projection, frozenset({entity_id}))
    if kind == "hypothesis" and entity_id in (state.hypotheses or {}):
        return _hypothesis_result(
            state, (state.hypotheses or {})[entity_id], resolved_urn, projection
        )
    if kind == "decision" and entity_id in state.decisions:
        return _decision_result(state, state.decisions[entity_id], resolved_urn, projection)
    if kind == "audit" and entity_id in (state.audits or {}):
        return _audit_result(state, (state.audits or {})[entity_id], resolved_urn, projection)
    raise KeyError(f"why target not found: {resolved_urn!r}")


__all__ = [
    "SCORECARD_CONSUMER",
    "SCORECARD_SCHEMA_VERSION",
    "OutputTrustLabel",
    "ScorecardValue",
    "StoreProjection",
    "TrustScorecard",
    "TrustTierCounts",
    "TrustWindow",
    "TypedStoreEnvelope",
    "VerifierReliabilityMetric",
    "WhyReference",
    "WhyResult",
    "assemble_why",
    "compute_trust_scorecard",
    "read_store_projection",
]
