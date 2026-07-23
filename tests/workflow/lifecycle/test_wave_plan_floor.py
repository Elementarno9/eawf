"""Tests for the plan-time typed-criteria floor (P30-I23-W26).

Pins the authoring counterpart of the close-time verifier: ``plan_wave``
and ``edit_wave_plan`` reject a legacy-string criterion set with a typed
error naming the floor, a typed ``criteria_floor_waiver`` (>= 20-char
reason) bypasses it and persists visibly on the wave row, and a criterion
claiming ``evidence_kind == "deterministic"`` without a gate is rejected
at authoring (the gateless-deterministic hole).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import (
    CriterionSpec,
    ResponseClause,
    grandfather_criterion,
)
from eawf.kernel.state.models import CriteriaFloorWaiver, State
from eawf.workflow.lifecycle._errors import (
    LifecycleGuardError,
    check_criteria_floor,
    check_disabled_waiver_policy,
)
from eawf.workflow.lifecycle.transitions import (
    LifecycleError,
    edit_wave_plan,
    open_iter,
    open_phase,
    plan_wave,
)
from tests.conftest import make_intent

pytestmark = pytest.mark.unit


def _empty_state() -> State:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": "2026-07-02T00:00:00Z",
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "Abc",
            "domains": ["infra"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
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


def _seeded_state() -> State:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    return state


def _waiver(
    reason: str = "repair burst: typed criteria follow via spec sync",
) -> CriteriaFloorWaiver:
    return CriteriaFloorWaiver(reason=reason, waived_at=datetime(2026, 7, 2, tzinfo=UTC))


def _typed_criterion(
    *, gate_ids: list[str] | None = None, evidence_kind: str = "deterministic"
) -> CriterionSpec:
    return CriterionSpec(
        id="CR-01",
        text="renders the humanized token total under pytest exit zero",
        kind="deterministic",
        acceptance_style="binary",
        evidence_kind=evidence_kind,
        gate_ids=list(gate_ids or []),
        quality_dimension="functional_suitability",
        measurable_signal="the units suite asserts the humanized figure lands",
        response=ResponseClause(
            observe="exits",
            object="zero from the units suite",
            locus="pytest",
        ),
    )


def _plan(state: State, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "wave_id": "P01-I01-W01",
        "iter_id": "P01-I01",
        "title": "add typed floor coverage",
        "file_scopes": ["src/x.py"],
        "effort_bucket": "M",
        "intent": make_intent(),
    }
    kwargs.update(overrides)
    return plan_wave(state, **kwargs)


# ---- check_criteria_floor: the floor primitive itself -----------------------


def test_check_criteria_floor_rejects_legacy_rows() -> None:
    legacy = [grandfather_criterion("ship the thing end to end", index=1)]
    with pytest.raises(LifecycleError, match="typed-criteria floor"):
        check_criteria_floor(legacy, entity_kind="wave", entity_id="W-X")


def test_check_criteria_floor_waiver_bypasses() -> None:
    legacy = [grandfather_criterion("ship the thing end to end", index=1)]
    check_criteria_floor(legacy, entity_kind="wave", entity_id="W-X", waiver=_waiver())


def test_check_criteria_floor_rejects_gateless_deterministic() -> None:
    with pytest.raises(LifecycleError, match="gateless-deterministic"):
        check_criteria_floor([_typed_criterion(gate_ids=[])], entity_kind="wave", entity_id="W-X")


def test_check_criteria_floor_empty_list_passes() -> None:
    check_criteria_floor([], entity_kind="wave", entity_id="W-X")


# ---- CR-01: legacy-string criterion sets are rejected ----------------------


def test_plan_wave_rejects_legacy_criterion_set() -> None:
    state = _seeded_state()
    legacy = [grandfather_criterion("implement the parser tokeniser module", index=1)]
    with pytest.raises(LifecycleError, match="typed-criteria floor"):
        _plan(state, success_criteria=legacy)
    assert "P01-I01-W01" not in state.waves


def test_plan_wave_empty_criteria_passes_floor() -> None:
    """The authoring flow lands the wave first; spec sync fills criteria."""
    state = _seeded_state()
    wave = _plan(state, success_criteria=[])
    assert wave.success_criteria == []
    assert wave.criteria_floor_waiver is None


def test_edit_wave_plan_rejects_legacy_replacement_set() -> None:
    state = _seeded_state()
    _plan(state, success_criteria=[])
    legacy = [grandfather_criterion("wire the telemetry dashboard exporter", index=1)]
    with pytest.raises(LifecycleError, match="typed-criteria floor"):
        edit_wave_plan(state, wave_id="P01-I01-W01", success_criteria=legacy)
    assert state.waves["P01-I01-W01"].success_criteria == []


# ---- CR-02: the typed waiver bypasses the floor and persists ----------------


def test_plan_wave_waiver_allows_legacy_set_and_persists() -> None:
    state = _seeded_state()
    legacy = [grandfather_criterion("implement the parser tokeniser module", index=1)]
    waiver = _waiver()
    wave = _plan(state, success_criteria=legacy, criteria_floor_waiver=waiver)
    assert wave.criteria_floor_waiver is not None
    assert wave.criteria_floor_waiver.reason == waiver.reason
    # The persisted record is the typed model, visible on the wave row.
    assert state.waves["P01-I01-W01"].criteria_floor_waiver == waiver


def test_edit_wave_plan_waiver_allows_legacy_replacement() -> None:
    state = _seeded_state()
    _plan(state, success_criteria=[])
    legacy = [grandfather_criterion("wire the telemetry dashboard exporter", index=1)]
    waiver = _waiver()
    wave = edit_wave_plan(
        state,
        wave_id="P01-I01-W01",
        success_criteria=legacy,
        criteria_floor_waiver=waiver,
    )
    assert wave.criteria_floor_waiver == waiver
    assert wave.success_criteria[0].kind == "legacy"


def test_plan_wave_disabled_rejects_floor_waiver_without_mutation() -> None:
    """Disabled policy rejects before the wave or reverse indexes mutate."""
    state = _seeded_state()
    before = state.model_dump_json()
    legacy = [grandfather_criterion("implement the parser tokeniser module", index=1)]

    with pytest.raises(LifecycleGuardError) as raised:
        _plan(
            state,
            success_criteria=legacy,
            criteria_floor_waiver=_waiver(),
            waiver_mode="disabled",
        )

    assert raised.value.code == "waiver_mode_disabled"
    assert state.model_dump_json() == before


def test_check_disabled_waiver_policy_directly_rejects_floor_waiver() -> None:
    """Shared policy contract carries the stable disabled guard code."""
    with pytest.raises(LifecycleGuardError) as raised:
        check_disabled_waiver_policy(
            waiver_mode="disabled",
            scope_id="P01-I01-W01",
            criteria=[],
            criteria_floor_waiver=_waiver(),
        )

    assert raised.value.code == "waiver_mode_disabled"


def test_edit_wave_plan_disabled_rejects_floor_waiver_without_mutation() -> None:
    """Rejected disabled edit leaves every existing plan field unchanged."""
    state = _seeded_state()
    _plan(state, success_criteria=[])
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as raised:
        edit_wave_plan(
            state,
            wave_id="P01-I01-W01",
            success_criteria=[
                grandfather_criterion("wire the telemetry dashboard exporter", index=1)
            ],
            criteria_floor_waiver=_waiver(),
            waiver_mode="disabled",
        )

    assert raised.value.code == "waiver_mode_disabled"
    assert state.model_dump_json() == before


def test_plan_wave_disabled_rejects_raw_criterion_reason_without_mutation() -> None:
    """Raw criterion reasons are forbidden at disabled authoring boundaries."""
    state = _seeded_state()
    before = state.model_dump_json()
    criterion = _typed_criterion(gate_ids=["G-01"]).model_copy(
        update={"waiver_reason": "historical raw waiver reason"}
    )

    with pytest.raises(LifecycleGuardError) as raised:
        _plan(state, success_criteria=[criterion], waiver_mode="disabled")

    assert raised.value.code == "waiver_mode_disabled"
    assert state.model_dump_json() == before


@pytest.mark.parametrize("mode", ["A", "B", "C"])
def test_plan_wave_permissive_modes_keep_raw_criterion_compatibility(mode: str) -> None:
    """A/B/C continue accepting historical raw criterion reasons."""
    state = _seeded_state()
    criterion = _typed_criterion(gate_ids=["G-01"]).model_copy(
        update={"waiver_reason": "historical raw waiver reason"}
    )

    wave = _plan(state, success_criteria=[criterion], waiver_mode=mode)

    assert wave.success_criteria[0].waiver_reason == "historical raw waiver reason"


def test_waiver_reason_floor_rejects_short_reason() -> None:
    with pytest.raises(ValidationError):
        CriteriaFloorWaiver(reason="too short", waived_at=datetime(2026, 7, 2, tzinfo=UTC))


# ---- CR-03: the gateless-deterministic hole is closed -----------------------


def test_plan_wave_rejects_gateless_deterministic_criterion() -> None:
    state = _seeded_state()
    with pytest.raises(LifecycleError, match="gateless-deterministic"):
        _plan(state, success_criteria=[_typed_criterion(gate_ids=[])])
    assert "P01-I01-W01" not in state.waves


def test_edit_wave_plan_rejects_gateless_deterministic_criterion() -> None:
    state = _seeded_state()
    _plan(state, success_criteria=[])
    with pytest.raises(LifecycleError, match="gateless-deterministic"):
        edit_wave_plan(
            state,
            wave_id="P01-I01-W01",
            success_criteria=[_typed_criterion(gate_ids=[])],
        )


def test_plan_wave_accepts_gated_deterministic_criterion() -> None:
    state = _seeded_state()
    wave = _plan(state, success_criteria=[_typed_criterion(gate_ids=["G-01"])])
    assert wave.success_criteria[0].gate_ids == ["G-01"]
    assert wave.criteria_floor_waiver is None
