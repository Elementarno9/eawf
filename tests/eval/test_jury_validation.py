"""Jury-validation cohort tests (P30-I09-W01).

The cohort closes the gap from a per-wave verdict outcome (the held-rate signal
the reputation scorer reads) to a *ground-truth-labelled* validation set the
jury can be scored against. These tests pin the two binary success criteria:

- C1: each verdict outcome joins to a silver ground truth from its held outcome;
  an operator gold label overrides the silver for its wave; a gold label naming
  a wave absent from state raises ``ValueError`` at ingestion;
- C2: an empty store returns ``ValidationCohort(silver=[], gold=[])`` with no
  raised exception and no fabricated labels.

Plus the held-signal label correctness (refuted -> known-bad, clean -> known-good,
in-flight -> excluded) and the model error paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    IterStatus,
    IterTrigger,
    PhaseStatus,
    WaveStatus,
)
from eawf.kernel.state.models import Iter, Phase, State, Wave
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportHeader,
    AgentReportPayload,
    AuditorReportBody,
    report_record_id,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_dir, store_path
from eawf.observability.eval.jury_validation import (
    GoldLabel,
    LabeledVerdict,
    LabelSource,
    ValidationCohort,
    build_jury_validation_cohort,
)
from eawf.observability.eval.reputation import VerdictOutcome

_T0 = datetime(2026, 5, 1, tzinfo=UTC)
_GOLD_STORE = "gold_label.jsonl"


def _empty_state() -> State:
    """Return a minimal but valid State with no phases/iters/waves."""
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def _phase(*, phase_id: str, status: PhaseStatus, audit_id: str | None = None) -> Phase:
    """Return a Phase carrying the fields the outcome loop reads."""
    return Phase(
        id=phase_id,
        scope_id="QR",
        title=f"phase {phase_id}",
        status=status,
        opened_at=_T0,
        closed_at=None if status is not PhaseStatus.CLOSED else _T0,
        audit_id=audit_id,
    )


def _iter(*, iter_id: str, status: IterStatus, trigger: IterTrigger) -> Iter:
    """Return an Iter carrying *status* + *trigger*."""
    phase_id = iter_id.split("-")[0]
    return Iter(
        id=iter_id,
        phase_id=phase_id,
        title=f"iter {iter_id}",
        status=status,
        trigger=trigger,
        opened_at=_T0,
    )


def _wave(*, wave_id: str, status: WaveStatus) -> Wave:
    """Return a Wave under the iter parsed from *wave_id*."""
    iter_id = "-".join(wave_id.split("-")[:2])
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title=f"wave {wave_id}",
        status=status,
        opened_at=_T0,
        closed_at=_T0 if status is WaveStatus.CLOSED else None,
    )


def _write_auditor_verdict(
    state_path: Path,
    *,
    base_id: str,
    index: int = 0,
    verdict: AgentReportVerdict = AgentReportVerdict.PASS,
    confidence: Confidence = Confidence.HIGH,
    runtime: str = "claude",
) -> None:
    """Append one AUDITOR verdict envelope at ``base_id`` to the on-disk store."""
    role = AgentSessionRole.AUDITOR
    report_id = report_record_id(role=role, base_id=base_id, attempt=1)
    moment = _T0 + timedelta(minutes=index)
    body = AuditorReportBody(
        verdict=verdict,
        confidence=confidence,
        summary="recorded auditor verdict",
        target_id=base_id,
    )
    header = AgentReportHeader(
        report_id=report_id,
        role=role,
        session_id=f"S{index:02d}",
        scope_id=f"{base_id}::audit",
        base_id=base_id,
        attempt=1,
        runtime=runtime,
        generated_at=moment,
        summary="recorded auditor verdict",
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(role),
        scope_id=base_id,
        created_at=moment,
        updated_at=None,
        summary="recorded auditor verdict",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(store_path(state_path, store_kind_for_role(role)), envelope)


def _append_gold_label(
    state_path: Path,
    *,
    wave_id: str,
    ground_truth: bool,
    index: int = 0,
) -> None:
    """Append one GoldLabel record to the plain JSONL gold store."""
    label = GoldLabel(
        wave_id=wave_id,
        ground_truth=ground_truth,
        labeled_at=_T0 + timedelta(minutes=index),
    )
    path = store_dir(state_path) / _GOLD_STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(label.model_dump_json() + "\n")


def _state_with_clean_closed_wave(*, wave_id: str = "P01-I01-W01") -> State:
    """Build a state where *wave_id* sits in a clean closed phase/iter (held=True)."""
    state = _empty_state()
    phase_id = wave_id.split("-")[0]
    iter_id = "-".join(wave_id.split("-")[:2])
    state.phases[phase_id] = _phase(phase_id=phase_id, status=PhaseStatus.CLOSED, audit_id="AUD-1")
    state.iters[iter_id] = _iter(
        iter_id=iter_id, status=IterStatus.CLOSED, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.CLOSED)
    return state


def _state_with_refuted_wave(*, wave_id: str = "P01-I01-W01") -> State:
    """Build a state where *wave_id* is refuted by a later reactive iter (held=False)."""
    state = _state_with_clean_closed_wave(wave_id=wave_id)
    phase_id = wave_id.split("-")[0]
    state.iters[f"{phase_id}-I02"] = _iter(
        iter_id=f"{phase_id}-I02", status=IterStatus.ACTIVE, trigger=IterTrigger.REACTIVE
    )
    return state


# --- C2: honest-empty path ------------------------------------------------


def test_build_jury_validation_cohort_empty_store_returns_empty_cohort(tmp_path: Path) -> None:
    """C2: an empty store yields ``ValidationCohort(silver=[], gold=[])``.

    No verdict rows and no gold store means there is nothing to label, so the
    builder returns an empty cohort with no raised exception and no fabricated
    label -- today's real result, not a bug.
    """
    state_path = tmp_path / "state.json"
    state = _state_with_clean_closed_wave()

    cohort = build_jury_validation_cohort(state, state_path)

    assert cohort == ValidationCohort(silver=[], gold=[])
    assert cohort.silver == []
    assert cohort.gold == []


def test_build_jury_validation_cohort_empty_state_and_store_returns_empty(tmp_path: Path) -> None:
    """An empty State with no store at all still returns an empty cohort, no raise."""
    state_path = tmp_path / "state.json"

    cohort = build_jury_validation_cohort(_empty_state(), state_path)

    assert cohort == ValidationCohort(silver=[], gold=[])


# --- C1: silver join correctness ------------------------------------------


def test_build_jury_validation_cohort_clean_wave_silver_ground_truth_true(tmp_path: Path) -> None:
    """C1: a clean closed wave (held=True) joins to a silver ground_truth=True."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id, verdict=AgentReportVerdict.PASS)
    state = _state_with_clean_closed_wave(wave_id=wave_id)

    cohort = build_jury_validation_cohort(state, state_path)

    assert len(cohort.silver) == 1
    assert cohort.gold == []
    row = cohort.silver[0]
    assert row.outcome.base_id == wave_id
    assert row.ground_truth is True
    assert row.label_source is LabelSource.SILVER


