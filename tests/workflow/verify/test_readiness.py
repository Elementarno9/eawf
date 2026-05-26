"""Tests for :func:`eawf.workflow.verify.readiness.compute` (P28-I01-W06).

The wave's success criteria pinned by these tests:

* the compute function is **pure read-only** — calling it does not
  mutate the input ``state`` (sc #1, paired with the typed mypy
  return on the function signature),
* legacy-only waves render ``ready=True`` plus one advisory warning
  per legacy criterion (sc #4),
* spec-only waves with all gates pass render ``ready=True``,
* spec-only waves with one gate fail render ``ready=False`` and the
  criterion surfaces as ``status="fail"``,
* spec-only waves with one gate waived count the waiver but still
  roll up to ``status="pass"``,
* mixed legacy + spec waves combine both view sets.

The spec layer attaches CriterionSpec / GateSpec via the
:func:`eawf.workflow.verify.readiness._load_criterion_specs` /
:func:`~eawf.workflow.verify.readiness._load_gate_specs` helpers (today
they return ``[]`` because no state field carries typed specs yet).
Tests monkeypatch those helpers to inject synthetic specs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.enums import ProjectStatus, ScopeKind, StoreKind
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord, mint_evidence_id
from eawf.kernel.store.paths import store_dir as _store_dir
from eawf.workflow.lifecycle.transitions import (
    claim_wave,
    open_iter,
    open_phase,
    plan_wave,
)
from eawf.workflow.verify import readiness as readiness_mod
from eawf.workflow.verify.models import CloseReadiness

WAVE_ID = "P01-I01-W01"


# ---- Fixtures ---------------------------------------------------------------


def _empty_state() -> State:
    """Build a minimal valid State with no phases / waves seeded."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:VFY",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="VFY",
                slug="vfy",
                title="VFY",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:VFY",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="VFY").model_dump(mode="json"),
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


def _seed_wave(state: State, *, success_criteria: list[str] | None = None) -> None:
    """Seed P01 / P01-I01 / WAVE_ID into *state* with given criteria."""
    open_phase(state, phase_id="P01", title="phase")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="iter")
    plan_wave(
        state,
        wave_id=WAVE_ID,
        iter_id="P01-I01",
        title="wave",
        file_scopes=["src/"],
        success_criteria=success_criteria or [],
    )
    claim_wave(state, wave_id=WAVE_ID, session_id="SES-1")


def _write_evidence_row(store_dir: Path, *, record: EvidenceRecord) -> None:
    """Append one :class:`EvidenceRecord` envelope to ``evidence.jsonl``."""
    store_dir.mkdir(parents=True, exist_ok=True)
    envelope = Envelope(
        id=record.id,
        kind=StoreKind.EVIDENCE,
        scope_id=record.scope_id,
        created_at=record.created_at,
        summary=record.summary,
        payload=record.model_dump(mode="json"),
    )
    evidence_path = store_dir / f"{StoreKind.EVIDENCE.value}.jsonl"
    with evidence_path.open("a", encoding="utf-8") as fh:
        fh.write(envelope.model_dump_json() + "\n")


def _make_criterion(
    cid: str, *, gate_ids: list[str] | None = None, waiver_reason: str | None = None
) -> CriterionSpec:
    return CriterionSpec(
        id=cid,
        text=f"criterion {cid}",
        kind="behavior",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=list(gate_ids or []),
        required=True,
        waiver_reason=waiver_reason,
    )


def _make_gate(gid: str, *, criterion_id: str) -> GateSpec:
    return GateSpec(
        id=gid,
        criterion_id=criterion_id,
        kind="command_exit_zero",
        args={},
        policy="block",
        cadence="every-wave",
        required=True,
    )


def _make_evidence_record(
    *,
    scope_id: str,
    status: str,
    refs: list[str],
) -> EvidenceRecord:
    return EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=scope_id,
        produced_by="tool",
        evidence_kind="deterministic",
        status=status,  # type: ignore[arg-type]
        summary=f"{status} evidence for {scope_id} refs={refs}",
        refs=list(refs),
        created_at=datetime.now(UTC),
    )


