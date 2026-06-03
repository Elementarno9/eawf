"""Render Claude Code ``SKILL.md`` markdown from a typed skill context.

Holds the pure rendering layer of the :mod:`eawf.surfaces.render.skills` package:
the typed render context / spec dataclasses and the Jinja2-backed
``render_skill_md`` helpers. The frozen :data:`SKILL_REGISTRY` data lives
in the sibling :mod:`eawf.surfaces.render.skills.registry`; the package
``__init__`` re-exports both halves so every historical
``from eawf.surfaces.render.skills import ...`` keeps resolving unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from importlib.resources import files
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, StrictUndefined

if TYPE_CHECKING:
    from eawf.platform.lint.validate_prose import ProseReport

logger = logging.getLogger(__name__)


_TEMPLATE_NAME: str = "SKILL.md.j2"
_TEMPLATES_PACKAGE: str = "eawf.platform.templates.claude"
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


@dataclass(frozen=True)
class SkillTemplateContext:
    """Inputs for one :func:`render_skill_md` call.

    Attributes:
        skill_name: Canonical Eä skill name (without the leading slash).
            Frontmatter ``name`` field. Mirrors the ten skill names
            recorded in :data:`~eawf.surfaces.render.envelope.SkillName`.
        description: One-sentence skill description used by the Claude
            Code skill loader for fuzzy matching.
        argument_hint: ``argument-hint`` string for slash invocation
            (e.g. ``"<topic-slug> [--final]"``).
        user_invocable: Whether the user can invoke the skill directly
            via the slash command.
        disable_model_invocation: Whether the model is barred from
            invoking the skill on its own.
        body: Skill body markdown (algorithm + checklist + output
            contract). Inserted after the frontmatter with plain prose
            unwrapped.
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
    markdown body the renderer normalises after the frontmatter.
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

    Mirrors :func:`eawf.surfaces.render.agents_md._load_environment` so loader
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


def _is_fence(line: str) -> bool:
    """Return whether *line* opens or closes a Markdown fence."""
    stripped = line.strip()
    return stripped.startswith(("```", "~~~"))


def _is_structural_markdown(line: str) -> bool:
    """Return whether *line* should remain a standalone Markdown line."""
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith(("#", ">", "|", "<!--", "::")):
        return True
    if _LIST_ITEM_RE.match(line):
        return True
    return _is_fence(line)


def _can_join_to_previous_list_line(previous: str, current: str) -> bool:
    """Return whether *current* is a continuation of a list item."""
    if not current.startswith(" "):
        return False
    stripped = current.strip()
    if not stripped or _is_structural_markdown(current):
        return False
    return bool(_LIST_ITEM_RE.match(previous))


def _unwrap_skill_body(body: str) -> str:
    """Collapse hard-wrapped prose while preserving Markdown structure."""
    output: list[str] = []
    paragraph: list[str] = []
    in_fence = False

    def flush_paragraph() -> None:
        if paragraph:
            output.append(" ".join(paragraph))
            paragraph.clear()

    for raw_line in body.rstrip("\n").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if _is_fence(line):
            flush_paragraph()
            output.append(line)
            in_fence = not in_fence
            continue
        if in_fence:
            output.append(line)
            continue
        if not stripped:
            flush_paragraph()
            output.append("")
            continue
        if output and _can_join_to_previous_list_line(output[-1], line):
            output[-1] = f"{output[-1]} {stripped}"
            continue
        if _is_structural_markdown(line):
            flush_paragraph()
            output.append(line)
            continue
        paragraph.append(stripped)

    flush_paragraph()
    return "\n".join(output)


def prose_check_rendered(text: str) -> ProseReport:
    """Run the Layer-2 prose chokepoint over rendered Markdown, fail-open.

    The generation-time half of the ``validate_prose`` chokepoint: the
    skill-render path calls this on a freshly-rendered artifact so a clarity
    finding surfaces *before* emit. It runs the in-process deterministic legs
    (EAWF013 / EAWF014 / EAWF017) in **fail-open** mode (``strict=False``) — a
    finding is logged as an advisory warning and the report is returned, but the
    render is never blocked. The strict, blocking enforcement is the CI gate
    (``eawf hook validate-prose --strict``); generation-time is advisory by
    design so a draft is never silently dropped at author time.

    Args:
        text: The rendered Markdown artifact (e.g. a ``SKILL.md``).

    Returns:
        The fail-open :class:`~eawf.platform.lint.validate_prose.ProseReport`.
        :meth:`~eawf.platform.lint.validate_prose.ProseReport.exit_code` is
        always ``0`` here; callers read ``has_findings`` to surface advisories.
    """
    from eawf.platform.lint.validate_prose import validate_prose

    report = validate_prose(text, strict=False)
    if report.has_findings:
        codes = ",".join(sorted(report.codes()))
        logger.warning(
            f"prose_check_rendered findings={len(report.findings)} codes={codes!r} (advisory)"
        )
    return report


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
        with hard-wrapped prose collapsed. The rendered text is passed
        through :func:`prose_check_rendered` (the generation-time Layer-2
        chokepoint) before return — advisory only, so a clarity finding is
        logged but never blocks the emit.
    """
    env = _load_environment()
    template = env.get_template(_TEMPLATE_NAME)
    rendered = template.render(
        skill_name=ctx.skill_name,
        description=ctx.description,
        argument_hint=ctx.argument_hint,
        user_invocable=ctx.user_invocable,
        disable_model_invocation=ctx.disable_model_invocation,
        body=_unwrap_skill_body(ctx.body),
    )
    if not rendered.endswith("\n"):
        rendered = rendered + "\n"
    prose_check_rendered(rendered)
    return rendered


def render_skill_md_from_spec(spec: SkillSpec) -> str:
    """Render a SKILL.md from a :class:`SkillSpec`.

    Convenience wrapper that builds the :class:`SkillTemplateContext`
    from *spec* and forwards to :func:`render_skill_md`. Used by both
    :mod:`eawf.runtime.runtimes.claude.plugin_install` (file-on-disk render) and
    :func:`eawf.surfaces.cli.commands.skill.render_cmd` (stdout dump) so the two
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
    "prose_check_rendered",
    "render_skill_md",
    "render_skill_md_from_spec",
]
