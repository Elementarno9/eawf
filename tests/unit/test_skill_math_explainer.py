"""The retired ``/math-explainer`` skill stays out of the shipped tree.

``/math-explainer`` is retired from the closed skill catalog (explanation
and proof split into ``/research`` and ``/test``), so its registry body is
deleted and a clean rendered tree must not carry its page.
"""

from __future__ import annotations

from pathlib import Path

from eawf.runtime.runtimes.claude.plugin_install import _render_skill
from eawf.workflow.skills.catalog import shipped_skill_specs
from eawf.workflow.skills.discovery import reconcile_skills


def _render_clean_tree(root: Path) -> None:
    for spec in shipped_skill_specs():
        skill_dir = root / spec.skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_render_skill(spec), encoding="utf-8")


def test_reconcile_clean_omits_retired_math_explainer(tmp_path: Path) -> None:
    """The retired /math-explainer is not shipped, so a clean tree omits it."""
    root = tmp_path / ".claude" / "skills"
    _render_clean_tree(root)
    assert not (root / "math-explainer").exists()
    report = reconcile_skills(root)
    assert report.has_drift is False
    assert "math-explainer" not in report.missing_on_disk
