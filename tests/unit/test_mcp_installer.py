"""Unit tests for :mod:`eawf.mcp.installer`.

Coverage:

- ``install_runtime_entry`` creates settings.json when absent.
- Pre-existing user entries (no ``__eawf_owner`` marker) are
  byte-equal across the install.
- Pre-existing user-owned entry under the same id raises
  :class:`IntegrityViolation`; ``--force`` overrides.
- ``remove_runtime_entry`` refuses on user-owned id; deletes
  Eä-owned id; leaves user entries.
- ``list_runtime_entries`` returns owner annotation.
- The module source does not access ``os.environ`` for any env-ref
  name (security barrier; mirrors the ``env_ref`` discipline).
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from eawf.mcp import installer
from eawf.mcp.installer import (
    IntegrityViolation,
    install_runtime_entry,
    list_runtime_entries,
    remove_runtime_entry,
)
from eawf.state.enums import McpRisk, McpStatus
from eawf.state.models import McpServer

pytestmark = pytest.mark.unit


def _make_server(
    *,
    server_id: str = "demo",
    command: str = "/usr/local/bin/demo",
    args: list[str] | None = None,
    env_refs: list[str] | None = None,
) -> McpServer:
    return McpServer(
        id=server_id,
        owner="eawf",
        command=command,
        args=args or [],
        env_refs=env_refs or [],
        risk=McpRisk.READ,
        write_capable=False,
        status=McpStatus.CONFIGURED,
        installed_targets=[],
    )


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_runtime_entry_creates_settings_when_absent(tmp_path: Path) -> None:
    server = _make_server(env_refs=["${ENV:DEMO_KEY}"])
    result = install_runtime_entry(
        server=server,
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    assert result.action == "created"
    settings = _read_json(result.target_path)
    assert "mcpServers" in settings
    entry = settings["mcpServers"]["demo"]  # type: ignore[index]
    assert entry["__eawf_owner"] == "eawf"
    assert entry["env"] == {"DEMO_KEY": "${ENV:DEMO_KEY}"}
    assert entry["command"] == "/usr/local/bin/demo"
    assert entry["transport"] == "stdio"


def test_install_runtime_entry_preserves_user_entry_byte_equal(tmp_path: Path) -> None:
    """A user-owned ``mcpServers["serena"]`` survives byte-equal."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    user_serena = {
        "command": "/usr/bin/serena",
        "args": ["--config", "x"],
        "env": {"SERENA_KEY": "literal-user-value"},
        "transport": "stdio",
    }
    settings_path.write_text(
        json.dumps({"mcpServers": {"serena": user_serena}}, indent=2) + "\n",
        encoding="utf-8",
    )
    server = _make_server(server_id="other")
    result = install_runtime_entry(
        server=server,
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    assert "serena" in result.user_entries_preserved
    parsed = _read_json(settings_path)
    assert parsed["mcpServers"]["serena"] == user_serena  # type: ignore[index]
    # The user entry is byte-stable inside the rendered JSON: no
    # ``__eawf_owner`` injection, no key reordering inside the entry.
    assert "__eawf_owner" not in parsed["mcpServers"]["serena"]  # type: ignore[index]


def test_install_runtime_entry_refuses_user_owned_collision_without_force(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {"mcpServers": {"dup": {"command": "/usr/bin/dup", "args": []}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    server = _make_server(server_id="dup")
    with pytest.raises(IntegrityViolation):
        install_runtime_entry(
            server=server,
            runtime="claude",
            target_dir=tmp_path,
            force=False,
        )


def test_install_runtime_entry_force_overrides_user_owned_collision(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {"mcpServers": {"dup": {"command": "/usr/bin/dup", "args": []}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    server = _make_server(server_id="dup", command="/eawf/replacement")
    result = install_runtime_entry(
        server=server,
        runtime="claude",
        target_dir=tmp_path,
        force=True,
    )
    assert result.action == "updated"
    parsed = _read_json(settings_path)
    entry = parsed["mcpServers"]["dup"]  # type: ignore[index]
    assert entry["__eawf_owner"] == "eawf"
    assert entry["command"] == "/eawf/replacement"


def test_install_runtime_entry_overwrites_eawf_owned_silently(tmp_path: Path) -> None:
    """Re-installing the same Eä-owned entry just rewrites it."""
    server = _make_server(command="/v1")
    install_runtime_entry(
        server=server,
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    server_v2 = _make_server(command="/v2")
    result = install_runtime_entry(
        server=server_v2,
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    assert result.action == "updated"
    parsed = _read_json(tmp_path / ".claude" / "settings.json")
    assert parsed["mcpServers"]["demo"]["command"] == "/v2"  # type: ignore[index]


def test_remove_runtime_entry_refuses_user_owned(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"mcpServers": {"manual": {"command": "/manual"}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(IntegrityViolation):
        remove_runtime_entry(
            server_id="manual",
            runtime="claude",
            target_dir=tmp_path,
            force=False,
        )


def test_remove_runtime_entry_deletes_only_eawf_owned(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    user_block = {"command": "/manual", "args": []}
    settings_path.write_text(
        json.dumps({"mcpServers": {"manual": user_block}}, indent=2) + "\n",
        encoding="utf-8",
    )
    install_runtime_entry(
        server=_make_server(server_id="ours", command="/ours"),
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    parsed_before = _read_json(settings_path)
    user_block_before = parsed_before["mcpServers"]["manual"]  # type: ignore[index]
    result = remove_runtime_entry(
        server_id="ours",
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    assert result.action == "removed"
    parsed_after = _read_json(settings_path)
    assert "ours" not in parsed_after["mcpServers"]  # type: ignore[operator]
    assert parsed_after["mcpServers"]["manual"] == user_block_before  # type: ignore[index]


def test_remove_runtime_entry_drops_empty_mcp_servers_key(tmp_path: Path) -> None:
    install_runtime_entry(
        server=_make_server(),
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    remove_runtime_entry(
        server_id="demo",
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    parsed = _read_json(tmp_path / ".claude" / "settings.json")
    assert "mcpServers" not in parsed


def test_remove_runtime_entry_absent_id_is_no_op(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({}, indent=2) + "\n", encoding="utf-8")
    result = remove_runtime_entry(
        server_id="missing",
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    assert result.action == "absent"


def test_list_runtime_entries_returns_owner_annotation(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "manual": {"command": "/manual"},
                    "ours": {
                        "command": "/ours",
                        "__eawf_owner": "eawf",
                        "__eawf_managed_at": "1970-01-01T00:00:00+00:00",
                        "args": [],
                        "env": {},
                        "transport": "stdio",
                    },
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    rows = list_runtime_entries(runtime="claude", target_dir=tmp_path)
    by_id = {r.id: r for r in rows}
    assert by_id["manual"].owner == "user"
    assert by_id["ours"].owner == "eawf"


def test_list_runtime_entries_missing_settings_returns_empty(tmp_path: Path) -> None:
    rows = list_runtime_entries(runtime="claude", target_dir=tmp_path)
    assert rows == []


def test_install_runtime_entry_unknown_runtime_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        install_runtime_entry(
            server=_make_server(),
            runtime="opencode",
            target_dir=tmp_path,
            force=False,
        )


def test_install_runtime_entry_malformed_settings_json_raises(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        install_runtime_entry(
            server=_make_server(),
            runtime="claude",
            target_dir=tmp_path,
            force=False,
        )


def test_installer_module_does_not_read_os_environ_for_env_refs() -> None:
    """``installer.py`` MUST NOT call ``os.environ.__getitem__`` or ``.get``.

    The env-ref tokens are literal strings on the wire. Reading
    ``os.environ`` from the installer would defeat the entire
    secret-never-on-disk discipline.
    """
    source_path = inspect.getsourcefile(installer)
    assert source_path is not None
    with open(source_path, encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=source_path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert not (node.value.id == "os" and node.attr == "environ"), (
                f"{source_path} accesses os.environ; the installer must "
                "never read the ambient environment for env-ref names"
            )
        if isinstance(node, ast.Name):
            assert node.id != "environ", (
                f"{source_path} references `environ`; the installer must "
                "stay on the literal-token side of the env barrier"
            )
