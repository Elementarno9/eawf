"""Codex skill and custom-agent render helpers.

Codex plugins carry reusable skills under the plugin root at
``.codex/plugins/eawf/skills/<name>/SKILL.md``. Codex custom agents are
project/user-scoped TOML files at ``.codex/agents/<role>.toml``; they are
not entries in the plugin manifest.
"""

from __future__ import annotations

from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.store.kinds.agent_report import store_kind_for_role
from eawf.surfaces.render.agents import AgentSpec
from eawf.surfaces.render.skills import (
    SkillSpec,
    SkillTemplateContext,
    render_skill_md,
)


def _toml_basic_string(value: str) -> str:
    """Render *value* as a fully escaped TOML basic string."""
    out = ['"']
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def render_codex_skill(spec: SkillSpec) -> str:
    """Render one Codex plugin skill ``SKILL.md`` body."""
    return render_skill_md(
        SkillTemplateContext(
            skill_name=spec.skill_name,
            description=spec.description,
            argument_hint=spec.argument_hint,
            user_invocable=spec.user_invocable,
            disable_model_invocation=spec.disable_model_invocation,
            body=spec.body,
        )
    )


def render_codex_agent_toml(spec: AgentSpec) -> str:
    """Render one Codex standalone custom-agent TOML file."""
    role = AgentSessionRole(spec.role)
    store_kind = store_kind_for_role(role).value
    instructions = "\n\n".join(
        [
            spec.body.rstrip("\n"),
            (f"On completion emit an `agent_end` report; it persists to the `{store_kind}` store."),
        ]
    )
    lines = [
        f"name = {_toml_basic_string(spec.role)}",
        f"description = {_toml_basic_string(spec.description)}",
        f"developer_instructions = {_toml_basic_string(instructions)}",
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "render_codex_agent_toml",
    "render_codex_skill",
]