# ---- Tests ------------------------------------------------------------------


def test_compute_is_pure_read_only(tmp_path: Path) -> None:
    """``compute`` does not mutate the input state (sc #1)."""
    state = _empty_state()
    _seed_wave(state, success_criteria=["legacy one"])
    before_snapshot = state.model_dump(mode="json")

    state_path = tmp_path / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(before_snapshot))
    store_dir = _store_dir(state_path)

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert isinstance(result, CloseReadiness)
    after_snapshot = state.model_dump(mode="json")
    assert after_snapshot == before_snapshot, "compute mutated the input state"


def test_legacy_only_wave_renders_ready_with_warnings(tmp_path: Path) -> None:
    """Legacy success_criteria render ``ready=True`` plus one warning each (sc #4)."""
    state = _empty_state()
    _seed_wave(state, success_criteria=["legacy a", "legacy b"])
    store_dir = _store_dir(tmp_path / "state.json")

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    assert len(result.criteria) == 2
    assert all(view.source == "legacy" for view in result.criteria)
    assert all(view.status == "pass" for view in result.criteria)
    assert all(view.gate_results is None for view in result.criteria)
    # One warning per legacy criterion.
    assert len(result.warnings) == 2
    assert all("not gated" in warning for warning in result.warnings)
    assert result.waived_gate_ids == []


def test_empty_wave_renders_ready_with_no_criteria_warning(tmp_path: Path) -> None:
    """A wave with neither legacy nor spec criteria still closes; advisory fires."""
    state = _empty_state()
    _seed_wave(state, success_criteria=[])
    store_dir = _store_dir(tmp_path / "state.json")

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    assert result.criteria == []
    assert result.warnings == ["no criteria attached to wave"]


