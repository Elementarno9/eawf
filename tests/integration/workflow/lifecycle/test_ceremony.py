"""Tests for earned-autonomy ceremony-mode recommendations and close cadence."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    AuditKind,
    AuditStatus,
    AuditVerdict,
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import AgentSession, Audit, CurrentPointers, Project, State
from eawf.runtime.vcs.checkpoint import requires_checkpoint_commit, resolve_checkpoint_cadence
from eawf.runtime.vcs.coauthor import VcsConfig
from eawf.workflow.lifecycle.ceremony import compute_ceremony
from eawf.workflow.lifecycle.transitions import (
    LifecycleError,
    close_iter,
    close_phase,
    open_iter,
    open_phase,
    phase_close_readiness,
    plan_wave,
)
from tests.conftest import make_intent

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
_OPERATOR_SESSION_ID = "SES-operator"


def _empty_state() -> State:
    """Return a minimal state for ceremony tests."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
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


def _seed_phase(state: State) -> None:
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    for idx in range(1, 5):
        plan_wave(
            state,
            wave_id=f"P01-I01-W0{idx}",
            iter_id="P01-I01",
            title=f"Wave {idx}",
            file_scopes=["src/"],
            effort_bucket="M",
            intent=make_intent(),
        )


def _seed_operator_session(state: State) -> None:
    state.agent_sessions[_OPERATOR_SESSION_ID] = AgentSession(
        id=_OPERATOR_SESSION_ID,
        role=AgentSessionRole.OPERATOR,
        runtime="claude",
        scope_id="QR",
        status=AgentSessionStatus.ACTIVE,
        started_at=_T0,
    )


def _close_wave(
    state: State,
    wave_id: str,
    *,
    offset: int,
    operator_confirmed: bool,
) -> None:
    wave = state.waves[wave_id]
    wave.status = WaveStatus.CLOSED
    wave.closed_at = _T0 + timedelta(minutes=offset)
    wave.claim_session_id = _OPERATOR_SESSION_ID if operator_confirmed else None


def test_compute_ceremony_recommends_mode_c_without_operator_confirmed_history() -> None:
    """No confirmed streak means high ceremony."""
    state = _empty_state()
    _seed_phase(state)

    recommendation = compute_ceremony(state, wave_id="P01-I01-W01")

    assert recommendation.mode == "C"
    assert recommendation.operator_confirmed_counter == 0
    assert recommendation.closed_wave_count == 0
    assert "no operator-confirmed streak" in recommendation.reason


def test_compute_ceremony_recommends_mode_b_after_one_confirmed_wave() -> None:
    """One operator-confirmed predecessor earns mode B."""
    state = _empty_state()
    _seed_phase(state)
    _seed_operator_session(state)
    _close_wave(state, "P01-I01-W01", offset=1, operator_confirmed=True)

    recommendation = compute_ceremony(state, wave_id="P01-I01-W02")

    assert recommendation.mode == "B"
    assert recommendation.operator_confirmed_counter == 1
    assert recommendation.operator_confirmed_wave_ids == ["P01-I01-W01"]


def test_compute_ceremony_recommends_mode_a_after_three_confirmed_waves() -> None:
    """Three consecutive operator-confirmed waves earn mode A."""
    state = _empty_state()
    _seed_phase(state)
    _seed_operator_session(state)
    _close_wave(state, "P01-I01-W01", offset=1, operator_confirmed=True)
    _close_wave(state, "P01-I01-W02", offset=2, operator_confirmed=True)
    _close_wave(state, "P01-I01-W03", offset=3, operator_confirmed=True)

    recommendation = compute_ceremony(state, wave_id="P01-I01-W04")

    assert recommendation.mode == "A"
    assert recommendation.operator_confirmed_counter == 3
    assert recommendation.operator_confirmed_wave_ids == [
        "P01-I01-W03",
        "P01-I01-W02",
        "P01-I01-W01",
    ]


def test_compute_ceremony_counter_recomputes_from_state_on_each_call() -> None:
    """Counter is derived on demand, so later state changes alter the result."""
    state = _empty_state()
    _seed_phase(state)
    _seed_operator_session(state)
    _close_wave(state, "P01-I01-W01", offset=1, operator_confirmed=True)
    _close_wave(state, "P01-I01-W02", offset=2, operator_confirmed=True)
    _close_wave(state, "P01-I01-W03", offset=3, operator_confirmed=True)
    assert compute_ceremony(state, wave_id="P01-I01-W04").operator_confirmed_counter == 3

    state.waves["P01-I01-W03"].claim_session_id = None
    recommendation = compute_ceremony(state, wave_id="P01-I01-W04")

    assert recommendation.operator_confirmed_counter == 0
    assert recommendation.mode == "C"


def test_compute_ceremony_unknown_wave_raises_key_error() -> None:
    """Unknown target wave fails loudly."""
    state = _empty_state()
    _seed_phase(state)

    with pytest.raises(KeyError, match="unknown wave"):
        compute_ceremony(state, wave_id="P01-I01-W99")


