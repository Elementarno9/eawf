"""Unit tests for ``eawf.runtimes.claude.plugin_doctor``.

Covers:

- Clean tree → :attr:`DoctorReport.clean` is ``True``; no drift / missing.
- Hand-edit → exactly one drifted entry; ``clean`` is ``False``.
- Missing file → ``missing`` list populated; ``clean`` is ``False``.
- Settings.json drift detected against manifest hash.
"""

from __future__ import annotations

from pathlib import Path

from eawf.runtimes.claude.plugin_doctor import doctor_plugin
from eawf.runtimes.claude.plugin_install import install_plugin


def test_doctor_clean_after_install(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    report = doctor_plugin(tmp_path)
    assert report.clean is True
    assert not report.drifted
    assert not report.missing
    # Total ok count = skills + agents + hooks + settings.
    assert len(report.ok) == 17 + 8 + 15 + 1


def test_doctor_detects_skill_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_path = tmp_path / ".claude" / "skills" / "polish" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n# drift\n")
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.region_id == "plugin.claude.skill.polish" for e in report.drifted)


def test_doctor_detects_agent_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    path = tmp_path / ".claude" / "agents" / "executor.md"
    path.write_text(path.read_text() + "\n# drift\n")
    report = doctor_plugin(tmp_path)
    assert any(e.region_id == "plugin.claude.agent.executor" for e in report.drifted)


def test_doctor_detects_hook_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    path = tmp_path / ".claude" / "hooks" / "post_commit.sh"
    path.write_text(path.read_text() + "\n# drift\n")
    report = doctor_plugin(tmp_path)
    assert any(e.region_id == "plugin.claude.hook.post_commit" for e in report.drifted)


def test_doctor_detects_missing_agent_end_hook(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    path = tmp_path / ".claude" / "hooks" / "agent_end.sh"
    path.unlink()
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.region_id == "plugin.claude.hook.agent_end" for e in report.missing)


def test_doctor_detects_missing_file(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_path = tmp_path / ".claude" / "skills" / "research" / "SKILL.md"
    skill_path.unlink()
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.region_id == "plugin.claude.skill.research" for e in report.missing)


def test_doctor_reports_settings_missing_when_install_never_ran(tmp_path: Path) -> None:
    """Without an install, the manifest is empty → settings.json reads as missing."""
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.region_id == "plugin.claude.settings" for e in report.missing)


def test_doctor_detects_settings_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.write_text(settings_path.read_text() + "\n", encoding="utf-8")
    report = doctor_plugin(tmp_path)
    assert any(e.region_id == "plugin.claude.settings" for e in report.drifted)


def test_doctor_each_entry_has_path_inside_target(tmp_path: Path) -> None:
    """Every entry's path must live under *target_dir* (no escaping)."""
    install_plugin(tmp_path)
    report = doctor_plugin(tmp_path)
    target_resolved = tmp_path.resolve()
    for entry in report.ok + report.drifted + report.missing:
        assert str(entry.path).startswith(str(target_resolved))


def test_doctor_clean_property_reflects_lists(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    report = doctor_plugin(tmp_path)
    assert report.clean == (not report.drifted and not report.missing)