def test_build_jury_validation_cohort_refuted_wave_silver_ground_truth_false(
    tmp_path: Path,
) -> None:
    """C1: a refuted wave (held=False) joins to a silver ground_truth=False.

    A later reactive iter refutes the verdict (``held is False``), so the silver
    ground truth is known-bad -- reusing the existing held-outcome signal.
    """
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _state_with_refuted_wave(wave_id=wave_id)

    cohort = build_jury_validation_cohort(state, state_path)

    assert len(cohort.silver) == 1
    row = cohort.silver[0]
    assert row.outcome.held is False
    assert row.ground_truth is False
    assert row.label_source is LabelSource.SILVER


def test_build_jury_validation_cohort_excludes_in_flight_verdict(tmp_path: Path) -> None:
    """An in-flight verdict (held=None) has no settled outcome -> no label, excluded.

    The verdict is on disk, but the wave is still open so its held-outcome is not
    yet observable. The cohort never fabricates a ground truth, so the row is
    dropped rather than guessed.
    """
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _empty_state()
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.ACTIVE, audit_id=None)
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.ACTIVE, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.IN_PROGRESS)

    cohort = build_jury_validation_cohort(state, state_path)

    assert cohort == ValidationCohort(silver=[], gold=[])


# --- C1: gold override -----------------------------------------------------


