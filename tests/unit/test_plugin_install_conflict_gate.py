"""Unit tests for the ``install claude`` conflict gate.

Covers the integration between :func:`detect_marketplace_install` and the
``eawf plugin install claude`` Typer command: detection short-circuits the
install with an actionable error under ``--no-input``, an interactive
prompt otherwise, and a clean override path via ``--force``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.runtimes.claude.plugin_conflict import CCPluginConflict

runner = CliRunner()


@pytest.fixture
def fake_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CCPluginConflict:
    """Force ``detect_marketplace_install`` to return a synthetic conflict."""
    fake_dir = tmp_path / "fake-cc-plugin"
    fake_dir.mkdir()
    conflict = CCPluginConflict(plugin_dir=fake_dir)
    monkeypatch.setattr(
        "eawf.cli.commands.plugin.detect_marketplace_install",
        lambda: conflict,
    )
    return conflict


@pytest.fixture
def no_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "eawf.cli.commands.plugin.detect_marketplace_install",
        lambda: None,
    )


@pytest.fixture(autouse=True)
def _isolate_codex_opencode_user_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force codex + opencode user-scope conflict detectors to find nothing.

    Otherwise tests on a developer machine that already has eawf installed
    under ``~/.codex/plugins/`` or ``~/.config/opencode/plugins/`` would
    trip the project-scope clash gate. Tests that need a synthetic
    user-scope conflict patch these detectors back to a stub in the
    body.
    """
    monkeypatch.setattr(
        "eawf.cli.commands.plugin.codex_detect_user_install",
        lambda: None,
    )
    monkeypatch.setattr(
        "eawf.cli.commands.plugin.opencode_detect_user_install",
        lambda: None,
    )


def test_install_claude_no_conflict_proceeds(tmp_path: Path, no_conflict: None) -> None:
    """Detector returns None → install runs through."""
    res = runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "plugin install" in res.output


def test_install_claude_with_conflict_force_bypasses(
    tmp_path: Path, fake_conflict: CCPluginConflict
) -> None:
    """``--force`` overrides the conflict gate."""
    res = runner.invoke(
        app, ["-w", str(tmp_path), "plugin", "install", "claude", "--dry-run", "--force"]
    )
    assert res.exit_code == 0, res.output
    assert "plugin install" in res.output


def test_install_claude_no_input_blocks_on_conflict(
    tmp_path: Path, fake_conflict: CCPluginConflict
) -> None:
    """``--no-input`` mode refuses the install with an actionable message."""
    res = runner.invoke(
        app,
        ["-w", str(tmp_path), "--no-input", "plugin", "install", "claude", "--dry-run"],
    )
    assert res.exit_code != 0
    combined = res.output + (res.stderr or "")
    assert "marketplace plugin" in combined or "marketplace" in combined.lower()
    assert "--force" in combined


def test_install_claude_interactive_prompt_decline_aborts(
    tmp_path: Path,
    fake_conflict: CCPluginConflict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User answers 'no' at the questionary prompt → clean abort, no install."""

    class _StubPrompt:
        def ask(self) -> bool:
            return False

    monkeypatch.setattr("questionary.confirm", lambda *a, **k: _StubPrompt())
    res = runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude", "--dry-run"])
    assert res.exit_code == 0
    assert "aborted" in res.output.lower()


def test_install_claude_interactive_prompt_accept_proceeds(
    tmp_path: Path,
    fake_conflict: CCPluginConflict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User answers 'yes' → install runs through."""

    class _StubPrompt:
        def ask(self) -> bool:
            return True

    monkeypatch.setattr("questionary.confirm", lambda *a, **k: _StubPrompt())
    res = runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "claude", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "plugin install" in res.output


def test_install_codex_skips_conflict_gate(tmp_path: Path, fake_conflict: CCPluginConflict) -> None:
    """Conflict gate is Claude-only; codex install ignores it."""
    res = runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "codex", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "plugin install codex" in res.output


def test_install_opencode_skips_conflict_gate(
    tmp_path: Path, fake_conflict: CCPluginConflict
) -> None:
    """Same — opencode ignores the claude gate."""
    res = runner.invoke(app, ["-w", str(tmp_path), "plugin", "install", "opencode", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "plugin install opencode" in res.output


def test_install_claude_user_scope_rejected(tmp_path: Path, no_conflict: None) -> None:
    """``--scope user`` is rejected for claude (CC marketplace owns user scope)."""
    res = runner.invoke(
        app,
        ["-w", str(tmp_path), "plugin", "install", "claude", "--scope", "user"],
    )
    assert res.exit_code == 3, res.output
    combined = res.output + (res.stderr or "")
    assert "project-scope only" in combined or "marketplace" in combined.lower()


def test_install_codex_user_scope_conflict_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project-scope codex install hits the user-scope clash detector."""
    fake_dir = tmp_path / "fake-user-codex"
    fake_dir.mkdir()
    from eawf.runtimes.codex.plugin_conflict import CodexUserPluginConflict

    monkeypatch.setattr(
        "eawf.cli.commands.plugin.codex_detect_user_install",
        lambda: CodexUserPluginConflict(plugin_dir=fake_dir),
    )
    res = runner.invoke(
        app,
        [
            "-w",
            str(tmp_path),
            "--no-input",
            "plugin",
            "install",
            "codex",
            "--dry-run",
        ],
    )
    assert res.exit_code != 0
    combined = res.output + (res.stderr or "")
    assert "user-scope" in combined.lower()


def test_install_codex_user_scope_force_bypasses_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dir = tmp_path / "fake-user-codex"
    fake_dir.mkdir()
    from eawf.runtimes.codex.plugin_conflict import CodexUserPluginConflict

    monkeypatch.setattr(
        "eawf.cli.commands.plugin.codex_detect_user_install",
        lambda: CodexUserPluginConflict(plugin_dir=fake_dir),
    )
    res = runner.invoke(
        app,
        [
            "-w",
            str(tmp_path),
            "plugin",
            "install",
            "codex",
            "--dry-run",
            "--force",
        ],
    )
    assert res.exit_code == 0, res.output


def test_install_opencode_user_scope_conflict_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project-scope opencode install hits the user-scope clash detector."""
    fake_file = tmp_path / "fake-user-opencode-eawf.js"
    fake_file.write_text("// fake\n", encoding="utf-8")
    from eawf.runtimes.opencode.plugin_conflict import OpenCodeUserPluginConflict

    monkeypatch.setattr(
        "eawf.cli.commands.plugin.opencode_detect_user_install",
        lambda: OpenCodeUserPluginConflict(plugin_file=fake_file),
    )
    res = runner.invoke(
        app,
        [
            "-w",
            str(tmp_path),
            "--no-input",
            "plugin",
            "install",
            "opencode",
            "--dry-run",
        ],
    )
    assert res.exit_code != 0
    combined = res.output + (res.stderr or "")
    assert "user-scope" in combined.lower()


def test_install_codex_user_scope_skips_clash_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The codex clash gate fires only on project-scope installs."""
    fake_dir = tmp_path / "fake-user-codex"
    fake_dir.mkdir()
    from eawf.runtimes.codex.plugin_conflict import CodexUserPluginConflict

    monkeypatch.setattr(
        "eawf.cli.commands.plugin.codex_detect_user_install",
        lambda: CodexUserPluginConflict(plugin_dir=fake_dir),
    )
    res = runner.invoke(
        app,
        [
            "-w",
            str(tmp_path),
            "--no-input",
            "plugin",
            "install",
            "codex",
            "--scope",
            "user",
            "--dry-run",
        ],
    )
    assert res.exit_code == 0, res.output
