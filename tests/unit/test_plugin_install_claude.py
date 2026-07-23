"""Unit tests for ``eawf.runtime.runtimes.claude.plugin_install``.

Covers:

- Happy path: install renders skills, agents, hooks, and settings.json.
- Idempotence: re-running yields byte-identical files (acceptance §2).
- Dry-run: no bytes written.
- ``--force``: hand-edited files are clobbered without raising.
- Hand-edit detection: install raises :class:`IntegrityViolation` when
  a managed file has drifted from the recorded hash.
- Hook scripts get the executable bit (``0o755``).
- ``__eawf_managed`` namespace is the only key Eä writes to
  ``settings.json``; user keys round-trip verbatim.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

import eawf
from eawf.runtime.runtimes.claude.plugin_install import (
    InstallResult,
    IntegrityViolation,
    install_plugin,
)
from eawf.surfaces.render.agents import AGENT_REGISTRY
from eawf.surfaces.render.hooks import HOOK_REGISTRY
from eawf.surfaces.render.skills import SKILL_REGISTRY


def _all_skill_paths(target_dir: Path) -> list[Path]:
    return [target_dir / ".claude" / "skills" / s.skill_name / "SKILL.md" for s in SKILL_REGISTRY]


def _all_agent_paths(target_dir: Path) -> list[Path]:
    return [target_dir / ".claude" / "agents" / f"{s.role}.md" for s in AGENT_REGISTRY]


def _installed_hook_specs() -> list:
    """Only handler-backed events (SESSION_END) are installed; the rest would
    be idle no-op wrappers, so the installer no longer writes them to disk."""
    return [spec for spec in HOOK_REGISTRY if spec.has_handler]


def _all_hook_paths(target_dir: Path) -> list[Path]:
    return [
        target_dir / ".claude" / "hooks" / f"{spec.event_type.value}.sh"
        for spec in _installed_hook_specs()
    ]


def test_install_plugin_writes_full_tree(tmp_path: Path) -> None:
    """Happy path: every registry entry produces a file on disk."""
    result = install_plugin(tmp_path)
    assert isinstance(result, InstallResult)
    assert len(result.skills) == len(SKILL_REGISTRY)
    assert len(result.agents) == len(AGENT_REGISTRY)
    assert len(result.hooks) == len(_installed_hook_specs())
    assert result.settings is not None and result.settings.action == "created"
    assert result.mcp_config is not None and result.mcp_config.action == "created"
    # Every file exists on disk.
    for p in _all_skill_paths(tmp_path):
        assert p.exists(), f"missing skill: {p}"
    for p in _all_agent_paths(tmp_path):
        assert p.exists(), f"missing agent: {p}"
    for p in _all_hook_paths(tmp_path):
        assert p.exists(), f"missing hook: {p}"
    settings = tmp_path / ".claude" / "settings.json"
    assert settings.exists()
    mcp_config = tmp_path / ".mcp.json"
    assert json.loads(mcp_config.read_text(encoding="utf-8")) == {"mcpServers": {}}
    assert not list((tmp_path / ".claude").rglob("*.lock"))
    assert not mcp_config.with_name(f"{mcp_config.name}.lock").exists()


def test_install_plugin_is_idempotent(tmp_path: Path) -> None:
    """Re-running install on a clean tree → byte-identical output, all unchanged."""
    install_plugin(tmp_path)
    # Snapshot every owned file's bytes.
    paths = _all_skill_paths(tmp_path) + _all_agent_paths(tmp_path) + _all_hook_paths(tmp_path)
    paths.append(tmp_path / ".claude" / "settings.json")
    paths.append(tmp_path / ".mcp.json")
    first_snapshot = {p: p.read_bytes() for p in paths}

    second = install_plugin(tmp_path)
    for p, expected in first_snapshot.items():
        assert p.read_bytes() == expected, f"file changed across runs: {p}"
    # All deltas report 'unchanged' on the second run.
    for delta in second.skills + second.agents + second.hooks:
        assert delta.action == "unchanged", f"unexpected action: {delta}"
    assert second.settings is not None and second.settings.action == "unchanged"
    assert second.mcp_config is not None and second.mcp_config.action == "unchanged"


def test_install_plugin_dry_run_writes_nothing(tmp_path: Path) -> None:
    """Dry-run reports actions but never touches disk."""
    result = install_plugin(tmp_path, dry_run=True)
    assert result.dry_run is True
    assert not (tmp_path / ".claude").exists(), "dry-run still wrote .claude tree"
    # Action labels reflect what *would* happen against the empty tree.
    for delta in result.skills + result.agents + result.hooks:
        assert delta.action == "created"
    assert result.mcp_config is not None and result.mcp_config.action == "created"


def test_install_plugin_aborts_on_hand_edit(tmp_path: Path) -> None:
    """Hand-editing a managed file makes the next install raise."""
    install_plugin(tmp_path)
    skill_path = tmp_path / ".claude" / "skills" / "research" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n# hand-edit\n")
    with pytest.raises(IntegrityViolation):
        install_plugin(tmp_path)


def test_install_plugin_force_clobbers_hand_edit(tmp_path: Path) -> None:
    """``force=True`` overrides the integrity check and rewrites the file."""
    install_plugin(tmp_path)
    skill_path = tmp_path / ".claude" / "skills" / "research" / "SKILL.md"
    pristine = skill_path.read_bytes()
    skill_path.write_text(pristine.decode("utf-8") + "\n# hand-edit\n")
    install_plugin(tmp_path, force=True)
    assert skill_path.read_bytes() == pristine


def test_install_plugin_settings_managed_namespace_only(tmp_path: Path) -> None:
    """Eä writes only the ``__eawf_managed`` key; user keys are preserved."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    user_payload = {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "permissions": {"allow": ["Bash(uv *)"]},
        "additionalDirectories": ["~/projects/example"],
    }
    settings_path.write_text(json.dumps(user_payload, indent=2) + "\n", encoding="utf-8")
    install_plugin(tmp_path)
    parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    assert parsed["$schema"] == user_payload["$schema"]
    assert parsed["permissions"] == user_payload["permissions"]
    assert parsed["additionalDirectories"] == user_payload["additionalDirectories"]
    assert "__eawf_managed" in parsed
    managed = parsed["__eawf_managed"]
    assert managed["version"] == eawf.__version__
    assert {s["name"] for s in managed["skills"]} == {s.skill_name for s in SKILL_REGISTRY}
    assert {a["name"] for a in managed["agents"]} == {a.role for a in AGENT_REGISTRY}
    assert "hash" in managed and len(managed["hash"]) == 16


