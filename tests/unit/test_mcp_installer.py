"""Unit tests for :mod:`eawf.runtime.mcp.installer`.

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
import tomllib
from pathlib import Path

import pytest

from eawf.kernel.state.enums import McpRisk, McpStatus
from eawf.kernel.state.models import McpServer
from eawf.runtime.mcp import installer
from eawf.runtime.mcp.installer import (
    IntegrityViolation,
    VerifyFailure,
    install_runtime_entry,
    list_runtime_entries,
    remove_runtime_entry,
)

pytestmark = pytest.mark.unit


def _make_server(
    *,
    server_id: str = "demo",
    command: str = "demo-mcp",
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
    assert entry["command"] == "demo-mcp"
    assert entry["transport"] == "stdio"


def test_install_runtime_entry_preserves_user_entry_byte_equal(tmp_path: Path) -> None:
    """A user-owned ``mcpServers["serena"]`` survives byte-equal."""
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    user_serena = {
        "command": "serena-mcp",
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
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {"mcpServers": {"dup": {"command": "dup-mcp", "args": []}}},
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
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {"mcpServers": {"dup": {"command": "dup-mcp", "args": []}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    server = _make_server(server_id="dup", command="eawf-replacement")
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
    assert entry["command"] == "eawf-replacement"


def test_install_runtime_entry_overwrites_eawf_owned_silently(tmp_path: Path) -> None:
    """Re-installing the same Eä-owned entry just rewrites it."""
    server = _make_server(command="v1-mcp")
    install_runtime_entry(
        server=server,
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    server_v2 = _make_server(command="v2-mcp")
    result = install_runtime_entry(
        server=server_v2,
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    assert result.action == "updated"
    parsed = _read_json(tmp_path / ".mcp.json")
    assert parsed["mcpServers"]["demo"]["command"] == "v2-mcp"  # type: ignore[index]


def test_remove_runtime_entry_refuses_user_owned(tmp_path: Path) -> None:
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"mcpServers": {"manual": {"command": "manual-mcp"}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(IntegrityViolation):
        remove_runtime_entry(
            server_id="manual",
            runtime="claude",
            target_dir=tmp_path,
            force=False,
        )


def test_remove_runtime_entry_force_overrides_user_owner(tmp_path: Path) -> None:
    """``force=True`` deletes a user-owned entry the IntegrityViolation would normally protect."""
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    user_block = {"command": "manual-mcp", "args": ["--keep"]}
    settings_path.write_text(
        json.dumps({"mcpServers": {"manual": user_block}}, indent=2) + "\n",
        encoding="utf-8",
    )
    result = remove_runtime_entry(
        server_id="manual",
        runtime="claude",
        target_dir=tmp_path,
        force=True,
    )
    assert result.action == "removed"
    parsed = _read_json(settings_path)
    # ``mcpServers`` is dropped when the only entry is removed.
    assert "mcpServers" not in parsed
    assert result.user_entries_preserved == []


def test_remove_runtime_entry_deletes_only_eawf_owned(tmp_path: Path) -> None:
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    user_block = {"command": "manual-mcp", "args": []}
    settings_path.write_text(
        json.dumps({"mcpServers": {"manual": user_block}}, indent=2) + "\n",
        encoding="utf-8",
    )
    install_runtime_entry(
        server=_make_server(server_id="ours", command="ours-mcp"),
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
    parsed = _read_json(tmp_path / ".mcp.json")
    assert "mcpServers" not in parsed


def test_remove_runtime_entry_absent_id_is_no_op(tmp_path: Path) -> None:
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({}, indent=2) + "\n", encoding="utf-8")
    result = remove_runtime_entry(
        server_id="missing",
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    assert result.action == "absent"


def test_list_runtime_entries_returns_owner_annotation(tmp_path: Path) -> None:
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "manual": {"command": "manual-mcp"},
                    "ours": {
                        "command": "ours-mcp",
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


def test_install_opencode_entry_creates_json_when_absent(tmp_path: Path) -> None:
    server = _make_server(args=["--flag"], env_refs=["${ENV:DEMO_KEY}"])
    result = install_runtime_entry(
        server=server,
        runtime="opencode",
        target_dir=tmp_path,
        force=False,
    )
    assert result.action == "created"
    assert result.target_path == tmp_path / "opencode.json"
    parsed = _read_json(result.target_path)
    entry = parsed["mcp"]["demo"]  # type: ignore[index]
    assert entry["command"] == "demo-mcp"
    assert entry["args"] == ["--flag"]
    assert entry["env"] == {"DEMO_KEY": "${ENV:DEMO_KEY}"}
    assert entry["__eawf_owner"] == "eawf"


def test_remove_opencode_entry_drops_empty_mcp_key(tmp_path: Path) -> None:
    install_runtime_entry(
        server=_make_server(),
        runtime="opencode",
        target_dir=tmp_path,
        force=False,
    )
    remove_runtime_entry(
        server_id="demo",
        runtime="opencode",
        target_dir=tmp_path,
        force=False,
    )
    parsed = _read_json(tmp_path / "opencode.json")
    assert "mcp" not in parsed


def test_install_runtime_entry_unknown_runtime_raises(tmp_path: Path) -> None:
    # ``opencode`` and ``codex`` landed in P14-W06/W07; use a still-
    # deferred id (``goose``) to exercise the rejection path.
    with pytest.raises(ValueError):
        install_runtime_entry(
            server=_make_server(),
            runtime="goose",
            target_dir=tmp_path,
            force=False,
        )


def test_install_runtime_entry_malformed_settings_json_raises(tmp_path: Path) -> None:
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        install_runtime_entry(
            server=_make_server(),
            runtime="claude",
            target_dir=tmp_path,
            force=False,
        )


def _read_toml(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_install_codex_entry_creates_toml_when_absent(tmp_path: Path) -> None:
    server = _make_server(args=["--flag"], env_refs=["${ENV:DEMO_KEY}"])
    result = install_runtime_entry(
        server=server,
        runtime="codex",
        target_dir=tmp_path,
        force=False,
    )
    assert result.action == "created"
    assert result.target_path == tmp_path / ".codex" / "config.toml"
    parsed = _read_toml(result.target_path)
    table = parsed["mcp_servers"]["demo"]  # type: ignore[index]
    assert table["command"] == "demo-mcp"
    assert table["args"] == ["--flag"]
    assert table["env"] == {"DEMO_KEY": "${ENV:DEMO_KEY}"}
    assert table["__eawf_owner"] == "eawf"


def test_install_codex_preserves_user_table_and_plugin_block(tmp_path: Path) -> None:
    """A user ``mcp_servers`` table and the plugin marker block survive."""
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "# user header\n"
        '[mcp_servers."user-keep"]\n'
        'command = "keep-mcp"\n\n'
        "# ---- __eawf_managed begin ----\n"
        "[plugins.eawf]\n"
        "enabled = true\n"
        "# ---- __eawf_managed end ----\n",
        encoding="utf-8",
    )
    result = install_runtime_entry(
        server=_make_server(server_id="ours", command="ours-mcp"),
        runtime="codex",
        target_dir=tmp_path,
        force=False,
    )
    assert result.user_entries_preserved == ["user-keep"]
    parsed = _read_toml(config)
    assert parsed["mcp_servers"]["user-keep"] == {"command": "keep-mcp"}  # type: ignore[index]
    assert parsed["plugins"] == {"eawf": {"enabled": True}}  # type: ignore[index]
    assert parsed["mcp_servers"]["ours"]["__eawf_owner"] == "eawf"  # type: ignore[index]


def test_install_codex_refuses_user_owned_collision_without_force(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers."dup"]\ncommand = "manual-mcp"\n', encoding="utf-8")
    with pytest.raises(IntegrityViolation):
        install_runtime_entry(
            server=_make_server(server_id="dup"),
            runtime="codex",
            target_dir=tmp_path,
            force=False,
        )


def test_install_codex_force_over_user_owned_raises_valueerror(tmp_path: Path) -> None:
    """Force cannot splice out a user table; the installer fails loudly."""
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers."dup"]\ncommand = "manual-mcp"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="manually"):
        install_runtime_entry(
            server=_make_server(server_id="dup"),
            runtime="codex",
            target_dir=tmp_path,
            force=True,
        )


def test_remove_codex_entry_deletes_only_eawf_owned(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers."manual"]\ncommand = "manual-mcp"\n', encoding="utf-8")
    install_runtime_entry(
        server=_make_server(server_id="ours", command="ours-mcp"),
        runtime="codex",
        target_dir=tmp_path,
        force=False,
    )
    result = remove_runtime_entry(
        server_id="ours",
        runtime="codex",
        target_dir=tmp_path,
        force=False,
    )
    assert result.action == "removed"
    parsed = _read_toml(config)
    assert "ours" not in parsed["mcp_servers"]  # type: ignore[operator]
    assert parsed["mcp_servers"]["manual"] == {"command": "manual-mcp"}  # type: ignore[index]


def test_remove_codex_entry_refuses_user_owned(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers."manual"]\ncommand = "manual-mcp"\n', encoding="utf-8")
    with pytest.raises(IntegrityViolation):
        remove_runtime_entry(
            server_id="manual",
            runtime="codex",
            target_dir=tmp_path,
            force=False,
        )


def test_remove_codex_entry_absent_id_is_no_op(tmp_path: Path) -> None:
    result = remove_runtime_entry(
        server_id="missing",
        runtime="codex",
        target_dir=tmp_path,
        force=False,
    )
    assert result.action == "absent"


def test_list_runtime_entries_codex_owner_annotation(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers."manual"]\ncommand = "manual-mcp"\n', encoding="utf-8")
    install_runtime_entry(
        server=_make_server(server_id="ours", command="ours-mcp"),
        runtime="codex",
        target_dir=tmp_path,
        force=False,
    )
    rows = list_runtime_entries(runtime="codex", target_dir=tmp_path)
    by_id = {r.id: r for r in rows}
    assert by_id["manual"].owner == "user"
    assert by_id["ours"].owner == "eawf"


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
def test_install_raises_verify_failure_on_corrupt_writeback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    """A write that lands the wrong bytes is caught by the read-back verify."""

    def _corrupt(path: Path, _text: str) -> None:
        # Materialise a syntactically valid but grant-wrong config so the
        # parse step in the verifier succeeds and the *content* check fails.
        path.parent.mkdir(parents=True, exist_ok=True)
        if runtime == "codex":
            path.write_text('[mcp_servers."demo"]\ncommand = "wrong-mcp"\n', encoding="utf-8")
        elif runtime == "opencode":
            path.write_text(
                json.dumps({"mcp": {"demo": {"command": "wrong-mcp"}}}),
                encoding="utf-8",
            )
        else:
            path.write_text(
                json.dumps({"mcpServers": {"demo": {"command": "wrong-mcp"}}}),
                encoding="utf-8",
            )

    monkeypatch.setattr(installer, "atomic_write_text", _corrupt)
    with pytest.raises(VerifyFailure):
        install_runtime_entry(
            server=_make_server(command="right-mcp"),
            runtime=runtime,
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
