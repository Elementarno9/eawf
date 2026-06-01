"""Unit tests for the read-only/mutating model-invocation contract and the
registry-vs-disk reconcile sweep (``eawf.workflow.skills.discovery``).

Two contracts are pinned here:

- **Model-invocation split.** Read-only / investigative skills
  (research, audit, review, security-review, blitz, differentiate) render
  with ``disable_model_invocation=False`` so the model may auto-invoke
  them; mutating / lifecycle skills (prep, ship, polish, init, roadmap,
  flow, memory, agent-dispatch, wave-spec) stay model-barred
  (``disable_model_invocation=True``) so the model cannot autonomously
  drive a state mutation or commit. ``spike`` and ``deep-research`` are
  not standalone registry skills — both are modes of ``/research`` (the
  spike-workflow convention + the ``deep-research`` survey depth), which
  is already model-invocable.
- **Reconcile.** :func:`reconcile_skills` diffs the frozen
  :data:`SKILL_REGISTRY` against a rendered ``<root>/<name>/SKILL.md``
  tree and reports missing-on-disk, extra-on-disk, and flag-mismatch
  drift. A clean tree has no drift; each injected drift class is caught.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude.plugin_install import _render_skill
from eawf.surfaces.render.skills import SKILL_REGISTRY
from eawf.workflow.skills.discovery import (
    SkillFrontmatterError,
    reconcile_skills,
)

# Read-only / investigative / advisory skills the model MAY auto-invoke.
# These either make no persisted change (research, audit, review,
# security-review, blitz, differentiate, mockup) or only resolve a value /
# record a telemetry-or-intent event while routing the actual mutation to
# the daemon (coauthor resolves a trailer; compress records a compression
# directive) — none drives a lifecycle / state transition on its own, so
# autonomous invocation is safe. ``mockup`` produces ASCII UI mockups as
# AskUserQuestion previews and drives no state mutation.
_READ_ONLY_SKILL_NAMES: frozenset[str] = frozenset(
    {
        "research",
        "audit",
        "review",
        "security-review",
        "blitz",
        "differentiate",
        "mockup",
        "coauthor",
        "compress",
    }
)

# Mutating / lifecycle skills the model is BARRED from auto-invoking. The
# six lifecycle drivers (prep, ship, polish, init, roadmap, flow) advance
# the workflow; the remaining three scaffold or dispatch a concrete
# lifecycle artifact (memory writes durable recall, wave-spec scaffolds a
# spec, agent-dispatch hands a wave to a runtime) — auto-invoking any of
# them would drive workflow progress without operator intent.
_MUTATING_SKILL_NAMES: frozenset[str] = frozenset(
    {
        "prep",
        "ship",
        "polish",
        "init",
        "roadmap",
        "flow",
        "memory",
        "agent-dispatch",
        "wave-spec",
    }
)

# Model-only code-quality playbooks: hidden from the slash menu
# (``user_invocable=False``) yet model-invocable.
_MODEL_ONLY_SKILL_NAMES: frozenset[str] = frozenset(
    {
        "refactor-god-class",
        "write-adr",
        "add-property-test",
        "extract-function",
        "extract-module",
        "graduate-research-code",
    }
)


def _registry_by_name() -> dict[str, object]:
    return {spec.skill_name: spec for spec in SKILL_REGISTRY}


# --- model-invocation classification contract -------------------------------


@pytest.mark.parametrize("name", sorted(_READ_ONLY_SKILL_NAMES))
def test_read_only_skill_is_model_invocable(name: str) -> None:
    """Each read-only skill renders ``disable_model_invocation=False``."""
    spec = _registry_by_name()[name]
    assert spec.disable_model_invocation is False, (
        f"{name} is read-only and must be model-invocable"
    )


@pytest.mark.parametrize("name", sorted(_MUTATING_SKILL_NAMES))
def test_mutating_skill_is_model_barred(name: str) -> None:
    """Each mutating/lifecycle skill stays ``disable_model_invocation=True``."""
    spec = _registry_by_name()[name]
    assert spec.disable_model_invocation is True, (
        f"{name} mutates state/commits and must stay model-barred"
    )


@pytest.mark.parametrize("name", sorted(_MODEL_ONLY_SKILL_NAMES))
def test_model_only_playbook_is_model_invocable(name: str) -> None:
    """Model-only playbooks stay hidden-but-model-invocable."""
    spec = _registry_by_name()[name]
    assert spec.user_invocable is False
    assert spec.disable_model_invocation is False


def test_classification_partitions_the_registry() -> None:
    """The three classified sets exactly cover every registry skill.

    Guards against a new skill landing without an explicit read-only /
    mutating / model-only classification (which would leave its
    model-invocability un-pinned).
    """
    classified = _READ_ONLY_SKILL_NAMES | _MUTATING_SKILL_NAMES | _MODEL_ONLY_SKILL_NAMES
    registry_names = {spec.skill_name for spec in SKILL_REGISTRY}
    assert classified == registry_names


def test_no_user_invocable_skill_is_both_read_only_and_mutating() -> None:
    """Read-only and mutating sets are disjoint (no double-classification)."""
    assert _READ_ONLY_SKILL_NAMES.isdisjoint(_MUTATING_SKILL_NAMES)


# --- reconcile sweep --------------------------------------------------------


def _render_clean_tree(root: Path) -> None:
    """Render a faithful ``<root>/<name>/SKILL.md`` tree from the registry."""
    for spec in SKILL_REGISTRY:
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
    for child in (root / "audit").iterdir():
        child.unlink()
    (root / "audit").rmdir()
    report = reconcile_skills(root)
    assert report.has_drift is True
    assert report.missing_on_disk == ("audit",)
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
    assert len(report.missing_on_disk) == len(SKILL_REGISTRY)
    assert report.extra_on_disk == ()
    assert report.flag_mismatches == ()


def test_reconcile_skills_description_with_colon_does_not_crash(tmp_path: Path) -> None:
    """A description carrying an embedded ``': '`` parses cleanly.

    The rendered ``description`` value is unquoted; ``/prep``'s text
    ("Activate the next PLANNED phase: surface its DAG ...") embeds a
    colon. The reconcile parser line-scans the two flag keys instead of
    YAML-loading the block so the colon does not derail the sweep.
    """
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    report = reconcile_skills(root)
    # ``/prep`` (colon in description) must NOT be reported missing.
    assert "prep" not in report.missing_on_disk
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
