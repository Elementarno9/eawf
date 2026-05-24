"""Render Claude Code ``SKILL.md`` files for installed Eä skills.

Per Phase 4 W05 acceptance §1/§5, ``eawf plugin install claude`` emits
one ``.claude/skills/<name>/SKILL.md`` per skill. The output mirrors the
hand-written placeholders that already live under ``.claude/skills/``:
YAML frontmatter (``name``/``description``/``argument-hint``/
``user-invocable``/``disable-model-invocation``) terminated by ``---``
and followed by a markdown body documenting the canonical algorithm,
the pre-flight checklist, and the output contract.

Public API::

    SkillTemplateContext         # typed dataclass for one render call
    render_skill_md(ctx) -> str  # pure: returns the rendered markdown
    SKILL_REGISTRY               # frozen tuple of every Eä skill spec

This package keeps the historical flat import surface
(``from eawf.surfaces.render.skills import SKILL_REGISTRY, render_skill_md, ...``)
intact: the rendering layer (typed context / spec dataclasses + the
Jinja2 ``render_skill_md`` helpers) lives in :mod:`.render` and the
frozen :data:`SKILL_REGISTRY` data lives in :mod:`.registry`. This
module re-exports both so external importers resolve every name from the
original location.
"""

from __future__ import annotations

from eawf.surfaces.render.skills.registry import SKILL_REGISTRY
from eawf.surfaces.render.skills.render import (
    SkillSpec,
    SkillTemplateContext,
    render_skill_md,
    render_skill_md_from_spec,
)

__all__ = [
    "SKILL_REGISTRY",
    "SkillSpec",
    "SkillTemplateContext",
    "render_skill_md",
    "render_skill_md_from_spec",
]
