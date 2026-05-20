"""Unit tests for ``eawf.runtimes.claude.plugin_update``.

Covers:

- Update succeeds on a clean tree, returning all-unchanged deltas.
- Update aborts with :class:`IntegrityViolation` (exit code 8) on
  hand-edits — acceptance §3.
- Update never accepts ``force`` (the surface delegates to
  ``install --force`` for that path).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.cli.exit_codes import INTEGRITY_VIOLATION
from eawf.runtimes.claude.plugin_install import IntegrityViolation, install_plugin
from eawf.runtimes.claude.plugin_update import update_plugin


def test_update_plugin_succeeds_on_clean_tree(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    result = update_plugin(tmp_path)
    for delta in result.skills + result.agents + result.hooks:
        assert delta.action == "unchanged", f"unexpected action: {delta}"
    assert result.settings is not None
    assert result.settings.action == "unchanged"


def test_update_plugin_aborts_on_hand_edit(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_path = tmp_path / ".claude" / "skills" / "audit" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n# hand-edit\n")
    with pytest.raises(IntegrityViolation):
        update_plugin(tmp_path)


def test_update_plugin_integrity_violation_exit_code_constant() -> None:
    """``INTEGRITY_VIOLATION`` aliases to ``STATE_CONFLICT`` (3) post C05 § 5.3."""
    assert INTEGRITY_VIOLATION == 3


def test_update_plugin_aborts_on_hand_edit_in_agent(tmp_path: Path) -> None:
    """Hand-edit in an agent file (not just SKILL.md) also aborts."""
    install_plugin(tmp_path)
    agent_path = tmp_path / ".claude" / "agents" / "researcher.md"
    agent_path.write_text(agent_path.read_text() + "\n# user-edit\n")
    with pytest.raises(IntegrityViolation):
        update_plugin(tmp_path)


def test_update_plugin_aborts_on_hand_edit_in_hook(tmp_path: Path) -> None:
    """Hand-edit in a hook script also aborts."""
    install_plugin(tmp_path)
    hook_path = tmp_path / ".claude" / "hooks" / "pre_commit.sh"
    hook_path.write_text(hook_path.read_text() + "\n# user-edit\n")
    with pytest.raises(IntegrityViolation):
        update_plugin(tmp_path)


def test_update_plugin_after_external_install_force_succeeds(tmp_path: Path) -> None:
    """If ``install --force`` rewrote drift, ``update`` then succeeds."""
    install_plugin(tmp_path)
    skill_path = tmp_path / ".claude" / "skills" / "audit" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n# drift\n")
    # Operator escalates to force install (the documented recipe).
    install_plugin(tmp_path, force=True)
    # Update now succeeds.
    update_plugin(tmp_path)


def test_update_plugin_keeps_user_settings_keys(tmp_path: Path) -> None:
    """User-owned keys in settings.json survive the update round-trip."""
    import json

    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {"$schema": "x", "permissions": {"allow": ["Bash(uv *)"]}, "skillOverrides": {}}
        ),
        encoding="utf-8",
    )
    install_plugin(tmp_path)
    update_plugin(tmp_path)
    parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    assert parsed["$schema"] == "x"
    assert parsed["permissions"] == {"allow": ["Bash(uv *)"]}
    assert "__eawf_managed" in parsed
