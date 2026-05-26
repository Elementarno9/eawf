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
    cid: str,
    *,
    gate_ids: list[str] | None = None,
    waiver_reason: str | None = None,
    evidence_kind: str = "jury",
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
        required=True,
        waiver_reason=waiver_reason,
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
    _write_evidence_row(
        store_dir,
        record=_make_evidence_record(scope_id=WAVE_ID, status="waived", refs=["GATE-waived"]),
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


def test_waiver_without_sha_metric_treated_as_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiver row missing ``metrics['wave_sha']`` is treated as fresh (W11).

    Defensive contract: the SHA freshness check fires ONLY when both
    the stamped SHA and the current SHA are present and disagree.
    Missing-stamp rows (e.g. produced by an older CLI version, or by
    the spec-attest path that does not stamp a SHA) are honoured to
    avoid silently breaking the W06 evidence pipeline.
    """
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

    unstamped = EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=WAVE_ID,
        produced_by="human",
        evidence_kind="attested",
        status="waived",
        summary="unstamped waiver",
        refs=["GATE-nostamp"],
        metrics=None,
        created_at=datetime.now(UTC),
    )
    _write_evidence_row(store_dir, record=unstamped)

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is True
    assert result.waived_gate_ids == ["GATE-nostamp"]


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
            ],
        ).exit_code
        == 0
    )
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
