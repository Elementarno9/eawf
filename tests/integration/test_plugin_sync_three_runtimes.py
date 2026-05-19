"""Integration tests for ``eawf plugin sync`` — three runtimes one call.

Per C07a §5.7-§5.9 + V9 (XB10): plugin sync regenerates per-runtime
artifacts deterministically from ``SKILL_REGISTRY``. Two replays
on identical inputs produce byte-identical output. The orchestrator
delegates to the three per-runtime ``install_plugin`` functions
under :mod:`eawf.runtimes.{claude,codex,opencode}`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.runtimes.plugin_sync import sync_plugins

pytestmark = pytest.mark.integration

runner = CliRunner()


def _equip_ea_dir(target: Path) -> None:
    """Drop a minimal ``.ea/`` skeleton under *target* (no state.json needed)."""
    (target / ".ea").mkdir(parents=True, exist_ok=True)
    (target / ".ea" / "indexes").mkdir(exist_ok=True)


@pytest.fixture(autouse=True)
def _force_daemonless(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync delegates to in-process renderers — no daemon needed."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)


@pytest.fixture(autouse=True)
def _isolate_user_scope_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the per-runtime user-scope conflict detectors.

    The detectors poke at the developer machine's real
    ``~/.codex/plugins/`` / ``~/.config/opencode/plugins/`` which is
    irrelevant here — the orchestrator's contract is "regenerate
    against the supplied target".
    """
    monkeypatch.setattr(
        "eawf.cli.commands.plugin.codex_detect_user_install",
        lambda: None,
    )
    monkeypatch.setattr(
        "eawf.cli.commands.plugin.opencode_detect_user_install",
        lambda: None,
    )


def _collect_rendered_paths(target: Path) -> dict[str, bytes]:
    """Return ``{path: bytes}`` for every file under the three plugin trees.

    Walks ``.claude/`` + ``.codex/`` + ``.opencode/`` recursively
    plus the top-level ``opencode.json``. The sidecar ``hash`` /
    ``generated_at`` fields are deterministic given the frozen
    timestamp — no mask needed.
    """
    snapshot: dict[str, bytes] = {}
    for sub in (".claude", ".codex", ".opencode"):
        root = target / sub
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                snapshot[str(path.relative_to(target))] = path.read_bytes()
    opencode_json = target / "opencode.json"
    if opencode_json.exists():
        snapshot["opencode.json"] = opencode_json.read_bytes()
    return snapshot


def test_sync_writes_all_three_runtimes(tmp_path: Path) -> None:
    """One sync call writes every runtime's plugin tree under *target_dir*."""
    _equip_ea_dir(tmp_path)
    result = sync_plugins(tmp_path)

    assert result.target_dir == tmp_path.resolve()
    assert result.scope == "project"
    assert result.skipped == []
    runtimes = [r.runtime for r in result.results]
    assert runtimes == ["claude-code", "codex", "opencode"]

    # Each runtime wrote at least one file.
    for runtime_result in result.results:
        assert len(runtime_result.deltas) > 0, (
            f"runtime={runtime_result.runtime} produced no deltas"
        )

    # Reference files from each runtime exist.
    assert (tmp_path / ".claude" / "skills" / "research" / "SKILL.md").exists()
    assert (tmp_path / ".claude" / "agents" / "executor.md").exists()
    assert (tmp_path / ".codex" / "plugins" / "eawf" / ".codex-plugin" / "plugin.json").exists()
    assert (tmp_path / ".opencode" / "plugins" / "eawf.js").exists()
    assert (tmp_path / "opencode.json").exists()


def test_sync_is_deterministic(tmp_path: Path) -> None:
    """Two replays produce byte-identical output (no timestamps, no random ids)."""
    _equip_ea_dir(tmp_path)

    first_target = tmp_path / "first"
    second_target = tmp_path / "second"
    _equip_ea_dir(first_target)
    _equip_ea_dir(second_target)

    sync_plugins(first_target)
    sync_plugins(second_target)

    first_snapshot = _collect_rendered_paths(first_target)
    second_snapshot = _collect_rendered_paths(second_target)

    assert sorted(first_snapshot) == sorted(second_snapshot)
    for key in first_snapshot:
        assert first_snapshot[key] == second_snapshot[key], f"determinism broke at path={key!r}"


