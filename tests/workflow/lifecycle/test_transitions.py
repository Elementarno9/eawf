"""Lifecycle transition tests for roadmap plan staging."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.roadmap_plan import RoadmapPlan
from eawf.kernel.state.enums import ProjectStatus, ScopeKind
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.workflow.lifecycle.transitions import plan_roadmap


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


def test_plan_roadmap_stages_waves_in_dependency_order() -> None:
    plan = RoadmapPlan.model_validate(
        {
            "phase": {"id": "P31", "title": "Plan import"},
            "iters": [
                {
                    "id": "P31-I01",
                    "title": "First iter",
                    "waves": [
                        {
                            "id": "P31-I01-W02",
                            "title": "Second wave",
                            "file_scopes": ["src/b"],
                            "deps": ["P31-I01-W01"],
                            "effort_bucket": "S",
                            "intent": {
                                "problem": "second wave needs staging",
                                "desired_outcome": "second wave is planned",
                                "priority_rationale": "stage after its dep wave",
                            },
                        },
                        {
                            "id": "P31-I01-W01",
                            "title": "First wave",
                            "file_scopes": ["src/a"],
                            "effort_bucket": "XS",
                            "intent": {
                                "problem": "first wave needs staging",
                                "desired_outcome": "first wave is planned",
                                "priority_rationale": "stage the leaf wave first",
                            },
                        },
                    ],
                }
            ],
        }
    )
    state = _empty_state()

    planned = plan_roadmap(state, plan=plan)

    assert planned.phase_id == "P31"
    assert planned.iter_ids == ["P31-I01"]
    assert planned.wave_ids == ["P31-I01-W01", "P31-I01-W02"]
    assert state.iters["P31-I01"].wave_ids == ["P31-I01-W01", "P31-I01-W02"]
    assert state.waves["P31-I01-W01"].blocks == ["P31-I01-W02"]


def test_roadmap_plan_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RoadmapPlan.model_validate(
            {
                "phase": {"id": "P31", "title": "Plan import", "extra": "no"},
                "iters": [{"id": "P31-I01", "title": "First iter"}],
            }
        )
