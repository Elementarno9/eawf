"""The orchestration contract and the concurrency plan derived from the Task graph.

SURF-094 and SURF-110 hold when every dispatching surface carries the one
resolved contract; SURF-095 when the plan is derived from dependency edges
and write claims, rendered before dispatch, and a hand-typed ordering is a
plan defect rather than a flag; SURF-093 and SURF-096 when the graph fields
are declared once, on the Task, and travel from the plan to the record the
scheduler and the coordinator read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.state.epoch2.plan_revision import PlanBody, PlannedTask, plan_content_digest
from eawf.kernel.state.epoch2.run import BatchScope, RunPurpose
from eawf.surfaces.render.skills.registry import SKILL_REGISTRY
from eawf.workflow.planning.apply import _planned_tasks
from eawf.workflow.planning.lenses import PlanFindingCode, PlanLens, run_plan_lenses
from eawf.workflow.planning.orchestration import (
    ORCHESTRATION_CONTRACT,
    ORCHESTRATION_STATEMENT,
    GraphTask,
    derive_concurrency_plan,
)
from eawf.workflow.skills import dispatch as dispatch_skill
from eawf.workflow.skills.bodies.dispatch import DispatchBody
from eawf.workflow.skills.catalog import shipped_skill_specs
from eawf.workflow.skills.engine import SkillContext
from tests.unit.workflow.planning.lenses.test_ownership_lens import SLOT, _task, body_payload

pytestmark = pytest.mark.unit

AT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


# ---- SURF-094 / SURF-110: the contract ships as a dispatch default -----------


def test_surf_094_contract_is_one_resolved_default() -> None:
    assert ORCHESTRATION_CONTRACT.coordinator == "proposes_and_dispatches"
    assert ORCHESTRATION_CONTRACT.coordinator_write_scope == "none"
    assert ORCHESTRATION_CONTRACT.execution == "child_or_sibling_runs_under_own_task_scope"
    assert ORCHESTRATION_CONTRACT.concurrency == "derived_from_task_graph"


def test_surf_094_contract_refuses_any_other_value() -> None:
    with pytest.raises(ValidationError):
        type(ORCHESTRATION_CONTRACT).model_validate({"coordinator_write_scope": "src"})


def test_surf_094_coordination_report_carries_the_contract_by_default() -> None:
    body = DispatchBody(batch_ref="BAT-0001", outcome="frontier_empty", reason="empty")
    assert body.model_dump(mode="json")["contract"] == ORCHESTRATION_CONTRACT.model_dump(
        mode="json"
    )


def test_surf_094_shipped_dispatch_page_states_the_contract() -> None:
    page = next(spec.body for spec in shipped_skill_specs() if spec.skill_name == "dispatch")
    assert ORCHESTRATION_STATEMENT in page
    assert "--max-parallel" not in page


def test_surf_110_registry_dispatch_body_states_the_contract() -> None:
    body = next(spec.body for spec in SKILL_REGISTRY if spec.skill_name == "dispatch")
    assert ORCHESTRATION_STATEMENT in body
    assert "--max-parallel" not in body


def test_surf_094_coordinating_scope_holds_no_write_scope() -> None:
    batch = f"{SLOT}/batch/BAT-0001"
    with pytest.raises(ValidationError):
        BatchScope(scope_kind="batch", batch_ref=batch, purpose=RunPurpose.PLAN, write_set=("src",))
    with pytest.raises(ValidationError):
        BatchScope(scope_kind="batch", batch_ref=batch, purpose=RunPurpose.IMPLEMENT)


# ---- SURF-095: the plan is derived from the graph ----------------------------


def test_surf_095_empty_graph_derives_no_stage() -> None:
    plan = derive_concurrency_plan(())
    assert plan.stages == ()
    assert plan.fan_out == ()


def test_surf_095_single_task_runs_alone_on_stage_one() -> None:
    plan = derive_concurrency_plan((GraphTask(ref="A"),))
    assert plan.stages == (("A",),)


def test_surf_095_independent_tasks_fan_out() -> None:
    plan = derive_concurrency_plan(
        (GraphTask(ref="A", write_claims=("src/a",)), GraphTask(ref="B", write_claims=("src/b",)))
    )
    assert plan.stages == (("A", "B"),)
    assert plan.sequential == ()
    assert plan.reasons == {}


def test_surf_095_dependency_forces_a_later_stage_and_says_why() -> None:
    plan = derive_concurrency_plan(
        (
            GraphTask(ref="C", depends_on=("B",)),
            GraphTask(ref="B", depends_on=("A",)),
            GraphTask(ref="A"),
        )
    )
    assert plan.stages == (("A",), ("B",), ("C",))
    assert plan.reasons == {"B": ("depends on A",), "C": ("depends on B",)}
    assert plan.sequential == ("B", "C")


def test_surf_095_overlapping_write_claims_never_share_a_stage() -> None:
    plan = derive_concurrency_plan(
        (
            GraphTask(ref="A", write_claims=("src",)),
            GraphTask(ref="B", write_claims=("src/eawf/x.py",)),
            GraphTask(ref="C", write_claims=("docs",)),
        )
    )
    assert plan.stages == (("A", "C"), ("B",))
    assert plan.reasons["B"] == ("writes src/eawf/x.py, which A also writes",)


def test_surf_095_exclusive_task_takes_a_stage_of_its_own() -> None:
    plan = derive_concurrency_plan(
        (GraphTask(ref="A"), GraphTask(ref="X", exclusive=True), GraphTask(ref="B"))
    )
    assert plan.stages == (("A", "B"), ("X",))
    assert plan.reasons["X"] == ("runs alone, so it cannot run beside A",)


def test_surf_095_task_after_an_exclusive_one_moves_past_it() -> None:
    plan = derive_concurrency_plan((GraphTask(ref="X", exclusive=True), GraphTask(ref="A")))
    assert plan.stages == (("X",), ("A",))
    assert plan.reasons["A"] == ("X runs alone",)


def test_surf_095_edge_to_finished_work_orders_nothing() -> None:
    plan = derive_concurrency_plan((GraphTask(ref="B", depends_on=("DONE",)),))
    assert plan.stages == (("B",),)
    assert plan.reasons == {}


def test_surf_095_plan_names_every_forced_wait() -> None:
    plan = derive_concurrency_plan(
        (GraphTask(ref="A"), GraphTask(ref="B"), GraphTask(ref="C", depends_on=("A",)))
    )
    assert plan.fan_out == ("A", "B")
    assert plan.sequential == ("C",)
    assert plan.reasons == {"C": ("depends on A",)}


def test_surf_095_repeated_task_is_refused() -> None:
    with pytest.raises(ValueError, match="once"):
        derive_concurrency_plan((GraphTask(ref="A"), GraphTask(ref="A")))


def test_surf_095_cycle_is_refused() -> None:
    with pytest.raises(ValueError, match="cycle"):
        derive_concurrency_plan(
            (GraphTask(ref="A", depends_on=("B",)), GraphTask(ref="B", depends_on=("A",)))
        )


def _with_intent(intent: str) -> PlanBody:
    payload = body_payload()
    payload["tasks"][0]["intent"] = intent
    return PlanBody.model_validate(payload)


@pytest.mark.parametrize(
    "intent",
    [
        "run these in parallel with the loader work",
        "Execute sequentially after the schema lands",
        "this one runs alone, never beside another agent",
        "never concurrent with the migration",
        "fan out the three probes",
    ],
)
def test_surf_095_ordering_typed_into_an_intent_is_a_plan_defect(intent: str) -> None:
    findings = [
        finding
        for finding in run_plan_lenses(_with_intent(intent))
        if finding.code is PlanFindingCode.DAG_ORDERING_IN_PROSE
    ]
    assert len(findings) == 1
    assert findings[0].lens is PlanLens.DAG
    assert findings[0].severity == "blocking"
    assert "depends_on" in findings[0].remediation


def test_surf_095_plain_intent_earns_no_ordering_finding() -> None:
    findings = run_plan_lenses(_with_intent("parse the parallelogram fixtures"))
    assert PlanFindingCode.DAG_ORDERING_IN_PROSE not in {finding.code for finding in findings}


# ---- SURF-093 / SURF-096: declared once, on the Task -------------------------


def test_surf_096_exclusive_is_declarable_on_a_planned_task() -> None:
    task = PlannedTask.model_validate({**_task(f"{SLOT}/task/EAWF-0001"), "exclusive": True})
    assert task.exclusive is True


def test_surf_093_apply_carries_the_graph_onto_the_task_record() -> None:
    payload = body_payload()
    payload["tasks"][1]["depends_on"] = [payload["tasks"][0]["urn"]]
    payload["tasks"][1]["exclusive"] = True
    tasks = _planned_tasks(PlanBody.model_validate(payload), at=AT)
    assert tasks[0].write_claims == ("src/eawf/product/base.py",)
    assert tasks[1].exclusive is True
    assert [str(ref) for ref in tasks[1].depends_on] == [payload["tasks"][0]["urn"]]


def test_surf_096_unset_exclusive_keeps_the_approved_plan_digest() -> None:
    body = PlanBody.model_validate(body_payload())
    assert "exclusive" not in body.model_dump(mode="json")["tasks"][0]
    explicit = body_payload()
    explicit["tasks"][0]["exclusive"] = False
    assert plan_content_digest(PlanBody.model_validate(explicit)) == plan_content_digest(body)


def _task_document() -> dict[str, Any]:
    payload = body_payload()
    payload["tasks"][1]["depends_on"] = [payload["tasks"][0]["urn"]]
    payload["tasks"][1]["write_claims"] = ["src/eawf/product/base.py"]
    payload["tasks"][0]["exclusive"] = True
    tasks = _planned_tasks(PlanBody.model_validate(payload), at=AT)
    return {"task": {task.key: task.model_dump(mode="json") for task in tasks}}


def test_surf_095_task_rows_project_their_graph() -> None:
    projection = build_route_projection(
        route="task.detail",
        document=_task_document(),
        cursor=1,
        scope_id="scope",
        generated_at=AT,
    )
    facts = {row.key: dict(row.facts) for row in projection.rows}
    assert facts["EAWF-0001"]["exclusive"] == "true"
    assert facts["EAWF-0002"]["depends_on"] == "EAWF-0001"
    assert facts["EAWF-0002"]["write_claims"] == "src/eawf/product/base.py"


def test_surf_095_dispatch_derives_its_plan_from_the_projected_graph() -> None:
    projection = build_route_projection(
        route="task.detail",
        document=_task_document(),
        cursor=1,
        scope_id="scope",
        generated_at=AT,
    )
    answers = {dispatch_skill.TASK_READ_METHOD: projection.model_dump(mode="json")}
    skill = dispatch_skill.DispatchSkill(caller=lambda method, _params: answers.get(method, {}))
    result = skill.action(SkillContext(scope="s", session="s", args={"batch_ref": "BAT-0001"}))
    body = DispatchBody.model_validate(result.body)
    assert body.plan.stages == [["EAWF-0001"], ["EAWF-0002"]]
    assert body.plan.reasons["EAWF-0002"] == ["depends on EAWF-0001"]
    assert body.frontier == ["EAWF-0001"]
    assert body.contract == ORCHESTRATION_CONTRACT


def test_surf_095_a_typed_parallelism_number_is_not_a_dispatch_flag() -> None:
    skill = dispatch_skill.DispatchSkill(caller=lambda _method, _params: {})
    result = skill.action(
        SkillContext(scope="s", session="s", args={"batch_ref": "BAT-0001", "max_parallel": 4})
    )
    body = DispatchBody.model_validate(result.body)
    assert body.outcome == "blocked"
    assert body.stopped_on == ["invocation_undeclared"]
