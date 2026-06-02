"""Verdict-to-outcome projection tests (P29-I05-W01).

The outcome loop closes the gap from a per-wave verdict (an AUDITOR report at
``base_id=wave_id``) to its realized, state-observable outcome, so a later
reputation/Brier scorer has data to score. These tests pin:

- the honest-empty path -- an empty report store yields ``[]`` (today's real
  result, not a bug);
- a clean closed iter -> ``held is True`` / ``outcome_source == "clean"``;
- a later reactive iter under the same phase -> ``held is False`` /
  ``outcome_source == "reactive"``;
- a reopened phase -> ``held is False`` / ``outcome_source == "reopen"``;
- an in-flight wave -> ``held is None`` (not yet observable);
- the confidence -> float mapping (high/med/low -> 0.9/0.7/0.55); and
- the model's ``extra="forbid"`` + ``confidence`` bound error paths.
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
from eawf.kernel.store.paths import store_path
from eawf.observability.eval import (
    VerdictOutcome,
    build_verdict_outcomes,
    confidence_to_float,
)
from eawf.observability.eval.reputation import _CONFIDENCE_TO_FLOAT

_T0 = datetime(2026, 5, 1, tzinfo=UTC)


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
            "subproject_id": None,
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


def _state_with_clean_closed_wave(*, wave_id: str = "P01-I01-W01") -> State:
    """Build a state where *wave_id* sits in a clean closed phase/iter."""
    state = _empty_state()
    phase_id = wave_id.split("-")[0]
    iter_id = "-".join(wave_id.split("-")[:2])
    state.phases[phase_id] = _phase(phase_id=phase_id, status=PhaseStatus.CLOSED, audit_id="AUD-1")
    state.iters[iter_id] = _iter(
        iter_id=iter_id, status=IterStatus.CLOSED, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.CLOSED)
    return state


# --- build_verdict_outcomes: honest-empty path ----------------------------


def test_build_verdict_outcomes_empty_store_returns_empty(tmp_path: Path) -> None:
    """An empty per-wave report store yields ``[]`` -- today's real result.

    This is the load-bearing honest-negative criterion: zero AUDITOR verdict
    rows exist on disk today, so the projection has nothing to project and must
    return an empty list rather than fabricate an outcome.
    """
    state_path = tmp_path / "state.json"
    state = _state_with_clean_closed_wave()

    assert build_verdict_outcomes(state, state_path) == []


def test_build_verdict_outcomes_skips_verdict_for_unknown_wave(tmp_path: Path) -> None:
    """A verdict whose base_id names no wave in state is skipped."""
    state_path = tmp_path / "state.json"
    _write_auditor_verdict(state_path, base_id="P99-I99-W99")
    state = _state_with_clean_closed_wave()

    assert build_verdict_outcomes(state, state_path) == []


# --- build_verdict_outcomes: clean held outcome ---------------------------


def test_build_verdict_outcomes_clean_closed_iter_holds(tmp_path: Path) -> None:
    """A pass verdict on a wave in a clean closed iter holds clean."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id, verdict=AgentReportVerdict.PASS)
    state = _state_with_clean_closed_wave(wave_id=wave_id)

    outcomes = build_verdict_outcomes(state, state_path)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.base_id == wave_id
    assert outcome.agent_role is AgentSessionRole.AUDITOR
    assert outcome.verdict is AgentReportVerdict.PASS
    assert outcome.held is True
    assert outcome.outcome_source == "clean"


def test_build_verdict_outcomes_carries_runtime_and_confidence(tmp_path: Path) -> None:
    """The projection copies the report runtime and maps its confidence bucket."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(
        state_path, base_id=wave_id, confidence=Confidence.MEDIUM, runtime="codex"
    )
    state = _state_with_clean_closed_wave(wave_id=wave_id)

    outcome = build_verdict_outcomes(state, state_path)[0]

    assert outcome.runtime == "codex"
    assert outcome.confidence == pytest.approx(0.7)


# --- build_verdict_outcomes: reactive refutation --------------------------


def test_build_verdict_outcomes_later_reactive_iter_refutes(tmp_path: Path) -> None:
    """A later reactive iter under the same phase refutes the verdict."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _state_with_clean_closed_wave(wave_id=wave_id)
    # A later repair iter covers the wave's scope.
    state.iters["P01-I02"] = _iter(
        iter_id="P01-I02", status=IterStatus.ACTIVE, trigger=IterTrigger.REACTIVE
    )

    outcome = build_verdict_outcomes(state, state_path)[0]

    assert outcome.held is False
    assert outcome.outcome_source == "reactive"