def test_managed_block_version_tracks_package_version(tmp_path: Path) -> None:
    """The ``__eawf_managed`` block version derives from ``eawf.__version__`` so
    the manifest advances on every release rather than pinning a stale ``1.0``
    literal that never re-triggers an upgrade."""
    install_plugin(tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    managed = json.loads(settings_path.read_text(encoding="utf-8"))["__eawf_managed"]
    assert managed["version"] == eawf.__version__
    assert managed["version"] != "1.0" or eawf.__version__ == "1.0"


def test_install_plugin_hooks_are_executable(tmp_path: Path) -> None:
    """Generated hook scripts must have the executable bit set on POSIX hosts."""
    install_plugin(tmp_path)
    for path in _all_hook_paths(tmp_path):
        mode = path.stat().st_mode
        assert mode & stat.S_IXUSR, f"hook script {path} missing user execute bit"
        assert mode & stat.S_IRUSR, f"hook script {path} missing user read bit"


def test_install_plugin_persists_manifest(tmp_path: Path) -> None:
    """``.ea/indexes/generated.json`` is updated with every owned file."""
    install_plugin(tmp_path)
    manifest_path = tmp_path / ".ea" / "indexes" / "generated.json"
    assert manifest_path.exists()
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert body["version"] == 1
    region_ids = {entry["region_id"] for entry in body["generated"].values()}
    assert "plugin.claude.skill.research" in region_ids
    assert "plugin.claude.agent.researcher" in region_ids
    # Only the handler-backed hook is tracked; the idle no-op events are gone.
    assert "plugin.claude.hook.session_end" in region_ids
    assert "plugin.claude.hook.pre_commit" not in region_ids
    assert "plugin.claude.settings" in region_ids


def test_install_plugin_rejects_invalid_settings_json(tmp_path: Path) -> None:
    """A non-JSON settings.json surfaces as :class:`ValueError`."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("not valid json\n", encoding="utf-8")
    with pytest.raises(ValueError):
        install_plugin(tmp_path)


def test_install_plugin_rejects_non_object_settings(tmp_path: Path) -> None:
    """A settings.json whose top-level body is not an object is rejected."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        install_plugin(tmp_path)


def test_install_plugin_renders_skill_md_with_correct_frontmatter(tmp_path: Path) -> None:
    """Spot check: the rendered SKILL.md contains the registry's description."""
    install_plugin(tmp_path)
    body = (tmp_path / ".claude" / "skills" / "research" / "SKILL.md").read_text(encoding="utf-8")
    spec = next(s for s in SKILL_REGISTRY if s.skill_name == "research")
    assert f"description: {json.dumps(spec.description, ensure_ascii=False)}\n" in body


def test_install_plugin_renders_hook_with_correct_event(tmp_path: Path) -> None:
    """Spot check: the rendered (handler-backed) hook script targets the right event."""
    install_plugin(tmp_path)
    session_end = (tmp_path / ".claude" / "hooks" / "session_end.sh").read_text(encoding="utf-8")
    assert "eawf hook run session_end" in session_end
    assert "--runtime claude" in session_end


def test_install_plugin_omits_handler_less_hooks(tmp_path: Path) -> None:
    """Only SESSION_END is installed; every handler-less event is absent on disk."""
    install_plugin(tmp_path)
    hooks_dir = tmp_path / ".claude" / "hooks"
    on_disk = {p.name for p in hooks_dir.iterdir()}
    assert on_disk == {"session_end.sh"}
    for event in (
        "pre_commit",
        "post_commit",
        "pre_push",
        "post_push",
        "session_start",
        "subagent_stop",
        "pre_compact",
        "agent_end",
    ):
        assert not (hooks_dir / f"{event}.sh").exists(), f"idle hook wired: {event}"


def test_install_plugin_returns_paths_under_target_dir(tmp_path: Path) -> None:
    """Every :class:`FileDelta` path is inside *target_dir*."""
    result = install_plugin(tmp_path)
    target_resolved = tmp_path.resolve()
    for delta in result.skills + result.agents + result.hooks:
        assert str(delta.path).startswith(str(target_resolved))
    assert result.settings is not None
    assert str(result.settings.path).startswith(str(target_resolved))
    assert result.mcp_config is not None
    assert str(result.mcp_config.path).startswith(str(target_resolved))


def test_install_plugin_settings_re_run_byte_identical(tmp_path: Path) -> None:
    """Acceptance §2: re-run produces byte-identical settings.json hash."""
    install_plugin(tmp_path)
    snapshot = (tmp_path / ".claude" / "settings.json").read_bytes()
    install_plugin(tmp_path)
    rerun = (tmp_path / ".claude" / "settings.json").read_bytes()
    assert snapshot == rerun


def test_install_plugin_preserves_existing_mcp_config(tmp_path: Path) -> None:
    """Existing MCP entries are owned by the MCP installer/user, not plugin install."""
    mcp_config = tmp_path / ".mcp.json"
    payload = {
        "mcpServers": {
            "eawf-mcp": {
                "command": "eawf-mcp",
                "args": [],
            }
        }
    }
    mcp_config.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    result = install_plugin(tmp_path)
    assert result.mcp_config is not None
    assert result.mcp_config.action == "unchanged"
    assert json.loads(mcp_config.read_text(encoding="utf-8")) == payload


def test_install_plugin_creates_target_dir(tmp_path: Path) -> None:
    """Missing ``.claude`` directory is created on demand."""
    target = tmp_path / "nested" / "workspace"
    install_plugin(target)
    assert (target / ".claude" / "skills").is_dir()
    assert (target / ".claude" / "agents").is_dir()
    assert (target / ".claude" / "hooks").is_dir()


def test_install_plugin_carries_through_unrelated_manifest_entries(tmp_path: Path) -> None:
    """Manifest entries for paths Eä does not own are preserved."""
    manifest_path = tmp_path / ".ea" / "indexes" / "generated.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    pre_existing = {
        "version": 1,
        "generated": {
            "AGENTS.md::rules": {
                "target": "AGENTS.md",
                "region_id": "rules",
                "version": "1.0",
                "hash": "0123456789abcdef",
                "generator": "profile:python",
                "generated_at": "2026-01-01T00:00:00+00:00",
            }
        },
    }
    manifest_path.write_text(json.dumps(pre_existing, indent=2), encoding="utf-8")
    install_plugin(tmp_path)
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "AGENTS.md::rules" in body["generated"]


