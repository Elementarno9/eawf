"""Unit tests for ``eawf.runtime.runtimes.claude.plugin_doctor``.

Covers:

- Clean tree → :attr:`DoctorReport.clean` is ``True``; no drift / missing.
- Hand-edit → exactly one drifted entry; ``clean`` is ``False``.
- Missing file → ``missing`` list populated; ``clean`` is ``False``.
- Settings.json drift detected against manifest hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from eawf.runtime.runtimes.claude.plugin_doctor import doctor_plugin
from eawf.runtime.runtimes.claude.plugin_install import install_plugin
from eawf.runtime.runtimes.claude.plugin_update import update_plugin
from eawf.surfaces.render.agents import AGENT_REGISTRY
from eawf.surfaces.render.hooks import HOOK_REGISTRY
from eawf.surfaces.render.manifest import load as load_manifest
from eawf.surfaces.render.manifest import save_atomic as save_manifest
from eawf.workflow.skills.catalog import shipped_skill_specs


def test_doctor_clean_after_install(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    report = doctor_plugin(tmp_path)
    assert report.clean is True
    assert not report.drifted
    assert not report.missing
    # Total ok count = skills + agents + installed (handler-backed) hooks + settings.
    installed_hooks = sum(1 for spec in HOOK_REGISTRY if spec.has_handler)
    assert len(report.ok) == (
        len(shipped_skill_specs()) + len(AGENT_REGISTRY) + installed_hooks + 1
    )


def test_doctor_detects_skill_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_path = tmp_path / ".claude" / "skills" / "verify" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n# drift\n")
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.region_id == "plugin.claude.skill.verify" for e in report.drifted)


def test_doctor_detects_agent_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    path = tmp_path / ".claude" / "agents" / "executor.md"
    path.write_text(path.read_text() + "\n# drift\n")
    report = doctor_plugin(tmp_path)
    assert any(e.region_id == "plugin.claude.agent.executor" for e in report.drifted)


def test_doctor_detects_hook_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    # session_end is the only handler-backed (installed) hook.
    path = tmp_path / ".claude" / "hooks" / "session_end.sh"
    path.write_text(path.read_text() + "\n# drift\n")
    report = doctor_plugin(tmp_path)
    assert any(e.region_id == "plugin.claude.hook.session_end" for e in report.drifted)


def test_doctor_detects_missing_session_end_hook(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    path = tmp_path / ".claude" / "hooks" / "session_end.sh"
    path.unlink()
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.region_id == "plugin.claude.hook.session_end" for e in report.missing)


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


def _rewrite_recorded_settings(target: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Rewrite ``settings.json`` and record its hash, as an older install left it.

    The manifest recording the rewritten bytes is what makes the stale file
    pass the byte check: the install that wrote it also recorded it.
    """
    settings_path = target / ".claude" / "settings.json"
    document = json.loads(settings_path.read_text(encoding="utf-8"))
    mutate(document)
    body = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
    settings_path.write_bytes(body)
    manifest_path = target / ".ea" / "indexes" / "generated.json"
    manifest = load_manifest(manifest_path)
    key = next(k for k, e in manifest.generated.items() if e.region_id == "plugin.claude.settings")
    recorded = hashlib.blake2b(body, digest_size=8).hexdigest()
    manifest.generated[key] = manifest.generated[key].model_copy(update={"hash": recorded})
    save_manifest(manifest_path, manifest)


def _rc2_managed_block(document: dict[str, Any]) -> None:
    """Give ``__eawf_managed`` the nested ``hooks`` key the rc2 install wrote."""
    document["__eawf_managed"]["hooks"] = document["__eawf_managed"].pop("hook_scripts")


def _rc2_stop_hook(document: dict[str, Any]) -> None:
    """Wire the eawf session-end wrapper under ``Stop``, as the rc2 install did."""
    document["hooks"]["Stop"] = document["hooks"].pop("SessionEnd")


def test_doctor_flags_rc2_managed_hooks_key(tmp_path: Path) -> None:
    """A recorded settings.json still carrying ``__eawf_managed.hooks`` is drift."""
    install_plugin(tmp_path)
    _rewrite_recorded_settings(tmp_path, _rc2_managed_block)
    report = doctor_plugin(tmp_path)
    assert any(e.region_id == "plugin.claude.settings" for e in report.ok)
    stale = [e for e in report.drifted if e.region_id == "plugin.claude.settings.managed_hooks"]
    assert len(stale) == 1
    assert stale[0].fix == "eawf plugin update claude"
    assert report.clean is False


