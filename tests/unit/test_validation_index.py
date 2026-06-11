"""Unit tests for the per-validation index in :mod:`eawf.kernel.validate.invariants`.

The index is a pure refactor of *how* parent->children lookups happen inside
the closure invariants. These tests pin three things:

- :func:`build_validation_index` groups iters by ``phase_id`` and waves by
  ``iter_id`` in ``State`` iteration order.
- :func:`check_closure_rules` yields byte-identical violations whether it
  builds its own index or receives a pre-built one.
- :func:`validate_state` is behaviourally unchanged: feeding the index through
  the invariants produces the exact same violation set as evaluating every
  invariant standalone.
"""

from __future__ import annotations

from typing import Any

from eawf.kernel.state.models import State
from eawf.kernel.validate.invariants import (
    ALL_INVARIANTS,
    ValidationIndex,
    Violation,
    build_validation_index,
    check_closure_rules,
)
from eawf.kernel.validate.strict import validate_state

# ---- Fixture builders -------------------------------------------------------


def _base_state_payload() -> dict[str, Any]:
    """Return a minimal repo-scoped payload that passes every invariant."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant-research",
            "title": "Quant Research",
            "description": "",
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


def _phase(phase_id: str, *, status: str = "active") -> dict[str, Any]:
    return {
        "id": phase_id,
        "scope_id": "QR",
        "title": f"Phase {phase_id}",
        "status": status,
        "iter_ids": [],
        "outcome_ids": [],
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": "2026-05-08T01:00:00Z" if status == "closed" else None,
        "audit_id": None,
    }


def _iter(iter_id: str, *, phase_id: str, status: str = "active") -> dict[str, Any]:
    return {
        "id": iter_id,
        "phase_id": phase_id,
        "title": f"Iter {iter_id}",
        "status": status,
        "wave_ids": [],
        "estimate_id": None,
        "audit_id": None,
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": "2026-05-08T01:00:00Z" if status == "closed" else None,
    }


def _wave(wave_id: str, *, iter_id: str, status: str = "in_progress") -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": iter_id,
        "title": f"Wave {wave_id}",
        "status": status,
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "claim_session_id": None,
        "worktree_id": None,
        "outcome": None,
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": "2026-05-08T01:00:00Z"
        if status in {"closed", "failed", "abandoned"}
        else None,
    }


def _violation_tuples(violations: list[Violation]) -> list[tuple[str, str, str]]:
    """Render violations as ordered ``(code, path, message)`` tuples."""
    return [(v.code, v.path, v.message) for v in violations]


def _invariants_without_index(state: State) -> list[Violation]:
    """Run every invariant standalone (no shared index) in registry order.

    This reproduces the pre-W17 ``validate_state`` loop where each invariant
    received only ``state`` and re-derived its own lookups, giving us a
    behavioural oracle to diff against.
    """
    out: list[Violation] = []
    for invariant in ALL_INVARIANTS:
        out.extend(invariant(state))
    return out


# ---- build_validation_index -------------------------------------------------


def test_build_validation_index_empty_state_has_empty_groupings() -> None:
    state = State.model_validate(_base_state_payload())
    index = build_validation_index(state)
    assert index.iters_by_phase == {}
    assert index.waves_by_iter == {}


def test_build_validation_index_groups_iters_by_phase() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["phases"]["P02"] = _phase("P02")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["iters"]["P01-I02"] = _iter("P01-I02", phase_id="P01")
    payload["iters"]["P02-I01"] = _iter("P02-I01", phase_id="P02")
    state = State.model_validate(payload)

    index = build_validation_index(state)

    assert [iid for iid, _ in index.iters_by_phase["P01"]] == ["P01-I01", "P01-I02"]
    assert [iid for iid, _ in index.iters_by_phase["P02"]] == ["P02-I01"]


def test_build_validation_index_groups_waves_by_iter() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["waves"]["P01-I01-W01"] = _wave("P01-I01-W01", iter_id="P01-I01")
    payload["waves"]["P01-I01-W02"] = _wave("P01-I01-W02", iter_id="P01-I01")
    state = State.model_validate(payload)

    index = build_validation_index(state)

    assert [wid for wid, _ in index.waves_by_iter["P01-I01"]] == [
        "P01-I01-W01",
        "P01-I01-W02",
    ]


def test_build_validation_index_omits_parents_without_children() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")  # phase with no iters
    state = State.model_validate(payload)

    index = build_validation_index(state)

    assert "P01" not in index.iters_by_phase


def test_build_validation_index_keeps_dangling_parent_keys() -> None:
    """An iter pointing at a missing phase still appears under that phase key.

    Parentage validity is the job of ``check_parent_ids``; the index is a
    pure grouping and must not silently drop dangling references.
    """
    payload = _base_state_payload()
    payload["iters"]["P99-I01"] = _iter("P99-I01", phase_id="P99")
    state = State.model_validate(payload)

    index = build_validation_index(state)

    assert [iid for iid, _ in index.iters_by_phase["P99"]] == ["P99-I01"]


def test_validation_index_is_frozen() -> None:
    index = ValidationIndex(iters_by_phase={}, waves_by_iter={})
    try:
        index.iters_by_phase = {}  # type: ignore[misc]
    except AttributeError, TypeError:
        pass
    else:  # pragma: no cover - frozen dataclass must reject assignment
        raise AssertionError("ValidationIndex should be frozen")


# ---- check_closure_rules: index vs no-index equivalence ---------------------


def test_check_closure_rules_matches_with_and_without_index_clean() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01", status="closed")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01", status="closed")
    payload["waves"]["P01-I01-W01"] = _wave("P01-I01-W01", iter_id="P01-I01", status="closed")
    state = State.model_validate(payload)
    index = build_validation_index(state)

    without = _violation_tuples(list(check_closure_rules(state)))
    with_index = _violation_tuples(list(check_closure_rules(state, index)))

    assert without == with_index == []


def test_check_closure_rules_matches_with_and_without_index_phase_open_iter() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01", status="closed")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01", status="active")
    state = State.model_validate(payload)
    index = build_validation_index(state)

    without = _violation_tuples(list(check_closure_rules(state)))
    with_index = _violation_tuples(list(check_closure_rules(state, index)))

    assert without == with_index
    assert "INV.CLOSURE.PHASE_HAS_OPEN_ITER" in {code for code, _, _ in with_index}


def test_check_closure_rules_matches_with_and_without_index_iter_open_wave() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01", status="closed")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01", status="closed")
    payload["waves"]["P01-I01-W01"] = _wave("P01-I01-W01", iter_id="P01-I01", status="in_progress")
    state = State.model_validate(payload)
    index = build_validation_index(state)

    without = _violation_tuples(list(check_closure_rules(state)))
    with_index = _violation_tuples(list(check_closure_rules(state, index)))

    assert without == with_index
    assert "INV.CLOSURE.ITER_HAS_OPEN_WAVE" in {code for code, _, _ in with_index}


def test_check_closure_rules_emission_order_preserved_with_index() -> None:
    """Multiple open iters under one closed phase must emit in State order."""
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01", status="closed")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01", status="active")
    payload["iters"]["P01-I02"] = _iter("P01-I02", phase_id="P01", status="planned")
    state = State.model_validate(payload)
    index = build_validation_index(state)

    without = _violation_tuples(list(check_closure_rules(state)))
    with_index = _violation_tuples(list(check_closure_rules(state, index)))

    assert without == with_index
    # Order follows iters dict insertion order: I01 before I02.
    paths = [path for _, path, _ in with_index]
    assert paths == ["/phases/P01", "/phases/P01"]
    messages = [msg for _, _, msg in with_index]
    assert "P01-I01" in messages[0]
    assert "P01-I02" in messages[1]


# ---- validate_state behavioural equivalence ---------------------------------


def _medium_payload() -> dict[str, Any]:
    """Return a multi-phase payload mixing clean and violating entities.

    Exercises both closure scans plus several other invariants so the
    equivalence assertion covers more than the refactored function.
    """
    payload = _base_state_payload()
    # Phase 1: closed, but holds an open iter (PHASE_HAS_OPEN_ITER) and a
    # closed iter that holds an open wave (ITER_HAS_OPEN_WAVE).
    payload["phases"]["P01"] = _phase("P01", status="closed")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01", status="active")
    payload["iters"]["P01-I02"] = _iter("P01-I02", phase_id="P01", status="closed")
    payload["waves"]["P01-I02-W01"] = _wave("P01-I02-W01", iter_id="P01-I02", status="in_progress")
    # Phase 2: fully clean and closed.
    payload["phases"]["P02"] = _phase("P02", status="closed")
    payload["iters"]["P02-I01"] = _iter("P02-I01", phase_id="P02", status="closed")
    payload["waves"]["P02-I01-W01"] = _wave("P02-I01-W01", iter_id="P02-I01", status="closed")
    # Phase 3: active with a healthy in-progress wave.
    payload["phases"]["P03"] = _phase("P03", status="active")
    payload["iters"]["P03-I01"] = _iter("P03-I01", phase_id="P03", status="active")
    payload["waves"]["P03-I01-W01"] = _wave("P03-I01-W01", iter_id="P03-I01", status="in_progress")
    return payload


def test_validate_state_equivalent_to_standalone_invariants() -> None:
    payload = _medium_payload()
    state = State.model_validate(payload)

    indexed = _violation_tuples(validate_state(payload).violations)
    standalone = _violation_tuples(_invariants_without_index(state))

    assert indexed == standalone


def test_validate_state_clean_payload_has_no_violations() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["waves"]["P01-I01-W01"] = _wave("P01-I01-W01", iter_id="P01-I01")
    payload["current"]["phase_id"] = "P01"
    payload["current"]["iter_id"] = "P01-I01"
    payload["current"]["active_wave_ids"] = ["P01-I01-W01"]

    report = validate_state(payload)

    assert report.ok
    assert report.violations == []


def test_validate_state_surfaces_both_closure_codes() -> None:
    payload = _medium_payload()
    report = validate_state(payload)
    codes = {v.code for v in report.violations}
    assert "INV.CLOSURE.PHASE_HAS_OPEN_ITER" in codes
    assert "INV.CLOSURE.ITER_HAS_OPEN_WAVE" in codes