def test_install_plugin_uses_default_timestamp_for_idempotence(tmp_path: Path) -> None:
    """The default timestamp is fixed so two installs yield identical bytes."""
    result_a = install_plugin(tmp_path / "a")
    result_b = install_plugin(tmp_path / "b")
    assert result_a.settings is not None and result_b.settings is not None
    settings_a = (tmp_path / "a" / ".claude" / "settings.json").read_text(encoding="utf-8")
    settings_b = (tmp_path / "b" / ".claude" / "settings.json").read_text(encoding="utf-8")
    # Strip the path-dependent settings-file content via the parsed body
    # (the file itself only differs in spacing if both targets render
    # the same managed body, which they should).
    body_a = json.loads(settings_a)
    body_b = json.loads(settings_b)
    assert body_a["__eawf_managed"] == body_b["__eawf_managed"]


def test_install_plugin_skips_manifest_persist_when_disabled(tmp_path: Path) -> None:
    """``persist_manifest=False`` is honoured (used by the integration test fixture)."""
    install_plugin(tmp_path, persist_manifest=False)
    assert not (tmp_path / ".ea" / "indexes" / "generated.json").exists()


def test_install_plugin_force_after_settings_drift_succeeds(tmp_path: Path) -> None:
    """Force re-render rewrites settings.json even if a managed file drifted."""
    install_plugin(tmp_path)
    skill_path = tmp_path / ".claude" / "skills" / "research" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n# drift\n")
    # Force overrides the integrity check.
    install_plugin(tmp_path, force=True)
    # Drift cleared.
    assert "# drift" not in skill_path.read_text(encoding="utf-8")


