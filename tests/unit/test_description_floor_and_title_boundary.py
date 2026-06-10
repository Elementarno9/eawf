"""Tests for the description-floor model validator and the title-clarity
mutation boundary (P29-I07-W02).

Two surfaces:

- The ``_DescribedEntity`` ``model_validator`` that floors a *present*
  ``description`` on Phase / Iter / Wave / Decision (grandfathering ``None``)
  and rejects a description that merely restates the title.
- The EAWF016 title-clarity gate wired into ``plan_wave`` (the
  mutation-boundary rejection the contract requires), re-raised as a
  ``LifecycleError``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    DecisionStatus,
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    CurrentPointers,
    Decision,
    Iter,
    Phase,
    Project,
    State,
    Wave,
)
from eawf.workflow.lifecycle.transitions import (
    LifecycleError,
    open_iter,
    open_phase,
    plan_wave,
)
from tests.conftest import make_intent

_NOW = datetime.now(UTC)


# ---- description floor: grandfather None ------------------------------------


def test_wave_description_none_is_grandfathered() -> None:
    wave = Wave(
        id="P01-I01-W01",
        iter_id="P01-I01",
        title="Add the gate",
        status=WaveStatus.PENDING,
        opened_at=_NOW,
    )
    assert wave.description is None


def test_phase_description_none_is_grandfathered() -> None:
    phase = Phase(
        id="P01",
        scope_id="QR",
        title="Bootstrap the kernel",
        status=PhaseStatus.PLANNED,
        opened_at=_NOW,
    )
    assert phase.description is None


# ---- description floor: too short -------------------------------------------


def test_wave_description_below_floor_rejected() -> None:
    with pytest.raises(ValidationError, match="too short to be useful"):
        Wave(
            id="P01-I01-W01",
            iter_id="P01-I01",
            title="Add the gate",
            description="short",
            status=WaveStatus.PENDING,
            opened_at=_NOW,
        )


def test_wave_description_whitespace_only_counts_as_too_short() -> None:
    with pytest.raises(ValidationError, match="too short to be useful"):
        Wave(
            id="P01-I01-W01",
            iter_id="P01-I01",
            title="Add the gate",
            description="           ",
            status=WaveStatus.PENDING,
            opened_at=_NOW,
        )


def test_iter_description_at_floor_is_accepted() -> None:
    # Exactly 12 non-whitespace chars clears the floor.
    it = Iter(
        id="P01-I01",
        phase_id="P01",
        title="Land the floor",
        description="ABCDEFGHIJKL",
        status=IterStatus.PLANNED,
        opened_at=_NOW,
    )
    assert it.description == "ABCDEFGHIJKL"


# ---- description floor: title-prefix duplicate ------------------------------


def test_decision_description_pure_restatement_rejected() -> None:
    # A near-pure restatement (title typed again, only trailing punctuation
    # added) carries no signal beyond the title and is rejected.
    with pytest.raises(ValidationError, match="merely repeats the title"):
        Decision(
            id="D17",
            scope_id="QR",
            title="Use Textual for the TUI",
            description="Use Textual for the TUI.",
            rationale="r",
            status=DecisionStatus.ACTIVE,
            created_at=_NOW,
        )


def test_decision_description_adding_the_why_is_accepted() -> None:
    # Opening with the title and then adding the *why* carries new content and
    # is accepted — the rule targets pure restatement, not any prefix overlap.
    decision = Decision(
        id="D17",
        scope_id="QR",
        title="Use Textual for the TUI",
        description="Use Textual for the TUI because Rich lacks a mode chassis.",
        rationale="r",
        status=DecisionStatus.ACTIVE,
        created_at=_NOW,
    )
    assert decision.description is not None


def test_decision_description_distinct_from_title_accepted() -> None:
    decision = Decision(
        id="D17",
        scope_id="QR",
        title="Use Textual for the TUI",
        description="Rich lacks a mode chassis; the rebuild needs one for live views.",
        rationale="r",
        status=DecisionStatus.ACTIVE,
        created_at=_NOW,
    )
    assert decision.description is not None


def test_wave_description_pure_restatement_is_case_insensitive() -> None:
    with pytest.raises(ValidationError, match="merely repeats the title"):
        Wave(
            id="P01-I01-W01",
            iter_id="P01-I01",
            title="Add the gate now",
            description="add the GATE NOW",
            status=WaveStatus.PENDING,
            opened_at=_NOW,
        )


def test_wave_description_preserving_truncated_title_is_accepted() -> None:
    # The v1.0 -> v1.1 migration truncates an over-length title and keeps the
    # full original in description; that preserved superset must not trip the
    # restatement rule (it carries the truncated tail as new content).
    title = "Wave " + "x" * 67  # exactly 72 chars
    wave = Wave(
        id="P01-I01-W01",
        iter_id="P01-I01",
        title=title,
        description="Wave " + "x" * 174,  # full pre-truncation original
        status=WaveStatus.PENDING,
        opened_at=_NOW,
    )
    assert wave.description is not None


# ---- mutation boundary: plan_wave rejects unclear titles --------------------


def _seed_iter() -> State:
    state = State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _NOW.isoformat(),
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
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="First iter")
    return state


def test_plan_wave_rejects_conventional_commit_prefix_title() -> None:
    state = _seed_iter()
    with pytest.raises(LifecycleError, match="title-clarity"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="feat: add the lint",
            file_scopes=["src/"],
            effort_bucket="M",
            intent=make_intent(),
        )
    # Rejected at the boundary: nothing persisted.
    assert "P01-I01-W01" not in state.waves


def test_plan_wave_rejects_bare_id_title() -> None:
    state = _seed_iter()
    with pytest.raises(LifecycleError, match="bare id"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="W01",
            file_scopes=["src/"],
            effort_bucket="M",
            intent=make_intent(),
        )


def test_plan_wave_accepts_clear_title() -> None:
    state = _seed_iter()
    wave = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="Add the title-clarity gate",
        file_scopes=["src/"],
        effort_bucket="M",
        intent=make_intent(),
    )
    assert wave.title == "Add the title-clarity gate"


def test_plan_wave_structural_guard_precedes_title_check() -> None:
    # A self-dep is a structural DAG error; it reports before the title gate
    # even when the title is also bad, so the DAG guard precedence is kept.
    state = _seed_iter()
    with pytest.raises(LifecycleError, match="cannot depend on itself"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="W01",
            file_scopes=["src/"],
            deps=["P01-I01-W01"],
            effort_bucket="M",
            intent=make_intent(),
        )
