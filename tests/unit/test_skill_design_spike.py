"""Unit tests for the ``/design`` and ``/spike`` skills (P29-I06-W01).

``/design`` and ``/spike`` are registry-resident workflow skills: each
renders a ``SKILL.md`` (so it installs as a slash command and reconciles
against the disk tree) but drives no state mutation, so neither has an
engine ``Skill`` subclass. They join ``/mockup`` as the design-surface
skills under the operator-facing workflow group.

Pinned here:

- both ``SkillSpec`` rows resolve under their canonical names;
- ``/design`` is user-invocable but NOT model-invocable (it is an
  operator-driven multi-round AUQ design pass);
- ``/spike`` is user-invocable AND model-invocable (it mirrors
  ``/research`` — the local frontmatter's ``disable-model-invocation:
  true`` is corrected to ``False`` in the registry row);
- each renders a frontmatter-shaped ``SKILL.md`` without raising, with
  the classification flags reflected in the YAML;
- neither body carries a dangling ``/smoke-test`` reference (no such
  skill is registered);
- ``reconcile_skills`` stays clean with both rows present.
"""

from __future__ import annotations

from pathlib import Path

from eawf.runtime.runtimes.claude.plugin_install import _render_skill
from eawf.surfaces.render.skills import (
    SKILL_REGISTRY,
    SkillSpec,
    render_skill_md_from_spec,
)
from eawf.workflow.skills.discovery import reconcile_skills


def _spec(name: str) -> SkillSpec:
    return next(s for s in SKILL_REGISTRY if s.skill_name == name)


def test_design_skill_row_resolves() -> None:
    """``/design`` is registered as a SkillSpec row in the registry."""
    spec = _spec("design")
    assert spec.skill_name == "design"
    assert spec.argument_hint == "<surface-slug> [--final] [--from-brief <path>]"


def test_spike_skill_row_resolves() -> None:
    """``/spike`` is registered as a SkillSpec row in the registry."""
    spec = _spec("spike")
    assert spec.skill_name == "spike"
    assert "<spike-slug>" in spec.argument_hint
    assert "--postmortem" in spec.argument_hint


def test_design_skill_is_user_invocable_not_model_invocable() -> None:
    """``/design`` is operator-driven: visible in the slash menu, model-barred."""
    spec = _spec("design")
    assert spec.user_invocable is True
    assert spec.disable_model_invocation is True


def test_spike_skill_is_user_and_model_invocable() -> None:
    """``/spike`` mirrors ``/research``: user- AND model-invocable.

    The local ``.claude/skills/spike/SKILL.md`` frontmatter wrongly sets
    ``disable-model-invocation: true``; the registry row is the committed
    render and corrects it to ``False`` so the model may reach for the
    spike on its own.
    """
    spec = _spec("spike")
    assert spec.user_invocable is True
    assert spec.disable_model_invocation is False


def test_design_skill_renders_frontmatter() -> None:
    """The rendered ``/design`` SKILL.md carries the frontmatter + body heading."""
    output = render_skill_md_from_spec(_spec("design"))
    assert output.startswith("---\n")
    assert "\nname: design\n" in output
    assert "\nuser-invocable: true\n" in output
    assert "\ndisable-model-invocation: true\n" in output
    assert "# /design" in output


def test_spike_skill_renders_frontmatter() -> None:
    """The rendered ``/spike`` SKILL.md carries the frontmatter + body heading."""
    output = render_skill_md_from_spec(_spec("spike"))
    assert output.startswith("---\n")
    assert "\nname: spike\n" in output
    assert "\nuser-invocable: true\n" in output
    assert "\ndisable-model-invocation: false\n" in output
    assert "# /spike" in output


def test_design_body_documents_rigour_mechanisms() -> None:
    """The ``/design`` body names the statechart + matrix + liveness contract."""
    body = _spec("design").body
    assert "statechart" in body
    assert "liveness contract" in body
    assert "AskUserQuestion" in body
    assert "L1..L11" in body


def test_spike_body_documents_direction_contract() -> None:
    """The ``/spike`` body names the direction-only + AUQ + next-line contract."""
    body = _spec("spike").body
    assert "direction" in body
    assert "AskUserQuestion" in body
    assert "next:" in body
    assert "--from-briefs" in body


def test_design_body_has_no_dangling_smoke_test_reference() -> None:
    """No ``/smoke-test`` skill is registered, so the body must not cite one."""
    assert "smoke-test" not in _spec("design").body


def test_spike_body_has_no_dangling_smoke_test_reference() -> None:
    """No ``/smoke-test`` skill is registered, so the body must not cite one."""
    assert "smoke-test" not in _spec("spike").body


def test_no_smoke_test_skill_registered() -> None:
    """Guard: ``smoke-test`` is not (and must not be) a registry row."""
    names = {s.skill_name for s in SKILL_REGISTRY}
    assert "smoke-test" not in names


def _render_clean_tree(root: Path) -> None:
    for spec in SKILL_REGISTRY:
        skill_dir = root / spec.skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_render_skill(spec), encoding="utf-8")


def test_reconcile_clean_with_design_and_spike_present(tmp_path: Path) -> None:
    """A tree rendered from the registry (incl /design + /spike) has zero drift."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    assert (root / "design" / "SKILL.md").is_file()
    assert (root / "spike" / "SKILL.md").is_file()
    report = reconcile_skills(root)
    assert report.has_drift is False
    for name in ("design", "spike"):
        assert name not in report.missing_on_disk
        assert name not in report.extra_on_disk
