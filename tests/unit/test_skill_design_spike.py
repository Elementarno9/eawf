"""Unit tests for the ``/spike`` skill and the retired ``/design`` page.

``/spike`` is a registry-resident workflow skill: it renders a
``SKILL.md`` (so it installs as a slash command and reconciles against
the disk tree) but drives no state mutation, so it has no engine
``Skill`` subclass. ``/design`` is retired from the catalog, so its
registry body is gone and a clean tree omits it.

Pinned here:

- the ``/spike`` ``SkillSpec`` row resolves under its canonical name;
- ``/spike`` is user-invocable AND model-invocable (it mirrors
  ``/research`` — the local frontmatter's ``disable-model-invocation:
  true`` is corrected to ``False`` in the registry row);
- it renders a frontmatter-shaped ``SKILL.md`` without raising, with
  the classification flags reflected in the YAML;
- the body carries no dangling ``/smoke-test`` reference (no such
  skill is registered);
- ``reconcile_skills`` stays clean, with ``/spike`` shipped and
  ``/design`` absent.
"""

from __future__ import annotations

from pathlib import Path

from eawf.runtime.runtimes.claude.plugin_install import _render_skill
from eawf.surfaces.render.skills import (
    SKILL_REGISTRY,
    SkillSpec,
    render_skill_md_from_spec,
)
from eawf.workflow.skills.catalog import shipped_skill_specs
from eawf.workflow.skills.discovery import reconcile_skills


def _spec(name: str) -> SkillSpec:
    return next(s for s in SKILL_REGISTRY if s.skill_name == name)


def test_spike_skill_row_resolves() -> None:
    """``/spike`` is registered as a SkillSpec row in the registry."""
    spec = _spec("spike")
    assert spec.skill_name == "spike"
    assert "<spike-slug>" in spec.argument_hint
    assert "--postmortem" in spec.argument_hint


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


def test_spike_skill_renders_frontmatter() -> None:
    """The rendered ``/spike`` SKILL.md carries the frontmatter + body heading."""
    output = render_skill_md_from_spec(_spec("spike"))
    assert output.startswith("---\n")
    assert "\nname: spike\n" in output
    assert "\nuser-invocable: true\n" in output
    assert "\ndisable-model-invocation: false\n" in output
    assert "# /spike" in output


def test_spike_body_documents_direction_contract() -> None:
    """The ``/spike`` body names the direction-only + AUQ + next-line contract."""
    body = _spec("spike").body
    assert "direction" in body
    assert "AskUserQuestion" in body
    assert "next:" in body
    assert "--from-briefs" in body


def test_spike_body_has_no_dangling_smoke_test_reference() -> None:
    """No ``/smoke-test`` skill is registered, so the body must not cite one."""
    assert "smoke-test" not in _spec("spike").body


def test_no_smoke_test_skill_registered() -> None:
    """Guard: ``smoke-test`` is not (and must not be) a registry row."""
    names = {s.skill_name for s in SKILL_REGISTRY}
    assert "smoke-test" not in names


def _render_clean_tree(root: Path) -> None:
    for spec in shipped_skill_specs():
        skill_dir = root / spec.skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_render_skill(spec), encoding="utf-8")


def test_reconcile_clean_ships_spike_and_omits_design(tmp_path: Path) -> None:
    """A clean catalog tree ships /spike and omits the retired /design."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    assert not (root / "design").exists()
    assert (root / "spike" / "SKILL.md").is_file()
    report = reconcile_skills(root)
    assert report.has_drift is False
    assert "spike" not in report.missing_on_disk
    assert "spike" not in report.extra_on_disk