def test_build_verdict_outcomes_earlier_reactive_iter_does_not_refute(tmp_path: Path) -> None:
    """A reactive iter that is NOT strictly later does not refute the wave."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I02-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _empty_state()
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.CLOSED, audit_id="AUD-1")
    # An earlier reactive iter (I01) precedes the wave's iter (I02): no refutation.
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.CLOSED, trigger=IterTrigger.REACTIVE
    )
    state.iters["P01-I02"] = _iter(
        iter_id="P01-I02", status=IterStatus.CLOSED, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.CLOSED)

    outcome = build_verdict_outcomes(state, state_path)[0]

    assert outcome.held is True
    assert outcome.outcome_source == "clean"


# --- build_verdict_outcomes: reopen refutation ----------------------------


def test_build_verdict_outcomes_reopened_phase_refutes(tmp_path: Path) -> None:
    """A reopened phase (ACTIVE + preserved audit_id) refutes the verdict."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _empty_state()
    # reopen_phase flips CLOSED -> ACTIVE but preserves audit_id.
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.ACTIVE, audit_id="AUD-1")
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.CLOSED, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.CLOSED)

    outcome = build_verdict_outcomes(state, state_path)[0]

    assert outcome.held is False
    assert outcome.outcome_source == "reopen"


def test_build_verdict_outcomes_never_closed_active_phase_is_not_reopen(tmp_path: Path) -> None:
    """An ACTIVE phase with no audit_id is in-flight, not a reopen."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _empty_state()
    # Never-closed ACTIVE phase: audit_id is None.
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.ACTIVE, audit_id=None)
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.ACTIVE, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.IN_PROGRESS)

    outcome = build_verdict_outcomes(state, state_path)[0]

    # In-flight, not refuted: not yet observable.
    assert outcome.held is None
    assert outcome.outcome_source is None


# --- build_verdict_outcomes: not-yet-observable ---------------------------


def test_build_verdict_outcomes_in_flight_wave_is_unobservable(tmp_path: Path) -> None:
    """An open wave in an open iter has no settled outcome -- held is None."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _empty_state()
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.ACTIVE, audit_id=None)
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.ACTIVE, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.IN_PROGRESS)

    outcome = build_verdict_outcomes(state, state_path)[0]

    assert outcome.held is None
    assert outcome.outcome_source is None


def test_build_verdict_outcomes_closed_wave_open_iter_is_unobservable(tmp_path: Path) -> None:
    """A closed wave whose iter is still open is not yet settled."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _empty_state()
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.ACTIVE, audit_id=None)
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.ACTIVE, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.CLOSED)

    outcome = build_verdict_outcomes(state, state_path)[0]

    assert outcome.held is None
    assert outcome.outcome_source is None


# --- build_verdict_outcomes: iter_id filter -------------------------------


def test_build_verdict_outcomes_iter_filter_restricts_cohort(tmp_path: Path) -> None:
    """The iter_id filter keeps only verdicts whose wave is under that iter."""
    state_path = tmp_path / "state.json"
    keep_wave = "P01-I01-W01"
    drop_wave = "P01-I02-W01"
    _write_auditor_verdict(state_path, base_id=keep_wave, index=0)
    _write_auditor_verdict(state_path, base_id=drop_wave, index=1)
    state = _empty_state()
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.CLOSED, audit_id="AUD-1")
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.CLOSED, trigger=IterTrigger.PROACTIVE
    )
    state.iters["P01-I02"] = _iter(
        iter_id="P01-I02", status=IterStatus.CLOSED, trigger=IterTrigger.PROACTIVE
    )
    state.waves[keep_wave] = _wave(wave_id=keep_wave, status=WaveStatus.CLOSED)
    state.waves[drop_wave] = _wave(wave_id=drop_wave, status=WaveStatus.CLOSED)

    outcomes = build_verdict_outcomes(state, state_path, iter_id="P01-I01")

    assert [outcome.base_id for outcome in outcomes] == [keep_wave]


# --- confidence_to_float mapping ------------------------------------------


def test_confidence_to_float_maps_high_med_low() -> None:
    """The ratified mapping is high/med/low -> 0.9/0.7/0.55."""
    assert confidence_to_float(Confidence.HIGH) == pytest.approx(0.9)
    assert confidence_to_float(Confidence.MEDIUM) == pytest.approx(0.7)
    assert confidence_to_float(Confidence.LOW) == pytest.approx(0.55)


def test_confidence_to_float_covers_every_confidence_value() -> None:
    """Every Confidence member has a mapping (no bucket falls through)."""
    assert set(_CONFIDENCE_TO_FLOAT) == set(Confidence)


# --- VerdictOutcome model error paths -------------------------------------


def test_verdict_outcome_rejects_out_of_range_confidence() -> None:
    """A confidence above 1.0 fails the [0.0, 1.0] bound."""
    with pytest.raises(ValidationError):
        VerdictOutcome(
            base_id="P01-I01-W01",
            agent_role=AgentSessionRole.AUDITOR,
            runtime="claude",
            verdict=AgentReportVerdict.PASS,
            confidence=1.5,
        )


def test_verdict_outcome_rejects_extra_field() -> None:
    """An unexpected field fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        VerdictOutcome(
            base_id="P01-I01-W01",
            agent_role=AgentSessionRole.AUDITOR,
            runtime="claude",
            verdict=AgentReportVerdict.PASS,
            confidence=0.9,
            unexpected="boom",
        )
