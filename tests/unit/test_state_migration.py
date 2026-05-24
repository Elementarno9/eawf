"""Unit tests for :mod:`eawf.kernel.state.migration`.

Covers :func:`abandon_orphaned_waves`: it abandons non-terminal waves and
iters orphaned under a terminal phase, leaves already-terminal records and
live-phase records untouched, is idempotent on re-run, and produces a state
that passes the closure-timestamp invariant. The zombie scenario is built
by direct payload validation because the post-fix ``archive_phase`` cascade
can no longer produce a terminal phase with PENDING children — the migration
exists precisely to repair states written by the pre-cascade code.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eawf.kernel.state.enums import (
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.migration import abandon_orphaned_waves
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.kernel.validate.strict import validate_state

_TS = datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC).isoformat()


def _state(
    *,
    phases: dict[str, str],
    iters: dict[str, tuple[str, str]],
    waves: dict[str, tuple[str, str]],
    current_phase: str | None = None,
    active_wave_ids: list[str] | None = None,
) -> State:
    """Build a typed state from compact ``{id: status}`` maps.

    Args:
        phases: ``{phase_id: status}``.
        iters: ``{iter_id: (phase_id, status)}``.
        waves: ``{wave_id: (iter_id, status)}``.
        current_phase: Value for ``current.phase_id``.
        active_wave_ids: Value for ``current.active_wave_ids``.
    """
    terminal_phase = {"closed", "archived"}
    terminal_iter = {"closed", "abandoned"}
    terminal_wave = {"closed", "failed", "abandoned"}
    phase_rows = {
        pid: {
            "id": pid,
            "scope_id": "QR",
            "subproject_id": None,
            "title": pid,
            "status": status,
            "iter_ids": [iid for iid, (p, _) in iters.items() if p == pid],
            "outcome_ids": [],
            "depends_on": [],
            "source_brief_ids": [],
            "opened_at": _TS,
            "closed_at": _TS if status in terminal_phase else None,
            "audit_id": None,
        }
        for pid, status in phases.items()
    }
    iter_rows = {
        iid: {
            "id": iid,
            "phase_id": phase_id,
            "title": iid,
            "status": status,
            "wave_ids": [wid for wid, (i, _) in waves.items() if i == iid],
            "estimate_id": None,
            "audit_id": None,
            "opened_at": _TS,
            "closed_at": _TS if status in terminal_iter else None,
        }
        for iid, (phase_id, status) in iters.items()
    }
    wave_rows = {
        wid: {
            "id": wid,
            "iter_id": iter_id,
            "title": wid,
            "status": status,
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "opened_at": _TS,
            "closed_at": _TS if status in terminal_wave else None,
        }
        for wid, (iter_id, status) in waves.items()
    }
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _TS,
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
            "current": CurrentPointers(
                project_code="QR",
                phase_id=current_phase,
                active_wave_ids=active_wave_ids or [],
            ).model_dump(mode="json"),
            "workspace": None,
            "phases": phase_rows,
            "iters": iter_rows,
            "waves": wave_rows,
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def test_abandon_orphaned_waves_flips_archived_phase_pending_waves() -> None:
    state = _state(
        phases={"P21": "archived"},
        iters={"P21-I01": ("P21", "planned")},
        waves={
            "P21-I01-W01": ("P21-I01", "pending"),
            "P21-I01-W02": ("P21-I01", "pending"),
        },
    )
    report = abandon_orphaned_waves(state)
    assert report.abandoned_wave_ids == ("P21-I01-W01", "P21-I01-W02")
    assert state.waves["P21-I01-W01"].status == WaveStatus.ABANDONED
    assert state.waves["P21-I01-W02"].status == WaveStatus.ABANDONED
    assert state.waves["P21-I01-W01"].closed_at is not None


def test_abandon_orphaned_waves_flips_orphan_iter() -> None:
    state = _state(
        phases={"P21": "archived"},
        iters={"P21-I01": ("P21", "planned")},
        waves={"P21-I01-W01": ("P21-I01", "pending")},
    )
    abandon_orphaned_waves(state)
    assert state.iters["P21-I01"].status == IterStatus.ABANDONED
    assert state.iters["P21-I01"].closed_at is not None


def test_abandon_orphaned_waves_leaves_live_phase_waves_untouched() -> None:
    state = _state(
        phases={"P26": "active", "P21": "archived"},
        iters={"P26-I01": ("P26", "active"), "P21-I01": ("P21", "planned")},
        waves={
            "P26-I01-W34": ("P26-I01", "pending"),
            "P21-I01-W01": ("P21-I01", "pending"),
        },
        current_phase="P26",
    )
    report = abandon_orphaned_waves(state)
    assert report.abandoned_wave_ids == ("P21-I01-W01",)
    # The live P26 wave keeps its PENDING status.
    assert state.waves["P26-I01-W34"].status == WaveStatus.PENDING
    assert state.phases["P26"].status == PhaseStatus.ACTIVE


def test_abandon_orphaned_waves_skips_already_terminal_waves() -> None:
    """A wave already CLOSED under an archived phase is not re-stamped."""
    state = _state(
        phases={"P21": "archived"},
        iters={"P21-I01": ("P21", "closed")},
        waves={"P21-I01-W01": ("P21-I01", "closed")},
    )
    report = abandon_orphaned_waves(state)
    assert report.abandoned_wave_ids == ()
    assert report.abandoned_iter_ids == ()
    assert not report.changed
    assert state.waves["P21-I01-W01"].status == WaveStatus.CLOSED


def test_abandon_orphaned_waves_idempotent_second_pass_noop() -> None:
    state = _state(
        phases={"P21": "archived"},
        iters={"P21-I01": ("P21", "planned")},
        waves={
            "P21-I01-W01": ("P21-I01", "pending"),
            "P21-I01-W02": ("P21-I01", "pending"),
        },
    )
    first = abandon_orphaned_waves(state)
    assert first.changed
    snapshot = state.model_dump(mode="json")
    second = abandon_orphaned_waves(state)
    assert not second.changed
    assert second.abandoned_wave_ids == ()
    # State is byte-identical after the no-op second pass.
    assert state.model_dump(mode="json") == snapshot


def test_abandon_orphaned_waves_drops_active_wave_pointer() -> None:
    state = _state(
        phases={"P21": "archived"},
        iters={"P21-I01": ("P21", "active")},
        waves={"P21-I01-W01": ("P21-I01", "in_progress")},
        active_wave_ids=["P21-I01-W01"],
    )
    abandon_orphaned_waves(state)
    assert state.current.active_wave_ids == []


def test_abandon_orphaned_waves_result_passes_invariants() -> None:
    state = _state(
        phases={"P21": "archived"},
        iters={"P21-I01": ("P21", "planned")},
        waves={"P21-I01-W01": ("P21-I01", "pending")},
    )
    abandon_orphaned_waves(state)
    report = validate_state(state.model_dump(mode="json"))
    assert report.state is not None
    assert report.violations == []


def test_abandon_orphaned_waves_clean_state_noop() -> None:
    """A state with only live-phase waves reports no change."""
    state = _state(
        phases={"P26": "active"},
        iters={"P26-I01": ("P26", "active")},
        waves={"P26-I01-W34": ("P26-I01", "pending")},
        current_phase="P26",
    )
    report = abandon_orphaned_waves(state)
    assert not report.changed
