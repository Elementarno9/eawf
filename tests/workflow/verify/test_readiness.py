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

The W06-era tests use ``evidence_kind="jury"`` on the criterion fixture
because they exercise the evidence-row scoring path. W08 introduces the
deterministic floor (``evidence_kind="deterministic"`` -> live
compile_gate + run_checks); those integration tests live below at
:func:`test_deterministic_floor_*` and use the
:func:`_make_deterministic_criterion` fixture which sets the live-run
opt-in explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec, QualityDimension
from eawf.kernel.state.enums import ProjectStatus, ScopeKind, StoreKind, WaveStatus
from eawf.kernel.state.models import CurrentPointers, Project, State, Wave
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord, mint_evidence_id
from eawf.kernel.store.paths import store_dir as _store_dir
from eawf.platform.profiles.models import FloorCheck, VerifyBlock
from eawf.workflow.audit_dsl import CheckSpec
from eawf.workflow.audit_dsl.kinds.transition_coverage import (
    built_states,
    check_transition_coverage,
    table_edges,
)
from eawf.workflow.lifecycle.transitions import (
    open_iter,
    open_phase,
    plan_wave,
)
from eawf.workflow.verify import readiness as readiness_mod
from eawf.workflow.verify.models import CloseReadiness
from tests._criteria_helpers import legacy_criteria
from tests._session_helpers import (
    claim_wave_with_session as claim_wave,
)
from tests._session_helpers import (
    seed_active_session_on_disk,
)
from tests.conftest import make_claim_criterion, make_floor_waiver, make_intent

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
    """Seed P01 / P01-I01 / WAVE_ID into *state* with given criteria.

    The string list is wrapped into grandfathered :class:`CriterionSpec` rows
    (the typed shape :attr:`Wave.success_criteria` now requires) so the seeded
    criteria mirror what the ``1.6 -> 1.7`` migration produces.
    """
    historical_criteria = legacy_criteria(*(success_criteria or []))
    open_phase(state, phase_id="P01", title="phase")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="iter")
    plan_wave(
        state,
        wave_id=WAVE_ID,
        iter_id="P01-I01",
        title="wave",
        file_scopes=["src/"],
        success_criteria=[make_claim_criterion()],
        effort_bucket="M",
        intent=make_intent(),
    )
    claim_wave(state, wave_id=WAVE_ID, session_id="SES-1")
    state.waves[WAVE_ID].success_criteria = historical_criteria
    state.waves[WAVE_ID].criteria_floor_waiver = make_floor_waiver()


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
    cid: str,
    *,
    gate_ids: list[str] | None = None,
    waiver_reason: str | None = None,
    evidence_kind: str = "jury",
    required: bool = True,
) -> CriterionSpec:
    """Build a synthetic CriterionSpec for the W06 evidence-row tests.

    Defaults ``evidence_kind="jury"`` so the criterion routes through
    the W06 evidence-row scoring path. The W08 deterministic floor is
    tested via :func:`_make_deterministic_criterion` further down so
    the live-run opt-in stays explicit at the call site.
    """
    return CriterionSpec(
        id=cid,
        text=f"criterion {cid}",
        kind="behavior",
        acceptance_style="binary",
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        gate_ids=list(gate_ids or []),
        required=required,
        waiver_reason=waiver_reason,
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="the W06 readiness compute resolves an evidence row for this criterion",
    )


def _make_gate(gid: str, *, criterion_id: str) -> GateSpec:
    # The ``command_exit_zero`` kind requires ``args['argv']`` per the
    # L0 argv-policy validator landed by W09 (defense-in-depth at the
    # spec layer). Use a benign allow-listed argv ``uv run pytest -q``.
    return GateSpec(
        id=gid,
        criterion_id=criterion_id,
        kind="command_exit_zero",
        args={"argv": ["uv", "run", "pytest", "-q"]},
        policy="block",
        cadence="every-wave",
        required=True,
    )


