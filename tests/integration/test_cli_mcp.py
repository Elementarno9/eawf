"""End-to-end CLI tests for ``eawf mcp ...`` on an epoch-2 tree.

Drives ``add → install → list → update → remove`` through the Typer
dispatcher, with the writers reaching the daemon's native ``mcp.*`` verbs
in process and every server landing as a ``capability`` row of the
selected generation's document.
"""

from __future__ import annotations

import builtins
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.store.compaction import read_document
from eawf.runtime.mcp.book import generation_document, read_servers
from eawf.surfaces.cli.app import app
from tests.integration._memory_native import QR_DOCUMENT, native_memory_tree

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield from native_memory_tree(tmp_path, monkeypatch)


def _json(*argv: str) -> dict[str, Any]:
    result = runner.invoke(app, ["--json", *argv])
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.output)
    return payload


def _add(server_id: str = "demo", *extra: str, key: str | None = None) -> dict[str, Any]:
    return _json(
        "mcp",
        "add",
        server_id,
        "--command",
        "demo-mcp",
        "--idempotency-key",
        key or f"add-{server_id}",
        *extra,
    )


def _install(workspace: Path, server_id: str, revision: int, *extra: str) -> Any:
    return runner.invoke(
        app,
        [
            "--json",
            "--no-input",
            "-w",
            str(workspace),
            "mcp",
            "install",
            server_id,
            "--expected-revision",
            str(revision),
            "--idempotency-key",
            f"install-{server_id}-{revision}",
            *extra,
        ],
    )


def _rows(state_path: Path) -> dict[str, Any]:
    document = generation_document(state_path.parent)
    assert document is not None, "the tree answers in epoch 2"
    return dict(read_servers(read_document(document)))


def test_mcp_add_install_list_remove_round_trip(tmp_path: Path, tmp_state: Path) -> None:
    """The registry row, the runtime config and the listing move together."""
    frozen = tmp_state.read_bytes()
    added = _add("demo", "--env-ref", "${ENV:DEMO_KEY}")
    assert added["owner"] == "eawf"
    assert added["status"] == "configured"
    assert added["revision"] == 1

    install = _install(tmp_path, "demo", 1)
    assert install.exit_code == 0, install.output
    install_payload = json.loads(install.output)
    assert install_payload["status"] == "installed"
    assert install_payload["revision"] == 2
    parsed = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    entry = parsed["mcpServers"]["demo"]
    assert entry["__eawf_owner"] == "eawf"
    assert entry["env"] == {"DEMO_KEY": "${ENV:DEMO_KEY}"}

    listed = _json("mcp", "list")
    assert listed["count"] == 1
    assert listed["servers"][0]["id"] == "demo"
    assert listed["servers"][0]["revision"] == 2
    assert listed["servers"][0]["installed_targets"] == ["claude"]

    removed = _json(
        "-w",
        str(tmp_path),
        "mcp",
        "remove",
        "demo",
        "--expected-revision",
        "2",
        "--idempotency-key",
        "remove-demo",
    )
    assert removed["revision"] == 3
    final_settings = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert "mcpServers" not in final_settings
    assert _json("mcp", "list")["count"] == 0
    # The row stays under its id, removed; the frozen document never moves.
    assert _rows(tmp_state)["demo"].server is None
    assert _rows(tmp_state)["demo"].revision == 3
    assert tmp_state.read_bytes() == frozen


def test_mcp_add_replays_its_answer_under_the_same_key(tmp_state: Path) -> None:
    first = _add("demo", key="same")
    second = _add("demo", key="same")
    assert second == first
    assert _rows(tmp_state)["demo"].revision == 1


def test_mcp_add_rejects_collision_without_force(tmp_state: Path) -> None:
    _add("demo")
    again = runner.invoke(
        app, ["mcp", "add", "demo", "--command", "other", "--idempotency-key", "add-again"]
    )
    assert again.exit_code == 1, again.output
    assert "mcp_exists" in again.output


def test_mcp_add_force_redefines_at_the_next_revision(tmp_state: Path) -> None:
    _add("demo")
    second = _json(
        "mcp", "add", "demo", "--command", "v2-mcp", "--force", "--idempotency-key", "add-v2"
    )
    assert second["command"] == "v2-mcp"
    assert second["revision"] == 2


def test_mcp_add_after_remove_continues_the_revision(tmp_path: Path, tmp_state: Path) -> None:
    """A re-added id never reuses a revision an old anchor still names."""
    _add("demo")
    _json(
        "-w",
        str(tmp_path),
        "mcp",
        "remove",
        "demo",
        "--expected-revision",
        "1",
        "--idempotency-key",
        "rm",
    )
    again = _add("demo", key="add-again")
    assert again["revision"] == 3


def test_mcp_update_warns_about_reinstall_required(tmp_path: Path, tmp_state: Path) -> None:
    _add("demo")
    assert _install(tmp_path, "demo", 1).exit_code == 0
    payload = _json(
        "mcp",
        "update",
        "demo",
        "--command",
        "demo-mcp-v2",
        "--expected-revision",
        "2",
        "--idempotency-key",
        "u1",
    )
    assert payload["reinstall_required"] is True
    assert payload["command"] == "demo-mcp-v2"
    assert payload["revision"] == 3

    update_text = runner.invoke(
        app,
        [
            "mcp",
            "update",
            "demo",
            "--command",
            "demo-mcp-v3",
            "--expected-revision",
            "3",
            "--idempotency-key",
            "u2",
        ],
    )
    assert update_text.exit_code == 0, update_text.output
    assert "eawf mcp install demo" in update_text.output


