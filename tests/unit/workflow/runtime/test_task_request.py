"""Building a Task's dispatch request: what is derived, and what is refused."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.identity.urn import parse_qualified_urn
from eawf.kernel.runtime.capsule import StopCondition
from eawf.kernel.runtime.compiled import RuntimeBinding, canonical_digest
from eawf.kernel.runtime.semantic import SemanticToolId, tool_input_schema
from eawf.kernel.spec.common import CriterionSpec, QualityDimension
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2 import Task
from eawf.kernel.state.epoch2.run import RunPurpose, TaskScope
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.native_dispatch import (
    DEFAULT_TOOL_GRANTS,
    DispatchParams,
    compile_for_dispatch,
)
from eawf.runtime.workspace.lease import root_leases
from eawf.surfaces.render.agents import AGENT_REGISTRY
from eawf.workflow.runtime.task_request import compile_task_request
from tests import _provider_helpers as fx
from tests.integration.runtime.daemon.test_native_dispatch import make_canary, root_ctx

#: The report fields eawf fills from the Run, which the worker is not asked for.
DERIVED_REPORT_FIELDS: tuple[str, ...] = ("report_schema_ref", "contract_digest")

pytestmark = pytest.mark.unit

BATCH_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"
MILESTONE_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030"
BASE = "a" * 40
ACTOR = "OP-0001"
EXECUTOR_BODY = next(spec.body for spec in AGENT_REGISTRY if spec.role == "executor")


def criterion(criterion_id: str, text: str) -> CriterionSpec:
    """Return one typed criterion with *criterion_id* and *text*."""
    return CriterionSpec(
        id=criterion_id,
        text=text,
        kind="functional_suitability",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["G-01"],
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="uv run pytest tests/unit/workflow exits zero",
    )


CRITERIA = (
    criterion("CR-01", "the request compiles for the planned Task"),
    criterion("CR-02", "the capsule carries the Task criteria digest"),
)


def make_task(**overrides: Any) -> Task:
    """Return a planned Task holding two criteria and one write claim."""
    fields: dict[str, Any] = {
        "uid": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "key": "EAWF-0042",
        "urn": fx.TASK_URN,
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": "2026-09-08T00:00:00Z",
        "updated_at": "2026-09-08T00:00:00Z",
        "priority": "P1",
        "intent": "Compile the dispatch request from the Task record",
        "contract_revision": 1,
        "status": "PLANNED",
        "batch_ref": BATCH_URN,
        "due_scope": MILESTONE_URN,
        "criteria": list(CRITERIA),
        "write_claims": ["src/eawf/workflow/runtime"],
    }
    fields.update(overrides)
    return Task.model_validate(fields)


def layered_config(**kwargs: Any) -> dict[str, dict[str, Any]]:
    """Return each layer's whole config document, holding the fixture runtime."""
    return {
        layer: {"project": {"code": "EAWF"}, "runtime": runtime}
        for layer, runtime in fx.documents(**kwargs).items()
    }


def build(
    task: Task | None = None,
    *,
    config: dict[str, dict[str, Any]] | None = None,
    bindings: list[RuntimeBinding] | None = None,
    unbound: dict[str, str] | None = None,
    provider: str = "codex",
    budget: int | None = 200_000,
    now: datetime = fx.COMPILED_AT,
) -> DispatchParams:
    """Compile the request of *task* with the fixture configuration."""
    return compile_task_request(
        task if task is not None else make_task(),
        parse_qualified_urn(fx.RUN_URN),
        provider=provider,
        config=config if config is not None else layered_config(),
        registry=fx.registry(),
        bindings=bindings if bindings is not None else [fx.binding()],
        unbound=unbound if unbound is not None else {},
        budget=budget,
        base=BASE,
        actor=ACTOR,
        now=now,
    )


def with_wall_seconds(wall_seconds: int) -> dict[str, dict[str, Any]]:
    """Return the layered config whose profile limits wall time to *wall_seconds*."""
    limits = {"wall_seconds": wall_seconds, "output_bytes": 1_048_576, "child_runs": 2}
    return layered_config(profiles=[fx.profile_document(limits=limits)])


def uncertified() -> RuntimeBinding:
    """Return the fixture binding with no model certified."""
    return fx.binding(certified_models=[])


# ---- the compile request -----------------------------------------------------


