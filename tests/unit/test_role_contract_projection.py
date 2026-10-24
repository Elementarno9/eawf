"""Unit tests for the RoleContract projection.

Exercises :func:`eawf.workflow.dispatch.renderer.build_role_contract` and the
``RoleContract`` projection carried by :class:`SubagentSpec`. The wave
introduces the keystone seam every per-role plugin surface
(Claude / Codex / OpenCode / dispatch ``SubagentSpec``) consumes — so
the dispatch wave prompt's role body is driven by the role registry
rather than a hardcoded constant.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AgentSessionRole,
)
from eawf.workflow.agents.specs.models import RoleContract, SubagentSpec
from eawf.workflow.agents.specs.roles import RoleSpec, get_role_spec
from eawf.workflow.dispatch import (
    build_role_contract,
)

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


# ---- Byte-difference: non-executor role does NOT render executor body ------


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
