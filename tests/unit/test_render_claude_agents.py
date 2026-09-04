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
- ``effective_agent_tools`` widens a spec's allowlist from the configured
  ``agents.extra_tools`` grant without disturbing the unconfigured render.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.render.agents import (
    AGENT_REGISTRY,
    ROLES,
    SERENA_READ_TOOLS,
    AgentSpec,
    AgentTemplateContext,
    effective_agent_tools,
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
    assert '\ndescription: "one-sentence agent description"\n' in output
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


# ---- W43: DoR/DoD contract blocks in the five core bodies -------------------


def test_five_bodies_carry_their_dor_dod_contract_blocks() -> None:
    """CR-01: the section-5 blocks land verbatim-anchored in each body."""
    from eawf.surfaces.render.agents import (
        _AUDITOR_BODY,
        _EXECUTOR_BODY,
        _OPERATOR_BODY,
        _PLANNER_BODY,
        _RESEARCHER_BODY,
    )

    assert "## DoR — refuse the dispatch unless ALL hold" in _EXECUTOR_BODY
    assert "## DoD — before you emit the close-ready report" in _EXECUTOR_BODY
    # The grandfathered-legacy carve-out is load-bearing: pre-drain waves
    # must not be walled with blocked verdicts.
    assert "grandfathered kind=legacy rows" in _EXECUTOR_BODY
    assert "do not refuse those" in _EXECUTOR_BODY
    # The evidence_refs-required DoD bullet was deferred out of W43 and
    # landed with W49 together with the report-schema rewrite, so the
    # role contract and the pinned schema demand the same thing.
    assert "evidence_refs is REQUIRED" in _EXECUTOR_BODY

    assert "## Dispatch-loop discipline (every iteration)" in _OPERATOR_BODY
    # 8 numbered items including the schema-bump daemon-stop rule.
    for item in range(1, 9):
        assert f"\n{item}. " in _OPERATOR_BODY
    assert "`eawf daemon stop`" in _OPERATOR_BODY

    assert "## Refuse-broken-artifact self-test" in _AUDITOR_BODY
    assert "UNVERIFIED, never passed" in _AUDITOR_BODY

    assert "## Typed-criteria floor (non-negotiable authoring bar)" in _PLANNER_BODY
    assert "Brief-coverage HALT" in _PLANNER_BODY

    assert "## Verify-before-claim ladder" in _RESEARCHER_BODY
    assert "dense [N] markers" in _RESEARCHER_BODY


def test_contract_blocks_reach_the_rendered_role_contract() -> None:
    """CR-01: the blocks propagate into a dispatch prompt's role contract
    via ROLE_REGISTRY -> RoleSpec.system_prompt."""
    from eawf.kernel.state.enums import AgentSessionRole
    from eawf.workflow.agents.specs.roles import get_role_spec

    executor = get_role_spec(AgentSessionRole.EXECUTOR)
    assert "## DoR — refuse the dispatch unless ALL hold" in executor.system_prompt
    auditor = get_role_spec(AgentSessionRole.AUDITOR)
    assert "## Refuse-broken-artifact self-test" in auditor.system_prompt


# --------------------------------------------------------------------------- #
# agents.extra_tools grant merge                                              #
# --------------------------------------------------------------------------- #


def _render_with(spec: AgentSpec, tools: tuple[str, ...]) -> str:
    """Render *spec* forcing an explicit tool tuple, bypassing the grant merge."""
    return render_agent_md(
        AgentTemplateContext(
            role=spec.role,
            description=spec.description,
            tools=tools,
            model=spec.model,
            color=spec.color,
            memory=spec.memory,
            body=spec.body,
        )
    )


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda s: s.role)
def test_effective_agent_tools_empty_grant_is_the_declared_allowlist(spec: AgentSpec) -> None:
    """An unconfigured repo renders exactly what AGENT_REGISTRY declares."""
    assert effective_agent_tools(spec, {}) == spec.tools


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda s: s.role)
def test_empty_grant_renders_byte_for_byte_identical_frontmatter(spec: AgentSpec) -> None:
    """The grant mechanism is inert until configured — no rendered-byte change."""
    baseline = _render_with(spec, spec.tools)
    merged = _render_with(spec, effective_agent_tools(spec, {}))
    assert merged == baseline


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda s: s.role)
def test_wildcard_grant_reaches_every_role(spec: AgentSpec) -> None:
    """A ``"*"`` entry widens every role's allowlist."""
    tools = effective_agent_tools(spec, {"*": ["ToolForAll"]})
    assert tools[: len(spec.tools)] == spec.tools
    assert tools[-1] == "ToolForAll"


def test_role_grant_reaches_only_that_role() -> None:
    """A role-keyed entry leaves the other roles untouched."""
    grant = {"researcher": ["ResearcherOnly"]}
    by_role = {spec.role: effective_agent_tools(spec, grant) for spec in AGENT_REGISTRY}
    assert "ResearcherOnly" in by_role["researcher"]
    for role, tools in by_role.items():
        if role != "researcher":
            assert "ResearcherOnly" not in tools


