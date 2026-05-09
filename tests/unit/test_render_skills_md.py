"""Unit tests for ``eawf.render.skills``.

Covers:

- Frontmatter shape mirrors the hand-written
  ``.claude/skills/<name>/SKILL.md`` placeholders.
- Every SKILL_REGISTRY entry renders without raising.
- Boolean flags emit lowercase ``true``/``false``.
- Trailing newline is present (end-of-file-fixer clean).
- ``StrictUndefined`` raises on missing context (defence in depth).
"""

from __future__ import annotations

import pytest
from jinja2 import UndefinedError

from eawf.render.skills import (
    SKILL_REGISTRY,
    SkillTemplateContext,
    render_skill_md,
)

_EXPECTED_SKILL_NAMES: set[str] = {
    "research",
    "prep",
    "audit",
    "ship",
    "review",
    "polish",
    "init",
    "roadmap",
    "differentiate",
    "flow",
}


def _ctx(skill_name: str = "research") -> SkillTemplateContext:
    """Return a minimal valid :class:`SkillTemplateContext` for tests."""
    return SkillTemplateContext(
        skill_name=skill_name,
        description="one-sentence description for the loader",
        argument_hint="<topic-slug>",
        user_invocable=True,
        disable_model_invocation=True,
        body="# /test\n\nSample body.",
    )


def test_render_skill_md_includes_all_frontmatter_fields() -> None:
    """Frontmatter contains every field the hand-written placeholders carry."""
    output = render_skill_md(_ctx())
    assert "---\n" in output
    assert "\nname: research\n" in output
    assert "\ndescription: one-sentence description for the loader\n" in output
    assert '\nargument-hint: "<topic-slug>"\n' in output
    assert "\nuser-invocable: true\n" in output
    assert "\ndisable-model-invocation: true\n" in output


def test_render_skill_md_emits_lowercase_booleans() -> None:
    """Boolean flags must render as lowercase ``true``/``false`` (Claude loader contract)."""
    ctx = SkillTemplateContext(
        skill_name="audit",
        description="d",
        argument_hint="",
        user_invocable=False,
        disable_model_invocation=False,
        body="# /audit\nbody",
    )
    output = render_skill_md(ctx)
    assert "\nuser-invocable: false\n" in output
    assert "\ndisable-model-invocation: false\n" in output
    # No accidental Pythonic ``True``/``False`` leaked through.
    assert "True" not in output
    assert "False" not in output


def test_render_skill_md_terminates_with_newline() -> None:
    """Output must end with ``\\n`` so end-of-file-fixer is a no-op."""
    output = render_skill_md(_ctx())
    assert output.endswith("\n"), repr(output[-10:])


def test_render_skill_md_body_inserted_verbatim() -> None:
    """Body markdown lands after the frontmatter without escaping."""
    output = render_skill_md(_ctx())
    assert "# /test\n" in output
    assert output.index("# /test") > output.index("---\n")


def test_render_skill_md_strips_trailing_body_newlines() -> None:
    """Body trailing newlines are normalised to one final newline."""
    ctx = SkillTemplateContext(
        skill_name="research",
        description="d",
        argument_hint="",
        user_invocable=True,
        disable_model_invocation=True,
        body="# x\n\n\n",
    )
    output = render_skill_md(ctx)
    # After stripping, body ends with "# x" and the template adds one '\n'.
    assert output.count("\n\n\n\n") == 0
    assert output.endswith("# x\n"), repr(output[-30:])


def test_skill_registry_carries_all_ten_v01_skills() -> None:
    """The registry mirrors :data:`~eawf.render.envelope.SkillName` exactly."""
    names = {spec.skill_name for spec in SKILL_REGISTRY}
    assert names == _EXPECTED_SKILL_NAMES


@pytest.mark.parametrize(
    "skill_name",
    sorted(_EXPECTED_SKILL_NAMES),
)
def test_each_registry_entry_renders_without_raising(skill_name: str) -> None:
    """Every registry skill must produce a rendered string with frontmatter."""
    spec = next(s for s in SKILL_REGISTRY if s.skill_name == skill_name)
    ctx = SkillTemplateContext(
        skill_name=spec.skill_name,
        description=spec.description,
        argument_hint=spec.argument_hint,
        user_invocable=spec.user_invocable,
        disable_model_invocation=spec.disable_model_invocation,
        body=spec.body,
    )
    output = render_skill_md(ctx)
    assert f"name: {skill_name}\n" in output
    assert "---\n" in output


def test_render_skill_md_strict_undefined_catches_typos() -> None:
    """StrictUndefined is enabled, so missing template context raises clearly.

    The current public API forces every field via the dataclass — a missing
    field would fail at the Pydantic-style construction. Defence in depth:
    render directly without the dataclass to confirm the underlying Jinja
    environment is strict.
    """
    from importlib.resources import files

    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    templates_path = str(files("eawf.templates.claude"))
    env = Environment(
        loader=FileSystemLoader(templates_path),
        undefined=StrictUndefined,
        keep_trailing_newline=False,
        autoescape=False,
    )
    template = env.get_template("SKILL.md.j2")
    with pytest.raises(UndefinedError):
        template.render(skill_name="research")
