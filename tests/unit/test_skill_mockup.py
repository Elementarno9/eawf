"""Unit tests for the ``/mockup`` advisory skill.

``/mockup`` is a registry-only advisory skill: it renders a ``SKILL.md``
(so it installs as a slash command and reconciles against the disk tree)
but has no engine ``Skill`` subclass because it drives no state mutation.
It is model- and user-invocable, like ``/differentiate``.

Pinned here:

- the ``SkillSpec`` row resolves under ``mockup``;
- the skill is user-invocable AND model-invocable;
- it renders a frontmatter-shaped ``SKILL.md`` without raising;
- ``reconcile_skills`` stays clean with ``/mockup`` present;
- the body markdown documents the advisory / read-only contract.
"""

from __future__ import annotations

from pathlib import Path

from eawf.runtime.runtimes.claude.plugin_install import _render_skill
from eawf.surfaces.render.skills import SKILL_REGISTRY, render_skill_md_from_spec
from eawf.workflow.skills.discovery import reconcile_skills


def _mockup_spec():
    return next(s for s in SKILL_REGISTRY if s.skill_name == "mockup")


def test_mockup_skill_row_resolves() -> None:
    """``/mockup`` is registered as a SkillSpec row in the registry."""
    spec = _mockup_spec()
    assert spec.skill_name == "mockup"
    assert spec.argument_hint == "<surface-slug>"


def test_mockup_skill_is_user_and_model_invocable() -> None:
    """``/mockup`` is advisory: visible in the slash menu AND model-invocable."""
    spec = _mockup_spec()
    assert spec.user_invocable is True
    assert spec.disable_model_invocation is False


def test_mockup_skill_renders_frontmatter() -> None:
    """The rendered SKILL.md carries the frontmatter shape + body heading."""
    output = render_skill_md_from_spec(_mockup_spec())
    assert output.startswith("---\n")
    assert "\nname: mockup\n" in output
    assert "\nuser-invocable: true\n" in output
    assert "\ndisable-model-invocation: false\n" in output
    assert "# /mockup" in output


def test_mockup_body_documents_advisory_contract() -> None:
    """The body names the read-only / advisory + AskUserQuestion contract."""
    body = _mockup_spec().body
    assert "AskUserQuestion" in body
    assert "advisory" in body
    assert "MockupBody" in body


def _render_clean_tree(root: Path) -> None:
    for spec in SKILL_REGISTRY:
        skill_dir = root / spec.skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_render_skill(spec), encoding="utf-8")


def test_reconcile_clean_with_mockup_present(tmp_path: Path) -> None:
    """A tree rendered from the registry (including /mockup) has zero drift."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    assert (root / "mockup" / "SKILL.md").is_file()
    report = reconcile_skills(root)
    assert report.has_drift is False
    assert "mockup" not in report.missing_on_disk
    assert "mockup" not in report.extra_on_disk
