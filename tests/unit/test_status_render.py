"""Tests for the in-memory helpers in :mod:`eawf.cli.commands.status`.

Exercises the pure projection functions (``_project_summary``, ``_active_waves``,
``_active_sessions``, ``_last_audit``, ``_last_closed_waves``, ``_blockers``)
and the git-info path with a stub :class:`subprocess.run` so no real ``git`` is
invoked. The status command itself is integration-tested in
``tests/integration/test_cli_status.py``.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from eawf.cli.commands import status as status_mod
from eawf.state.enums import (
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
from eawf.state.models import (
    AgentSession,
    Audit,
    CurrentPointers,
    Iter,
    Phase,
    Project,
    State,
    Wave,
)

_DT = datetime(2026, 5, 8, tzinfo=UTC)


def _state(
    *,
    waves: dict[str, Wave] | None = None,
    iters: dict[str, Iter] | None = None,
    phases: dict[str, Phase] | None = None,
    audits: dict[str, Audit] | None = None,
    sessions: dict[str, AgentSession] | None = None,
    current: CurrentPointers | None = None,
    project: Project | None = None,
) -> State:
    """Build a minimal valid :class:`State` for projection-helper tests."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _DT.isoformat(),
            "project": (project or _project()).model_dump(mode="json"),
            "current": (current or CurrentPointers(project_code="QR")).model_dump(mode="json"),
            "workspace": None,
            "phases": {k: v.model_dump(mode="json") for k, v in (phases or {}).items()},
            "iters": {k: v.model_dump(mode="json") for k, v in (iters or {}).items()},
            "waves": {k: v.model_dump(mode="json") for k, v in (waves or {}).items()},
            "audits": (
                {k: v.model_dump(mode="json") for k, v in audits.items()}
                if audits is not None
                else None
            ),
            "artifacts": {},
            "agent_sessions": {k: v.model_dump(mode="json") for k, v in (sessions or {}).items()},
            "plugins": {},
            "indexes": {},
        }
    )


def _project() -> Project:
    return Project(
        code="QR",
        slug="quant-research",
        title="Quant Research",
        description="",
        domains=["quant"],
        default_branch="main",
        status=ProjectStatus.ACTIVE,
        repo_urn="urn:eawf:v1:repo:QR",
    )


def _wave(
    wave_id: str = "P01-I01-W01",
    status: WaveStatus = WaveStatus.IN_PROGRESS,
    *,
    closed_at: datetime | None = None,
    iter_id: str = "P01-I01",
    claim_session_id: str | None = "S-1",
) -> Wave:
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title="W",
        status=status,
        deps=[],
        file_scopes=["src/foo.py"],
        claim_session_id=claim_session_id,
        worktree_id=None,
        commit="abc123" if status == WaveStatus.CLOSED else None,
        outcome="ok" if status == WaveStatus.CLOSED else None,
        opened_at=_DT,
        closed_at=closed_at,
    )


def _iter(iter_id: str = "P01-I01", *, audit_id: str | None = None) -> Iter:
    return Iter(
        id=iter_id,
        phase_id=iter_id.split("-I")[0],
        title="I",
        status=IterStatus.ACTIVE,
        wave_ids=[],
        estimate_id=None,
        audit_id=audit_id,
        opened_at=_DT,
        closed_at=None,
    )


def _phase(phase_id: str = "P01", *, audit_id: str | None = None) -> Phase:
    return Phase(
        id=phase_id,
        scope_id="QR",
        subproject_id=None,
        title="Phase",
        status=PhaseStatus.ACTIVE,
        iter_ids=[],
        outcome_ids=[],
        opened_at=_DT,
        closed_at=None,
        audit_id=audit_id,
    )


def _audit(audit_id: str = "A-1", verdict: AuditVerdict | None = AuditVerdict.PASS) -> Audit:
    return Audit(
        id=audit_id,
        scope_id="P01",
        kind=AuditKind.SHIP_GATE,
        status=AuditStatus.COMPLETE,
        report_artifact_id=None,
        check_results=[],
        integrity_results=[],
        created_at=_DT,
        verdict=verdict,
    )


def _session(sid: str = "S-1", scope_id: str = "P01-I01-W01") -> AgentSession:
    return AgentSession(
        id=sid,
        role=AgentSessionRole.EXECUTOR,
        runtime="claude",
        scope_id=scope_id,
        status=AgentSessionStatus.ACTIVE,
        claimed_wave_ids=[],
        worktree_ids=[],
        artifact_ids=[],
        started_at=_DT,
        ended_at=None,
        summary=None,
    )


# ---- _project_summary ------------------------------------------------------


def test_project_summary_returns_compact_projection() -> None:
    s = _state()
    out = status_mod._project_summary(s)
    assert out == {"code": "QR", "title": "Quant Research", "status": "active"}


