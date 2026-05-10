"""Unit tests for the wave DAG persistence layer (B026).

Covers the transition-level behaviour of :func:`plan_wave` after the
addition of the reverse-``blocks`` index and the cycle guard. CLI-level
graph/next-ready coverage lives alongside the other CLI lifecycle
integration tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.lifecycle.transitions import (
    LifecycleError,
    claim_wave,
    close_wave,
    open_iter,
    open_phase,
    plan_wave,
)
from eawf.state.enums import ProjectStatus, ScopeKind
from eawf.state.models import CurrentPointers, Project, State


def _empty_state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
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


def _seed_iter() -> State:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    return state


# ---- reverse-blocks index ---------------------------------------------------


def test_plan_wave_appends_to_blocks_index() -> None:
    """Planning W01 with deps=[W02] must append W01 to W02.blocks."""
    state = _seed_iter()
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="W2",
        file_scopes=["src/foo/"],
    )
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="W1",
        file_scopes=["src/bar/"],
        deps=["P01-I01-W02"],
    )
    assert state.waves["P01-I01-W02"].blocks == ["P01-I01-W01"]
    assert state.waves["P01-I01-W01"].deps == ["P01-I01-W02"]
    # And the new wave's own blocks list starts empty.
    assert state.waves["P01-I01-W01"].blocks == []


def test_plan_wave_blocks_index_multi_dependents() -> None:
    """Two waves depending on W01 both appear in W01.blocks (id-ordered)."""
    state = _seed_iter()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="W1",
        file_scopes=["src/"],
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="W2",
        file_scopes=["src/"],
        deps=["P01-I01-W01"],
    )
    plan_wave(
        state,
        wave_id="P01-I01-W03",
        iter_id="P01-I01",
        title="W3",
        file_scopes=["src/"],
        deps=["P01-I01-W01"],
    )
    assert state.waves["P01-I01-W01"].blocks == ["P01-I01-W02", "P01-I01-W03"]


def test_plan_wave_idempotent_blocks() -> None:
    """plan_wave rejects duplicate id; the dep's blocks must not gain a dup.

    The duplicate-id guard fires first so the rest of the function is not
    re-executed — but we still want to confirm that a re-plan attempt
    never produces a duplicate entry in the dep's ``blocks`` list.
    """
    state = _seed_iter()
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="W2",
        file_scopes=["src/"],
    )
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="W1",
        file_scopes=["src/"],
        deps=["P01-I01-W02"],
    )
    # Re-plan with the same id must error.
    with pytest.raises(LifecycleError, match="already exists"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="W1-bis",
            file_scopes=["src/"],
            deps=["P01-I01-W02"],
        )
    assert state.waves["P01-I01-W02"].blocks == ["P01-I01-W01"]


# ---- cycle validation --------------------------------------------------------


def test_plan_wave_self_dep_refused() -> None:
    """Self-dependency is a trivial cycle and must be rejected."""
    state = _seed_iter()
    with pytest.raises(LifecycleError, match="cannot depend on itself"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="W1",
            file_scopes=["src/"],
            deps=["P01-I01-W01"],
        )


def test_plan_wave_cycle_refused() -> None:
    """Chain W01→W02; planning W03 with deps=[W02] is fine, but a back-edge cycles.

    The classic two-cycle test would require *retroactively* mutating an
    existing wave's deps which is out of scope; the spec demands instead
    a stronger version: build a linear chain and then attempt to insert a
    new wave that closes the loop. Concretely: plan W01, plan W02
    (deps=[W01]) — OK. Then attempt to introduce a new wave W03 whose
    ``deps`` include W02 *and* mutate the chain so W01 lists W03. The
    only forward-only construction that produces a cycle is to attempt
    to make the new wave a *parent* of an existing wave by listing the
    existing wave as a dep when the existing wave already lists a
    descendant. We use the explicit case from the spec: W01→W02 then
    plan W02 with deps=[W01] (duplicate-id of W02) — but that's caught
    by the duplicate guard first. The clean cycle assertion is
    therefore a back-edge insertion: plan a chain A→B and then try a
    new wave C with deps=[B, plus_a_descendant_of_C], which requires
    we first arrange the descendant. The minimal reproducer below uses
    a forged state to construct a fictitious cycle: we manually mutate
    one wave's deps after creation to point at the prospective new wave,
    and let plan_wave detect the cycle on insertion.
    """
    state = _seed_iter()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="W1",
        file_scopes=["src/"],
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="W2",
        file_scopes=["src/"],
        deps=["P01-I01-W01"],
    )
    # Forge a back-edge: pretend W01 already depends on a (yet-to-be-planned) W03.
    # plan_wave then tries to insert W03 with deps=[W02]; that closes the loop
    # W01 -> W03 -> W02 -> W01 and must be refused.
    state.waves["P01-I01-W01"].deps = ["P01-I01-W03"]
    with pytest.raises(LifecycleError, match="cycle"):
        plan_wave(
            state,
            wave_id="P01-I01-W03",
            iter_id="P01-I01",
            title="W3",
            file_scopes=["src/"],
            deps=["P01-I01-W02"],
        )
    # The forged back-edge should leave state.waves[W03] absent (rollback).
    assert "P01-I01-W03" not in state.waves


def test_plan_wave_unknown_dep_already_refused() -> None:
    """Sanity: the pre-existing 'unknown dep' guard fires before cycle check."""
    state = _seed_iter()
    with pytest.raises(LifecycleError, match="unknown dep"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="W1",
            file_scopes=["src/"],
            deps=["P01-I01-W99"],
        )


# ---- happy path round-trip ---------------------------------------------------


def test_plan_wave_chain_persists_blocks_and_deps() -> None:
    """A→B→C chain produces deps[]/blocks[] symmetry across every link."""
    state = _seed_iter()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="A",
        file_scopes=["src/"],
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="B",
        file_scopes=["src/"],
        deps=["P01-I01-W01"],
    )
    plan_wave(
        state,
        wave_id="P01-I01-W03",
        iter_id="P01-I01",
        title="C",
        file_scopes=["src/"],
        deps=["P01-I01-W02"],
    )
    assert state.waves["P01-I01-W01"].deps == []
    assert state.waves["P01-I01-W01"].blocks == ["P01-I01-W02"]
    assert state.waves["P01-I01-W02"].deps == ["P01-I01-W01"]
    assert state.waves["P01-I01-W02"].blocks == ["P01-I01-W03"]
    assert state.waves["P01-I01-W03"].deps == ["P01-I01-W02"]
    assert state.waves["P01-I01-W03"].blocks == []


def test_plan_wave_close_round_trip_keeps_blocks_intact() -> None:
    """Closing a dep wave does not touch the persisted blocks index."""
    state = _seed_iter()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="A",
        file_scopes=["src/"],
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="B",
        file_scopes=["src/"],
        deps=["P01-I01-W01"],
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    close_wave(state, wave_id="P01-I01-W01", commit="abc", outcome="ok")
    assert state.waves["P01-I01-W01"].blocks == ["P01-I01-W02"]
