"""Unit tests for ``eawf.surfaces.render.agents`` (Claude subagent markdown).

Distinct from ``tests/unit/test_render_agents_md.py``, which exercises
the AGENTS.md renderer (``eawf.surfaces.render.agents_md``). This file targets
the per-subagent-file renderer added in Phase 4 W05.

Covers:

- Frontmatter shape mirrors the hand-written ``.claude/agents/<role>.md``
  placeholders.
- Every AGENT_REGISTRY role renders without raising.
- ``tools`` list emits inline (``[Read, Grep]``) — matches the existing
  hand-written shape.
- Boolean ``memory`` field emits lowercase ``true``/``false``.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.render.agents import (
    AGENT_REGISTRY,
    ROLES,
    AgentTemplateContext,
    render_agent_md,
)

_EXPECTED_ROLES: set[str] = {
    "researcher",
    "planner",
    "executor",
    "auditor",
    "reviewer",
    "polisher",
    "operator",
    "domain-specialist",
}
_ROLE_SPECIFIC_FIELDS: dict[str, str] = {
    "researcher": '"question"',
    "planner": '"waves"',
    "executor": '"commit_sha"',
    "auditor": '"criteria"',
    "reviewer": '"findings"',
    "polisher": '"changes"',
    "operator": '"completed_wave_ids"',
    "domain-specialist": '"assessment"',
}


def _ctx(role: str = "researcher") -> AgentTemplateContext:
    return AgentTemplateContext(
        role=role,
        description="one-sentence agent description",
        tools=("Read", "Grep", "Bash"),
        model="opus",
        color="blue",
        memory=True,
        body="# Researcher\n\nbody",
    )


def test_roles_constant_has_eight_canonical_entries() -> None:
    """ROLES tuple matches the AgentSession.role enum count from the spec."""
    assert len(ROLES) == 8
    assert set(ROLES) == _EXPECTED_ROLES


def test_render_agent_md_includes_all_frontmatter_fields() -> None:
    output = render_agent_md(_ctx())
    assert "---\n" in output
    assert "\nname: researcher\n" in output
    assert "\ndescription: one-sentence agent description\n" in output
    assert "\nmodel: opus\n" in output
    assert "\ncolor: blue\n" in output
    assert "\nmemory: true\n" in output


def test_render_agent_md_emits_inline_tools_list() -> None:
    """Tools render as ``[A, B, C]`` (matches existing .claude/agents/*.md)."""
    ctx = AgentTemplateContext(
        role="auditor",
        description="d",
        tools=("Read", "Grep", "Glob", "Bash"),
        model="opus",
        color="red",
        memory=False,
        body="# Auditor\nbody",
    )
    output = render_agent_md(ctx)
    assert "\ntools: [Read, Grep, Glob, Bash]\n" in output


def test_render_agent_md_memory_false_emits_lowercase() -> None:
    ctx = AgentTemplateContext(
        role="auditor",
        description="d",
        tools=("Read",),
        model="opus",
        color="red",
        memory=False,
        body="# Auditor",
    )
    output = render_agent_md(ctx)
    assert "\nmemory: false\n" in output
    assert "True" not in output
    assert "False" not in output


def test_agent_registry_carries_all_eight_roles() -> None:
    names = {spec.role for spec in AGENT_REGISTRY}
    assert names == _EXPECTED_ROLES


@pytest.mark.parametrize("role", sorted(_EXPECTED_ROLES))
def test_each_registry_role_renders_without_raising(role: str) -> None:
    spec = next(s for s in AGENT_REGISTRY if s.role == role)
    ctx = AgentTemplateContext(
        role=spec.role,
        description=spec.description,
        tools=spec.tools,
        model=spec.model,
        color=spec.color,
        memory=spec.memory,
        body=spec.body,
    )
    output = render_agent_md(ctx)
    assert f"name: {role}\n" in output
    # Sanity: at least one tool rendered.
    assert "tools: [" in output


@pytest.mark.parametrize("role", sorted(_EXPECTED_ROLES))
def test_each_registry_role_includes_typed_output_contract(role: str) -> None:
    spec = next(s for s in AGENT_REGISTRY if s.role == role)
    ctx = AgentTemplateContext(
        role=spec.role,
        description=spec.description,
        tools=spec.tools,
        model=spec.model,
        color=spec.color,
        memory=spec.memory,
        body=spec.body,
    )
    output = render_agent_md(ctx)
    assert "## Typed output envelope\n" in output
    assert f'"role": "{role}"' in output
    assert '"verdict": "pass"' in output
    assert _ROLE_SPECIFIC_FIELDS[role] in output


def test_render_agent_md_terminates_with_newline() -> None:
    output = render_agent_md(_ctx())
    assert output.endswith("\n")


def test_render_agent_md_empty_tools_renders_empty_list() -> None:
    """Empty tool list still produces a syntactically valid frontmatter line."""
    ctx = AgentTemplateContext(
        role="domain-specialist",
        description="d",
        tools=(),
        model="opus",
        color="magenta",
        memory=True,
        body="# Domain",
    )
    output = render_agent_md(ctx)
    assert "\ntools: []\n" in output