def test_grant_duplicates_collapse_and_order_is_stable() -> None:
    """Base tools win their position; a repeat anywhere later is dropped."""
    spec = next(s for s in AGENT_REGISTRY if s.role == "researcher")
    tools = effective_agent_tools(
        spec,
        {"*": ["Read", "Shared"], "researcher": ["Shared", "Grep", "Own"]},
    )
    assert tools == (*spec.tools, "Shared", "Own")
    assert len(tools) == len(set(tools))


def test_wildcard_precedes_role_grant_in_render_order() -> None:
    """Wildcard extras land before the role's own extras."""
    spec = next(s for s in AGENT_REGISTRY if s.role == "executor")
    tools = effective_agent_tools(spec, {"*": ["Wide"], "executor": ["Narrow"]})
    assert tools.index("Wide") < tools.index("Narrow")


def test_grant_for_an_unrelated_role_is_ignored() -> None:
    """A grant keyed to another role contributes nothing to this render."""
    spec = next(s for s in AGENT_REGISTRY if s.role == "reviewer")
    assert effective_agent_tools(spec, {"polisher": ["PolisherOnly"]}) == spec.tools


# --------------------------------------------------------------------------- #
# Serena read triple: granted in the registry, never through extra_tools.
# --------------------------------------------------------------------------- #

#: Roles whose work is reading an unfamiliar tree — the operator orienting
#: before a dispatch, the domain specialist landing in a package it did not
#: write. Both answer "where is X defined" / "who calls X" constantly, and
#: both are read-only about it.
_SERENA_GRANTED_ROLES: set[str] = {"operator", "domain-specialist"}

#: Native language-server tool names. A subagent frontmatter allowlist cannot
#: reach them, so naming one would advertise an affordance the role does not
#: have.
_LSP_TOOL_MARKERS: tuple[str, ...] = ("LSP", "workspaceSymbol", "goToDefinition")


def test_serena_read_triple_is_the_three_read_only_symbol_tools() -> None:
    """The granted triple is exactly the three read-only Serena tools."""
    assert SERENA_READ_TOOLS == (
        "mcp__serena__find_symbol",
        "mcp__serena__find_referencing_symbols",
        "mcp__serena__get_symbols_overview",
    )


@pytest.mark.parametrize("role", sorted(_SERENA_GRANTED_ROLES))
def test_serena_triple_reaches_the_rendered_frontmatter(role: str) -> None:
    """Each granted role's rendered ``tools:`` line carries all three."""
    spec = next(s for s in AGENT_REGISTRY if s.role == role)
    rendered = _render_with(spec, effective_agent_tools(spec, {}))
    tools_line = next(line for line in rendered.splitlines() if line.startswith("tools:"))

    for tool in SERENA_READ_TOOLS:
        assert tool in tools_line


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda s: s.role)
def test_serena_grant_is_registry_resident_not_config_resident(spec: AgentSpec) -> None:
    """The grant survives an empty ``agents.extra_tools`` map.

    ``agents.extra_tools`` ships empty, so a grant that lived only there is
    stripped from every role the next time the plugin tree is rendered.
    """
    expected = SERENA_READ_TOOLS if spec.role in _SERENA_GRANTED_ROLES else ()
    granted = tuple(t for t in effective_agent_tools(spec, {}) if t.startswith("mcp__serena__"))

    assert granted == expected


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda s: s.role)
def test_serena_grant_preserves_the_declared_base_allowlist(spec: AgentSpec) -> None:
    """The triple widens the allowlist; it never displaces a declared tool."""
    base = [tool for tool in spec.tools if not tool.startswith("mcp__serena__")]

    assert base == [t for t in spec.tools if t not in SERENA_READ_TOOLS]
    assert len(spec.tools) == len(set(spec.tools))


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda s: s.role)
def test_no_role_frontmatter_emits_an_lsp_tool(spec: AgentSpec) -> None:
    """No rendered frontmatter names a native language-server tool."""
    rendered = _render_with(spec, effective_agent_tools(spec, {}))
    tools_line = next(line for line in rendered.splitlines() if line.startswith("tools:"))

    for marker in _LSP_TOOL_MARKERS:
        assert marker not in tools_line


def test_serena_triple_is_absent_from_the_ungranted_role_frontmatter() -> None:
    """Roles outside the grant set render no ``mcp__serena__`` tool at all."""
    for spec in AGENT_REGISTRY:
        if spec.role in _SERENA_GRANTED_ROLES:
            continue
        rendered = _render_with(spec, effective_agent_tools(spec, {}))
        assert "mcp__serena__" not in rendered.split("\n---\n", 1)[0]


def test_researcher_body_ladder_opens_with_the_symbol_tool_rung() -> None:
    """The researcher's verify ladder names the symbol tools on rung (a)."""
    body = next(s for s in AGENT_REGISTRY if s.role == "researcher").body
    ladder = body.split("## Verify-before-claim ladder", 1)[1]

    assert ladder.lstrip().startswith("(a) Resolve the symbol with the symbol tools")
    for tool in SERENA_READ_TOOLS:
        assert tool in ladder
