"""Unit tests for the RoleContract model and the RoleSpec fields it mirrors."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AgentSessionRole,
)
from eawf.workflow.agents.specs.models import RoleContract
from eawf.workflow.agents.specs.roles import get_role_spec

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