def _make_evidence_record(
    *,
    scope_id: str,
    status: str,
    refs: list[str],
    metrics: dict[str, str] | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=scope_id,
        produced_by="tool",
        evidence_kind="deterministic",
        status=status,  # type: ignore[arg-type]
        summary=f"{status} evidence for {scope_id} refs={refs}",
        refs=list(refs),
        metrics=metrics,
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


# ---- Direct loader pins (W02: un-idle _load_gate_specs) ---------------------


def test_load_gate_specs_reads_wave_gates_without_monkeypatch() -> None:
    """``_load_gate_specs`` returns the wave's typed ``gates`` rows directly.

    Pins the W02 un-idle: the loader reads
    :attr:`~eawf.kernel.state.models.Wave.gates` from the state row with no
    test monkeypatching, mirroring :func:`_load_criterion_specs`.
    """
    state = _empty_state()
    _seed_wave(state)
    gate = _make_gate("GATE-direct", criterion_id="CRIT-direct")
    state.waves[WAVE_ID].gates = [gate]

    loaded = readiness_mod._load_gate_specs(WAVE_ID, state)

    assert [g.id for g in loaded] == ["GATE-direct"]
    assert loaded[0] is gate
    # The loader returns a fresh list (not the underlying field) so callers
    # cannot mutate the wave row through the return value.
    assert loaded is not state.waves[WAVE_ID].gates


def test_load_gate_specs_returns_empty_for_wave_without_gates() -> None:
    """A seeded wave with no gates resolves to ``[]`` (default-empty field)."""
    state = _empty_state()
    _seed_wave(state)

    assert readiness_mod._load_gate_specs(WAVE_ID, state) == []


def test_load_gate_specs_returns_empty_for_unknown_scope() -> None:
    """A non-wave / unknown scope id resolves to ``[]`` rather than raising."""
    state = _empty_state()

    assert readiness_mod._load_gate_specs("P99-I99-W99", state) == []


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


def test_load_active_waiver_mode_disabled_is_absorbing_across_composition(
    tmp_path: Path,
) -> None:
    """A permissive repo overlay cannot weaken a disabled enabled profile."""
    profile_dir = tmp_path / ".ea" / "profiles"
    profile_dir.mkdir(parents=True)
    (tmp_path / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled: [permissive, locked]\nverify:\n  waiver_mode: A\n",
        encoding="utf-8",
    )
    profile_dir.joinpath("permissive.yaml").write_text(
        "name: permissive\nverify:\n  waiver_mode: C\n",
        encoding="utf-8",
    )
    profile_dir.joinpath("locked.yaml").write_text(
        "name: locked\nverify:\n  waiver_mode: disabled\n",
        encoding="utf-8",
    )
    state = _empty_state()

    mode = readiness_mod.load_active_waiver_mode(
        WAVE_ID,
        state,
        repo_root=tmp_path,
        config_root=tmp_path,
    )

    assert mode == "disabled"


def test_readiness_does_not_fall_back_on_invalid_verify_config(tmp_path: Path) -> None:
    """Malformed strict policy config rejects instead of defaulting to mode B."""
    from pydantic import ValidationError

    config_dir = tmp_path / ".ea"
    config_dir.mkdir()
    config_dir.joinpath("config.yaml").write_text(
        "verify:\n  waiver_mode: disabled\n  waiver_mod: A\n",
        encoding="utf-8",
    )
    state = _empty_state()
    _seed_wave(state)
    before = state.model_dump_json()

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        readiness_mod.compute(
            WAVE_ID,
            state=state,
            store_dir=_store_dir(tmp_path / "state.json"),
            repo_root=tmp_path,
            config_root=tmp_path,
        )

    assert state.model_dump_json() == before


@pytest.mark.parametrize(
    ("config_body", "message"),
    [
        ("profiles: disabled\n", "profiles config must be a mapping"),
        ("profiles:\n  enabled: disabled\n", "profiles.enabled must be a list"),
        (
            "profiles:\n  enabled: [7]\n",
            "profiles.enabled entries must be strings",
        ),
    ],
)
def test_load_active_waiver_mode_rejects_invalid_profile_shape(
    tmp_path: Path,
    config_body: str,
    message: str,
) -> None:
    """Malformed profile selection cannot silently fall back to mode B."""
    config_dir = tmp_path / ".ea"
    config_dir.mkdir()
    config_dir.joinpath("config.yaml").write_text(config_body, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        readiness_mod.load_active_waiver_mode(
            WAVE_ID,
            _empty_state(),
            repo_root=tmp_path,
            config_root=tmp_path,
        )


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


def test_non_required_criterion_failure_does_not_block_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An advisory (``required=False``) criterion that fails does NOT flip ready.

    A required criterion that passes plus a non-required criterion that fails
    must leave ``ready=True``: the advisory criterion is surfaced in the view
    (status fail) but never blocks the close, matching the oracle path's
    skip of non-required criteria. This is the cadence="ship" pixel-diff case.
    """
    state = _empty_state()
    _seed_wave(state)
    state_path = tmp_path / "state.json"
    store_dir = _store_dir(state_path)

    req = _make_criterion("CRIT-req", gate_ids=["GATE-req"], required=True)
    adv = _make_criterion("CRIT-adv", gate_ids=["GATE-adv"], required=False)
    gate_req = _make_gate("GATE-req", criterion_id="CRIT-req")
    gate_adv = _make_gate("GATE-adv", criterion_id="CRIT-adv")

    monkeypatch.setattr(
        readiness_mod, "_load_criterion_specs", lambda scope_id, state_arg: [req, adv]
    )
    monkeypatch.setattr(
        readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate_req, gate_adv]
    )
    _write_evidence_row(
        store_dir,
        record=_make_evidence_record(scope_id=WAVE_ID, status="pass", refs=["GATE-req"]),
    )
    _write_evidence_row(
        store_dir,
        record=_make_evidence_record(scope_id=WAVE_ID, status="fail", refs=["GATE-adv"]),
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    adv_view = next(v for v in result.criteria if v.id == "CRIT-adv")
    assert adv_view.status == "fail"
    assert adv_view.required is False


def test_spec_wave_waived_gate_passes_and_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waived gate flips the criterion to ``waived`` + records the waived id (W11).

    W11 contract: when every gate on a criterion is waived, the
    rolled-up criterion status is ``waived`` (not ``pass``) so
    renderers can flag operator overrides explicitly. The per-gate
    :class:`GateResult.status` still surfaces ``pass`` because the
    gate is treated as satisfied for ``ready`` rollup.
    """
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
    monkeypatch.setattr(
        readiness_mod,
        "derive_wave_sha",
        lambda scope_id, repo_root=None: "waived_sha_123",
    )
    _write_evidence_row(
        store_dir,
        record=_make_evidence_record(
            scope_id=WAVE_ID,
            status="waived",
            refs=["GATE-waived"],
            metrics={"wave_sha": "waived_sha_123"},
        ),
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    waived_view = next(v for v in result.criteria if v.id == "CRIT-waived")
    assert waived_view.status == "waived"
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


def test_sha_stale_waiver_is_filtered_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A waiver whose stamped ``wave_sha`` no longer matches is ignored (W11 sc #6).

    SHA-bound freshness contract: each waiver carries the wave's
    commit SHA in ``metrics['wave_sha']``. When the wave advances to
    a new SHA, prior waivers are stale and MUST NOT contribute to
    ``waived_gate_ids`` (the gate goes back to ``blocked`` because no
    fresh evidence references it).
    """
    state = _empty_state()
    _seed_wave(state)
    state_path = tmp_path / "state.json"
    store_dir = _store_dir(state_path)

    criterion = _make_criterion("CRIT-stale", gate_ids=["GATE-stale"])
    gate = _make_gate("GATE-stale", criterion_id="CRIT-stale")

    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])

    # Pin the "current" wave sha to a known value via monkeypatching
    # derive_wave_sha so the test is hermetic against the git repo
    # under tmp_path.
    monkeypatch.setattr(
        readiness_mod,
        "derive_wave_sha",
        lambda scope_id, repo_root=None: "current_sha_xyz",
    )

    # Write a waiver row stamped with an OUTDATED wave sha.
    stale = EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=WAVE_ID,
        produced_by="human",
        evidence_kind="attested",
        status="waived",
        summary="stale waiver",
        refs=["GATE-stale"],
        metrics={"wave_sha": "old_sha_abc"},
        created_at=datetime.now(UTC),
    )
    _write_evidence_row(store_dir, record=stale)

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    # The stale waiver was filtered; gate has no fresh evidence;
    # rolled-up to blocked + ready=False.
    assert result.ready is False
    assert result.waived_gate_ids == []
    view = next(v for v in result.criteria if v.id == "CRIT-stale")
    assert view.status == "blocked"
    assert view.gate_results is not None
    assert view.gate_results[0].status == "blocked"


def test_sha_matching_waiver_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A waiver whose stamped ``wave_sha`` matches counts as fresh (W11)."""
    state = _empty_state()
    _seed_wave(state)
    state_path = tmp_path / "state.json"
    store_dir = _store_dir(state_path)

    criterion = _make_criterion("CRIT-fresh", gate_ids=["GATE-fresh"])
    gate = _make_gate("GATE-fresh", criterion_id="CRIT-fresh")

    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])
    monkeypatch.setattr(
        readiness_mod,
        "derive_wave_sha",
        lambda scope_id, repo_root=None: "fresh_sha_999",
    )

    fresh = EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=WAVE_ID,
        produced_by="human",
        evidence_kind="attested",
        status="waived",
        summary="fresh waiver",
        refs=["GATE-fresh"],
        metrics={"wave_sha": "fresh_sha_999"},
        created_at=datetime.now(UTC),
    )
    _write_evidence_row(store_dir, record=fresh)

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    assert result.waived_gate_ids == ["GATE-fresh"]
    view = next(v for v in result.criteria if v.id == "CRIT-fresh")
    assert view.status == "waived"


@pytest.mark.parametrize("mode", ["A", "B", "C"])
@pytest.mark.parametrize("metrics", [None, {}])
def test_waiver_without_metrics_or_evidence_sha_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    metrics: dict[str, str] | None,
) -> None:
    """A/B/C reject absent metrics and metrics missing ``wave_sha``."""
    state = _empty_state()
    _seed_wave(state)
    state_path = tmp_path / "state.json"
    store_dir = _store_dir(state_path)

    criterion = _make_criterion("CRIT-nostamp", gate_ids=["GATE-nostamp"])
    gate = _make_gate("GATE-nostamp", criterion_id="CRIT-nostamp")
    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])
    monkeypatch.setattr(
        readiness_mod,
        "derive_wave_sha",
        lambda scope_id, repo_root=None: "any_sha_111",
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda *_a, **_k: VerifyBlock(waiver_mode=mode),
    )

    unstamped = EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=WAVE_ID,
        produced_by="human",
        evidence_kind="attested",
        status="waived",
        summary="unstamped waiver",
        refs=["GATE-nostamp"],
        metrics=metrics,
        created_at=datetime.now(UTC),
    )
    _write_evidence_row(store_dir, record=unstamped)

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is False
    assert result.waived_gate_ids == []


