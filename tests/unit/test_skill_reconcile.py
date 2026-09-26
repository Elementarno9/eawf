"""Unit tests for the read-only/mutating model-invocation contract and the
registry-vs-disk reconcile sweep (``eawf.workflow.skills.discovery``).

Two contracts are pinned here:

- **Model-invocation split.** Read-only / investigative skills
  (research, audit, review, security-review, blitz, differentiate,
  mockup, spike) render with ``disable_model_invocation=False`` so the
  model may auto-invoke them; mutating / lifecycle skills (prep, ship,
  polish, init, roadmap, flow, memory, agent-dispatch, wave-spec) stay
  model-barred (``disable_model_invocation=True``) so the model cannot
  autonomously drive a state mutation or commit. ``design`` is the
  read-only-but-model-barred case: it writes only under ``.ea/local/``
  yet stays model-barred because it is an operator-driven multi-round
  ``AskUserQuestion`` design pass, not a model-autoinvocable probe.
  ``deep-research`` is not a standalone registry skill — it is a mode of
  ``/research`` (the survey depth), which is already model-invocable.
- **Reconcile.** :func:`reconcile_skills` diffs the frozen
  :data:`SKILL_REGISTRY` against a rendered ``<root>/<name>/SKILL.md``
  tree and reports missing-on-disk, extra-on-disk, and flag-mismatch
  drift. A clean tree has no drift; each injected drift class is caught.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude.plugin_install import _render_skill
from eawf.workflow.skills.catalog import shipped_skill_specs
from eawf.workflow.skills.discovery import (
    SkillFrontmatterError,
    reconcile_skills,
)

# Read-only skills the model MAY auto-invoke: none of them mutates canonical
# state, so autonomous invocation cannot drive a lifecycle transition.
_READ_ONLY_SKILL_NAMES: frozenset[str] = frozenset(
    {"mockup", "refactor", "research", "spike", "test", "why"}
)

# Skills the model is BARRED from auto-invoking: every canonical-mutating
# skill, plus every operator-only (``user_only``) skill whatever its effects.
_MODEL_BARRED_SKILL_NAMES: frozenset[str] = frozenset(
    {
        "accept",
        "attend",
        "backlog",
        "campaign",
        "decide",
        "dispatch",
        "integrate",
        "memory",
        "milestone",
        "plan",
        "reflect",
        "release",
        "track",
        "verify",
    }
)


def _registry_by_name() -> dict[str, object]:
    return {spec.skill_name: spec for spec in shipped_skill_specs()}


# --- model-invocation classification contract -------------------------------


@pytest.mark.parametrize("name", sorted(_READ_ONLY_SKILL_NAMES))
def test_read_only_skill_is_model_invocable(name: str) -> None:
    """Each read-only skill renders ``disable_model_invocation=False``."""
    spec = _registry_by_name()[name]
    assert spec.disable_model_invocation is False, (
        f"{name} is read-only and must be model-invocable"
    )


@pytest.mark.parametrize("name", sorted(_MODEL_BARRED_SKILL_NAMES))
def test_mutating_skill_is_model_barred(name: str) -> None:
    """Each mutating or operator-only skill stays ``disable_model_invocation=True``."""
    spec = _registry_by_name()[name]
    assert spec.disable_model_invocation is True, (
        f"{name} mutates state or is operator-only and must stay model-barred"
    )


def test_every_shipped_skill_is_user_invocable() -> None:
    """No presented skill is hidden from the slash menu."""
    assert all(spec.user_invocable for spec in shipped_skill_specs())


def test_classification_partitions_the_registry() -> None:
    """The two classified sets exactly and disjointly cover every shipped skill.

    Guards against a new skill landing without an explicit read-only /
    model-barred classification (which would leave its model-invocability
    un-pinned).
    """
    assert _READ_ONLY_SKILL_NAMES.isdisjoint(_MODEL_BARRED_SKILL_NAMES)
    registry_names = {spec.skill_name for spec in shipped_skill_specs()}
    assert registry_names == _READ_ONLY_SKILL_NAMES | _MODEL_BARRED_SKILL_NAMES


# --- reconcile sweep --------------------------------------------------------


def _render_clean_tree(root: Path) -> None:
    """Render a faithful ``<root>/<name>/SKILL.md`` tree from the registry."""
    for spec in shipped_skill_specs():
        skill_dir = root / spec.skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_render_skill(spec), encoding="utf-8")


def test_reconcile_skills_clean_tree_reports_no_drift(tmp_path: Path) -> None:
    """A tree rendered straight from the registry has zero drift."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    report = reconcile_skills(root)
    assert report.has_drift is False
    assert report.missing_on_disk == ()
    assert report.extra_on_disk == ()
    assert report.flag_mismatches == ()
    assert report.skills_root == root


