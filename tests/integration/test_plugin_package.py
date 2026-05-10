"""Integration tests for ``eawf plugin package claude``.

Acceptance scenarios from Phase 6 W05: full tree shape, idempotent
re-render, refusal of foreign non-empty targets, dry-run write
suppression, marketplace/readme gating.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.cli.exit_codes import INTEGRITY_VIOLATION, INVALID_INPUT
from eawf.runtimes.claude.plugin_install import IntegrityViolation
from eawf.runtimes.claude.plugin_package import package_plugin

pytestmark = pytest.mark.integration

runner = CliRunner()


def _tree_digest(root: Path) -> str:
    """Return a stable digest over (relpath, content) for every regular file."""
    sha = hashlib.sha256()
    paths = sorted(root.rglob("*"))
    for path in paths:
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        sha.update(rel.encode("utf-8"))
        sha.update(b"\x00")
        sha.update(path.read_bytes())
        sha.update(b"\x01")
    return sha.hexdigest()


def test_package_emits_full_tree(tmp_path: Path) -> None:
    """Full tree contains plugin/marketplace manifests, all skills, all agents."""
    target = tmp_path / "eawf-plugin"
    package_plugin(target, include_marketplace=True, include_readme=True)
    manifest = json.loads((target / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "eawf"
    assert manifest["license"] == "MIT"
    assert manifest["skills"] == "./skills"
    assert manifest["agents"] == "./agents"
    # PII guards on the manifest body.
    serialised = json.dumps(manifest)
    assert "@" not in serialised
    assert "/Users/" not in serialised
    marketplace = json.loads((target / ".claude-plugin" / "marketplace.json").read_text())
    assert marketplace["name"] == "eawf-local"
    assert marketplace["plugins"][0]["source"] == "./"
    assert (target / "skills" / "research" / "SKILL.md").exists()
    assert (target / "skills" / "flow" / "SKILL.md").exists()
    assert len(list((target / "skills").iterdir())) == 10
    assert (target / "agents" / "auditor.md").exists()
    assert len(list((target / "agents").iterdir())) == 8
    # Forbidden surfaces.
    assert not (target / "hooks").exists()
    assert not (target / "settings.json").exists()
    assert not (target / ".claude").exists()
    assert not (target / ".ea").exists()
    # README emitted by default.
    assert (target / "README.md").exists()


def test_package_idempotent(tmp_path: Path) -> None:
    """Two consecutive renders produce a byte-identical tree."""
    target = tmp_path / "eawf-plugin"
    package_plugin(target)
    digest1 = _tree_digest(target)
    package_plugin(target, force=True)
    digest2 = _tree_digest(target)
    assert digest1 == digest2


def test_package_refuses_nonempty_target_without_force(tmp_path: Path) -> None:
    """Non-empty target that is not an eawf plugin output → IntegrityViolation."""
    target = tmp_path / "eawf-plugin"
    target.mkdir()
    (target / "stranger.txt").write_text("hi", encoding="utf-8")
    with pytest.raises(IntegrityViolation):
        package_plugin(target, force=False)


def test_package_allows_repackage_of_own_output(tmp_path: Path) -> None:
    """Re-package into a directory holding an eawf plugin output succeeds."""
    target = tmp_path / "eawf-plugin"
    package_plugin(target)
    # Should NOT raise — own previous output is identifiable via plugin.json name.
    package_plugin(target, force=False)


def test_package_dry_run_writes_nothing(tmp_path: Path) -> None:
    """Dry-run resolves the plan but creates no directories or files."""
    target = tmp_path / "eawf-plugin"
    result = package_plugin(target, dry_run=True)
    assert not target.exists()
    assert result.dry_run is True
    assert len(result.skills) == 10
    assert len(result.agents) == 8


def test_package_skips_marketplace_when_disabled(tmp_path: Path) -> None:
    """``include_marketplace=False`` omits ``marketplace.json``."""
    target = tmp_path / "eawf-plugin"
    package_plugin(target, include_marketplace=False)
    assert (target / ".claude-plugin" / "plugin.json").exists()
    assert not (target / ".claude-plugin" / "marketplace.json").exists()


def test_package_skips_readme_when_disabled(tmp_path: Path) -> None:
    """``include_readme=False`` omits ``README.md``."""
    target = tmp_path / "eawf-plugin"
    package_plugin(target, include_readme=False)
    assert not (target / "README.md").exists()
    # Manifest still emitted regardless.
    assert (target / ".claude-plugin" / "plugin.json").exists()


def test_package_force_overrides_foreign_target(tmp_path: Path) -> None:
    """``force=True`` allows writing into a non-empty foreign tree."""
    target = tmp_path / "eawf-plugin"
    target.mkdir()
    (target / "stranger.txt").write_text("hi", encoding="utf-8")
    package_plugin(target, force=True)
    assert (target / ".claude-plugin" / "plugin.json").exists()
    # Foreign file is preserved (we never delete arbitrary content).
    assert (target / "stranger.txt").exists()


# --------------------------------------------------------------------------- #
# CLI surface coverage                                                        #
# --------------------------------------------------------------------------- #


def test_package_cli_default_target(tmp_path: Path) -> None:
    """``eawf plugin package claude`` uses ``<workspace>/build/eawf-plugin/`` by default."""
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "package", "claude"])
    assert result.exit_code == 0, result.stdout
    expected = tmp_path / "build" / "eawf-plugin"
    assert (expected / ".claude-plugin" / "plugin.json").exists()
    assert (expected / "skills" / "research" / "SKILL.md").exists()
    assert (expected / "agents" / "auditor.md").exists()


def test_package_cli_custom_target_dry_run(tmp_path: Path) -> None:
    """``--target`` + ``--dry-run`` writes nothing but exits 0."""
    target = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "plugin",
            "package",
            "claude",
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "dry-run" in result.stdout
    assert not target.exists()


def test_package_cli_unknown_runtime_exits_invalid_input(tmp_path: Path) -> None:
    """Unsupported runtime maps to exit 3."""
    result = runner.invoke(
        app,
        [
            "plugin",
            "package",
            "opencode",
            "--target",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == INVALID_INPUT, result.stdout


def test_package_cli_refuses_nonempty_target(tmp_path: Path) -> None:
    """CLI maps IntegrityViolation to exit 8."""
    target = tmp_path / "out"
    target.mkdir()
    (target / "stranger.txt").write_text("hi", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "plugin",
            "package",
            "claude",
            "--target",
            str(target),
        ],
    )
    assert result.exit_code == INTEGRITY_VIOLATION, result.stdout


def test_package_cli_json_output(tmp_path: Path) -> None:
    """``--json`` emits the canonical envelope shape."""
    target = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "--json",
            "plugin",
            "package",
            "claude",
            "--target",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    assert payload["wrote_marketplace"] is True
    assert payload["wrote_readme"] is True
    assert len(payload["skills"]) == 10
    assert len(payload["agents"]) == 8
    assert payload["target"].endswith("out")


def test_package_cli_no_marketplace_no_readme(tmp_path: Path) -> None:
    """``--no-marketplace --no-readme`` still emits the plugin manifest."""
    target = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "plugin",
            "package",
            "claude",
            "--target",
            str(target),
            "--no-marketplace",
            "--no-readme",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert (target / ".claude-plugin" / "plugin.json").exists()
    assert not (target / ".claude-plugin" / "marketplace.json").exists()
    assert not (target / "README.md").exists()