def test_project_summary_returns_none_when_project_missing() -> None:
    s = State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.WORKSPACE.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _DT.isoformat(),
            "project": None,
            "current": CurrentPointers().model_dump(mode="json"),
            "workspace": {
                "code": "QR",
                "title": "QR",
                "repos": {},
                "current_repo_code": None,
            },
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )
    assert status_mod._project_summary(s) is None


# ---- _active_waves --------------------------------------------------------


def test_active_waves_lists_active_pointer_targets() -> None:
    waves = {"P01-I01-W01": _wave(status=WaveStatus.IN_PROGRESS)}
    cp = CurrentPointers(
        project_code="QR",
        phase_id="P01",
        iter_id="P01-I01",
        active_wave_ids=["P01-I01-W01"],
    )
    s = _state(waves=waves, iters={"P01-I01": _iter()}, phases={"P01": _phase()}, current=cp)
    out = status_mod._active_waves(s)
    assert len(out) == 1
    assert out[0]["id"] == "P01-I01-W01"
    assert out[0]["status"] == "in_progress"


def test_active_waves_skips_dangling_pointers() -> None:
    cp = CurrentPointers(
        project_code="QR",
        phase_id="P01",
        iter_id="P01-I01",
        active_wave_ids=["P01-I01-W99"],
    )
    s = _state(
        waves={"P01-I01-W01": _wave(status=WaveStatus.IN_PROGRESS)},
        iters={"P01-I01": _iter()},
        phases={"P01": _phase()},
        current=cp,
    )
    assert status_mod._active_waves(s) == []


def test_active_waves_empty_when_no_pointers() -> None:
    s = _state()
    assert status_mod._active_waves(s) == []


# ---- _active_sessions -----------------------------------------------------


def test_active_sessions_projection_strips_internals() -> None:
    cp = CurrentPointers(project_code="QR", active_session_ids=["S-1"])
    s = _state(sessions={"S-1": _session()}, current=cp)
    out = status_mod._active_sessions(s)
    assert out == [
        {
            "id": "S-1",
            "role": AgentSessionRole.EXECUTOR.value,
            "runtime": "claude",
            "scope_id": "P01-I01-W01",
            "status": AgentSessionStatus.ACTIVE.value,
        }
    ]


def test_active_sessions_skips_dangling_ids() -> None:
    cp = CurrentPointers(project_code="QR", active_session_ids=["S-NOPE"])
    s = _state(current=cp)
    assert status_mod._active_sessions(s) == []


# ---- _last_audit ----------------------------------------------------------


def test_last_audit_returns_projection_for_known_id() -> None:
    audit = _audit(verdict=AuditVerdict.MAJOR)
    s = _state(audits={"A-1": audit})
    out = status_mod._last_audit(s, "A-1")
    assert out == {"id": "A-1", "kind": "ship-gate", "verdict": "major"}


def test_last_audit_returns_none_for_missing_id() -> None:
    s = _state(audits={})
    assert status_mod._last_audit(s, "A-NOPE") is None


def test_last_audit_returns_none_when_audit_id_is_none() -> None:
    s = _state(audits={"A-1": _audit()})
    assert status_mod._last_audit(s, None) is None


def test_last_audit_returns_none_when_state_audits_is_none() -> None:
    s = _state()
    assert s.audits is None
    assert status_mod._last_audit(s, "A-1") is None


# ---- _last_closed_waves ---------------------------------------------------


def test_last_closed_waves_orders_newest_first() -> None:
    closed_a = _wave(
        wave_id="P01-I01-W01",
        status=WaveStatus.CLOSED,
        closed_at=datetime(2026, 5, 5, tzinfo=UTC),
    )
    closed_b = _wave(
        wave_id="P01-I01-W02",
        status=WaveStatus.CLOSED,
        closed_at=datetime(2026, 5, 8, tzinfo=UTC),
    )
    open_c = _wave(wave_id="P01-I01-W03", status=WaveStatus.IN_PROGRESS)
    s = _state(
        waves={
            "P01-I01-W01": closed_a,
            "P01-I01-W02": closed_b,
            "P01-I01-W03": open_c,
        },
        iters={"P01-I01": _iter()},
        phases={"P01": _phase()},
    )
    out = status_mod._last_closed_waves(s)
    assert out == ["P01-I01-W02", "P01-I01-W01"]


def test_last_closed_waves_respects_limit() -> None:
    waves = {
        f"P01-I01-W{i:02d}": _wave(
            wave_id=f"P01-I01-W{i:02d}",
            status=WaveStatus.CLOSED,
            closed_at=datetime(2026, 5, i, tzinfo=UTC),
        )
        for i in range(1, 6)
    }
    s = _state(waves=waves, iters={"P01-I01": _iter()}, phases={"P01": _phase()})
    assert len(status_mod._last_closed_waves(s, limit=3)) == 3