def test_compile_request_is_the_task_scope_implement_executor_request() -> None:
    params = build()
    expected_scope = TaskScope(
        scope_kind="task",
        purpose=RunPurpose.IMPLEMENT,
        task_ref=parse_qualified_urn(fx.TASK_URN),
        write_set=("src/eawf/workflow/runtime",),
    )
    request = params.compile_request
    assert str(params.urn) == fx.RUN_URN
    assert str(request.run_ref) == fx.RUN_URN
    assert request.run_scope == expected_scope
    assert request.purpose is RunPurpose.IMPLEMENT
    assert request.agent_role is AgentSessionRole.EXECUTOR
    assert request.unattended is True
    assert params.actor == ACTOR


def test_idempotency_key_is_stable_for_one_task_and_run() -> None:
    assert build().idempotency_key == build().idempotency_key


def test_a_task_without_write_claims_is_refused_naming_the_task_field() -> None:
    with pytest.raises(DaemonValidationError) as raised:
        build(make_task(write_claims=[]))
    message = str(raised.value)
    assert "dispatch_write_claims_required" in message
    assert "EAWF-0042 declares no write_claims" in message
    assert "updates.write_claims" in message


def test_a_task_with_one_write_claim_compiles_to_that_write_set() -> None:
    params = build(make_task(write_claims=["src"]))
    assert params.compile_request.run_scope.write_set == ("src",)


def test_bindings_are_narrowed_to_the_chosen_provider() -> None:
    other = fx.binding(
        driver_ref=fx.OTHER_DRIVER_REF,
        manifest=fx.manifest_document(manifest_id="claude-sdk", provider_id="claude"),
    )
    params = build(bindings=[other, fx.binding()])
    assert [b.manifest.provider_id for b in params.bindings] == ["codex"]


def test_a_provider_no_binding_serves_is_refused() -> None:
    with pytest.raises(DaemonValidationError, match="no_eligible_profile"):
        build(provider="claude")


def test_a_provider_with_an_empty_binding_set_is_refused() -> None:
    with pytest.raises(DaemonValidationError, match="no_eligible_profile"):
        build(bindings=[])


def test_an_unbound_provider_refusal_names_the_cause_and_remedy() -> None:
    cause = "runtime claude-code is unbound (never_certified: no row); run `eawf runtime certify`"
    with pytest.raises(DaemonValidationError) as caught:
        build(provider="claude", unbound={"claude": cause})
    assert str(caught.value).endswith(f"no installed driver serves provider 'claude': {cause}")


def test_another_providers_unbound_cause_is_not_named() -> None:
    with pytest.raises(DaemonValidationError) as caught:
        build(provider="claude", unbound={"codex": "runtime codex is unbound (expired: x)"})
    assert str(caught.value).endswith("no installed driver serves provider 'claude'")


# ---- the capsule -------------------------------------------------------------


def test_capsule_is_pinned_to_the_task_criteria_and_the_caller_budget() -> None:
    capsule = build(budget=200_000).capsule
    assert capsule.criteria_digest == canonical_digest(
        [row.model_dump(mode="json") for row in CRITERIA]
    )
    assert capsule.stop_conditions == (
        StopCondition.SCOPE_AMBIGUITY,
        StopCondition.APPROVAL_REQUIRED,
        StopCondition.BUDGET_EXHAUSTED,
        StopCondition.CONTRACT_MISMATCH,
        StopCondition.QUARANTINE,
    )
    assert capsule.tool_grants == (
        "workspace_apply_patch",
        "repo_read",
        "repo_search",
        *DEFAULT_TOOL_GRANTS,
    )
    assert DEFAULT_TOOL_GRANTS == ("submit_report",)
    assert capsule.tool_denials == ()
    assert capsule.token_budget == 200_000
    assert capsule.report_schema_ref == "schema://executor-report/v1"


def test_no_budget_leaves_the_capsule_uncapped() -> None:
    assert build(budget=None).capsule.token_budget is None


def test_a_single_token_budget_is_kept() -> None:
    assert build(budget=1).capsule.token_budget == 1


@pytest.mark.parametrize("budget", [0, -1])
def test_a_budget_below_one_is_refused(budget: int) -> None:
    with pytest.raises(ValidationError, match="token_budget"):
        build(budget=budget)


def test_reordering_criteria_changes_the_digest() -> None:
    forward = build(make_task(criteria=list(CRITERIA))).capsule.criteria_digest
    reverse = build(make_task(criteria=list(reversed(CRITERIA)))).capsule.criteria_digest
    assert forward != reverse


def test_a_single_criterion_task_digests_that_criterion() -> None:
    capsule = build(make_task(criteria=[CRITERIA[0]])).capsule
    assert capsule.criteria_digest == canonical_digest([CRITERIA[0].model_dump(mode="json")])


# ---- the prompt --------------------------------------------------------------