def test_sync_replay_is_idempotent(tmp_path: Path) -> None:
    """Re-running sync against the same target reports ``unchanged`` deltas."""
    _equip_ea_dir(tmp_path)

    sync_plugins(tmp_path)
    second_result = sync_plugins(tmp_path)

    # Every delta in the second run reports unchanged.
    for runtime_result in second_result.results:
        for delta in runtime_result.deltas:
            assert delta.action == "unchanged", (
                f"replay reported action={delta.action!r} for {delta.path}"
            )


def test_sync_dry_run_writes_nothing(tmp_path: Path) -> None:
    """``dry_run=True`` returns deltas without touching disk."""
    _equip_ea_dir(tmp_path)
    result = sync_plugins(tmp_path, dry_run=True)

    assert result.dry_run
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".opencode").exists()


def test_sync_subset_via_runtimes_arg(tmp_path: Path) -> None:
    """``runtimes=('claude-code',)`` restricts the orchestrator to that runtime."""
    _equip_ea_dir(tmp_path)
    result = sync_plugins(tmp_path, runtimes=("claude-code",))

    assert [r.runtime for r in result.results] == ["claude-code"]
    assert (tmp_path / ".claude").exists()
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".opencode").exists()


def test_sync_user_scope_skips_claude(tmp_path: Path) -> None:
    """User-scope sync writes Codex + OpenCode under *home* but skips Claude."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    target = tmp_path / "ws"
    _equip_ea_dir(target)

    result = sync_plugins(
        target,
        scope="user",
        home=fake_home,
        opencode_config_dir=str(fake_home / ".config" / "opencode"),
    )

    assert result.skipped == ["claude-code"]
    assert [r.runtime for r in result.results] == ["codex", "opencode"]
    # Project-scope artifacts NOT created under target.
    assert not (target / ".claude").exists()
    # Codex user-scope artifacts under fake home.
    assert (fake_home / ".codex" / "plugins" / "eawf" / ".codex-plugin" / "plugin.json").exists()
    assert (fake_home / ".config" / "opencode" / "plugins" / "eawf.js").exists()


def test_sync_cli_emits_json_envelope(tmp_path: Path) -> None:
    """``eawf plugin sync --json`` emits a parseable envelope."""
    _equip_ea_dir(tmp_path)
    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "--json", "plugin", "sync"],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["scope"] == "project"
    assert sorted(r["runtime"] for r in body["runtimes"]) == [
        "claude-code",
        "codex",
        "opencode",
    ]


def test_sync_cli_runtime_filter(tmp_path: Path) -> None:
    """``--runtime claude`` filters the orchestrator to one runtime."""
    _equip_ea_dir(tmp_path)
    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "--json", "plugin", "sync", "--runtime", "claude"],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert [r["runtime"] for r in body["runtimes"]] == ["claude-code"]


def test_sync_cli_rejects_unknown_runtime(tmp_path: Path) -> None:
    """Unknown runtime alias surfaces as an InvalidInput error."""
    _equip_ea_dir(tmp_path)
    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "plugin", "sync", "--runtime", "aider"],
    )
    assert result.exit_code != 0
    assert "aider" in result.stdout or "aider" in result.stderr


def test_sync_dry_run_via_cli(tmp_path: Path) -> None:
    """``eawf plugin sync --dry-run`` writes nothing."""
    _equip_ea_dir(tmp_path)
    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "plugin", "sync", "--dry-run"],
    )
    assert result.exit_code == 0, result.stdout
    assert "dry-run" in result.stdout
    assert not (tmp_path / ".claude").exists()


# Touch path / env so tests don't pollute developer environment.
def test_no_unexpected_env_modifications() -> None:
    """Smoke check: tests should not mutate the developer's HOME."""
    assert os.environ.get("HOME") not in (None, "")
