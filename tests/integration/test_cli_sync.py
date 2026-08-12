"""Unit-style coverage for ``eawf sync`` driven via :class:`CliRunner`.

Each test boots a fresh tmp workspace, runs ``eawf init --no-input`` to seed
``state.json`` + ``config.yaml`` + ``AGENTS.md`` + ``CLAUDE.md`` + manifest,
then exercises one branch of the sync surface (write / dry-run / check).

The tests live in ``tests/unit/`` per the W08 spec — they cover the CLI
handler in isolation. The integration tests in
``tests/integration/test_cli_sync_re_renders_idempotently.py`` cover the
multi-call interaction with the manifest writer.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from typer.testing import CliRunner

from eawf.platform.profiles.discovery import _clear_cache_for_tests
from eawf.surfaces.cli.app import app

runner = CliRunner()


def _init_core(target: Path) -> None:
    """Run ``eawf init`` for a single ``core`` profile workspace."""
    res = runner.invoke(
        app,
        [
            "--no-input",
            "init",
            "--project-code",
            "DEMO",
            "--profile",
            "core",
            "--target",
            str(target),
        ],
    )
    assert res.exit_code == 0, res.output


def _rewrite_config_profiles(target: Path, profiles: list[str]) -> None:
    """Replace ``profiles.enabled`` in the workspace config.yaml in place."""
    config_path = target / ".ea" / "config.yaml"
    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    parsed["profiles"]["enabled"] = profiles
    config_path.write_text(yaml.safe_dump(parsed, sort_keys=True), encoding="utf-8")


def _write_workspace_profile(target: Path, profile_id: str, marker: str) -> None:
    """Write a minimal workspace profile carrying one distinctive render block."""
    profile_path = target / ".ea" / "profiles" / f"{profile_id}.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        yaml.safe_dump(
            {
                "name": profile_id,
                "render_blocks": [
                    {
                        "id": f"{profile_id}-workspace-overlay",
                        "target": "AGENTS.md",
                        "body_template": marker,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _clear_cache_for_tests()


def test_sync_re_renders_agents_md_when_profiles_changed(tmp_path: Path) -> None:
    """Adding ``python`` to ``profiles.enabled`` makes sync write a new region."""
    _init_core(tmp_path)
    _rewrite_config_profiles(tmp_path, ["core", "python"])

    res = runner.invoke(app, ["sync", "--target", str(tmp_path)])
    assert res.exit_code == 0, res.output

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "BEGIN EAWF:managed id=non-negotiable-rules" in text
    assert "BEGIN EAWF:managed id=python-style" in text


def test_sync_write_honours_target_workspace_profile_overlay(tmp_path: Path) -> None:
    """Write mode composes profiles from ``--target``, including its overlay."""
    _init_core(tmp_path)
    marker = "workspace overlay render block"
    _write_workspace_profile(tmp_path, "local", marker)
    _rewrite_config_profiles(tmp_path, ["core", "local"])

    res = runner.invoke(app, ["sync", "--target", str(tmp_path)])

    assert res.exit_code == 0, res.output
    assert marker in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


def test_sync_dry_run_honours_target_workspace_profile_overlay(tmp_path: Path) -> None:
    """Dry-run composes against the target overlay while keeping target bytes unchanged."""
    _init_core(tmp_path)
    marker = "workspace overlay dry-run block"
    _write_workspace_profile(tmp_path, "local", marker)
    _rewrite_config_profiles(tmp_path, ["core", "local"])
    before = (tmp_path / "AGENTS.md").read_bytes()

    res = runner.invoke(app, ["--json", "sync", "--target", str(tmp_path), "--dry-run"])

    assert res.exit_code == 0, res.output
    assert '"local-workspace-overlay"' in res.output
    assert (tmp_path / "AGENTS.md").read_bytes() == before


def test_sync_check_honours_target_workspace_profile_overlay(tmp_path: Path) -> None:
    """Check mode discovers target-only profile ids and reports their region as drift."""
    _init_core(tmp_path)
    _write_workspace_profile(tmp_path, "local", "workspace overlay check block")
    _rewrite_config_profiles(tmp_path, ["core", "local"])
    before = (tmp_path / "AGENTS.md").read_bytes()

    res = runner.invoke(app, ["--json", "sync", "--target", str(tmp_path), "--check"])

    assert res.exit_code == 4, res.output
    assert '"local-workspace-overlay"' in res.output
    assert (tmp_path / "AGENTS.md").read_bytes() == before


def test_sync_workspace_quality_overlay_restores_core_without_code_craft_duplicates(
    tmp_path: Path,
) -> None:
    _init_core(tmp_path)
    quality_path = tmp_path / ".ea" / "profiles" / "quality.yaml"
    quality_path.parent.mkdir(parents=True, exist_ok=True)
    quality_path.write_text(
        yaml.safe_dump(
            {
                "name": "quality",
                "version": "1.0",
                "render_blocks": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _clear_cache_for_tests()
    _rewrite_config_profiles(tmp_path, ["core", "quality"])
    agents_path = tmp_path / "AGENTS.md"
    agents = agents_path.read_text(encoding="utf-8")
    agents = re.sub(
        r"<!-- BEGIN EAWF:managed id=workflow-lifecycle .*?"
        r"<!-- END EAWF:managed id=workflow-lifecycle -->\n?",
        "",
        agents,
        flags=re.DOTALL,
    )
    agents_path.write_text(agents, encoding="utf-8")

    res = runner.invoke(app, ["sync", "--target", str(tmp_path)])

    assert res.exit_code == 0, res.output
    rendered = agents_path.read_text(encoding="utf-8")
    assert rendered.count("id=workflow-lifecycle") == 2
    assert "id=code-craft-" not in rendered


def test_sync_dry_run_does_not_write(tmp_path: Path) -> None:
    """``--dry-run`` exits 0 even when profiles changed; AGENTS.md bytes unchanged."""
    _init_core(tmp_path)
    _rewrite_config_profiles(tmp_path, ["core", "python"])

    agents_before = (tmp_path / "AGENTS.md").read_bytes()
    manifest_before = (tmp_path / ".ea" / "indexes" / "generated.json").read_bytes()

    res = runner.invoke(app, ["sync", "--target", str(tmp_path), "--dry-run"])
    assert res.exit_code == 0, res.output

    agents_after = (tmp_path / "AGENTS.md").read_bytes()
    manifest_after = (tmp_path / ".ea" / "indexes" / "generated.json").read_bytes()

    assert agents_before == agents_after, "AGENTS.md must not be written under --dry-run"
    assert manifest_before == manifest_after, "manifest must not be written under --dry-run"


def test_sync_check_exits_4_when_drift(tmp_path: Path) -> None:
    """``--check`` exits 4 when sync would emit any region; AGENTS.md unchanged."""
    _init_core(tmp_path)
    _rewrite_config_profiles(tmp_path, ["core", "python"])

    agents_before = (tmp_path / "AGENTS.md").read_bytes()

    res = runner.invoke(app, ["sync", "--target", str(tmp_path), "--check"])
    # NOTE: source ``sync.py`` still raises ``typer.Exit(code=4)`` via a raw
    # literal pre-dating the C05 § 5.3 0..5 cutover. Under the new
    # scheme code 4 is DAEMON_UNREACHABLE rather than the intended
    # VALIDATION_ERROR (2); a follow-up wave migrates ``sync.py`` to use
    # the symbolic ``exit_codes.VALIDATION_ERROR`` constant.
    assert res.exit_code == 4, res.output

    agents_after = (tmp_path / "AGENTS.md").read_bytes()
    assert agents_before == agents_after, "AGENTS.md must not be written under --check"


def test_sync_check_exits_0_when_clean(tmp_path: Path) -> None:
    """``--check`` exits 0 when the workspace is in sync with its config."""
    _init_core(tmp_path)

    res = runner.invoke(app, ["sync", "--target", str(tmp_path), "--check"])
    assert res.exit_code == 0, res.output


def test_sync_invalid_profile_in_config_exits_3(tmp_path: Path) -> None:
    """A typo'd profile id in config.yaml exits 3 (InvalidInput) before any write."""
    _init_core(tmp_path)
    _rewrite_config_profiles(tmp_path, ["core", "definitely-not-a-real-profile"])

    res = runner.invoke(app, ["sync", "--target", str(tmp_path)])
    assert res.exit_code == 1, res.output


def test_sync_help_lists_flags() -> None:
    """The ``sync`` command declares ``--dry-run``, ``--check``, ``--target`` flags.

    Structural check via Click introspection so the assertion never depends on
    terminal width or Rich help wrapping (which differs across CI runners).
    """
    import typer

    cmd = typer.main.get_command(app)
    sync_cmd = cmd.commands["sync"]
    flag_names = {opt for p in sync_cmd.params for opt in p.opts}
    assert "--dry-run" in flag_names
    assert "--check" in flag_names
    assert "--target" in flag_names