def test_prompt_sections_follow_body_task_intent_criteria_claims_finish(tmp_path: Path) -> None:
    prompt = build().prompt
    task = make_task()
    marks = [
        EXECUTOR_BODY.rstrip(),
        f"# Task {task.key}",
        "## Intent",
        task.intent,
        "## Criteria",
        f"- CR-01 {CRITERIA[0].text}",
        f"- CR-02 {CRITERIA[1].text}",
        "## Write claims",
        "- src/eawf/workflow/runtime",
        "## Changing files",
        "`workspace_apply_patch`",
        "## Finish",
        "`submit_report`",
    ]
    positions = [prompt.index(mark) for mark in marks]
    assert positions == sorted(positions)
    assert prompt.startswith(EXECUTOR_BODY.rstrip())
    assert prompt.rstrip().endswith("from your Run and stores the body.")
    assert len(prompt) <= 200_000
    assert str(tmp_path) not in prompt
    assert str(Path.cwd()) not in prompt


def test_an_oversized_prompt_is_refused() -> None:
    claims = [f"src/{index:04d}/{'x' * 480}" for index in range(450)]
    with pytest.raises(ValidationError, match="prompt"):
        build(make_task(write_claims=claims))


# ---- lease, base and provider documents --------------------------------------


@pytest.mark.parametrize(
    ("wall_seconds", "ttl"),
    [
        (1, 601),
        (2400, 3000),
        (85_799, 86_399),
        (85_800, 86_400),
        (85_801, 86_400),
        (86_400, 86_400),
    ],
)
def test_lease_ttl_is_compiled_wall_time_plus_grace_capped_at_a_day(
    wall_seconds: int, ttl: int
) -> None:
    assert build(config=with_wall_seconds(wall_seconds)).lease_ttl_seconds == ttl


def test_base_is_the_commit_passed_in() -> None:
    assert build().base == BASE


def test_provider_documents_are_each_layers_runtime_mapping() -> None:
    config = layered_config()
    assert build(config=config).provider_documents == {
        layer: document["runtime"] for layer, document in config.items()
    }


def test_an_unknown_provider_layer_is_refused() -> None:
    config = {**layered_config(), "branch": {"runtime": {}}}
    with pytest.raises(DaemonValidationError, match="dispatch_provider_config_invalid"):
        build(config=config)


def test_a_runtime_entry_that_is_not_a_mapping_is_refused() -> None:
    config = {**layered_config(), "repository": {"runtime": ["profiles"]}}
    with pytest.raises(DaemonValidationError, match="dispatch_provider_config_invalid"):
        build(config=config)


def test_a_layer_without_runtime_contributes_nothing() -> None:
    config = {**layered_config(), "repository": {"project": {"code": "EAWF"}}}
    assert build(config=config).provider_documents["repository"] == {}


def test_an_empty_config_names_no_route() -> None:
    with pytest.raises(DaemonValidationError, match="no_matching_route"):
        build(config={})


# ---- refusal before any lease ------------------------------------------------


def test_an_uncertified_binding_is_refused_by_the_compiler() -> None:
    with pytest.raises(DaemonValidationError, match="model_uncertified"):
        build(bindings=[uncertified()])


def test_compile_for_dispatch_refuses_an_uncertified_request_before_a_lease(
    tmp_path: Path,
) -> None:
    canary = make_canary(tmp_path / "repo")
    params = build().model_copy(update={"bindings": (uncertified(),)})

    with pytest.raises(DaemonValidationError, match="model_uncertified"):
        compile_for_dispatch(params, now=fx.COMPILED_AT)

    assert root_leases(root_ctx(canary, tmp_path / "runtime")) == ()


def test_the_finish_asks_only_for_report_content_eawf_cannot_derive() -> None:
    """The worker is told the content fields and that eawf fills the contract."""
    finish = build().prompt.split("## Finish", 1)[1]
    schema = tool_input_schema(SemanticToolId.SUBMIT_REPORT)
    content = [name for name in schema["properties"] if name not in DERIVED_REPORT_FIELDS]
    assert all(f"`{name}`" in finish for name in content if name != "tool_id")
    assert "contract digest" in finish
    assert all(f"`{name}`" not in finish for name in DERIVED_REPORT_FIELDS)


def test_the_prompt_names_every_patch_field_a_worker_fills() -> None:
    """The worker is told the one write tool and each field its payload takes."""
    section = build().prompt.split("## Changing files", 1)[1].split("## Finish", 1)[0]
    schema = tool_input_schema(SemanticToolId.WORKSPACE_APPLY_PATCH)
    file_fields = schema["properties"]["files"]["items"]["properties"]
    assert "`workspace_apply_patch`" in section
    assert "`files`" in section
    assert all(f"`{name}`" in section for name in file_fields)