def test_install_plugin_returns_dry_run_flag(tmp_path: Path) -> None:
    """The result carries ``dry_run`` so the CLI can label the message."""
    real = install_plugin(tmp_path)
    dry = install_plugin(tmp_path / "dry", dry_run=True)
    assert real.dry_run is False
    assert dry.dry_run is True


def test_install_plugin_preserves_unrelated_skill_files(tmp_path: Path) -> None:
    """A user-added file under ``.claude/skills/<name>/`` not owned by Eä is left alone."""
    install_plugin(tmp_path)
    extra = tmp_path / ".claude" / "skills" / "research" / "extra.md"
    extra.write_text("user-added\n", encoding="utf-8")
    install_plugin(tmp_path)  # idempotent re-run
    assert extra.read_text(encoding="utf-8") == "user-added\n"


def test_install_wires_session_end_into_cc_stop_hook(tmp_path: Path) -> None:
    """The CC Stop hook is wired so session_end -> daemon runtime.capture fires.

    Without a real ``hooks`` entry CC never invokes the rendered
    ``.claude/hooks/session_end.sh`` wrapper, so the runtime.capture chain
    never runs and EU capture stays dark (the W34 root cause).
    """
    install_plugin(tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hooks = settings.get("hooks", {})
    assert "Stop" in hooks, "CC Stop hook must be wired for EU capture"
    commands = [h["command"] for entry in hooks["Stop"] for h in entry.get("hooks", [])]
    assert any(c.endswith("session_end.sh") for c in commands)
    # Project-relative via the CC-provided project-root variable.
    assert all("$CLAUDE_PROJECT_DIR/.claude/hooks/" in c for c in commands)


def test_install_settings_hooks_preserve_user_entries_and_stay_idempotent(tmp_path: Path) -> None:
    """A user / other-plugin hook entry survives; Eä's entries do not duplicate."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    user_hook = {"matcher": "", "hooks": [{"type": "command", "command": "my-own-hook.sh"}]}
    settings_path.write_text(
        json.dumps({"hooks": {"Stop": [user_hook]}}, indent=2) + "\n", encoding="utf-8"
    )

    install_plugin(tmp_path)
    first = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["Stop"]
    assert user_hook in first, "a user hook entry must be preserved"
    assert any("session_end.sh" in h["command"] for e in first for h in e["hooks"])

    install_plugin(tmp_path)  # idempotent: no growth
    second = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["Stop"]
    assert second == first
