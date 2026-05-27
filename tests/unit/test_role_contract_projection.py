"""Unit tests for the RoleContract projection (P28-I01-W12).

Exercises :func:`eawf.workflow.dispatch.renderer.build_role_contract` and the
``RoleContract`` projection carried by :class:`SubagentSpec`. The wave
introduces the keystone seam every per-role plugin surface
(Claude / Codex / OpenCode / dispatch ``SubagentSpec``) consumes — so
the dispatch wave prompt's role body is driven by the role registry
rather than a hardcoded constant.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AgentSessionRole,
    EffortBucket,
    ProjectStatus,
    ScopeKind,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.workflow.agents.specs.models import RoleContract, SubagentSpec
from eawf.workflow.agents.specs.roles import RoleSpec, get_role_spec
from eawf.workflow.dispatch import (
    build_role_contract,
    build_subagent_spec,
    render_wave_prompt,
)
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave


def _empty_state() -> State:
    """Return a minimal State with project=QR scope_id=QR."""
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


def _seed_wave_with_role(state: State, *, role: AgentSessionRole) -> str:
    """Seed P01 → P01-I01 → P01-I01-W01 carrying *role* as ``agent_role``."""
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="Wave with role",
        file_scopes=["src/foo/"],
        agent_role=role,
        effort_bucket=EffortBucket.M,
    )
    return "P01-I01-W01"


# ---- RoleContract strict-schema invariants ---------------------------------


def test_role_contract_rejects_unknown_field() -> None:
    """``RoleContract`` forbids extra top-level keys (project rule 2)."""
    with pytest.raises(ValidationError):
        RoleContract.model_validate(
            {
                "role": "executor",
                "summary": "s",
                "system_prompt": "body",
                "report_schema_ref": "executor_report",
                "bogus": True,
            }
        )


def test_role_contract_requires_report_schema_ref() -> None:
    """``report_schema_ref`` is required and non-empty (min_length=1)."""
    with pytest.raises(ValidationError):
        RoleContract.model_validate(
            {
                "role": "executor",
                "summary": "s",
                "system_prompt": "body",
                "report_schema_ref": "",
            }
        )


def test_role_contract_defaults_are_safe() -> None:
    """A minimal RoleContract defaults the optional fields safely."""
    contract = RoleContract.model_validate(
        {
            "role": "auditor",
            "summary": "fresh-context audit",
            "system_prompt": "body",
            "report_schema_ref": "auditor_report",
        }
    )
    assert contract.allowed_tools == []
    assert contract.denied_tools == []
    assert contract.model is None
    assert contract.memory is False
    assert contract.stop_conditions == []


# ---- RoleSpec back-compat alias --------------------------------------------


def test_role_spec_body_alias_returns_system_prompt() -> None:
    """The legacy ``body`` attribute returns :attr:`system_prompt`."""
    spec = get_role_spec(AgentSessionRole.EXECUTOR)
    assert spec.body == spec.system_prompt


def test_role_spec_carries_extended_fields() -> None:
    """``RoleSpec`` carries the P28-W12 extended fields (tools, model, memory)."""
    spec = get_role_spec(AgentSessionRole.EXECUTOR)
    assert spec.allowed_tools  # executor has at least one tool granted
    assert "Read" in spec.allowed_tools
    assert spec.model == "opus"
    assert spec.memory is True
    assert spec.report_schema_ref == "executor_report"
    assert spec.denied_tools == []
    assert spec.stop_conditions == []


# ---- build_role_contract projection ----------------------------------------


def test_build_role_contract_projects_role_spec_fields() -> None:
    """``build_role_contract`` projects every RoleSpec field onto the contract."""
    role = get_role_spec(AgentSessionRole.AUDITOR)
    contract = build_role_contract(role)
    assert contract.role == "auditor"
    assert contract.summary == role.summary
    assert contract.system_prompt == role.system_prompt
    assert contract.allowed_tools == sorted(role.allowed_tools)
    assert contract.model == role.model
    assert contract.memory == role.memory
    assert contract.report_schema_ref == role.report_schema_ref
    assert contract.stop_conditions == list(role.stop_conditions)


def test_build_role_contract_sorts_tool_lists_for_determinism() -> None:
    """``allowed_tools`` and ``denied_tools`` sort lexicographically."""
    role = RoleSpec(
        role=AgentSessionRole.EXECUTOR,
        summary="s",
        system_prompt="body",
        allowed_tools=["Zeta", "Alpha", "Mu"],
        denied_tools=["Gamma", "Beta"],
        report_schema_ref="executor_report",
        report_store_kind="executor_report",
    )
    contract = build_role_contract(role)
    assert contract.allowed_tools == ["Alpha", "Mu", "Zeta"]
    assert contract.denied_tools == ["Beta", "Gamma"]


def test_build_role_contract_with_no_state_omits_sandbox_intersection() -> None:
    """Without ``state`` / ``wave_id`` the role's tool lists pass through."""
    role = RoleSpec(
        role=AgentSessionRole.EXECUTOR,
        summary="s",
        system_prompt="body",
        allowed_tools=["Read", "Bash"],
        report_schema_ref="executor_report",
        report_store_kind="executor_report",
    )
    contract = build_role_contract(role)
    assert contract.allowed_tools == ["Bash", "Read"]
    assert contract.denied_tools == []


