"""The authority capsule is frozen, digest-bound, and a positive allow-list.

A capsule is the only statement of what one Run may do, so it has to
refuse two things outright: an edit after sealing, and a grant nobody
catalogued. Both are checked here from the outside -- an assignment
raises, a hand-edited field fails to load, and a tool id the catalog does
not carry is refused at validation rather than at call time.

The digest is pinned to the three things that decide authority: the
grants, the denials, and the budget ceilings. Any of them moving moves
``contract_digest``, which is what the provider process echoes back on
every call, so a widened capsule cannot pass as the one that was sealed.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.capsule import AuthorityCapsule, CapsuleBudget, StopCondition
from eawf.kernel.runtime.semantic import SemanticToolId

pytestmark = pytest.mark.unit

RUN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
OTHER_RUN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"
TASK = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
BATCH = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"
GRANTS = ("repo_read", "repo_search", "workspace_apply_patch", "submit_candidate", "submit_report")
BUDGET = {"wall_seconds": 2400, "output_bytes": 1_048_576, "tokens": 200_000}


def digest(character: str) -> str:
    """Return a well-formed digest made of one repeated hex *character*."""
    return f"sha256:{character * 64}"


def capsule_fields(**overrides: Any) -> dict[str, Any]:
    """Return a sealable executor capsule with *overrides* applied."""
    fields: dict[str, Any] = {
        "run_ref": RUN,
        "scope_ref": TASK,
        "scope_digest": digest("a"),
        "agent_role": "executor",
        "purpose": "implement",
        "authority": {
            "state": "proposal_only",
            "workspace": "scoped_write",
            "git": "read_metadata",
        },
        "tool_grants": list(GRANTS),
        "tool_denials": ["run_scoped_command"],
        "filesystem_policy_ref": "policy://filesystem/task-workspace",
        "budget": dict(BUDGET),
        "criteria_digest": digest("b"),
        "policy_digest": digest("c"),
        "compiled_spec_digest": digest("d"),
        "report_schema_ref": "schema://report/executor/v1",
        "stop_conditions": ["scope_ambiguity", "approval_required", "budget_exhausted"],
    }
    fields.update(overrides)
    return fields


def review_fields(**overrides: Any) -> dict[str, Any]:
    """Return a sealable read-only reviewer capsule over one Batch."""
    fields: dict[str, Any] = {
        "scope_ref": BATCH,
        "agent_role": "reviewer",
        "purpose": "review",
        "authority": {"state": "read_only", "workspace": "read_only"},
        "tool_grants": ["repo_read", "submit_report"],
        "tool_denials": [],
    }
    fields.update(overrides)
    return capsule_fields(**fields)


# ---- immutability -------------------------------------------------------------


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("tool_grants", ("repo_read",)),
        ("tool_denials", ()),
        ("report_schema_ref", "schema://report/planner/v1"),
        ("contract_digest", digest("e")),
    ],
)
def test_capsule_refuses_assignment(field_name: str, value: Any) -> None:
    capsule = AuthorityCapsule.seal(capsule_fields())
    before = getattr(capsule, field_name)
    with pytest.raises(ValidationError, match="frozen"):
        setattr(capsule, field_name, value)
    assert getattr(capsule, field_name) == before


def test_capsule_refuses_assignment_on_its_nested_records() -> None:
    capsule = AuthorityCapsule.seal(capsule_fields())
    with pytest.raises(ValidationError, match="frozen"):
        capsule.budget.wall_seconds = 86_400
    with pytest.raises(ValidationError, match="frozen"):
        capsule.authority.workspace = "scoped_write"
    assert capsule.budget.wall_seconds == BUDGET["wall_seconds"]


def test_capsule_round_trips_through_its_own_dump() -> None:
    capsule = AuthorityCapsule.seal(capsule_fields())
    assert AuthorityCapsule.model_validate(capsule.model_dump()) == capsule
    assert hash(AuthorityCapsule.model_validate(capsule.model_dump())) == hash(capsule)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("tool_grants", ["repo_read", "repo_search", "workspace_apply_patch", "submit_report"]),
        ("tool_denials", []),
        ("scope_digest", digest("f")),
    ],
)
def test_capsule_refuses_a_field_edited_after_sealing(field_name: str, value: Any) -> None:
    document = AuthorityCapsule.seal(capsule_fields()).model_dump(mode="json")
    document[field_name] = value
    with pytest.raises(ValidationError, match="contract_digest does not match"):
        AuthorityCapsule.model_validate(document)


# ---- the digest binds authority ------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"tool_grants": [*GRANTS, "budget_status"]},
        {"tool_grants": list(GRANTS[:-1])},
        {"tool_denials": []},
        {"tool_denials": ["run_scoped_command", "budget_status"]},
        {"budget": {**BUDGET, "wall_seconds": 2401}},
        {"budget": {**BUDGET, "output_bytes": 2_097_152}},
        {"budget": {**BUDGET, "tokens": 200_001}},
        {"budget": {**BUDGET, "child_runs": 4}},
    ],
    ids=[
        "grant-added",
        "grant-removed",
        "denial-removed",
        "denial-added",
        "wall-ceiling",
        "output-ceiling",
        "token-ceiling",
        "child-ceiling",
    ],
)
def test_contract_digest_moves_with_grants_denials_and_budget(overrides: dict[str, Any]) -> None:
    sealed = AuthorityCapsule.seal(capsule_fields())
    changed = AuthorityCapsule.seal(capsule_fields(**overrides))
    assert changed.contract_digest != sealed.contract_digest


def test_contract_digest_is_stable_for_equal_content() -> None:
    assert (
        AuthorityCapsule.seal(capsule_fields()).contract_digest
        == AuthorityCapsule.seal(capsule_fields()).contract_digest
    )


def test_seal_computes_the_digest_the_caller_never_spells() -> None:
    capsule = AuthorityCapsule.seal(capsule_fields())
    assert capsule.contract_digest.startswith("sha256:")
    assert capsule.contract_digest != f"sha256:{'0' * 64}"


# ---- tool resolution -----------------------------------------------------------


def test_empty_tool_grants_yield_no_semantic_tools() -> None:
    capsule = AuthorityCapsule.seal(capsule_fields(tool_grants=[], tool_denials=[]))
    assert capsule.semantic_tools == ()


def test_a_denial_removes_a_granted_tool() -> None:
    capsule = AuthorityCapsule.seal(
        capsule_fields(tool_grants=["repo_read", "repo_search"], tool_denials=["repo_search"])
    )
    assert capsule.semantic_tools == (SemanticToolId.REPO_READ,)


def test_a_denial_of_an_ungranted_tool_changes_nothing() -> None:
    capsule = AuthorityCapsule.seal(
        capsule_fields(tool_grants=["repo_read"], tool_denials=["budget_status"])
    )
    assert capsule.semantic_tools == (SemanticToolId.REPO_READ,)


def test_denying_every_grant_yields_no_semantic_tools() -> None:
    capsule = AuthorityCapsule.seal(
        capsule_fields(tool_grants=["repo_read"], tool_denials=["repo_read"])
    )
    assert capsule.semantic_tools == ()


def test_semantic_tools_keep_grant_order() -> None:
    capsule = AuthorityCapsule.seal(capsule_fields())
    assert capsule.semantic_tools == (
        SemanticToolId.REPO_READ,
        SemanticToolId.REPO_SEARCH,
        SemanticToolId.WORKSPACE_APPLY_PATCH,
        SemanticToolId.SUBMIT_CANDIDATE,
        SemanticToolId.SUBMIT_REPORT,
    )


@pytest.mark.parametrize("field_name", ["tool_grants", "tool_denials"])
@pytest.mark.parametrize("tool_id", ["request_integration", "request_worktree", "repo"])
def test_capsule_refuses_a_tool_the_catalog_does_not_carry(field_name: str, tool_id: str) -> None:
    with pytest.raises(ValidationError, match="names no catalog tool"):
        AuthorityCapsule.seal(capsule_fields(**{field_name: ["repo_read", tool_id]}))


@pytest.mark.parametrize("tool_id", ["REPO_READ", "repo read", "", "_repo_read"])
def test_capsule_refuses_a_tool_id_the_grammar_denies(tool_id: str) -> None:
    with pytest.raises(ValidationError):
        AuthorityCapsule.seal(capsule_fields(tool_grants=[tool_id]))


def test_capsule_refuses_a_repeated_grant() -> None:
    with pytest.raises(ValidationError, match="appears more than once"):
        AuthorityCapsule.seal(capsule_fields(tool_grants=["repo_read", "repo_read"]))


# ---- scope and lineage rules ---------------------------------------------------


@pytest.mark.parametrize("tool_id", ["workspace_apply_patch", "submit_candidate"])
def test_a_writing_tool_needs_a_mutating_task_scoped_run(tool_id: str) -> None:
    with pytest.raises(ValidationError, match="mutating task-scoped Run"):
        AuthorityCapsule.seal(review_fields(tool_grants=["repo_read", tool_id]))


def test_a_writing_tool_denied_outright_is_admitted_on_a_read_only_run() -> None:
    capsule = AuthorityCapsule.seal(
        review_fields(
            tool_grants=["repo_read", "workspace_apply_patch"],
            tool_denials=["workspace_apply_patch"],
        )
    )
    assert capsule.semantic_tools == (SemanticToolId.REPO_READ,)


def test_a_writing_tool_needs_a_mutating_purpose_on_a_task_scope() -> None:
    with pytest.raises(ValidationError, match="mutating task-scoped Run"):
        AuthorityCapsule.seal(
            capsule_fields(purpose="audit", agent_role="auditor", tool_grants=["submit_candidate"])
        )


def test_a_read_only_capsule_seals_without_a_writing_tool() -> None:
    capsule = AuthorityCapsule.seal(review_fields())
    assert capsule.semantic_tools == (SemanticToolId.REPO_READ, SemanticToolId.SUBMIT_REPORT)


def test_capsule_refuses_naming_itself_as_its_own_parent() -> None:
    with pytest.raises(ValidationError, match="must name another Run"):
        AuthorityCapsule.seal(capsule_fields(parent_run_ref=RUN))


def test_capsule_accepts_a_parent_run() -> None:
    capsule = AuthorityCapsule.seal(review_fields(parent_run_ref=OTHER_RUN))
    assert capsule.parent_run_ref is not None


@pytest.mark.parametrize("scope_ref", ["", "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task", "TASK-1"])
def test_capsule_refuses_a_scope_reference_that_is_not_a_urn(scope_ref: str) -> None:
    with pytest.raises(ValidationError):
        AuthorityCapsule.seal(capsule_fields(scope_ref=scope_ref))


# ---- bounded fields ------------------------------------------------------------


@pytest.mark.parametrize(
    ("stop_conditions", "admitted"),
    [
        ([], False),
        (["scope_ambiguity"], True),
        ([condition.value for condition in StopCondition], True),
        (["ran_out_of_ideas"], False),
    ],
    ids=["empty", "single", "every-condition", "uncatalogued"],
)
def test_stop_conditions_are_closed_and_non_empty(
    stop_conditions: list[str], admitted: bool
) -> None:
    fields = capsule_fields(stop_conditions=stop_conditions)
    if admitted:
        assert len(AuthorityCapsule.seal(fields).stop_conditions) == len(stop_conditions)
        return
    with pytest.raises(ValidationError):
        AuthorityCapsule.seal(fields)


@pytest.mark.parametrize(
    ("wall_seconds", "admitted"),
    [(0, False), (1, True), (86_400, True), (86_401, False)],
    ids=["zero", "single", "max", "over-max"],
)
def test_budget_wall_ceiling_is_bounded(wall_seconds: int, admitted: bool) -> None:
    document = {**BUDGET, "wall_seconds": wall_seconds}
    if admitted:
        assert CapsuleBudget.model_validate(document).wall_seconds == wall_seconds
        return
    with pytest.raises(ValidationError):
        CapsuleBudget.model_validate(document)


def test_budget_refuses_a_non_integer_ceiling() -> None:
    with pytest.raises(ValidationError, match="int_type"):
        CapsuleBudget.model_validate({**BUDGET, "output_bytes": "1048576"})


def test_budget_refuses_an_unknown_ceiling() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CapsuleBudget.model_validate({**BUDGET, "gpu_minutes": 10})


def test_budget_leaves_an_unpriced_ceiling_absent() -> None:
    budget = CapsuleBudget.model_validate({"wall_seconds": 60, "output_bytes": 1024})
    assert budget.cost_microusd is None
    assert budget.child_runs == 0


def test_capsule_refuses_an_unknown_field() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AuthorityCapsule.seal(capsule_fields(ambient_tools=True))


def test_capsule_refuses_a_missing_required_field() -> None:
    fields = capsule_fields()
    del fields["budget"]
    with pytest.raises(ValidationError, match="missing"):
        AuthorityCapsule.seal(fields)


def test_capsule_protocol_version_is_pinned() -> None:
    assert AuthorityCapsule.seal(capsule_fields()).protocol_version == "agent-run/v1"
    with pytest.raises(ValidationError):
        AuthorityCapsule.seal(capsule_fields(protocol_version="agent-run/v2"))
