"""Trust scorecard and why-surface tests for P28-I03-W13."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.state.enums import (
    ActualStatus,
    AuditKind,
    AuditStatus,
    AuditVerdict,
    Confidence,
    DecisionStatus,
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    ActualSummary,
    Audit,
    CurrentPointers,
    Decision,
    EstimateSummary,
    Iter,
    Phase,
    Project,
    State,
    Wave,
)
from eawf.kernel.state.urn import build as build_urn
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.actual import ActualPayload
from eawf.kernel.store.kinds.audit import AuditPayload
from eawf.kernel.store.kinds.estimate import EstimatePayload
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli.app import app
from eawf.workflow.estimation.trust_scorecard import (
    TrustWindow,
    assemble_why,
    compute_trust_scorecard,
    read_store_projection,
)

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


def _empty_state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:TR",
            "updated_at": _T0.isoformat(),
            "project": Project(
                code="TR",
                slug="tr",
                title="TR",
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:TR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="TR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _wave(
    wave_id: str,
    *,
    status: WaveStatus = WaveStatus.CLOSED,
    closed_at: datetime | None = _T0,
) -> Wave:
    return Wave(
        id=wave_id,
        iter_id="-".join(wave_id.split("-")[:2]),
        title=f"wave {wave_id}",
        status=status,
        deps=[],
        blocks=[],
        file_scopes=[],
        success_criteria=[],
        opened_at=_T0 - timedelta(days=2),
        closed_at=closed_at,
    )


def _state_with_entities() -> State:
    state = _empty_state()
    state.phases["P01"] = Phase(
        id="P01",
        scope_id="P01",
        title="Trust phase",
        status=PhaseStatus.CLOSED,
        iter_ids=["P01-I01"],
        opened_at=_T0 - timedelta(days=3),
        closed_at=_T0,
    )
    state.iters["P01-I01"] = Iter(
        id="P01-I01",
        phase_id="P01",
        title="Trust iter",
        status=IterStatus.CLOSED,
        wave_ids=["P01-I01-W01", "P01-I01-W02", "P01-I01-W03"],
        opened_at=_T0 - timedelta(days=2),
        closed_at=_T0,
    )
    state.waves["P01-I01-W01"] = _wave("P01-I01-W01", closed_at=_T0 - timedelta(days=1))
    state.waves["P01-I01-W02"] = _wave("P01-I01-W02", closed_at=_T0)
    state.waves["P01-I01-W03"] = _wave(
        "P01-I01-W03",
        status=WaveStatus.IN_PROGRESS,
        closed_at=None,
    )
    state.estimates = {
        "P01-I01-W01": EstimateSummary(
            id="EST-P01-I01-W01",
            scope_id="P01-I01-W01",
            expected_eu=1.0,
            pessimistic_eu=2.0,
            expected_minutes=30.0,
            pessimistic_minutes=60.0,
            display="1 EU",
            confidence=Confidence.MEDIUM,
            current_store_record_id="REC-EST-1",
            updated_at=_T0,
        )
    }
    state.actuals = {
        "P01-I01-W01": ActualSummary(
            id="ACT-P01-I01-W01",
            scope_id="P01-I01-W01",
            status=ActualStatus.DONE,
            elapsed_eu=1.2,
            current_store_record_id="REC-ACT-1",
            updated_at=_T0,
        )
    }
    state.decisions["D-01"] = Decision(
        id="D-01",
        scope_id="P01",
        title="Use trust labels",
        rationale="labels keep provenance scan-friendly",
        status=DecisionStatus.ACTIVE,
        created_at=_T0,
    )
    state.audits = {
        "AUD-01": Audit(
            id="AUD-01",
            scope_id="P01-I01-W01",
            kind=AuditKind.REVIEW,
            status=AuditStatus.COMPLETE,
            verdict=AuditVerdict.PASS,
            created_at=_T0,
        )
    }
    return state


def _envelope(
    *,
    record_id: str,
    kind: StoreKind,
    scope_id: str,
    payload: dict[str, Any],
    created_at: datetime = _T0,
) -> Envelope:
    return Envelope(
        id=record_id,
        kind=kind,
        scope_id=scope_id,
        created_at=created_at,
        updated_at=created_at if kind != StoreKind.EVENT else None,
        summary=f"{kind.value} {scope_id}",
        payload=payload,
    )


def _append(path: Path, envelope: Envelope) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        fh.write(envelope.model_dump_json().encode("utf-8") + b"\n")


def _write_repo(tmp_path: Path, state: State) -> Path:
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir()
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


def _estimate_payload() -> EstimatePayload:
    return EstimatePayload(
        scope_type="wave",
        source="prep",
        grain="wave",
        expected_eu=1.0,
        pessimistic_eu=2.0,
        expected_minutes=30.0,
        pessimistic_minutes=60.0,
        display="1 EU",
        display_category="bucket",
        confidence=Confidence.MEDIUM,
        coefficients_profile="test",
    )


def _actual_payload() -> ActualPayload:
    return ActualPayload(
        segments=[],
        elapsed_eu=1.2,
        calibration_eligible=True,
        outcome="done",
        idle_policy="excluded",
    )


def _evidence(
    record_id: str,
    scope_id: str,
    *,
    evidence_kind: str,
    status: str = "pass",
    refs: list[str] | None = None,
    created_at: datetime = _T0,
) -> EvidenceRecord:
    return EvidenceRecord(
        id=record_id,
        scope_id=scope_id,
        produced_by="tool" if evidence_kind == "deterministic" else "human",
        evidence_kind=evidence_kind,
        status=status,
        summary=f"{evidence_kind} evidence for {scope_id}",
        refs=refs or [],
        created_at=created_at,
    )


def _seed_stores(state_path: Path) -> None:
    old = _T0 - timedelta(days=40)
    _append(
        store_path(state_path, StoreKind.ESTIMATE),
        _envelope(
            record_id="REC-EST-1",
            kind=StoreKind.ESTIMATE,
            scope_id="P01-I01-W01",
            payload=_estimate_payload().model_dump(mode="json"),
        ),
    )
    _append(
        store_path(state_path, StoreKind.ACTUAL),
        _envelope(
            record_id="REC-ACT-1",
            kind=StoreKind.ACTUAL,
            scope_id="P01-I01-W01",
            payload=_actual_payload().model_dump(mode="json"),
        ),
    )
    _append(
        store_path(state_path, StoreKind.AUDIT),
        _envelope(
            record_id="AUD-01",
            kind=StoreKind.AUDIT,
            scope_id="P01-I01-W01",
            payload=AuditPayload(
                audit_kind=AuditKind.REVIEW,
                verdict=AuditVerdict.PASS,
                check_results=[],
            ).model_dump(mode="json"),
        ),
    )
    _append(
        store_path(state_path, StoreKind.EVIDENCE),
        _envelope(
            record_id="EV-W01",
            kind=StoreKind.EVIDENCE,
            scope_id="P01-I01-W01",
            payload=_evidence("EV-W01", "P01-I01-W01", evidence_kind="deterministic").model_dump(
                mode="json"
            ),
        ),
    )
    _append(
        store_path(state_path, StoreKind.EVIDENCE),
        _envelope(
            record_id="EV-W02",
            kind=StoreKind.EVIDENCE,
            scope_id="P01-I01-W02",
            payload=_evidence("EV-W02", "P01-I01-W02", evidence_kind="attested").model_dump(
                mode="json"
            ),
        ),
    )
    _append(
        store_path(state_path, StoreKind.EVIDENCE),
        _envelope(
            record_id="EV-OLD",
            kind=StoreKind.EVIDENCE,
            scope_id="P01-I01-W01",
            payload=_evidence(
                "EV-OLD",
                "P01-I01-W01",
                evidence_kind="attested",
                created_at=old,
            ).model_dump(mode="json"),
            created_at=old,
        ),
    )
    _append(
        store_path(state_path, StoreKind.EVIDENCE),
        _envelope(
            record_id="EV-D-01",
            kind=StoreKind.EVIDENCE,
            scope_id="D-01",
            payload=_evidence(
                "EV-D-01",
                "D-01",
                evidence_kind="attested",
                refs=[build_urn("decision", owner="TR", id="D-01")],
            ).model_dump(mode="json"),
        ),
    )
    _append(
        store_path(state_path, StoreKind.EVIDENCE),
        _envelope(
            record_id="EV-AUD-01",
            kind=StoreKind.EVIDENCE,
            scope_id="AUD-01",
            payload=_evidence("EV-AUD-01", "AUD-01", evidence_kind="deterministic").model_dump(
                mode="json"
            ),
        ),
    )


def test_scorecard_reads_append_only_stores_and_labels_tiers(tmp_path: Path) -> None:
    state = _state_with_entities()
    state_path = _write_repo(tmp_path, state)
    _seed_stores(state_path)

    projection = read_store_projection(state_path)
    scorecard = compute_trust_scorecard(
        state,
        store_projection=projection,
        window="all",
        now=_T0,
    )

    assert scorecard.store_record_counts == {
        "estimate": 1,
        "actual": 1,
        "audit": 1,
        "evidence": 5,
    }
    labels = {label.scope_id: label.tier for label in scorecard.output_labels}
    assert labels["P01-I01-W01"] == "verified"
    assert labels["P01-I01-W02"] == "attested"
    assert labels["P01-I01-W03"] == "deferred_outcome"
    assert scorecard.tier_counts.verified == 1
    assert scorecard.tier_counts.attested == 1
    assert scorecard.tier_counts.deferred_outcome == 1
    assert scorecard.verifier_reliability.status == "computed"


def test_scorecard_supports_30d_and_n_wave_windows(tmp_path: Path) -> None:
    state = _state_with_entities()
    state_path = _write_repo(tmp_path, state)
    _seed_stores(state_path)
    projection = read_store_projection(state_path)

    recent = compute_trust_scorecard(state, store_projection=projection, window="30d", now=_T0)
    last_wave = compute_trust_scorecard(
        state,
        store_projection=projection,
        window=TrustWindow.parse("1-waves"),
        now=_T0,
    )

    assert recent.window == "30d"
    assert recent.store_record_counts["evidence"] == 4
    assert last_wave.window == "1-waves"
    assert [label.scope_id for label in last_wave.output_labels] == ["P01-I01-W02"]
    assert last_wave.output_labels[0].tier == "attested"


@pytest.mark.parametrize(
    ("urn", "kind", "tier"),
    [
        ("urn:eawf:v1:phase:TR/P01", "phase", "deferred_outcome"),
        ("urn:eawf:v1:iter:TR/P01-I01", "iter", "deferred_outcome"),
        ("urn:eawf:v1:wave:TR/P01-I01-W01", "wave", "verified"),
        ("urn:eawf:v1:decision:TR/D-01", "decision", "attested"),
        ("urn:eawf:v1:audit:TR/AUD-01", "audit", "verified"),
    ],
)
def test_assemble_why_supports_five_urn_kinds(
    tmp_path: Path,
    urn: str,
    kind: str,
    tier: str,
) -> None:
    state = _state_with_entities()
    state_path = _write_repo(tmp_path, state)
    _seed_stores(state_path)
    projection = read_store_projection(state_path)

    result = assemble_why(state, urn, store_projection=projection)

    assert result.kind == kind
    assert result.tier == tier
    assert result.refs


def test_why_cli_emits_json_payload(tmp_path: Path) -> None:
    state = _state_with_entities()
    state_path = _write_repo(tmp_path, state)
    _seed_stores(state_path)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--json", "-w", str(state_path.parent.parent), "why", "urn:eawf:v1:wave:TR/P01-I01-W01"],
    )

    assert result.exit_code == 0, result.output
    payload = orjson.loads(result.stdout)
    assert payload["kind"] == "wave"
    assert payload["tier"] == "verified"
    assert {ref["kind"] for ref in payload["refs"]} >= {"evidence", "actual", "estimate"}