# ---- build_subagent_spec wires role_contract through -----------------------


def test_build_subagent_spec_attaches_role_contract_for_role_bearing_wave() -> None:
    """A wave with ``agent_role`` set carries a typed ``RoleContract``."""
    state = _empty_state()
    wave_id = _seed_wave_with_role(state, role=AgentSessionRole.EXECUTOR)
    spec = build_subagent_spec(state, wave_id)
    assert isinstance(spec.role_contract, RoleContract)
    assert spec.role_contract.role == "executor"
    assert (
        spec.role_contract.system_prompt == get_role_spec(AgentSessionRole.EXECUTOR).system_prompt
    )


def test_build_subagent_spec_role_contract_none_when_no_role() -> None:
    """A wave without ``agent_role`` keeps ``role_contract`` at ``None``."""
    state = _empty_state()
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="Roleless wave",
        file_scopes=["src/"],
        effort_bucket="M",
    )
    spec = build_subagent_spec(state, "P01-I01-W01")
    assert spec.role_contract is None


# ---- Byte-difference: non-executor role does NOT render executor body ------


def test_render_wave_prompt_for_auditor_does_not_carry_executor_body() -> None:
    """The dispatch prompt for an auditor wave carries the auditor body, not executor.

    Success criterion 2 of P28-I01-W12: a non-executor role renders with
    its own ``system_prompt``, NOT the hardcoded executor body. The
    executor and auditor body diverge in their leading line — the
    executor opens with "You implement what the planner specified" and
    the auditor with "You are skeptical by design".
    """
    auditor_state = _empty_state()
    _seed_wave_with_role(auditor_state, role=AgentSessionRole.AUDITOR)
    auditor_prompt = render_wave_prompt(auditor_state, "P01-I01-W01")

    executor_state = _empty_state()
    _seed_wave_with_role(executor_state, role=AgentSessionRole.EXECUTOR)
    executor_prompt = render_wave_prompt(executor_state, "P01-I01-W01")

    # Both prompts now carry a Role contract section (post-W12 seam).
    assert "## Role contract" in auditor_prompt
    assert "## Role contract" in executor_prompt

    # The auditor prompt carries the auditor body's leading line.
    assert "You are skeptical by design." in auditor_prompt
    # …and NOT the executor body's leading line.
    assert "You implement what the planner specified." not in auditor_prompt

    # The executor prompt carries the executor body — symmetrical check.
    assert "You implement what the planner specified." in executor_prompt
    assert "You are skeptical by design." not in executor_prompt

    # And the two byte streams differ — same wave id, different role,
    # different prompt bodies.
    assert auditor_prompt != executor_prompt


def test_render_wave_prompt_role_contract_section_carries_system_prompt() -> None:
    """The rendered ``## Role contract`` section holds the role's system_prompt."""
    state = _empty_state()
    _seed_wave_with_role(state, role=AgentSessionRole.PLANNER)
    prompt = render_wave_prompt(state, "P01-I01-W01")
    assert "## Role contract" in prompt
    # The planner body's leading line appears inside the section.
    assert "You produce specs that an `executor` can implement" in prompt


def test_render_wave_prompt_no_role_omits_role_contract_section() -> None:
    """A wave without ``agent_role`` omits the ``## Role contract`` section.

    Preserves byte-equivalence with the pre-W12 renderer for callers
    that have not yet plumbed the role registry through.
    """
    state = _empty_state()
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="Roleless wave",
        file_scopes=["src/"],
        effort_bucket="M",
    )
    prompt = render_wave_prompt(state, "P01-I01-W01")
    assert "## Role contract" not in prompt


# ---- Stop conditions section ------------------------------------------------


def test_render_stop_conditions_section_appears_when_role_lists_any() -> None:
    """A role with ``stop_conditions`` renders the ``## Stop conditions`` section."""
    spec = SubagentSpec(
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="Stop-condition wave",
        scope_id="QR",
        role_contract=RoleContract(
            role="executor",
            summary="s",
            system_prompt="body",
            report_schema_ref="executor_report",
            stop_conditions=["scope_violation", "budget_exhaustion"],
        ),
    )
    rendered = spec.render()
    assert "## Stop conditions" in rendered
    assert "- scope_violation" in rendered
    assert "- budget_exhaustion" in rendered


def test_render_stop_conditions_section_omitted_when_role_lists_none() -> None:
    """An empty ``stop_conditions`` list omits the section."""
    spec = SubagentSpec(
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="No stop conditions",
        scope_id="QR",
        role_contract=RoleContract(
            role="executor",
            summary="s",
            system_prompt="body",
            report_schema_ref="executor_report",
        ),
    )
    rendered = spec.render()
    assert "## Stop conditions" not in rendered


# ---- SubagentSpec accepts role_contract via the strict schema --------------


def test_subagent_spec_rejects_unknown_role_contract_field() -> None:
    """The role_contract sub-model also forbids extra keys."""
    with pytest.raises(ValidationError):
        SubagentSpec.model_validate(
            {
                "wave_id": "P01-I01-W01",
                "iter_id": "P01-I01",
                "title": "x",
                "scope_id": "QR",
                "role_contract": {
                    "role": "executor",
                    "summary": "s",
                    "system_prompt": "body",
                    "report_schema_ref": "executor_report",
                    "bogus": True,
                },
            }
        )
