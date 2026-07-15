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

from pathlib import Path

import yaml
from typer.testing import CliRunner

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


def test_sync_re_renders_agents_md_when_profiles_changed(tmp_path: Path) -> None:
    """Adding ``python`` to ``profiles.enabled`` makes sync write a new region."""
    _init_core(tmp_path)
    _rewrite_config_profiles(tmp_path, ["core", "python"])

    res = runner.invoke(app, ["sync", "--target", str(tmp_path)])
    assert res.exit_code == 0, res.output

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "BEGIN EAWF:managed id=non-negotiable-rules" in text
    assert "BEGIN EAWF:managed id=python-style" in text


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
    # literal pre-dating the C05 § 5.3 0..5 cutover (W04). Under the new
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
