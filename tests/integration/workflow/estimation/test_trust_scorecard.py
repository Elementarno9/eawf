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
    HypothesisStatus,
    HypothesisVerdict,
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
    Hypothesis,
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
    state.hypotheses = {
        "H03-12": Hypothesis(
            id="H03-12",
            scope_id="P01",
            title="Trust labels improve scan time",
            metric="time-to-locate",
            confirm="under 5s",
            reject="over 30s",
            status=HypothesisStatus.CONFIRMED,
            verdict=HypothesisVerdict.CONFIRMED,
            audit_id="AUD-01",
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
    _append(
        store_path(state_path, StoreKind.EVIDENCE),
        _envelope(
            record_id="EV-H03-12",
            kind=StoreKind.EVIDENCE,
            scope_id="H03-12",
            payload=_evidence("EV-H03-12", "H03-12", evidence_kind="attested").model_dump(
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
        "evidence": 6,
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
    assert recent.store_record_counts["evidence"] == 5
    assert [label.scope_id for label in recent.output_labels] == [
        "P01-I01-W01",
        "P01-I01-W02",
    ]
    assert last_wave.window == "1-waves"
    assert [label.scope_id for label in last_wave.output_labels] == ["P01-I01-W02"]
    assert last_wave.output_labels[0].tier == "attested"


def test_scorecard_treats_state_actual_as_closed_outcome() -> None:
    state = _state_with_entities()
    state.actuals["P01-I01-W02"] = ActualSummary(
        id="ACT-P01-I01-W02",
        scope_id="P01-I01-W02",
        status=ActualStatus.DONE,
        elapsed_eu=0.0,
        current_store_record_id="REC-P01-I01-W02",
        updated_at=_T0,
    )

    scorecard = compute_trust_scorecard(state, store_projection=None, window="all", now=_T0)

    labels = {label.scope_id: label for label in scorecard.output_labels}
    assert labels["P01-I01-W02"].tier == "unavailable"
    assert labels["P01-I01-W02"].reason == "no verifier or attestation evidence"


@pytest.mark.parametrize(
    ("urn", "kind", "tier"),
    [
        ("urn:eawf:v1:phase:TR/P01", "phase", "deferred_outcome"),
        ("urn:eawf:v1:iter:TR/P01-I01", "iter", "deferred_outcome"),
        ("urn:eawf:v1:wave:TR/P01-I01-W01", "wave", "verified"),
        ("urn:eawf:v1:hypothesis:TR/H03-12", "hypothesis", "attested"),
        ("urn:eawf:v1:decision:TR/D-01", "decision", "attested"),
        ("urn:eawf:v1:audit:TR/AUD-01", "audit", "verified"),
    ],
)
def test_assemble_why_supports_superset_urn_kinds(
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


def test_assemble_why_hypothesis_surfaces_verdict_and_audit(tmp_path: Path) -> None:
    state = _state_with_entities()
    state_path = _write_repo(tmp_path, state)
    _seed_stores(state_path)
    projection = read_store_projection(state_path)

    result = assemble_why(
        state,
        "urn:eawf:v1:hypothesis:TR/H03-12",
        store_projection=projection,
    )

    assert result.kind == "hypothesis"
    assert result.id == "H03-12"
    assert result.title == "Trust labels improve scan time"
    assert "confirmed" in result.summary
    assert any(ref.kind == "audit" and "AUD-01" in ref.urn for ref in result.refs)


@pytest.mark.parametrize(
    ("bare_id", "kind"),
    [
        ("P01", "phase"),
        ("P01-I01", "iter"),
        ("P01-I01-W01", "wave"),
        ("H03-12", "hypothesis"),
    ],
)
def test_assemble_why_routes_bare_id_by_shape(
    tmp_path: Path,
    bare_id: str,
    kind: str,
) -> None:
    state = _state_with_entities()
    state_path = _write_repo(tmp_path, state)
    _seed_stores(state_path)
    projection = read_store_projection(state_path)

    result = assemble_why(state, bare_id, store_projection=projection)

    assert result.kind == kind
    assert result.id == bare_id
    assert result.urn == f"urn:eawf:v1:{kind}:TR/{bare_id}"


def test_assemble_why_unknown_hypothesis_id_raises_key_error(tmp_path: Path) -> None:
    state = _state_with_entities()
    state_path = _write_repo(tmp_path, state)
    _seed_stores(state_path)
    projection = read_store_projection(state_path)

    with pytest.raises(KeyError, match="why target not found"):
        assemble_why(state, "H99-99", store_projection=projection)


def test_assemble_why_unknown_urn_target_raises_key_error(tmp_path: Path) -> None:
    state = _state_with_entities()
    state_path = _write_repo(tmp_path, state)
    _seed_stores(state_path)
    projection = read_store_projection(state_path)

    with pytest.raises(KeyError, match="why target not found"):
        assemble_why(state, "urn:eawf:v1:wave:TR/P99-I99-W99", store_projection=projection)


def test_assemble_why_malformed_urn_raises_value_error() -> None:
    state = _state_with_entities()

    with pytest.raises(ValueError, match="not a urn:eawf URN"):
        assemble_why(state, "urn:eawf:bogus", store_projection=None)


def test_assemble_why_unrecognised_bare_target_raises_value_error() -> None:
    state = _state_with_entities()

    with pytest.raises(ValueError, match="unrecognised why target"):
        assemble_why(state, "not-an-id", store_projection=None)


def test_assemble_why_unsupported_urn_kind_raises_value_error() -> None:
    state = _state_with_entities()

    # ``artifact`` is a valid URN kind but carries no trust-tier story, so the
    # why surface rejects it (distinct from an unknown-kind parse failure).
    with pytest.raises(ValueError, match="unsupported why URN kind"):
        assemble_why(state, "urn:eawf:v1:artifact:TR/ART-01", store_projection=None)


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


def test_why_cli_explains_hypothesis_urn(tmp_path: Path) -> None:
    state = _state_with_entities()
    state_path = _write_repo(tmp_path, state)
    _seed_stores(state_path)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(state_path.parent.parent),
            "why",
            "urn:eawf:v1:hypothesis:TR/H03-12",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = orjson.loads(result.stdout)
    assert payload["kind"] == "hypothesis"
    assert payload["id"] == "H03-12"


def test_why_cli_routes_bare_id(tmp_path: Path) -> None:
    state = _state_with_entities()
    state_path = _write_repo(tmp_path, state)
    _seed_stores(state_path)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--json", "-w", str(state_path.parent.parent), "why", "H03-12"],
    )

    assert result.exit_code == 0, result.output
    payload = orjson.loads(result.stdout)
    assert payload["kind"] == "hypothesis"
    assert payload["urn"] == "urn:eawf:v1:hypothesis:TR/H03-12"


def test_why_cli_rejects_unknown_target(tmp_path: Path) -> None:
    state = _state_with_entities()
    state_path = _write_repo(tmp_path, state)
    _seed_stores(state_path)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-w", str(state_path.parent.parent), "why", "not-an-id"],
    )

    assert result.exit_code != 0
    assert "unrecognised why target" in result.output
