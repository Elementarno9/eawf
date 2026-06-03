"""Unit tests for the ``/math-explainer`` authoring skill (P29-I07-W11).

``/math-explainer`` is a registry-resident workflow skill: it renders a
``SKILL.md`` (so it installs as a slash command and reconciles against
the disk tree) but drives no state mutation, so it has no engine
``Skill`` subclass. It joins ``/design`` as an operator-driven,
model-barred authoring pass that writes only under ``.ea/local/`` —
here authoring a verification-grounded ``MathExplainer`` over the typed
``MathClaim`` rows from ``kernel/spec/math.py``.

Pinned here:

- the ``SkillSpec`` row resolves under its canonical name;
- ``/math-explainer`` is user-invocable but NOT model-invocable (it is
  an operator-driven facet + clarity-loop authoring pass, the
  ``/design`` classification);
- it renders a frontmatter-shaped ``SKILL.md`` without raising, with the
  classification flags reflected in the YAML;
- the body documents the in-skill clarity loop (``vale-prose`` +
  ``EAWF019`` + ``draft validate``) over ``MathClaim`` / ``MathExplainer``
  and the four-facet per-claim contract;
- ``reconcile_skills`` stays clean with the row present.
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


def test_math_explainer_skill_row_resolves() -> None:
    """``/math-explainer`` is registered as a SkillSpec row in the registry."""
    spec = _spec("math-explainer")
    assert spec.skill_name == "math-explainer"
    assert "<explainer-slug>" in spec.argument_hint
    assert "--final" in spec.argument_hint


def test_math_explainer_skill_is_user_invocable_not_model_invocable() -> None:
    """``/math-explainer`` is operator-driven: slash-visible, model-barred.

    It mirrors ``/design`` — a multi-step authoring pass that writes only
    under ``.ea/local/``, so the model must not reach for it autonomously.
    """
    spec = _spec("math-explainer")
    assert spec.user_invocable is True
    assert spec.disable_model_invocation is True


def test_math_explainer_skill_renders_frontmatter() -> None:
    """The rendered SKILL.md carries the frontmatter + body heading."""
    output = render_skill_md_from_spec(_spec("math-explainer"))
    assert output.startswith("---\n")
    assert "\nname: math-explainer\n" in output
    assert "\nuser-invocable: true\n" in output
    assert "\ndisable-model-invocation: true\n" in output
    assert "# /math-explainer" in output


def test_math_explainer_body_documents_in_skill_clarity_loop() -> None:
    """The body names the three clarity-loop legs and their owners."""
    body = _spec("math-explainer").body
    assert "vale-prose" in body
    assert "eawf019-math-facets" in body
    assert "draft validate" in body


def test_math_explainer_body_documents_typed_models_and_facets() -> None:
    """The body grounds the flow in the W09 typed models + four facets."""
    body = _spec("math-explainer").body
    assert "MathExplainer" in body
    assert "MathClaim" in body
    assert "four-facet" in body or "four facets" in body
    # The runnable-example facet is a command_exit_zero GateSpec, and the
    # refute/certify assurance split is the typed home for verifier strength.
    assert "GateSpec" in body
    assert "assurance" in body


def test_math_explainer_body_has_no_dangling_smoke_test_reference() -> None:
    """No ``/smoke-test`` skill is registered, so the body must not cite one."""
    assert "smoke-test" not in _spec("math-explainer").body


def _render_clean_tree(root: Path) -> None:
    for spec in SKILL_REGISTRY:
        skill_dir = root / spec.skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_render_skill(spec), encoding="utf-8")


def test_reconcile_clean_with_math_explainer_present(tmp_path: Path) -> None:
    """A tree rendered from the registry (incl /math-explainer) has zero drift."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    assert (root / "math-explainer" / "SKILL.md").is_file()
    report = reconcile_skills(root)
    assert report.has_drift is False
    assert "math-explainer" not in report.missing_on_disk
    assert "math-explainer" not in report.extra_on_disk