@pytest.mark.parametrize("mode", ["A", "B", "C"])
def test_waiver_without_current_sha_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """A/B/C stale a stamped waiver when current SHA cannot be derived."""
    state = _empty_state()
    _seed_wave(state)
    store_dir = _store_dir(tmp_path / "state.json")
    criterion = _make_criterion("CRIT-no-current", gate_ids=["GATE-no-current"])
    gate = _make_gate("GATE-no-current", criterion_id=criterion.id)
    monkeypatch.setattr(readiness_mod, "_load_criterion_specs", lambda *_a: [criterion])
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda *_a: [gate])
    monkeypatch.setattr(readiness_mod, "derive_wave_sha", lambda *_a, **_k: None)
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda *_a, **_k: VerifyBlock(waiver_mode=mode),
    )
    _write_evidence_row(
        store_dir,
        record=_make_evidence_record(
            scope_id=WAVE_ID,
            status="waived",
            refs=[gate.id],
            metrics={"wave_sha": "stamped_sha_123"},
        ),
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is False
    assert result.waived_gate_ids == []


def test_disabled_mode_ignores_historical_waiver_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Historical waived rows remain stored but cannot satisfy readiness."""
    state = _empty_state()
    _seed_wave(state)
    state.waves[WAVE_ID].criteria_floor_waiver = None
    store_dir = _store_dir(tmp_path / "state.json")
    criterion = _make_criterion("CRIT-disabled", gate_ids=["GATE-disabled"])
    gate = _make_gate("GATE-disabled", criterion_id=criterion.id)
    monkeypatch.setattr(readiness_mod, "_load_criterion_specs", lambda *_a: [criterion])
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda *_a: [gate])
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda *_a, **_k: VerifyBlock(waiver_mode="disabled"),
    )
    monkeypatch.setattr(readiness_mod, "derive_wave_sha", lambda *_a, **_k: "same_sha_123")
    _write_evidence_row(
        store_dir,
        record=_make_evidence_record(
            scope_id=WAVE_ID,
            status="waived",
            refs=[gate.id],
            metrics={"wave_sha": "same_sha_123"},
        ),
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is False
    assert result.waived_gate_ids == []
    assert result.criteria[0].status == "blocked"


@pytest.mark.parametrize("mode", ["A", "B", "C"])
def test_raw_criterion_waiver_remains_compatible_in_permissive_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """A/B/C retain legacy raw ``waiver_reason`` readiness semantics."""
    state = _empty_state()
    _seed_wave(state)
    state.waves[WAVE_ID].criteria_floor_waiver = None
    criterion = _make_criterion("CRIT-raw", waiver_reason="historical operator waiver")
    state.waves[WAVE_ID].success_criteria = [criterion]
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda *_a, **_k: VerifyBlock(waiver_mode=mode),
    )

    result = readiness_mod.compute(
        WAVE_ID,
        state=state,
        store_dir=_store_dir(tmp_path / "state.json"),
        repo_root=tmp_path,
    )

    assert result.ready is True
    assert result.criteria[0].status == "waived"


def test_disabled_mode_rejects_historical_raw_criterion_waiver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabled mode rejects raw reasons regardless of row age."""
    from eawf.workflow.lifecycle._errors import LifecycleGuardError

    state = _empty_state()
    _seed_wave(state)
    state.waves[WAVE_ID].criteria_floor_waiver = None
    state.waves[WAVE_ID].success_criteria = [
        _make_criterion("CRIT-raw", waiver_reason="historical operator waiver")
    ]
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda *_a, **_k: VerifyBlock(waiver_mode="disabled"),
    )

    with pytest.raises(LifecycleGuardError) as raised:
        readiness_mod.compute(
            WAVE_ID,
            state=state,
            store_dir=_store_dir(tmp_path / "state.json"),
            repo_root=tmp_path,
        )
    assert raised.value.code == "waiver_mode_disabled"


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