def test_mcp_update_at_a_stale_revision_is_refused(tmp_state: Path) -> None:
    _add("demo")
    stale = runner.invoke(
        app,
        [
            "mcp",
            "update",
            "demo",
            "--command",
            "x",
            "--expected-revision",
            "7",
            "--idempotency-key",
            "stale",
        ],
    )
    assert stale.exit_code != 0, stale.output
    assert "revision_conflict" in stale.output
    assert _rows(tmp_state)["demo"].revision == 1


def test_mcp_install_at_a_stale_revision_touches_no_runtime_config(
    tmp_path: Path, tmp_state: Path
) -> None:
    _add("demo")
    result = _install(tmp_path, "demo", 5)
    assert result.exit_code != 0, result.output
    assert not (tmp_path / ".mcp.json").exists()


def test_mcp_update_no_changes_returns_invalid_input(tmp_state: Path) -> None:
    _add("demo")
    result = runner.invoke(
        app, ["mcp", "update", "demo", "--expected-revision", "1", "--idempotency-key", "u"]
    )
    assert result.exit_code == 1, result.output
    assert "mcp_update_empty" in result.output


def test_mcp_install_without_no_input_user_declined_when_stdin_not_tty(
    tmp_path: Path, tmp_state: Path
) -> None:
    """Without ``--no-input`` and a non-TTY stdin, fail closed."""
    _add("demo")
    result = runner.invoke(
        app,
        [
            "-w",
            str(tmp_path),
            "mcp",
            "install",
            "demo",
            "--expected-revision",
            "1",
            "--idempotency-key",
            "i",
        ],
    )
    assert result.exit_code == 1, result.output
    assert _rows(tmp_state)["demo"].revision == 1


def test_mcp_install_user_declines_at_prompt(
    tmp_path: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add("demo")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "n")
    result = runner.invoke(
        app,
        [
            "-w",
            str(tmp_path),
            "mcp",
            "install",
            "demo",
            "--expected-revision",
            "1",
            "--idempotency-key",
            "i",
        ],
    )
    assert result.exit_code == 1, result.output


def test_mcp_install_unknown_runtime_invalid_input(tmp_path: Path, tmp_state: Path) -> None:
    _add("demo")
    result = _install(tmp_path, "demo", 1, "--runtime", "goose")
    assert result.exit_code == 1, result.output


def test_mcp_install_missing_id_returns_not_found(tmp_path: Path, tmp_state: Path) -> None:
    result = _install(tmp_path, "ghost", 1)
    assert result.exit_code == 1, result.output
    assert "not registered" in result.output


def test_mcp_add_malformed_env_ref_invalid_input(tmp_state: Path) -> None:
    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "demo",
            "--command",
            "demo-mcp",
            "--env-ref",
            "BAD",
            "--idempotency-key",
            "k",
        ],
    )
    assert result.exit_code == 1, result.output


def test_mcp_remove_missing_id_returns_not_found(tmp_state: Path) -> None:
    result = runner.invoke(
        app, ["mcp", "remove", "ghost", "--expected-revision", "1", "--idempotency-key", "k"]
    )
    assert result.exit_code == 1, result.output


def test_mcp_list_owner_filter_user(tmp_path: Path, tmp_state: Path) -> None:
    """``--owner user`` reads the runtime config; missing config emits a note."""
    payload = _json("-w", str(tmp_path), "mcp", "list", "--owner", "user")
    assert payload["count"] == 0
    assert payload["notes"]


def test_mcp_list_owner_filter_invalid(tmp_state: Path) -> None:
    result = runner.invoke(app, ["mcp", "list", "--owner", "weird"])
    assert result.exit_code == 1, result.output


def test_mcp_remove_keep_runtime_entry_does_not_touch_settings(
    tmp_path: Path, tmp_state: Path
) -> None:
    _add("demo")
    assert _install(tmp_path, "demo", 1).exit_code == 0
    settings_path = tmp_path / ".mcp.json"
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
            "--expected-revision",
            "2",
            "--idempotency-key",
            "rm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert settings_path.read_bytes() == settings_before


def test_mcp_writers_require_their_anchors(tmp_state: Path) -> None:
    """A write without its anchor is refused by the parser, before any call."""
    no_key = runner.invoke(app, ["mcp", "add", "demo", "--command", "c"])
    assert no_key.exit_code == 2, no_key.output
    no_revision = runner.invoke(app, ["mcp", "remove", "demo", "--idempotency-key", "k"])
    assert no_revision.exit_code == 2, no_revision.output


def test_mcp_writes_on_an_epoch1_tree_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: an epoch-1 registry is frozen until the cutover imports it."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(QR_DOCUMENT), encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    before = state_path.read_bytes()
    result = runner.invoke(
        app,
        [
            "--no-input",
            "-w",
            str(tmp_path),
            "mcp",
            "install",
            "demo",
            "--expected-revision",
            "1",
            "--idempotency-key",
            "k",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "eawf migrate" in result.output
    assert state_path.read_bytes() == before
