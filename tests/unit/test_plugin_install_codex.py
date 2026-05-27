"""Unit tests for the Codex runtime plugin installer (P14-I02-W01).

Covers the native plugin layout (``<plugin_root>/.codex-plugin/plugin.json``
plus skills/agents/hooks under the plugin root) and the scope-aware
install path: ``scope="project"`` writes under
``<target>/.codex/plugins/eawf/``; ``scope="user"`` writes under
``<home>/.codex/plugins/eawf/`` with ``home`` injected via the
``fake_home`` fixture.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from eawf.runtime.runtimes.codex import doctor_plugin, expected_paths, install_plugin
from eawf.runtime.runtimes.codex.plugin_install import IntegrityViolation
from eawf.runtime.runtimes.codex.skills import render_codex_agent_toml
from eawf.surfaces.render.agents import AGENT_REGISTRY
from eawf.surfaces.render.hooks import HOOK_REGISTRY
from eawf.surfaces.render.skills import SKILL_REGISTRY

_GOLDEN_DIR = Path(__file__).parents[1] / "golden" / "plugin_install" / "codex"


@pytest.fixture()
def fake_home(tmp_path: Path) -> Path:
    """Synthetic ``$HOME`` for ``scope="user"`` paths."""
    home = tmp_path / "fake-home"
    home.mkdir()
    return home


def _install_kwargs(scope: str, fake_home: Path) -> dict[str, object]:
    return {"scope": scope, "home": fake_home} if scope == "user" else {"scope": scope}


def _plugin_root(target: Path, scope: str, fake_home: Path) -> Path:
    if scope == "project":
        return target / ".codex" / "plugins" / "eawf"
    return fake_home / ".codex" / "plugins" / "eawf"


def _config_path(target: Path, scope: str, fake_home: Path) -> Path:
    if scope == "project":
        return target / ".codex" / "config.toml"
    return fake_home / ".codex" / "config.toml"


def _agents_dir(target: Path, scope: str, fake_home: Path) -> Path:
    if scope == "project":
        return target / ".codex" / "agents"
    return fake_home / ".codex" / "agents"


def test_rendered_executor_agent_toml_matches_golden() -> None:
    executor = next(spec for spec in AGENT_REGISTRY if spec.role == "executor")
    expected = (_GOLDEN_DIR / "agents" / "executor.toml").read_bytes()
    assert render_codex_agent_toml(executor).encode("utf-8") == expected
    parsed = tomllib.loads(expected.decode("utf-8"))
    assert parsed["name"] == "executor"
    assert parsed["developer_instructions"].endswith("`executor_report` store.")


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_creates_plugin_layout(tmp_path: Path, fake_home: Path, scope: str) -> None:
    result = install_plugin(tmp_path, **_install_kwargs(scope, fake_home))
    root = _plugin_root(tmp_path, scope, fake_home)
    assert (root / ".codex-plugin" / "plugin.json").is_file()
    assert (root / ".codex-plugin" / ".eawf-managed.json").is_file()
    assert len(result.skills) == len(SKILL_REGISTRY)
    assert len(result.agents) == len(AGENT_REGISTRY)
    assert len(result.hooks) == len(HOOK_REGISTRY)
    assert result.scope == scope
    for delta in result.skills:
        assert delta.action == "created"
    for delta in result.agents:
        assert delta.action == "created"
    # Codex plugin.json has no top-level ``agents`` key, so agents are
    # emitted at the Codex scope root rather than under plugin_root.
    assert (root / "agents").exists() is False
    # Skills stay plugin-scoped; the scope root must not get a flat
    # ``.codex/skills`` copy.
    assert (_config_path(tmp_path, scope, fake_home).parent / "skills").exists() is False
    # Codex requires each skill on disk as a directory containing SKILL.md
    # (not a flat <name>.md). Verify the directory layout for every skill.
    for spec in SKILL_REGISTRY:
        skill_dir = root / "skills" / spec.skill_name
        assert skill_dir.is_dir(), skill_dir
        assert (skill_dir / "SKILL.md").is_file(), skill_dir / "SKILL.md"
    for spec in AGENT_REGISTRY:
        agent_path = _agents_dir(tmp_path, scope, fake_home) / f"{spec.role}.toml"
        parsed = tomllib.loads(agent_path.read_text(encoding="utf-8"))
        assert parsed["name"] == spec.role
        assert parsed["description"] == spec.description
        assert spec.body.splitlines()[0] in parsed["developer_instructions"]


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_idempotent_second_run_unchanged(
    tmp_path: Path, fake_home: Path, scope: str
) -> None:
    install_plugin(tmp_path, **_install_kwargs(scope, fake_home))
    second = install_plugin(tmp_path, **_install_kwargs(scope, fake_home))
    assert second.config is not None
    assert second.config.action == "unchanged"
    assert second.manifest is not None
    assert second.manifest.action == "unchanged"
    assert second.sidecar is not None
    assert second.sidecar.action == "unchanged"
    for delta in second.skills + second.agents + second.hooks:
        assert delta.action == "unchanged", (delta.path, delta.action)


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_dry_run_writes_nothing(tmp_path: Path, fake_home: Path, scope: str) -> None:
    result = install_plugin(tmp_path, dry_run=True, **_install_kwargs(scope, fake_home))
    assert result.dry_run is True
    assert not _plugin_root(tmp_path, scope, fake_home).exists()
    assert not _agents_dir(tmp_path, scope, fake_home).exists()


def test_install_rejects_hand_edited_skill_without_force(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_paths, _ = expected_paths(tmp_path)
    first_skill_region = next(k for k in skill_paths if k.startswith("plugin.codex.skill."))
    first_skill = skill_paths[first_skill_region]
    first_skill.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(IntegrityViolation):
        install_plugin(tmp_path)


def test_install_rejects_hand_edited_agent_without_force(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    paths, _ = expected_paths(tmp_path)
    first_agent_region = next(k for k in paths if k.startswith("plugin.codex.agent."))
    first_agent = paths[first_agent_region]
    first_agent.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(IntegrityViolation):
        install_plugin(tmp_path)


def test_install_force_overrides_hand_edit(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_paths, _ = expected_paths(tmp_path)
    first_skill_region = next(k for k in skill_paths if k.startswith("plugin.codex.skill."))
    first_skill = skill_paths[first_skill_region]
    first_skill.write_text("tampered\n", encoding="utf-8")
    result = install_plugin(tmp_path, force=True)
    assert any(d.action == "updated" for d in result.skills)


def test_install_rejects_hand_edited_sidecar_without_force(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    sidecar = tmp_path / ".codex" / "plugins" / "eawf" / ".codex-plugin" / ".eawf-managed.json"
    body = json.loads(sidecar.read_text(encoding="utf-8"))
    body["skills"] = []
    sidecar.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(IntegrityViolation):
        install_plugin(tmp_path)


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_codex_emits_manifest_toml(tmp_path: Path, fake_home: Path, scope: str) -> None:
    """Manifest is JSON at ``.codex-plugin/plugin.json`` per Codex schema."""
    install_plugin(tmp_path, **_install_kwargs(scope, fake_home))
    manifest_path = _plugin_root(tmp_path, scope, fake_home) / ".codex-plugin" / "plugin.json"
    assert manifest_path.is_file()
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert body["name"] == "eawf"
    assert body["version"] == "1.0"
    assert body["description"]
    assert body["skills"] == "./skills/"
    assert body["hooks"] == "./hooks/"
    assert "interface" in body
    iface = body["interface"]
    assert iface["displayName"] == "Eä Workflow"
    assert iface["category"] == "Productivity"
    assert iface["shortDescription"]
    assert iface["longDescription"]
    assert isinstance(iface["capabilities"], list)
    assert isinstance(iface["defaultPrompt"], list)


def test_manifest_interface_omits_url_fields(tmp_path: Path) -> None:
    """Machine-specific URLs / missing asset paths must not leak into the manifest."""
    install_plugin(tmp_path)
    manifest_path = tmp_path / ".codex" / "plugins" / "eawf" / ".codex-plugin" / "plugin.json"
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    iface = body["interface"]
    for k in ("websiteURL", "privacyPolicyURL", "termsOfServiceURL", "composerIcon", "logo"):
        assert k not in iface, f"{k} leaks into manifest"


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_codex_writes_enabled_entry_in_config_toml(
    tmp_path: Path, fake_home: Path, scope: str
) -> None:
    install_plugin(tmp_path, **_install_kwargs(scope, fake_home))
    config_path = _config_path(tmp_path, scope, fake_home)
    text = config_path.read_text(encoding="utf-8")
    assert "[plugins.eawf]" in text
    assert "enabled = true" in text
    assert "# ---- __eawf_managed begin ----" in text
    assert "# ---- __eawf_managed end ----" in text
    # Legacy __eawf_managed TOML table must NOT appear inside config.toml.
    assert "[[__eawf_managed.skills]]" not in text


def test_config_toml_preserves_user_sections(tmp_path: Path) -> None:
    cfg_path = tmp_path / ".codex" / "config.toml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text('user_setting = "keep_me"\n', encoding="utf-8")
    install_plugin(tmp_path)
    text = cfg_path.read_text(encoding="utf-8")
    assert 'user_setting = "keep_me"' in text
    assert "[plugins.eawf]" in text


@pytest.mark.parametrize("scope", ["project", "user"])
def test_doctor_reports_clean_after_install(tmp_path: Path, fake_home: Path, scope: str) -> None:
    install_plugin(tmp_path, **_install_kwargs(scope, fake_home))
    report = doctor_plugin(tmp_path, scope=scope, home=fake_home if scope == "user" else None)
    assert report.clean is True, (report.drifted, report.missing)
    assert not report.drifted
    assert not report.missing


def test_doctor_flags_missing_files(tmp_path: Path) -> None:
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert report.missing


def test_doctor_flags_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    skill_paths, _ = expected_paths(tmp_path)
    first_skill_region = next(k for k in skill_paths if k.startswith("plugin.codex.skill."))
    first_skill = skill_paths[first_skill_region]
    first_skill.write_text("tampered\n", encoding="utf-8")
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert report.drifted


def test_doctor_flags_agent_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    paths, _ = expected_paths(tmp_path)
    first_agent_region = next(k for k in paths if k.startswith("plugin.codex.agent."))
    first_agent = paths[first_agent_region]
    first_agent.write_text("tampered\n", encoding="utf-8")
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.kind == "agent" for e in report.drifted)


def test_doctor_flags_sidecar_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    sidecar = tmp_path / ".codex" / "plugins" / "eawf" / ".codex-plugin" / ".eawf-managed.json"
    body = json.loads(sidecar.read_text(encoding="utf-8"))
    body["hooks"] = []
    sidecar.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.kind == "sidecar" for e in report.drifted)


def test_doctor_report_plugin_root_points_at_scope_dir(tmp_path: Path, fake_home: Path) -> None:
    """Regression: user-scope doctor must surface ``~/.codex/plugins/eawf``
    even when no plugin files are installed (B1 hotfix)."""
    report = doctor_plugin(tmp_path, scope="user", home=fake_home)
    assert report.plugin_root == fake_home / ".codex" / "plugins" / "eawf"
    project_report = doctor_plugin(tmp_path, scope="project")
    assert project_report.plugin_root == tmp_path / ".codex" / "plugins" / "eawf"


def test_doctor_reports_legacy_flat_paths(tmp_path: Path) -> None:
    """Flat ``<target>/.codex/{skills,hooks}/`` is reported as legacy."""
    legacy_dir = tmp_path / ".codex" / "skills"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "stub.md").write_text("# stub\n", encoding="utf-8")
    install_plugin(tmp_path)
    report = doctor_plugin(tmp_path)
    assert legacy_dir in report.legacy_paths


def test_doctor_does_not_report_current_agents_dir_as_legacy(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    report = doctor_plugin(tmp_path)
    assert tmp_path / ".codex" / "agents" not in report.legacy_paths