def test_reconcile_skills_missing_on_disk(tmp_path: Path) -> None:
    """Deleting a rendered skill dir surfaces it as missing-on-disk."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    for child in (root / "verify").iterdir():
        child.unlink()
    (root / "verify").rmdir()
    report = reconcile_skills(root)
    assert report.has_drift is True
    assert report.missing_on_disk == ("verify",)
    assert report.extra_on_disk == ()
    assert report.flag_mismatches == ()


def test_reconcile_skills_extra_on_disk(tmp_path: Path) -> None:
    """A SKILL.md dir with no registry row surfaces as extra-on-disk."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    stray = root / "totally-bogus"
    stray.mkdir()
    (stray / "SKILL.md").write_text(
        "---\nname: totally-bogus\nuser-invocable: true\n"
        "disable-model-invocation: false\n---\nbody\n",
        encoding="utf-8",
    )
    report = reconcile_skills(root)
    assert report.has_drift is True
    assert report.extra_on_disk == ("totally-bogus",)
    assert report.missing_on_disk == ()
    assert report.flag_mismatches == ()


def test_reconcile_skills_flag_mismatch(tmp_path: Path) -> None:
    """An on-disk flag that disagrees with the registry is reported."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    research_md = root / "research" / "SKILL.md"
    research_md.write_text(
        research_md.read_text(encoding="utf-8").replace(
            "disable-model-invocation: false",
            "disable-model-invocation: true",
        ),
        encoding="utf-8",
    )
    report = reconcile_skills(root)
    assert report.has_drift is True
    assert report.missing_on_disk == ()
    assert report.extra_on_disk == ()
    assert [m.name for m in report.flag_mismatches] == ["research"]
    mismatch = report.flag_mismatches[0]
    assert mismatch.registry_flags.disable_model_invocation is False
    assert mismatch.disk_flags.disable_model_invocation is True


def test_reconcile_skills_missing_root_reports_all_missing(tmp_path: Path) -> None:
    """A non-existent root marks every registry skill missing-on-disk."""
    report = reconcile_skills(tmp_path / "does-not-exist")
    assert report.has_drift is True
    assert len(report.missing_on_disk) == len(shipped_skill_specs())
    assert report.extra_on_disk == ()
    assert report.flag_mismatches == ()


def test_reconcile_skills_description_with_colon_does_not_crash(tmp_path: Path) -> None:
    """A description carrying an embedded ``': '`` parses cleanly.

    The rendered ``description`` value is unquoted; ``/dispatch``'s text
    ("Activate the next PLANNED phase: surface its DAG ...") embeds a
    colon. The reconcile parser line-scans the two flag keys instead of
    YAML-loading the block so the colon does not derail the sweep.
    """
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    report = reconcile_skills(root)
    # ``/dispatch`` (colon in description) must NOT be reported missing.
    assert "dispatch" not in report.missing_on_disk
    assert report.has_drift is False


def test_reconcile_skills_ignores_dir_without_skill_md(tmp_path: Path) -> None:
    """A subdir lacking SKILL.md is skipped, not reported as extra."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    (root / "stray-no-md").mkdir()
    report = reconcile_skills(root)
    assert "stray-no-md" not in report.extra_on_disk
    assert report.has_drift is False


def test_parse_rendered_flags_rejects_non_boolean_token(tmp_path: Path) -> None:
    """A non-boolean flag token raises a frontmatter error (skipped + warned)."""
    from eawf.workflow.skills.discovery import _parse_rendered_flags

    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: bad\nuser-invocable: maybe\ndisable-model-invocation: false\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillFrontmatterError, match="user-invocable"):
        _parse_rendered_flags(skill_md)


def test_parse_rendered_flags_rejects_missing_frontmatter(tmp_path: Path) -> None:
    """A SKILL.md with no ``---`` block raises a frontmatter error."""
    from eawf.workflow.skills.discovery import _parse_rendered_flags

    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("no frontmatter here\n", encoding="utf-8")
    with pytest.raises(SkillFrontmatterError, match="frontmatter"):
        _parse_rendered_flags(skill_md)


# --- the rendered argument-hint is the catalog grammar ----------------------


def _rendered_argument_hint(rendered: str) -> str:
    """Return the ``argument-hint:`` frontmatter line from a rendered SKILL.md."""
    for line in rendered.splitlines():
        if line.startswith("argument-hint:"):
            return line
    raise AssertionError("rendered SKILL.md carries no argument-hint frontmatter line")


@pytest.mark.parametrize("name", sorted(_READ_ONLY_SKILL_NAMES | _MODEL_BARRED_SKILL_NAMES))
def test_rendered_skill_md_argument_hint_carries_runtime_options(name: str) -> None:
    """Every option the catalog grammar declares lands in the rendered hint line."""
    from eawf.workflow.skills.catalog import resolve_skill

    spec = _registry_by_name()[name]
    hint_line = _rendered_argument_hint(_render_skill(spec))
    for opt in resolve_skill(name).grammar.options:
        assert opt in hint_line, f"{name} rendered SKILL.md dropped {opt!r} from its argument-hint"