def test_spec_wave_all_gates_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec wave with every gate ``pass`` rolls up to ``ready=True``."""
    state = _empty_state()
    _seed_wave(state)
    state_path = tmp_path / "state.json"
    store_dir = _store_dir(state_path)

    criterion = _make_criterion("CRIT-pass", gate_ids=["GATE-1"])
    gate = _make_gate("GATE-1", criterion_id="CRIT-pass")

    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])
    _write_evidence_row(
        store_dir,
        record=_make_evidence_record(scope_id=WAVE_ID, status="pass", refs=["GATE-1"]),
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    spec_views = [v for v in result.criteria if v.source == "spec"]
    assert len(spec_views) == 1
    view = spec_views[0]
    assert view.id == "CRIT-pass"
    assert view.status == "pass"
    assert view.gate_results is not None
    assert len(view.gate_results) == 1
    assert view.gate_results[0].gate_id == "GATE-1"
    assert view.gate_results[0].status == "pass"
    assert result.waived_gate_ids == []


def test_spec_wave_one_gate_fail_flips_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failing gate flips the criterion to ``fail`` and ``ready=False``."""
    state = _empty_state()
    _seed_wave(state)
    state_path = tmp_path / "state.json"
    store_dir = _store_dir(state_path)

    criterion = _make_criterion("CRIT-fail", gate_ids=["GATE-fail"])
    gate = _make_gate("GATE-fail", criterion_id="CRIT-fail")

    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])
    _write_evidence_row(
        store_dir,
        record=_make_evidence_record(scope_id=WAVE_ID, status="fail", refs=["GATE-fail"]),
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is False
    fail_view = next(v for v in result.criteria if v.id == "CRIT-fail")
    assert fail_view.status == "fail"
    assert fail_view.gate_results is not None
    assert fail_view.gate_results[0].status == "fail"


def test_spec_wave_waived_gate_passes_and_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waived gate flips the criterion to ``pass`` + records the waived id."""
    state = _empty_state()
    _seed_wave(state)
    state_path = tmp_path / "state.json"
    store_dir = _store_dir(state_path)

    criterion = _make_criterion("CRIT-waived", gate_ids=["GATE-waived"])
    gate = _make_gate("GATE-waived", criterion_id="CRIT-waived")

    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])
    _write_evidence_row(
        store_dir,
        record=_make_evidence_record(scope_id=WAVE_ID, status="waived", refs=["GATE-waived"]),
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    waived_view = next(v for v in result.criteria if v.id == "CRIT-waived")
    assert waived_view.status == "pass"
    assert waived_view.gate_results is not None
    assert waived_view.gate_results[0].status == "pass"
    assert result.waived_gate_ids == ["GATE-waived"]


def test_mixed_legacy_and_spec_combine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both view families surface together on a mixed wave."""
    state = _empty_state()
    _seed_wave(state, success_criteria=["legacy mix"])
    state_path = tmp_path / "state.json"
    store_dir = _store_dir(state_path)

    criterion = _make_criterion("CRIT-mix", gate_ids=["GATE-mix"])
    gate = _make_gate("GATE-mix", criterion_id="CRIT-mix")

    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])
    _write_evidence_row(
        store_dir,
        record=_make_evidence_record(scope_id=WAVE_ID, status="pass", refs=["GATE-mix"]),
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    sources = sorted({view.source for view in result.criteria})
    assert sources == ["legacy", "spec"]
    # One legacy + one spec criterion + one legacy warning.
    assert len(result.criteria) == 2
    assert len(result.warnings) == 1
    assert "not gated" in result.warnings[0]


def test_compute_unknown_wave_raises_key_error(tmp_path: Path) -> None:
    """An unknown scope_id raises KeyError with the canonical phrasing."""
    state = _empty_state()
    store_dir = _store_dir(tmp_path / "state.json")

    with pytest.raises(KeyError, match="unknown wave"):
        readiness_mod.compute(
            "P99-I99-W99",
            state=state,
            store_dir=store_dir,
            repo_root=tmp_path,
        )


def test_evidence_for_other_scope_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An evidence row for another scope does not score this wave's gate."""
    state = _empty_state()
    _seed_wave(state)
    state_path = tmp_path / "state.json"
    store_dir = _store_dir(state_path)

    criterion = _make_criterion("CRIT-iso", gate_ids=["GATE-iso"])
    gate = _make_gate("GATE-iso", criterion_id="CRIT-iso")

    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])
    # Evidence for a DIFFERENT scope.
    _write_evidence_row(
        store_dir,
        record=_make_evidence_record(scope_id="P02-I01-W01", status="pass", refs=["GATE-iso"]),
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    # No matching evidence => gate is blocked => criterion is blocked =>
    # ready=False.
    assert result.ready is False
    view = next(v for v in result.criteria if v.id == "CRIT-iso")
    assert view.status == "blocked"
    assert view.gate_results is not None
    assert view.gate_results[0].status == "blocked"


def test_idempotent_on_same_inputs(tmp_path: Path) -> None:
    """Two calls with the same inputs return equal results (idempotency)."""
    state = _empty_state()
    _seed_wave(state, success_criteria=["legacy x"])
    store_dir = _store_dir(tmp_path / "state.json")

    first = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)
    second = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert first.model_dump() == second.model_dump()


def test_advisory_failure_returns_not_ready_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wave with a failing synthetic CriterionView still returns cleanly — sc #6.

    Pure-function pin: a non-ready readiness does NOT raise from
    :func:`compute`. The seam-level pin that the close path never
    blocks on a non-ready readiness lives in
    :mod:`tests.workflow.verify.test_seams`; here we assert the
    compute boundary returns ``ready=False`` cleanly so the caller
    is free to ignore it.
    """
    state = _empty_state()
    _seed_wave(state)
    store_dir = _store_dir(tmp_path / "state.json")

    # Force a spec-source view by patching the load helpers — a
    # criterion with no gates + required=True rolls up to pending.
    criterion = _make_criterion("CRIT-pending")
    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [])

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is False
    pending_view = next(v for v in result.criteria if v.id == "CRIT-pending")
    assert pending_view.status == "pending"