# ---- W08 deterministic-floor integration ----------------------------------


def _make_deterministic_criterion(cid: str, *, gate_ids: list[str] | None = None) -> CriterionSpec:
    """Build a deterministic CriterionSpec — opts into the W08 live-run floor."""
    return _make_criterion(cid, gate_ids=gate_ids, evidence_kind="deterministic")


def _make_command_gate(
    gid: str,
    *,
    criterion_id: str,
    argv: list[str],
) -> GateSpec:
    """Build a ``command_exit_zero`` GateSpec with *argv*.

    Argv heads must satisfy the L0 argv-policy
    (:func:`eawf.runtime.sandbox.argv_policy.validate_gate_argv`) per
    W09 — practical W08 tests use ``["git", <subverb>, ...]`` where the
    subverb is in :data:`eawf.runtime.sandbox.argv_policy.GIT_ALLOWED_SUBVERBS`
    (read-only inspection only).
    """
    return GateSpec(
        id=gid,
        criterion_id=criterion_id,
        kind="command_exit_zero",
        args={"argv": list(argv)},
        policy="block",
        cadence="every-wave",
        required=True,
    )


def _init_test_repo(repo_root: Path) -> None:
    """Initialise *repo_root* as a minimal git repo with one commit.

    The W08 deterministic floor invokes the W15-hardened runner which
    in turn calls :func:`eawf.workflow.lifecycle.wave_sha.derive_diff_base`
    + :func:`eawf.platform.lint._conditional.changed_files`; both expect
    a real git tree under *repo_root*. The seed commit + ``main``
    branch lets ``git merge-base HEAD main`` succeed so the runner
    does not spend time chasing a fail-open fallback.
    """
    import subprocess

    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo_root), "init", "-q", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo_root), "config", "user.name", "test"], check=True)
    (repo_root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "seed"], check=True)


