"""End-to-end CLI tests for ``eawf mcp ...``.

Drives ``add → install → list → update → remove`` through the Typer
dispatcher against a temp ``state.json`` and a temp workspace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()


def _seed_state(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    body = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "subproject_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return state_path


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_path = _seed_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.delenv("EA_LOCK_TIMEOUT", raising=False)
    return state_path


def test_mcp_add_install_list_remove_round_trip(tmp_path: Path, tmp_state: Path) -> None:
    """Full happy path: state.mcp_servers populates, settings.json patches,
    list reflects the change, remove reverts both layers."""
    add = runner.invoke(
        app,
        [
            "--json",
            "mcp",
            "add",
            "demo",
            "--command",
            "/demo",
            "--env-ref",
            "${ENV:DEMO_KEY}",
        ],
    )
    assert add.exit_code == 0, add.output
    add_payload = json.loads(add.output)
    assert add_payload["owner"] == "eawf"
    assert add_payload["status"] == "configured"

    install = runner.invoke(
        app,
        [
            "--no-input",
            "--json",
            "-w",
            str(tmp_path),
            "mcp",
            "install",
            "demo",
        ],
    )
    assert install.exit_code == 0, install.output
    install_payload = json.loads(install.output)
    assert install_payload["status"] == "installed"
    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.exists()
    parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    entry = parsed["mcpServers"]["demo"]
    assert entry["__eawf_owner"] == "eawf"
    assert entry["env"] == {"DEMO_KEY": "${ENV:DEMO_KEY}"}

    listed = runner.invoke(app, ["--json", "mcp", "list"])
    assert listed.exit_code == 0, listed.output
    list_payload = json.loads(listed.output)
    assert list_payload["count"] == 1
    assert list_payload["servers"][0]["id"] == "demo"

    removed = runner.invoke(
        app,
        ["--json", "-w", str(tmp_path), "mcp", "remove", "demo"],
    )
    assert removed.exit_code == 0, removed.output
    final_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "mcpServers" not in final_settings


def test_mcp_update_warns_about_reinstall_required(tmp_path: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["mcp", "add", "demo", "--command", "/demo"])
    runner.invoke(
        app,
        ["--no-input", "-w", str(tmp_path), "mcp", "install", "demo"],
    )
    update = runner.invoke(
        app,
        [
            "--json",
            "mcp",
            "update",
            "demo",
            "--command",
            "/demo-v2",
        ],
    )
    assert update.exit_code == 0, update.output
    payload = json.loads(update.output)
    assert payload["reinstall_required"] is True
    assert payload["command"] == "/demo-v2"

    update_text = runner.invoke(
        app,
        ["mcp", "update", "demo", "--command", "/demo-v3"],
    )
    assert update_text.exit_code == 0, update_text.output
    assert "eawf mcp install demo" in update_text.output


def test_mcp_install_no_input_skips_prompt(tmp_path: Path, tmp_state: Path) -> None:
    """``--no-input`` succeeds with exit 0 even when stdin is closed."""
    runner.invoke(app, ["mcp", "add", "demo", "--command", "/demo"])
    result = runner.invoke(
        app,
        ["--no-input", "-w", str(tmp_path), "mcp", "install", "demo"],
    )
    assert result.exit_code == 0, result.output


def test_mcp_install_without_no_input_user_declined_when_stdin_not_tty(
    tmp_path: Path, tmp_state: Path
) -> None:
    """Without ``--no-input`` and a non-TTY stdin, fail closed (exit 7)."""
    runner.invoke(app, ["mcp", "add", "demo", "--command", "/demo"])
    # CliRunner.invoke pipes a non-TTY stdin by default. The handler
    # must raise UserDeclined rather than silently proceed.
    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "mcp", "install", "demo"],
    )
    assert result.exit_code == 1, result.output  # USER_DECLINED


def test_mcp_install_user_declines_at_prompt(tmp_path: Path, tmp_state: Path) -> None:
    """Answering ``n`` to the prompt raises UserDeclined.

    We can simulate a TTY-like stdin by feeding ``input=`` to
    CliRunner — the handler reads the answer via :func:`input` so
    the prompt path runs even when isatty() returns False, but only
    after we explicitly mark the handler as a TTY context. Since
    that is not portable here, the cleanest test is to monkeypatch
    ``sys.stdin.isatty`` to return True and ``builtins.input`` to
    return ``"n"``.
    """
    import builtins
    import sys

    runner.invoke(app, ["mcp", "add", "demo", "--command", "/demo"])

    real_isatty = sys.stdin.isatty
    sys.stdin.isatty = lambda: True  # type: ignore[method-assign]
    real_input = builtins.input
    builtins.input = lambda _prompt="": "n"  # type: ignore[assignment]
    try:
        result = runner.invoke(
            app,
            ["-w", str(tmp_path), "mcp", "install", "demo"],
        )
    finally:
        sys.stdin.isatty = real_isatty  # type: ignore[method-assign]
        builtins.input = real_input  # type: ignore[assignment]
    assert result.exit_code == 1, result.output


def test_mcp_install_unknown_runtime_invalid_input(tmp_path: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["mcp", "add", "demo", "--command", "/demo"])
    result = runner.invoke(
        app,
        [
            "--no-input",
            "-w",
            str(tmp_path),
            "mcp",
            "install",
            "demo",
            "--runtime",
            "opencode",
        ],
    )
    assert result.exit_code == 1, result.output


def test_mcp_install_missing_id_returns_not_found(tmp_path: Path, tmp_state: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--no-input",
            "-w",
            str(tmp_path),
            "mcp",
            "install",
            "ghost",
        ],
    )
    assert result.exit_code == 1, result.output


def test_mcp_add_malformed_env_ref_invalid_input(tmp_state: Path) -> None:
    result = runner.invoke(
        app,
        ["mcp", "add", "demo", "--command", "/demo", "--env-ref", "BAD"],
    )
    assert result.exit_code == 1, result.output


def test_mcp_remove_missing_id_returns_not_found(tmp_state: Path) -> None:
    result = runner.invoke(app, ["mcp", "remove", "ghost"])
    assert result.exit_code == 1, result.output


def test_mcp_list_owner_filter_user(tmp_path: Path, tmp_state: Path) -> None:
    """``--owner user`` reads the runtime config; missing config emits a note."""
    result = runner.invoke(
        app,
        ["--json", "-w", str(tmp_path), "mcp", "list", "--owner", "user"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 0
    # Note added because the runtime config is absent under tmp_path.
    assert payload["notes"]


def test_mcp_list_owner_filter_invalid(tmp_state: Path) -> None:
    result = runner.invoke(
        app,
        ["mcp", "list", "--owner", "weird"],
    )
    assert result.exit_code == 1, result.output


def test_mcp_remove_keep_runtime_entry_does_not_touch_settings(
    tmp_path: Path, tmp_state: Path
) -> None:
    runner.invoke(app, ["mcp", "add", "demo", "--command", "/demo"])
    runner.invoke(
        app,
        ["--no-input", "-w", str(tmp_path), "mcp", "install", "demo"],
    )
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_before = settings_path.read_bytes()
    result = runner.invoke(
        app,
        [
            "-w",
            str(tmp_path),
            "mcp",
            "remove",
            "demo",
            "--keep-runtime-entry",
        ],
    )
    assert result.exit_code == 0, result.output
    assert settings_path.read_bytes() == settings_before


def test_mcp_add_force_overrides_existing_eawf_entry(tmp_state: Path) -> None:
    runner.invoke(app, ["mcp", "add", "demo", "--command", "/v1"])
    second = runner.invoke(
        app,
        ["--json", "mcp", "add", "demo", "--command", "/v2", "--force"],
    )
    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["command"] == "/v2"


def test_mcp_update_no_changes_returns_invalid_input(tmp_state: Path) -> None:
    runner.invoke(app, ["mcp", "add", "demo", "--command", "/demo"])
    result = runner.invoke(app, ["mcp", "update", "demo"])
    assert result.exit_code == 1, result.output
