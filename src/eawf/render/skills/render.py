"""Render Claude Code ``SKILL.md`` markdown from a typed skill context.

Holds the pure rendering layer of the :mod:`eawf.render.skills` package:
the typed render context / spec dataclasses and the Jinja2-backed
``render_skill_md`` helpers. The frozen :data:`SKILL_REGISTRY` data lives
in the sibling :mod:`eawf.render.skills.registry`; the package
``__init__`` re-exports both halves so every historical
``from eawf.render.skills import ...`` keeps resolving unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.resources import files

from jinja2 import Environment, FileSystemLoader, StrictUndefined

logger = logging.getLogger(__name__)


_TEMPLATE_NAME: str = "SKILL.md.j2"
_TEMPLATES_PACKAGE: str = "eawf.templates.claude"


@dataclass(frozen=True)
class SkillTemplateContext:
    """Inputs for one :func:`render_skill_md` call.

    Attributes:
        skill_name: Canonical Eä skill name (without the leading slash).
            Frontmatter ``name`` field. Mirrors the ten skill names
            recorded in :data:`~eawf.render.envelope.SkillName`.
        description: One-sentence skill description used by the Claude
            Code skill loader for fuzzy matching.
        argument_hint: ``argument-hint`` string for slash invocation
            (e.g. ``"<topic-slug> [--final]"``).
        user_invocable: Whether the user can invoke the skill directly
            via the slash command.
        disable_model_invocation: Whether the model is barred from
            invoking the skill on its own.
        body: Skill body markdown (algorithm + checklist + output
            contract). Inserted verbatim after the frontmatter.
    """

    skill_name: str
    description: str
    argument_hint: str
    user_invocable: bool
    disable_model_invocation: bool
    body: str


@dataclass(frozen=True)
class SkillSpec:
    """Frozen v0.1 skill spec used by :data:`SKILL_REGISTRY`.

    Mirrors the hand-written ``.claude/skills/<name>/SKILL.md`` shape so
    the renderer-vs-handwritten swap stays byte-clean. ``body`` is the
    markdown body the renderer pastes after the frontmatter.
    """

    skill_name: str
    description: str
    argument_hint: str
    user_invocable: bool
    disable_model_invocation: bool
    body: str
    version: str = "1.0"
    requires: tuple[str, ...] = field(default_factory=tuple)


def _load_environment() -> Environment:
    """Load a Jinja2 environment rooted at the bundled claude templates dir.

    Mirrors :func:`eawf.render.agents_md._load_environment` so loader
    behaviour is consistent across renderers (StrictUndefined,
    ``keep_trailing_newline=False``, autoescape off).
    """
    templates_dir = files(_TEMPLATES_PACKAGE)
    templates_path = str(templates_dir)
    env = Environment(
        loader=FileSystemLoader(templates_path),
        undefined=StrictUndefined,
        keep_trailing_newline=False,
        autoescape=False,
    )
    return env


def render_skill_md(ctx: SkillTemplateContext) -> str:
    """Render a Claude Code ``SKILL.md`` from *ctx*.

    Args:
        ctx: Typed render context. Every attribute is mandatory — the
            Jinja2 ``StrictUndefined`` setting catches a missing key with
            an :class:`~jinja2.exceptions.UndefinedError` rather than
            silently emitting ``""``.

    Returns:
        The rendered markdown text. The frontmatter shape mirrors the
        hand-written placeholder files; the body block is *ctx.body*
        wrapped only by a leading blank line.
    """
    env = _load_environment()
    template = env.get_template(_TEMPLATE_NAME)
    rendered = template.render(
        skill_name=ctx.skill_name,
        description=ctx.description,
        argument_hint=ctx.argument_hint,
        user_invocable=ctx.user_invocable,
        disable_model_invocation=ctx.disable_model_invocation,
        body=ctx.body.rstrip("\n"),
    )
    if not rendered.endswith("\n"):
        rendered = rendered + "\n"
    return rendered


def render_skill_md_from_spec(spec: SkillSpec) -> str:
    """Render a SKILL.md from a :class:`SkillSpec`.

    Convenience wrapper that builds the :class:`SkillTemplateContext`
    from *spec* and forwards to :func:`render_skill_md`. Used by both
    :mod:`eawf.runtime.runtimes.claude.plugin_install` (file-on-disk render) and
    :func:`eawf.cli.commands.skill.render_cmd` (stdout dump) so the two
    code paths emit byte-identical bytes for the same registry entry.
    """
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


__all__ = [
    "SkillSpec",
    "SkillTemplateContext",
    "render_skill_md",
    "render_skill_md_from_spec",
]