def test_deterministic_floor_passing_gate_yields_pass_criterion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC #2: deterministic gate runs end-to-end + lands as pass.

    Uses ``["git", "status", "--porcelain"]`` — a read-only argv
    allowed by the L0 policy and reliably exit-zero against the
    seeded test repo. The result must surface as
    ``CriterionView(status="pass", gate_results=[GateResult(status="pass")])``
    with no mocks on the runner.
    """
    state = _empty_state()
    _seed_wave(state)
    _init_test_repo(tmp_path)
    store_dir = _store_dir(tmp_path / "state.json")

    criterion = _make_deterministic_criterion("CRIT-det-pass", gate_ids=["GATE-det-pass"])
    gate = _make_command_gate(
        "GATE-det-pass",
        criterion_id="CRIT-det-pass",
        argv=["git", "status", "--porcelain"],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    view = next(v for v in result.criteria if v.id == "CRIT-det-pass")
    assert view.source == "spec"
    assert view.status == "pass"
    assert view.gate_results is not None
    assert len(view.gate_results) == 1
    assert view.gate_results[0].gate_id == "GATE-det-pass"
    assert view.gate_results[0].status == "pass"
    assert result.waived_gate_ids == []


def test_deterministic_floor_failing_gate_yields_fail_criterion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live failing deterministic gate -> ``CriterionView(status='fail')``.

    Uses ``["git", "show", "nonexistent-ref-xyz"]`` — passes the L0
    policy (``show`` is in
    :data:`~eawf.runtime.sandbox.argv_policy.GIT_ALLOWED_SUBVERBS`)
    and reliably exits non-zero (git surfaces a "bad revision" error).
    The criterion rolls up to ``fail`` and ``ready=False``.
    """
    state = _empty_state()
    _seed_wave(state)
    _init_test_repo(tmp_path)
    store_dir = _store_dir(tmp_path / "state.json")

    criterion = _make_deterministic_criterion("CRIT-det-fail", gate_ids=["GATE-det-fail"])
    gate = _make_command_gate(
        "GATE-det-fail",
        criterion_id="CRIT-det-fail",
        argv=["git", "show", "nonexistent-ref-xyz-w08"],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is False
    view = next(v for v in result.criteria if v.id == "CRIT-det-fail")
    assert view.status == "fail"
    assert view.gate_results is not None
    assert view.gate_results[0].status == "fail"


def test_deterministic_floor_waiver_preempts_live_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC #6: fresh waiver on a deterministic gate overrides the live run.

    The gate's argv would fail (``git show <bad-ref>``) BUT the
    waiver evidence row pre-empts the live invocation. The criterion
    rolls up to ``waived`` and the gate id appears in
    ``waived_gate_ids``. Cross-pins W11's waiver semantics on top of
    the W08 deterministic floor.
    """
    state = _empty_state()
    _seed_wave(state)
    _init_test_repo(tmp_path)
    store_dir = _store_dir(tmp_path / "state.json")

    criterion = _make_deterministic_criterion("CRIT-det-waived", gate_ids=["GATE-det-waived"])
    gate = _make_command_gate(
        "GATE-det-waived",
        criterion_id="CRIT-det-waived",
        # Same failing argv as the fail test — if the live run fires,
        # the assertion below would see status="fail" instead of
        # "pass"+waived.
        argv=["git", "show", "would-fail-but-waived"],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])
    # Pin the wave sha so the waiver passes the SHA-bound freshness
    # filter (W11) without touching the git tree.
    monkeypatch.setattr(
        readiness_mod,
        "derive_wave_sha",
        lambda scope_id, repo_root=None: "fresh_sha_for_w08",
    )

    waiver = EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=WAVE_ID,
        produced_by="human",
        evidence_kind="attested",
        status="waived",
        summary="operator waived the deterministic gate",
        refs=["GATE-det-waived"],
        metrics={"wave_sha": "fresh_sha_for_w08"},
        created_at=datetime.now(UTC),
    )
    _write_evidence_row(store_dir, record=waiver)

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    view = next(v for v in result.criteria if v.id == "CRIT-det-waived")
    assert view.status == "waived"
    assert view.gate_results is not None
    assert view.gate_results[0].status == "pass"
    assert result.waived_gate_ids == ["GATE-det-waived"]


def test_waived_floor_check_skips_run_and_surfaces_waived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh waiver on a profile FLOOR check skips its subprocess.

    Regression for the close-path daemon hang: the wave-close floor pack
    shells out to ``pytest -q`` / ``pre-commit`` / ``mypy``. An operator
    ``--waive <check>`` MUST suppress the run -- otherwise the full
    suite executes synchronously under the close gate (the daemon-hang
    root cause). The floor cmd here would exit non-zero if it ran
    (``git show <bad-ref>``); the waiver makes the criterion roll up to
    ``waived`` + ``ready=True``, proving the subprocess was skipped.
    Mirrors :func:`test_deterministic_floor_waiver_preempts_live_run`
    for the floor (no-typed-criteria) path.
    """
    state = _empty_state()
    _seed_wave(state)  # no typed criteria -> the profile floor pack renders
    store_dir = _store_dir(tmp_path / "state.json")

    block = VerifyBlock(
        enforce=True,
        argv_allowlist=[],
        floor_checks=[
            FloorCheck(
                name="floor-pytest",
                # Would exit non-zero if it ran; the waiver pre-empts it.
                cmd=["git", "show", "would-fail-but-waived-floor"],
                scope="all",
                cadence="every-wave",
                policy="warn",
            )
        ],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda scope_id, state_arg, **kwargs: block,
    )
    # Pin the wave sha so the waiver passes the SHA-bound freshness
    # filter (W11) without touching the git tree.
    monkeypatch.setattr(
        readiness_mod,
        "derive_wave_sha",
        lambda scope_id, repo_root=None: "fresh_sha_for_floor",
    )

    waiver = EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=WAVE_ID,
        produced_by="human",
        evidence_kind="attested",
        status="waived",
        summary="operator waived the floor pytest check",
        refs=["floor-pytest"],
        metrics={"wave_sha": "fresh_sha_for_floor"},
        created_at=datetime.now(UTC),
    )
    _write_evidence_row(store_dir, record=waiver)

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    view = next(v for v in result.criteria if v.id == "floor-pytest")
    assert view.source == "floor"
    assert view.status == "waived"
    assert view.gate_results is not None
    assert view.gate_results[0].status == "pass"