def test_build_jury_validation_cohort_gold_overrides_silver_for_its_wave(tmp_path: Path) -> None:
    """C1: an operator gold label overrides the silver label for its wave.

    The held-outcome would label the clean wave silver-True, but the operator
    pins it bad; the row moves to the gold subset carrying the operator truth and
    drops out of the silver set.
    """
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id, verdict=AgentReportVerdict.PASS)
    state = _state_with_clean_closed_wave(wave_id=wave_id)
    _append_gold_label(state_path, wave_id=wave_id, ground_truth=False)

    cohort = build_jury_validation_cohort(state, state_path)

    assert cohort.silver == []
    assert len(cohort.gold) == 1
    row = cohort.gold[0]
    assert row.outcome.base_id == wave_id
    # Operator override wins over the silver held=True signal.
    assert row.ground_truth is False
    assert row.label_source is LabelSource.GOLD


def test_build_jury_validation_cohort_gold_only_overrides_named_wave(tmp_path: Path) -> None:
    """A gold label for one wave leaves sibling waves on the silver tier."""
    state_path = tmp_path / "state.json"
    gold_wave = "P01-I01-W01"
    silver_wave = "P01-I01-W02"
    _write_auditor_verdict(state_path, base_id=gold_wave, index=0)
    _write_auditor_verdict(state_path, base_id=silver_wave, index=1)
    state = _state_with_clean_closed_wave(wave_id=gold_wave)
    state.waves[silver_wave] = _wave(wave_id=silver_wave, status=WaveStatus.CLOSED)
    _append_gold_label(state_path, wave_id=gold_wave, ground_truth=True)

    cohort = build_jury_validation_cohort(state, state_path)

    assert [row.outcome.base_id for row in cohort.gold] == [gold_wave]
    assert [row.outcome.base_id for row in cohort.silver] == [silver_wave]


def test_build_jury_validation_cohort_latest_gold_label_wins(tmp_path: Path) -> None:
    """Append-only: the latest gold label per wave supersedes an earlier one."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _state_with_clean_closed_wave(wave_id=wave_id)
    # Earlier mistake then a correction: the later record wins.
    _append_gold_label(state_path, wave_id=wave_id, ground_truth=True, index=0)
    _append_gold_label(state_path, wave_id=wave_id, ground_truth=False, index=5)

    cohort = build_jury_validation_cohort(state, state_path)

    assert len(cohort.gold) == 1
    assert cohort.gold[0].ground_truth is False


# --- C1: unknown-wave gold label fails fast --------------------------------


def test_build_jury_validation_cohort_gold_unknown_wave_raises(tmp_path: Path) -> None:
    """C1: a gold label naming a wave absent from state raises ValueError.

    There is no wave to anchor the label on, so a typo or stale id is a hard
    error at ingestion rather than a silently dropped row.
    """
    state_path = tmp_path / "state.json"
    state = _state_with_clean_closed_wave()
    _append_gold_label(state_path, wave_id="P99-I99-W99", ground_truth=True)

    with pytest.raises(ValueError, match="gold label names unknown wave: 'P99-I99-W99'"):
        build_jury_validation_cohort(state, state_path)


# --- model error paths -----------------------------------------------------


def test_gold_label_rejects_extra_field() -> None:
    """An unexpected GoldLabel field fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        GoldLabel(
            wave_id="P01-I01-W01",
            ground_truth=True,
            labeled_at=_T0,
            unexpected="boom",
        )


def test_labeled_verdict_rejects_extra_field() -> None:
    """An unexpected LabeledVerdict field fails ``extra='forbid'``."""
    outcome = VerdictOutcome(
        base_id="P01-I01-W01",
        agent_role=AgentSessionRole.AUDITOR,
        runtime="claude",
        verdict=AgentReportVerdict.PASS,
        confidence=0.9,
        held=True,
        outcome_source="clean",
    )
    with pytest.raises(ValidationError):
        LabeledVerdict(
            outcome=outcome,
            ground_truth=True,
            label_source=LabelSource.SILVER,
            unexpected="boom",
        )


def test_validation_cohort_rejects_extra_field() -> None:
    """An unexpected ValidationCohort field fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        ValidationCohort(silver=[], gold=[], unexpected="boom")