def test_doctor_flags_rc2_stop_hook_entry(tmp_path: Path) -> None:
    """An eawf hook wired under an event the install no longer wires is drift."""
    install_plugin(tmp_path)
    _rewrite_recorded_settings(tmp_path, _rc2_stop_hook)
    report = doctor_plugin(tmp_path)
    assert any(e.region_id == "plugin.claude.settings" for e in report.ok)
    stale = [e for e in report.drifted if e.region_id == "plugin.claude.settings.hook.Stop"]
    assert len(stale) == 1
    assert stale[0].fix == "eawf plugin update claude"


def test_doctor_keeps_a_user_stop_hook(tmp_path: Path) -> None:
    """A ``Stop`` hook of the operator's own is theirs, not stale eawf wiring."""
    install_plugin(tmp_path)

    def _user_stop(document: dict[str, Any]) -> None:
        document["hooks"]["Stop"] = [
            {"matcher": "", "hooks": [{"type": "command", "command": "notify-send done"}]}
        ]

    _rewrite_recorded_settings(tmp_path, _user_stop)
    report = doctor_plugin(tmp_path)
    assert report.clean is True


def test_doctor_keeps_a_user_hook_script_beside_the_eawf_wrappers(tmp_path: Path) -> None:
    """A script of the operator's own under ``.claude/hooks/`` is not eawf wiring."""
    install_plugin(tmp_path)

    def _user_script(document: dict[str, Any]) -> None:
        command = {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/notify.sh"}
        document["hooks"]["Stop"] = [{"matcher": "", "hooks": [command]}]

    _rewrite_recorded_settings(tmp_path, _user_script)
    report = doctor_plugin(tmp_path)
    assert report.clean is True, report.drifted


def test_doctor_marketplace_with_only_user_hook_scripts_is_not_double_load(tmp_path: Path) -> None:
    """Boundary: user scripts under ``.claude/hooks/`` in eawf's events load nothing twice."""
    install_plugin(tmp_path)

    def _user_scripts(document: dict[str, Any]) -> None:
        command = {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/lint.sh"}
        document["hooks"] = {
            event: [{"matcher": "", "hooks": [command]}] for event in document["hooks"]
        }

    _rewrite_recorded_settings(tmp_path, _user_scripts)
    report = doctor_plugin(tmp_path, marketplace_installed=True)
    assert not any(e.region_id == "plugin.claude.hooks.double_load" for e in report.drifted)


def test_update_clears_rc2_settings(tmp_path: Path) -> None:
    """The fix the doctor names leaves a clean report behind."""
    install_plugin(tmp_path)

    def _rc2(document: dict[str, Any]) -> None:
        _rc2_managed_block(document)
        _rc2_stop_hook(document)

    _rewrite_recorded_settings(tmp_path, _rc2)
    assert len(doctor_plugin(tmp_path).drifted) == 2
    update_plugin(tmp_path)
    report = doctor_plugin(tmp_path)
    assert report.clean is True
    document = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "Stop" not in document["hooks"]
    assert "hooks" not in document["__eawf_managed"]


def test_doctor_flags_hooks_loaded_twice_with_marketplace(tmp_path: Path) -> None:
    """Project hooks plus the marketplace plugin's hooks fire every handler twice."""
    install_plugin(tmp_path)
    report = doctor_plugin(tmp_path, marketplace_installed=True)
    doubled = [e for e in report.drifted if e.region_id == "plugin.claude.hooks.double_load"]
    assert len(doubled) == 1
    assert doubled[0].fix is not None
    assert "/plugin uninstall" in doubled[0].fix


def test_doctor_without_marketplace_has_no_double_load(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    assert doctor_plugin(tmp_path, marketplace_installed=False).clean is True


def test_doctor_marketplace_without_project_hooks_is_not_double_load(tmp_path: Path) -> None:
    """With no eawf entry in the project hooks block, nothing loads twice."""
    install_plugin(tmp_path)
    _rewrite_recorded_settings(tmp_path, lambda document: document.pop("hooks"))
    report = doctor_plugin(tmp_path, marketplace_installed=True)
    assert not any(e.region_id == "plugin.claude.hooks.double_load" for e in report.drifted)