def test_deterministic_floor_compile_none_yields_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deterministic gate whose compile_gate returns None -> ``blocked``.

    Patches compile_gate to return ``None`` (the defensive branch the
    readiness loader cannot trip via construction without bypassing
    Pydantic). The gate result lands as ``blocked``, the criterion
    as ``blocked``, and ``ready=False`` — pure-function pin: the
    boundary still returns cleanly.
    """
    from eawf.workflow.verify import readiness as readiness_mod_inner

    state = _empty_state()
    _seed_wave(state)
    store_dir = _store_dir(tmp_path / "state.json")

    criterion = _make_deterministic_criterion("CRIT-det-blocked", gate_ids=["GATE-det-blocked"])
    gate = _make_command_gate(
        "GATE-det-blocked",
        criterion_id="CRIT-det-blocked",
        argv=["git", "status"],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])
    monkeypatch.setattr(readiness_mod_inner, "compile_gate", lambda gate_arg, *, criterion: None)

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is False
    view = next(v for v in result.criteria if v.id == "CRIT-det-blocked")
    assert view.status == "blocked"
    assert view.gate_results is not None
    assert view.gate_results[0].status == "blocked"


def test_transition_coverage_blocks_on_missing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing built FSM state fails the gate and blocks close readiness."""
    state = _empty_state()
    _seed_wave(state)
    store_dir = _store_dir(tmp_path / "state.json")

    missing = WaveStatus.ABANDONED.value
    args = {
        "table": "wave",
        "built_states": sorted(built_states("wave") - {missing}),
        "covered_edges": [list(edge) for edge in table_edges("wave")],
    }
    check = CheckSpec(kind="transition_coverage", name="GATE-missing-state", args=args)
    check_result = check_transition_coverage(check, tmp_path)
    assert check_result.status == "fail"
    assert check_result.details is not None
    assert missing in check_result.details

    criterion = _make_deterministic_criterion(
        "CRIT-missing-state",
        gate_ids=["GATE-missing-state"],
    )
    gate = GateSpec(
        id="GATE-missing-state",
        criterion_id="CRIT-missing-state",
        kind="transition_coverage",
        args=args,
        policy="block",
        cadence="every-wave",
        required=True,
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is False
    view = next(v for v in result.criteria if v.id == "CRIT-missing-state")
    assert view.status == "fail"
    assert view.gate_results is not None
    assert view.gate_results[0].status == "fail"


def test_legacy_path_unchanged_under_w08(tmp_path: Path) -> None:
    """SC #3: legacy ``list[str]`` waves still render the W06 warning path.

    A wave with no typed specs (the v0.4.0 default for every wave)
    yields ``CriterionView(source='legacy', status='pass')`` plus
    one ``not gated`` warning per legacy criterion. Pins the
    backward-compatible behaviour W08 must preserve.
    """
    state = _empty_state()
    _seed_wave(state, success_criteria=["legacy w08 a", "legacy w08 b"])
    store_dir = _store_dir(tmp_path / "state.json")

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    assert all(view.source == "legacy" for view in result.criteria)
    assert len(result.warnings) == 2
    assert all("not gated" in w for w in result.warnings)


def test_seams_dont_block_on_failing_deterministic_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC #5: the 3 close seams stay non-blocking under W08's deterministic floor.

    Uses :class:`typer.testing.CliRunner` to drive ``eawf wave close``
    end-to-end with a deterministic gate whose live argv exit-codes
    non-zero. The W06 advisory contract (the close seams attach
    readiness as **advisory**, never blocking) must survive the W08
    integration — ``ready=False`` from the readiness compute does
    not raise out of ``_close_and_pin``.
    """
    from typer.testing import CliRunner

    from eawf.surfaces.cli.app import app

    _init_test_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    runner = CliRunner()
    assert (
        runner.invoke(
            app,
            ["project", "init", "VFY", "--title", "V", "--domains", "x"],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "wave",
                "plan",
                "P01-I01",
                "--id",
                WAVE_ID,
                "--title",
                "wave",
                "--files",
                "src/",
                "--success",
                "legacy",
                "--criteria-floor-waiver",
                "test fixture models a migration-era legacy wave",
                "--effort-bucket",
                "M",
            ],
        ).exit_code
        == 0
    )
    seed_active_session_on_disk(state_path, session_id="SES-w08")
    assert runner.invoke(app, ["wave", "claim", WAVE_ID, "--session", "SES-w08"]).exit_code == 0

    # Inject a deterministic criterion + failing gate at the live
    # close seam by monkeypatching the loaders on the readiness
    # module the seam imports.
    criterion = _make_deterministic_criterion("CRIT-seam-fail", gate_ids=["GATE-seam-fail"])
    gate = _make_command_gate(
        "GATE-seam-fail",
        criterion_id="CRIT-seam-fail",
        argv=["git", "show", "no-such-ref-w08-seam"],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [gate])

    # Close path must succeed even though readiness will report
    # ready=False because the deterministic gate fails live.
    result = runner.invoke(app, ["wave", "close", WAVE_ID, "--outcome", "ok"])
    assert result.exit_code == 0, result.stdout


# ---- resolve_wave_verify_block: verdict-always band narrowing (W12) ----------


def _band_scoped_block() -> VerifyBlock:
    """The shipped band-scoped intent: enforce + jury on at the fleet level.

    Mirrors the fixture in ``tests/platform/profiles/test_verify_block.py``:
    a non-empty ``uiux_bands`` records the band-scoped enforcement intent that
    :func:`~eawf.workflow.verify.readiness.resolve_wave_verify_block` narrows
    per wave.
    """
    return VerifyBlock(
        enforce=True,
        cross_vendor_jury=True,
        uiux_bands=["tui", "render"],
        jury_vendors=["claude", "codex", "opencode"],
    )


def _non_band_wave(
    *,
    wave_id: str,
    title: str,
    effort_bucket: str | None = None,
) -> Wave:
    """Build a minimal claimed, gateless, non-band :class:`Wave`.

    The wave carries a backend ``file_scopes`` (no UI surface) and a
    band-free ``title`` so :func:`wave_in_uiux_band` misses; it declares no
    gates so :func:`classify_risk_tier` returns ``MECH`` (the gate-risk arm
    misses too). The only lever that varies is *effort_bucket*: an ``XL``
    bucket makes :func:`verdict_requirement` return ``"always"`` while an
    omitted (``None``) bucket leaves the wave mechanical.
    """
    payload: dict[str, object] = {
        "id": wave_id,
        "iter_id": "P30-I23",
        "title": title,
        "status": "claimed",
        "file_scopes": ["src/eawf/kernel/spec/wave.py"],
        "gates": [],
        "success_criteria": [
            {
                "id": "CR-01",
                "text": "backend rollup criterion",
                "kind": "legacy",
                "acceptance_style": "binary",
                "evidence_kind": "attested",
                "quality_dimension": "functional_suitability",
                "measurable_signal": "grandfathered legacy criterion",
            }
        ],
        "opened_at": "2026-06-12T00:00:00Z",
        "claimed_at": "2026-06-12T00:00:00Z",
    }
    if effort_bucket is not None:
        payload["effort_bucket"] = effort_bucket
    return Wave.model_validate(payload)


def test_resolve_wave_verify_block_verdict_always_wave_never_narrows() -> None:
    """CR-01: a non-band, gateless, verdict-always wave keeps enforce=True.

    The wave misses the band arm (backend file_scope, band-free title) and the
    gate-risk arm (no gates -> ``MECH``); its ``XL`` effort bucket makes
    :func:`~eawf.workflow.dispatch.verdict.verdict_requirement` return
    ``"always"``, so the new third preservation arm holds the block
    un-narrowed rather than de-scoping the mandatory fresh-auditor verdict.
    """
    wave = _non_band_wave(
        wave_id="P30-I23-W12",
        title="backend rollup",
        effort_bucket="XL",
    )
    resolved = readiness_mod.resolve_wave_verify_block(_band_scoped_block(), wave)
    assert resolved is not None
    assert resolved.enforce is True
    assert resolved.cross_vendor_jury is True


def test_resolve_wave_verify_block_mechanical_wave_still_narrows() -> None:
    """CR-02: a non-band mechanical (non-verdict-always) wave narrows to enforce=False.

    Same shape as the verdict-always case but with no forcing effort bucket,
    judgment role, gate, or security keyword, so
    :func:`~eawf.workflow.dispatch.verdict.verdict_requirement` returns
    ``"sampled"`` / ``"skip"`` (never ``"always"``). The low-risk band
    narrowing is preserved: only verdict-always waves are held un-narrowed.
    """
    wave = _non_band_wave(
        wave_id="P30-I23-W98",
        title="backend rollup",
    )
    resolved = readiness_mod.resolve_wave_verify_block(_band_scoped_block(), wave)
    assert resolved is not None
    assert resolved.enforce is False
    assert resolved.cross_vendor_jury is False