# ---- checkpoint-commit cadence ----------------------------------------------
#
# ``vcs.checkpoint_requires_commit`` gates the coherent-scope closes (iter and
# phase) on the single checkpoint commit the close names. A batched close lands
# several waves under one ``state:`` bookkeeping commit, so the cadence
# verifies that one ref and never a per-wave pin.


def _seed_commit(root: Path) -> str:
    """Initialise a throwaway repository at *root* and return its one commit SHA."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
        "GIT_CONFIG_SYSTEM": str(root / "gitconfig-system"),
    }

    def _vcs(*args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        return out.stdout.strip()

    _vcs("init", "--initial-branch=main")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _vcs("add", "seed.txt")
    _vcs("commit", "-m", "[P01] state: seed checkpoint")
    return _vcs("rev-parse", "HEAD")


def _write_vcs_config(root: Path, *, requires_commit: bool) -> None:
    """Write the repo-layer ``vcs.checkpoint_requires_commit`` override."""
    ea_dir = root / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    flag = "true" if requires_commit else "false"
    (ea_dir / "config.yaml").write_text(
        f"vcs:\n  checkpoint_requires_commit: {flag}\n", encoding="utf-8"
    )


def _closable_iter_state() -> State:
    """Return a state whose four waves under ``P01-I01`` are all CLOSED."""
    state = _empty_state()
    _seed_phase(state)
    _seed_operator_session(state)
    for idx in range(1, 5):
        _close_wave(state, f"P01-I01-W0{idx}", offset=idx, operator_confirmed=True)
    return state


def _closable_phase_state() -> State:
    """Return a structurally closable ``P01`` with a real passing ship-gate audit."""
    state = _closable_iter_state()
    it = state.iters["P01-I01"]
    it.status = IterStatus.CLOSED
    it.closed_at = _T0 + timedelta(minutes=10)
    it.audit_id = "AUD-ITER"
    state.audits = dict(state.audits or {})
    state.audits["AUD-PH"] = Audit(
        id="AUD-PH",
        scope_id="P01",
        kind=AuditKind.SHIP_GATE,
        status=AuditStatus.COMPLETE,
        created_at=_T0,
        verdict=AuditVerdict.PASS,
        check_results=[{"name": "tests", "passed": True, "details": "suite green"}],
    )
    return state


def test_close_iter_checkpoint_requires_commit_refuses_unknown_ref(tmp_path: Path) -> None:
    """Flag true: a checkpoint ref that names no commit refuses the close."""
    _seed_commit(tmp_path)
    _write_vcs_config(tmp_path, requires_commit=True)
    state = _closable_iter_state()

    with pytest.raises(LifecycleError, match="does not exist"):
        close_iter(
            state,
            iter_id="P01-I01",
            audit_id="AUD-1",
            checkpoint_commit="0" * 40,
            project_root=tmp_path,
        )

    assert state.iters["P01-I01"].status is not IterStatus.CLOSED


def test_close_iter_checkpoint_requires_commit_accepts_real_ref(tmp_path: Path) -> None:
    """Flag true: a checkpoint ref naming a real commit lets the close proceed."""
    sha = _seed_commit(tmp_path)
    _write_vcs_config(tmp_path, requires_commit=True)
    state = _closable_iter_state()

    closed = close_iter(
        state,
        iter_id="P01-I01",
        audit_id="AUD-1",
        checkpoint_commit=sha,
        project_root=tmp_path,
    )

    assert closed.status is IterStatus.CLOSED


def test_close_iter_checkpoint_requires_commit_false_proceeds(tmp_path: Path) -> None:
    """Flag false: the same unknown ref no longer refuses the close."""
    _seed_commit(tmp_path)
    _write_vcs_config(tmp_path, requires_commit=False)
    state = _closable_iter_state()

    closed = close_iter(
        state,
        iter_id="P01-I01",
        audit_id="AUD-1",
        checkpoint_commit="0" * 40,
        project_root=tmp_path,
    )

    assert closed.status is IterStatus.CLOSED


def test_close_iter_checkpoint_requires_commit_rejects_blank_ref(tmp_path: Path) -> None:
    """Flag true: a whitespace-only checkpoint ref is a refusal, not a silent skip."""
    _seed_commit(tmp_path)
    _write_vcs_config(tmp_path, requires_commit=True)
    state = _closable_iter_state()

    with pytest.raises(LifecycleError, match="blank checkpoint commit"):
        close_iter(
            state,
            iter_id="P01-I01",
            audit_id="AUD-1",
            checkpoint_commit="   ",
            project_root=tmp_path,
        )


def test_close_iter_checkpoint_requires_commit_inert_without_project_root() -> None:
    """No repo root in hand leaves the cadence inert rather than refusing."""
    state = _closable_iter_state()

    closed = close_iter(
        state,
        iter_id="P01-I01",
        audit_id="AUD-1",
        checkpoint_commit="0" * 40,
        project_root=None,
    )

    assert closed.status is IterStatus.CLOSED


def test_close_iter_checkpoint_requires_commit_inert_outside_work_tree(tmp_path: Path) -> None:
    """Flag true but no work tree: the cadence cannot answer, so it stays inert."""
    _write_vcs_config(tmp_path, requires_commit=True)
    state = _closable_iter_state()

    closed = close_iter(
        state,
        iter_id="P01-I01",
        audit_id="AUD-1",
        checkpoint_commit="0" * 40,
        project_root=tmp_path,
    )

    assert closed.status is IterStatus.CLOSED


def test_close_iter_checkpoint_requires_commit_ignores_unnamed_checkpoint(tmp_path: Path) -> None:
    """A close that names no checkpoint is untouched by the cadence."""
    _seed_commit(tmp_path)
    _write_vcs_config(tmp_path, requires_commit=True)
    state = _closable_iter_state()

    closed = close_iter(state, iter_id="P01-I01", audit_id="AUD-1", project_root=tmp_path)

    assert closed.status is IterStatus.CLOSED


def test_close_phase_checkpoint_requires_commit_refuses_unknown_ref(tmp_path: Path) -> None:
    """Flag true: phase close refuses a checkpoint ref that names no commit."""
    _seed_commit(tmp_path)
    _write_vcs_config(tmp_path, requires_commit=True)
    state = _closable_phase_state()

    with pytest.raises(LifecycleError, match="does not exist"):
        close_phase(
            state,
            phase_id="P01",
            audit_id="AUD-PH",
            checkpoint="0" * 40,
            project_root=tmp_path,
        )

    assert state.phases["P01"].status is not PhaseStatus.CLOSED


def test_close_phase_checkpoint_requires_commit_accepts_real_ref(tmp_path: Path) -> None:
    """Flag true: a real checkpoint ref lets the phase close proceed."""
    sha = _seed_commit(tmp_path)
    _write_vcs_config(tmp_path, requires_commit=True)
    state = _closable_phase_state()

    closed = close_phase(
        state,
        phase_id="P01",
        audit_id="AUD-PH",
        checkpoint=sha,
        project_root=tmp_path,
    )

    assert closed.status is PhaseStatus.CLOSED


def test_close_phase_checkpoint_requires_commit_false_proceeds(tmp_path: Path) -> None:
    """Flag false: the same unknown ref no longer refuses the phase close."""
    _seed_commit(tmp_path)
    _write_vcs_config(tmp_path, requires_commit=False)
    state = _closable_phase_state()

    closed = close_phase(
        state,
        phase_id="P01",
        audit_id="AUD-PH",
        checkpoint="0" * 40,
        project_root=tmp_path,
    )

    assert closed.status is PhaseStatus.CLOSED


def test_phase_close_readiness_checkpoint_requires_commit_criterion(tmp_path: Path) -> None:
    """The refusal surfaces as a typed ``checkpoint-commit`` readiness criterion."""
    _seed_commit(tmp_path)
    _write_vcs_config(tmp_path, requires_commit=True)
    state = _closable_phase_state()

    readiness = phase_close_readiness(
        state,
        phase_id="P01",
        audit_id="AUD-PH",
        require_audit=True,
        checkpoint_commit="0" * 40,
        project_root=tmp_path,
    )

    view = next(v for v in readiness.criteria if v.id == "checkpoint-commit")
    assert view.status == "blocked"
    assert readiness.ready is False


def test_phase_close_readiness_omits_checkpoint_criterion_when_unnamed(tmp_path: Path) -> None:
    """No checkpoint named means no ``checkpoint-commit`` criterion at all."""
    _seed_commit(tmp_path)
    _write_vcs_config(tmp_path, requires_commit=True)
    state = _closable_phase_state()

    readiness = phase_close_readiness(
        state,
        phase_id="P01",
        audit_id="AUD-PH",
        require_audit=True,
        project_root=tmp_path,
    )

    assert [v.id for v in readiness.criteria].count("checkpoint-commit") == 0
    assert readiness.ready is True


def test_resolve_checkpoint_cadence_rejects_non_bool_leaf(tmp_path: Path) -> None:
    """A non-bool ``vcs.checkpoint_requires_commit`` leaf fails loudly."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    (ea_dir / "config.yaml").write_text(
        "vcs:\n  checkpoint_requires_commit: maybe\n", encoding="utf-8"
    )

    with pytest.raises(ValidationError):
        resolve_checkpoint_cadence(tmp_path)


def test_resolve_checkpoint_cadence_without_root_is_inert() -> None:
    """No root resolves to ``False`` -- the cadence has nothing to verify against."""
    assert resolve_checkpoint_cadence(None) is False


def test_requires_checkpoint_commit_reads_the_typed_flag() -> None:
    """The pure predicate mirrors the validated ``VcsConfig`` field."""
    payload = dict(BUILT_IN_DEFAULTS["vcs"])
    payload["checkpoint_requires_commit"] = False

    assert requires_checkpoint_commit(VcsConfig.model_validate(payload)) is False
    payload["checkpoint_requires_commit"] = True
    assert requires_checkpoint_commit(VcsConfig.model_validate(payload)) is True
