"""Unit tests for the subagent role registry (P27-I03-W14).

Exercises :mod:`eawf.workflow.agents.specs.roles`: the :data:`ROLE_REGISTRY`
coverage of every :class:`~eawf.kernel.state.enums.AgentSessionRole`, the
per-runtime contract rendering across all kept runtimes, and the error
paths (unknown runtime / missing role).
"""

from __future__ import annotations

import pytest

from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.store.kinds.agent_report import store_kind_for_role
from eawf.render.agents import AGENT_REGISTRY
from eawf.workflow.agents.specs.roles import (
    KEPT_RUNTIMES,
    ROLE_REGISTRY,
    RoleSpec,
    get_role_spec,
    render_role_contract,
)


def test_registry_covers_every_role() -> None:
    """Every :class:`AgentSessionRole` member has a registered spec."""
    assert set(ROLE_REGISTRY) == set(AgentSessionRole)


def test_kept_runtimes_are_the_three_v03_runtimes() -> None:
    """The kept runtime tuple is the D12 set in canonical order."""
    assert KEPT_RUNTIMES == ("claude-code", "codex", "opencode")


@pytest.mark.parametrize("role", list(AgentSessionRole), ids=lambda r: r.value)
@pytest.mark.parametrize("runtime", list(KEPT_RUNTIMES))
def test_every_role_renders_to_every_kept_runtime(role: AgentSessionRole, runtime: str) -> None:
    """Each role renders a non-empty contract block to each kept runtime."""
    block = render_role_contract(role, runtime)  # type: ignore[arg-type]
    assert block.strip()
    # Heading names both the role and the runtime.
    assert block.splitlines()[0] == f"## Role: {role.value} ({runtime})"
    # The block carries the role's report store-kind pointer.
    assert store_kind_for_role(role).value in block


def test_render_includes_runtime_specific_placement_note() -> None:
    """The placement note differs per runtime (claude file vs codex nest)."""
    cc = render_role_contract(AgentSessionRole.EXECUTOR, "claude-code")
    codex = render_role_contract(AgentSessionRole.EXECUTOR, "codex")
    opencode = render_role_contract(AgentSessionRole.EXECUTOR, "opencode")
    assert ".claude/agents/<role>.md" in cc
    assert "Codex skill bundle" in codex
    assert ".opencode/agent/<role>.md" in opencode


def test_render_reuses_agent_registry_body_verbatim() -> None:
    """The contract body is reused from AGENT_REGISTRY (single source of truth)."""
    executor_agent = next(spec for spec in AGENT_REGISTRY if spec.role == "executor")
    block = render_role_contract(AgentSessionRole.EXECUTOR, "claude-code")
    # The agent body's leading line appears verbatim in the rendered block.
    assert executor_agent.body.strip().splitlines()[0] in block
    assert "You implement what the planner specified." in block


def test_role_summary_matches_agent_registry_description() -> None:
    """``RoleSpec.summary`` mirrors the matching ``AgentSpec.description``."""
    for spec in AGENT_REGISTRY:
        role = AgentSessionRole(spec.role)
        assert ROLE_REGISTRY[role].summary == spec.description


def test_get_role_spec_returns_role_spec() -> None:
    """``get_role_spec`` returns the registered :class:`RoleSpec`."""
    spec = get_role_spec(AgentSessionRole.AUDITOR)
    assert isinstance(spec, RoleSpec)
    assert spec.role is AgentSessionRole.AUDITOR


def test_render_rejects_unknown_runtime() -> None:
    """An unknown runtime raises ``ValueError`` with the canonical message."""
    spec = get_role_spec(AgentSessionRole.EXECUTOR)
    with pytest.raises(ValueError, match="unknown runtime: 'goose'"):
        spec.render("goose")  # type: ignore[arg-type]


def test_render_role_contract_rejects_unknown_runtime() -> None:
    """The free-function entry point also rejects unknown runtimes."""
    with pytest.raises(ValueError, match="unknown runtime"):
        render_role_contract(AgentSessionRole.PLANNER, "cursor")  # type: ignore[arg-type]


def test_role_spec_rejects_unknown_field() -> None:
    """``RoleSpec`` forbids extra keys (strict schema)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RoleSpec.model_validate(
            {
                "role": AgentSessionRole.EXECUTOR.value,
                "summary": "s",
                "body": "b",
                "report_store_kind": "executor_report",
                "bogus": True,
            }
        )


def test_role_spec_rejects_empty_report_store_kind() -> None:
    """``report_store_kind`` must be non-empty (min_length=1)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RoleSpec.model_validate(
            {
                "role": AgentSessionRole.EXECUTOR.value,
                "summary": "s",
                "body": "b",
                "report_store_kind": "",
            }
        )