def test_last_closed_waves_empty_when_none_closed() -> None:
    s = _state(
        waves={"P01-I01-W01": _wave(status=WaveStatus.IN_PROGRESS)},
        iters={"P01-I01": _iter()},
        phases={"P01": _phase()},
    )
    assert status_mod._last_closed_waves(s) == []


# ---- _blockers ------------------------------------------------------------


def test_blockers_flags_active_unclaimed_wave() -> None:
    wave = _wave(status=WaveStatus.IN_PROGRESS, claim_session_id=None)
    cp = CurrentPointers(
        project_code="QR",
        phase_id="P01",
        iter_id="P01-I01",
        active_wave_ids=["P01-I01-W01"],
    )
    s = _state(
        waves={"P01-I01-W01": wave},
        iters={"P01-I01": _iter()},
        phases={"P01": _phase()},
        current=cp,
    )
    blockers = status_mod._blockers(s)
    assert any("unclaimed" in b for b in blockers)


def test_blockers_empty_when_state_clean() -> None:
    s = _state()
    assert status_mod._blockers(s) == []


def test_blockers_flags_dual_session_for_same_scope() -> None:
    cp = CurrentPointers(project_code="QR", active_session_ids=["S-1", "S-2"])
    s = _state(
        sessions={"S-1": _session(sid="S-1"), "S-2": _session(sid="S-2")},
        current=cp,
    )
    blockers = status_mod._blockers(s)
    assert any("share scope" in b for b in blockers)


# ---- _git_info ------------------------------------------------------------


def test_git_info_collapses_to_none_when_subprocess_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every git invocation hits FileNotFoundError → all fields None."""

    def _fake_run(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    info = status_mod._git_info(cwd=tmp_path)
    assert info == {"head": None, "branch": None, "dirty": None}


def test_git_info_returns_clean_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = iter(
        [
            _stub_proc("abcdef0123456789abcdef0123456789abcdef01"),  # pragma: allowlist secret
            _stub_proc("main"),
            _stub_proc(""),  # empty porcelain → not dirty
        ]
    )

    def _fake_run(*_args: Any, **_kwargs: Any) -> Any:
        return next(sequence)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    info = status_mod._git_info(cwd=tmp_path)
    assert info == {
        "head": "abcdef0123456789abcdef0123456789abcdef01",  # pragma: allowlist secret
        "branch": "main",
        "dirty": False,
    }


def test_git_info_marks_dirty_when_porcelain_nonempty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = iter(
        [
            _stub_proc("abcd"),
            _stub_proc("feature/x"),
            _stub_proc(" M src/foo.py"),
        ]
    )

    def _fake_run(*_args: Any, **_kwargs: Any) -> Any:
        return next(sequence)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    info = status_mod._git_info(cwd=tmp_path)
    assert info["dirty"] is True


def test_git_info_handles_called_process_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rev-parse HEAD fails on a fresh repo with no commits — head must be None."""

    sequence = iter(
        [
            subprocess.CalledProcessError(returncode=128, cmd=["git", "rev-parse", "HEAD"]),
            _stub_proc("main"),
            _stub_proc(""),
        ]
    )

    def _fake_run(*_args: Any, **_kwargs: Any) -> Any:
        nxt = next(sequence)
        if isinstance(nxt, subprocess.CalledProcessError):
            raise nxt
        return nxt

    monkeypatch.setattr(subprocess, "run", _fake_run)
    info = status_mod._git_info(cwd=tmp_path)
    assert info["head"] is None
    assert info["branch"] == "main"
    assert info["dirty"] is False


def _stub_proc(stdout: str) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout + "\n"
    proc.returncode = 0
    return proc


# ---- _format_text ---------------------------------------------------------


def test_format_text_includes_project_and_current() -> None:
    payload: dict[str, Any] = {
        "project": {"code": "QR", "title": "Quant Research", "status": "active"},
        "current": {
            "project_code": "QR",
            "subproject_id": None,
            "phase_id": "P01",
            "iter_id": "P01-I01",
            "active_wave_ids": ["P01-I01-W01"],
            "active_session_ids": [],
        },
        "git": {"head": "abcdef0123", "branch": "main", "dirty": False},
        "blockers": [],
    }
    text = status_mod._format_text(payload)
    assert "project: QR" in text
    assert "phase=P01" in text
    assert "blockers: none" in text


def test_format_text_handles_no_project() -> None:
    payload: dict[str, Any] = {
        "project": None,
        "current": {
            "project_code": None,
            "subproject_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "git": {"head": None, "branch": None, "dirty": None},
        "blockers": ["one"],
    }
    text = status_mod._format_text(payload)
    assert "project: <none>" in text
    assert "phase=None" in text
    assert "blockers: one" in text
    # W5: an unset git branch must render as the explicit ``<unknown>``
    # sentinel, never the literal string ``"None"`` from f-string coercion.
    assert "branch=<unknown>" in text
    assert "branch=None" not in text
