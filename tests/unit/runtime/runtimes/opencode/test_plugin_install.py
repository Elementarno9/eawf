"""Unit tests for the OpenCode runtime plugin installer.

Covers the native plugin layout (``.opencode/plugins/eawf.js`` plus
sidecar ``.eawf-managed.json``), removal of the legacy
``plugins:[...]`` array patch, and scope-aware installs under
``$OPENCODE_CONFIG_DIR/plugins/eawf.js`` or
``<home>/.config/opencode/plugins/eawf.js``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import eawf
from eawf.runtime.runtimes.opencode import doctor_plugin, install_plugin
from eawf.runtime.runtimes.opencode.plugin_install import (
    IntegrityViolation,
    expected_paths,
    expected_plugin_js_bytes,
)


@pytest.fixture()
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fake-home"
    home.mkdir()
    return home


@pytest.fixture()
def fake_opencode_config_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / "fake-xdg-opencode"
    cfg.mkdir()
    return cfg


def _install_kwargs(scope: str, fake_home: Path, fake_xdg: Path) -> dict[str, object]:
    """Build kwargs for *scope*. ``user`` scope binds both ``home`` and
    ``opencode_config_dir`` so the XDG override path is exercised."""
    if scope == "project":
        return {"scope": "project"}
    return {"scope": "user", "home": fake_home, "opencode_config_dir": str(fake_xdg)}


def _plugin_js_path(target: Path, scope: str, xdg: Path) -> Path:
    if scope == "project":
        return target / ".opencode" / "plugins" / "eawf.js"
    return xdg / "plugins" / "eawf.js"


def _sidecar_path(target: Path, scope: str, xdg: Path) -> Path:
    if scope == "project":
        return target / ".opencode" / "plugins" / ".eawf-managed.json"
    return xdg / "plugins" / ".eawf-managed.json"


def _config_path(target: Path, scope: str, xdg: Path) -> Path:
    if scope == "project":
        return target / "opencode.json"
    return xdg / "opencode.json"


def _agent_frontmatter(body: str) -> dict[str, object]:
    marker = "---\n"
    assert body.startswith(marker)
    _, frontmatter, _ = body.split(marker, 2)
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_writes_plugin_js_sidecar_and_config(
    tmp_path: Path, fake_home: Path, fake_opencode_config_dir: Path, scope: str
) -> None:
    result = install_plugin(tmp_path, **_install_kwargs(scope, fake_home, fake_opencode_config_dir))
    js_path = _plugin_js_path(tmp_path, scope, fake_opencode_config_dir)
    sidecar = _sidecar_path(tmp_path, scope, fake_opencode_config_dir)
    config = _config_path(tmp_path, scope, fake_opencode_config_dir)
    assert js_path.is_file()
    assert sidecar.is_file()
    assert config.is_file()
    parsed = json.loads(config.read_text(encoding="utf-8"))
    assert "mcp" in parsed
    assert result.plugin_js is not None
    assert result.plugin_js.action == "created"
    assert result.sidecar is not None
    assert result.scope == scope
    plugin_root = tmp_path / ".opencode" if scope == "project" else fake_opencode_config_dir
    assert not list(plugin_root.rglob("*.lock"))
    assert not config.with_name(f"{config.name}.lock").exists()


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_does_not_patch_plugins_array(
    tmp_path: Path, fake_home: Path, fake_opencode_config_dir: Path, scope: str
) -> None:
    """Regression guard: legacy installer wrote 'plugin.js' into the
    ``plugins:[...]`` array. Native layout relies on auto-discovery; the
    array must stay empty (or untouched if user-authored)."""
    install_plugin(tmp_path, **_install_kwargs(scope, fake_home, fake_opencode_config_dir))
    config = _config_path(tmp_path, scope, fake_opencode_config_dir)
    parsed = json.loads(config.read_text(encoding="utf-8"))
    plugins = parsed.get("plugins")
    assert plugins is None or plugins == [], plugins


def test_install_opencode_does_not_patch_plugins_array_for_user_authored(
    tmp_path: Path,
) -> None:
    """User-authored ``plugins`` array survives unchanged; installer
    never inserts ``plugin.js`` / ``eawf.js`` into it."""
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"plugins": ["other.js"], "mcp": {}}), encoding="utf-8")
    install_plugin(tmp_path)
    parsed = json.loads(cfg.read_text(encoding="utf-8"))
    assert parsed["plugins"] == ["other.js"]


def test_install_opencode_user_scope_writes_into_xdg(
    tmp_path: Path, fake_home: Path, fake_opencode_config_dir: Path
) -> None:
    install_plugin(
        tmp_path,
        scope="user",
        home=fake_home,
        opencode_config_dir=str(fake_opencode_config_dir),
    )
    assert (fake_opencode_config_dir / "plugins" / "eawf.js").is_file()
    assert (fake_opencode_config_dir / "plugins" / ".eawf-managed.json").is_file()
    assert (fake_opencode_config_dir / "opencode.json").is_file()


def test_install_opencode_user_scope_env_var_fallback(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent kwarg + ``$OPENCODE_CONFIG_DIR`` set → XDG override
    routes through the env var."""
    env_dir = tmp_path / "env-xdg"
    env_dir.mkdir()
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(env_dir))
    install_plugin(tmp_path, scope="user", home=fake_home)
    assert (env_dir / "plugins" / "eawf.js").is_file()


def test_install_opencode_user_scope_home_default(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent kwarg + absent env var → ``<home>/.config/opencode/``."""
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    install_plugin(tmp_path, scope="user", home=fake_home)
    assert (fake_home / ".config" / "opencode" / "plugins" / "eawf.js").is_file()


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_idempotent(
    tmp_path: Path, fake_home: Path, fake_opencode_config_dir: Path, scope: str
) -> None:
    install_plugin(tmp_path, **_install_kwargs(scope, fake_home, fake_opencode_config_dir))
    second = install_plugin(tmp_path, **_install_kwargs(scope, fake_home, fake_opencode_config_dir))
    assert second.plugin_js is not None
    assert second.plugin_js.action == "unchanged"
    assert second.sidecar is not None
    assert second.sidecar.action == "unchanged"
    assert second.config is not None
    assert second.config.action == "unchanged"


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_dry_run_writes_nothing(
    tmp_path: Path, fake_home: Path, fake_opencode_config_dir: Path, scope: str
) -> None:
    result = install_plugin(
        tmp_path, dry_run=True, **_install_kwargs(scope, fake_home, fake_opencode_config_dir)
    )
    assert result.dry_run is True
    assert not _plugin_js_path(tmp_path, scope, fake_opencode_config_dir).exists()


def test_install_refuses_hand_edited_plugin_js(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    js_path = _plugin_js_path(tmp_path, "project", tmp_path)
    js_path.write_text("// hand edit\n", encoding="utf-8")
    with pytest.raises(IntegrityViolation):
        install_plugin(tmp_path)


def test_install_refuses_hand_edited_sidecar(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    sidecar = _sidecar_path(tmp_path, "project", tmp_path)
    body = json.loads(sidecar.read_text(encoding="utf-8"))
    body["commands"] = []
    sidecar.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(IntegrityViolation):
        install_plugin(tmp_path)


def test_install_refuses_hand_edited_agent_without_force(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    paths, _ = expected_paths(tmp_path)
    first_agent_region = next(k for k in paths if k.startswith("plugin.opencode.agent."))
    first_agent = paths[first_agent_region]
    first_agent.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(IntegrityViolation):
        install_plugin(tmp_path)


def test_install_refuses_hand_edited_command_without_force(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    paths, _ = expected_paths(tmp_path)
    first_command_region = next(k for k in paths if k.startswith("plugin.opencode.command."))
    first_command = paths[first_command_region]
    first_command.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(IntegrityViolation):
        install_plugin(tmp_path)


def test_install_force_overrides_hand_edit(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    js_path = _plugin_js_path(tmp_path, "project", tmp_path)
    js_path.write_text("// hand edit\n", encoding="utf-8")
    result = install_plugin(tmp_path, force=True)
    assert result.plugin_js is not None
    assert result.plugin_js.action == "updated"


def test_install_preserves_user_top_level_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "opencode.json"
    config_path.write_text(
        json.dumps({"theme": "midnight", "mcp": {"foo": "bar"}}), encoding="utf-8"
    )
    install_plugin(tmp_path)
    parsed = json.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["theme"] == "midnight"
    assert parsed["mcp"]["foo"] == "bar"
    # No managed namespace baked into user's opencode.json — sidecar owns hashes.
    assert "__eawf_managed" not in parsed


def test_install_rejects_non_object_config(tmp_path: Path) -> None:
    (tmp_path / "opencode.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        install_plugin(tmp_path)


@pytest.mark.parametrize("scope", ["project", "user"])
def test_doctor_reports_clean_after_install(
    tmp_path: Path, fake_home: Path, fake_opencode_config_dir: Path, scope: str
) -> None:
    install_plugin(tmp_path, **_install_kwargs(scope, fake_home, fake_opencode_config_dir))
    if scope == "project":
        report = doctor_plugin(tmp_path)
    else:
        report = doctor_plugin(
            tmp_path,
            scope="user",
            home=fake_home,
            opencode_config_dir=str(fake_opencode_config_dir),
        )
    assert report.clean is True, (report.drifted, report.missing)


def test_doctor_flags_missing(tmp_path: Path) -> None:
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert report.missing


def test_doctor_flags_plugin_js_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    js_path = _plugin_js_path(tmp_path, "project", tmp_path)
    js_path.write_text("// tampered\n", encoding="utf-8")
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.kind == "plugin_js" for e in report.drifted)


def test_doctor_flags_sidecar_missing_hash(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    sidecar = _sidecar_path(tmp_path, "project", tmp_path)
    sidecar.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.kind == "sidecar" for e in report.drifted)


def test_doctor_flags_sidecar_registry_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    sidecar = _sidecar_path(tmp_path, "project", tmp_path)
    body = json.loads(sidecar.read_text(encoding="utf-8"))
    body["agents"] = []
    sidecar.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.kind == "sidecar" for e in report.drifted)


def test_doctor_reports_legacy_workspace_root_paths(tmp_path: Path) -> None:
    """Legacy ``<target>/plugin.js`` + opencode.json with __eawf_managed
    are reported as ``legacy_paths``."""
    legacy_js = tmp_path / "plugin.js"
    legacy_js.write_text("// legacy\n", encoding="utf-8")
    legacy_cfg = tmp_path / "opencode.json"
    legacy_cfg.write_text(
        json.dumps({"mcp": {}, "__eawf_managed": {"version": "0.x"}}), encoding="utf-8"
    )
    install_plugin(tmp_path, force=True)
    report = doctor_plugin(tmp_path)
    assert legacy_js in report.legacy_paths


def test_expected_plugin_js_carries_version_stamp() -> None:
    body = expected_plugin_js_bytes().decode("utf-8")
    assert "__EAWF_PLUGIN_VERSION__" not in body
    assert f"version: '{eawf.__version__}'" in body


def test_expected_plugin_js_wires_runtime_and_agent_end_hook() -> None:
    body = expected_plugin_js_bytes().decode("utf-8")
    assert "['hook', 'run', eventType, '--runtime', 'opencode']" in body
    assert "onAgentEnd: (ctx) => dispatchHook('agent_end', ctx)" in body


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_emits_agents_per_registry(
    tmp_path: Path, fake_home: Path, fake_opencode_config_dir: Path, scope: str
) -> None:
    """Each AGENT_REGISTRY entry produces ``<base>/agents/<role>.md``."""
    from eawf.surfaces.render.agents import AGENT_REGISTRY

    result = install_plugin(tmp_path, **_install_kwargs(scope, fake_home, fake_opencode_config_dir))
    base = tmp_path / ".opencode" if scope == "project" else fake_opencode_config_dir
    for spec in AGENT_REGISTRY:
        agent_path = base / "agents" / f"{spec.role}.md"
        assert agent_path.is_file(), agent_path
        body = agent_path.read_text(encoding="utf-8")
        assert body.startswith("---\n")
        assert "mode: subagent" in body
        assert spec.description.splitlines()[0] in body
    assert len(result.agents) == len(AGENT_REGISTRY)


def test_install_emits_agent_permission_and_legacy_tools_acls(tmp_path: Path) -> None:
    """OpenCode agents must carry current and legacy per-agent ACLs."""
    install_plugin(tmp_path)
    executor_md = tmp_path / ".opencode" / "agents" / "executor.md"
    frontmatter = _agent_frontmatter(executor_md.read_text(encoding="utf-8"))
    permission = frontmatter["permission"]
    tools = frontmatter["tools"]
    assert isinstance(permission, dict)
    assert isinstance(tools, dict)
    assert permission["edit"] == "allow"
    assert permission["bash"] == "allow"
    assert permission["websearch"] == "deny"
    assert tools["edit"] is True
    assert tools["write"] is True
    assert tools["websearch"] is False


def test_install_agent_read_permission_keeps_env_denies(tmp_path: Path) -> None:
    """Explicit read allow-list preserves OpenCode's sensitive-file defaults."""
    install_plugin(tmp_path)
    researcher_md = tmp_path / ".opencode" / "agents" / "researcher.md"
    frontmatter = _agent_frontmatter(researcher_md.read_text(encoding="utf-8"))
    permission = frontmatter["permission"]
    assert isinstance(permission, dict)
    read_permission = permission["read"]
    assert read_permission == {
        "*": "allow",
        "*.env": "deny",
        "*.env.*": "deny",
        "*.env.example": "allow",
    }


def test_install_permission_task_gates_subagent_spawn(tmp_path: Path) -> None:
    """Only the operator role can spawn Eä subagents through OpenCode task."""
    install_plugin(tmp_path)
    operator_md = tmp_path / ".opencode" / "agents" / "operator.md"
    executor_md = tmp_path / ".opencode" / "agents" / "executor.md"
    operator_frontmatter = _agent_frontmatter(operator_md.read_text(encoding="utf-8"))
    executor_frontmatter = _agent_frontmatter(executor_md.read_text(encoding="utf-8"))
    operator_permission = operator_frontmatter["permission"]
    executor_permission = executor_frontmatter["permission"]
    assert isinstance(operator_permission, dict)
    assert isinstance(executor_permission, dict)
    operator_task = operator_permission["task"]
    executor_task = executor_permission["task"]
    assert isinstance(operator_task, dict)
    assert isinstance(executor_task, dict)
    assert operator_task["*"] == "deny"
    assert operator_task["executor"] == "allow"
    assert operator_task["auditor"] == "allow"
    assert "operator" not in operator_task
    assert executor_task == {"*": "deny"}


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_emits_commands_for_invocable_skills(
    tmp_path: Path, fake_home: Path, fake_opencode_config_dir: Path, scope: str
) -> None:
    """Each ``user_invocable=True`` SKILL_REGISTRY entry produces
    ``<base>/commands/<name>.md``."""
    from eawf.surfaces.render.skills import SKILL_REGISTRY

    result = install_plugin(tmp_path, **_install_kwargs(scope, fake_home, fake_opencode_config_dir))
    base = tmp_path / ".opencode" if scope == "project" else fake_opencode_config_dir
    invocable = [s for s in SKILL_REGISTRY if s.user_invocable]
    for spec in invocable:
        cmd_path = base / "commands" / f"{spec.skill_name}.md"
        assert cmd_path.is_file(), cmd_path
        body = cmd_path.read_text(encoding="utf-8")
        assert body.startswith("---\n")
        assert f"description: {json.dumps(spec.description, ensure_ascii=False)}" in body
    assert len(result.commands) == len(invocable)


def test_doctor_flags_missing_agent_files(
    tmp_path: Path, fake_home: Path, fake_opencode_config_dir: Path
) -> None:
    install_plugin(tmp_path)
    agent_md = tmp_path / ".opencode" / "agents" / "researcher.md"
    agent_md.unlink()
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.kind == "agent" and "researcher" in str(e.path) for e in report.missing)


def test_doctor_flags_drifted_command_files(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    cmd_md = tmp_path / ".opencode" / "commands" / "research.md"
    cmd_md.write_text("--- tampered ---\n", encoding="utf-8")
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.kind == "command" and "research" in str(e.path) for e in report.drifted)
