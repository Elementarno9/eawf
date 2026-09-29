"""SURF-168: every host configuration key eawf writes carries a source and a probe.

The record table is checked against the probe module it cites, and each
writer is checked to refuse a key without a writable record before any
byte reaches the host document.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from eawf.kernel.economics.governor import DEFAULT_GOVERNOR
from eawf.kernel.state.enums import McpRisk, McpStatus
from eawf.kernel.state.models import McpServer
from eawf.runtime.harness import host_keys
from eawf.runtime.harness.host_keys import (
    HOST_KEYS,
    PROBE_MODULE,
    WRITABLE_EFFECTS,
    HostKeyRecord,
    UnrecordedHostKeyError,
    require_recorded,
)
from eawf.runtime.mcp.installer import install_runtime_entry
from eawf.runtime.runtimes.claude import plugin_install as claude_install
from eawf.runtime.runtimes.claude.statusline_install import patch_settings
from eawf.runtime.runtimes.codex import plugin_install as codex_install

_REPO = Path(__file__).resolve().parents[4]


def _probe_names() -> set[str]:
    tree = ast.parse((_REPO / PROBE_MODULE).read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def _server() -> McpServer:
    return McpServer(
        id="demo",
        owner="eawf",
        command="demo-mcp",
        args=[],
        env_refs=[],
        risk=McpRisk.READ,
        write_capable=False,
        status=McpStatus.CONFIGURED,
        installed_targets=[],
    )


# ---- the record table ---------------------------------------------------------------------


def test_surf_168_every_probed_record_cites_a_probe_that_exists() -> None:
    probes = _probe_names()
    cited = {
        record.evidence.partition("::")[2] for record in HOST_KEYS if record.effect != "unprobed"
    }
    assert cited
    assert cited <= probes, sorted(cited - probes)


def test_surf_168_every_probe_backs_at_least_one_record() -> None:
    cited = {record.evidence.partition("::")[2] for record in HOST_KEYS}
    probes = {name for name in _probe_names() if name.startswith("test_")}
    assert probes <= cited, sorted(probes - cited)


def test_surf_168_each_key_is_recorded_once() -> None:
    keys = [(record.document, record.path) for record in HOST_KEYS]
    assert len(keys) == len(set(keys))


def test_surf_168_keys_eawf_stopped_writing_are_recorded_unwritable() -> None:
    by_key = {(record.document, record.path): record.effect for record in HOST_KEYS}
    assert by_key[("codex_config", "plugins.eawf.enabled")] == "no_effect"
    assert by_key[("claude_mcp", "mcpServers.*.transport")] == "no_effect"
    assert by_key[("codex_config", "agents.default_subagent_model")] == "unprobed"
    assert by_key[("codex_config", "agents.job_max_runtime_seconds")] == "unprobed"
    assert by_key[("opencode_config", "mcp")] == "unprobed"


@pytest.mark.parametrize("source", ["", "SURF-12", "surf-168", "SURF-1680", "SURF 168"])
def test_surf_168_record_refuses_a_source_that_is_not_a_requirement_id(source: str) -> None:
    with pytest.raises(ValueError, match="not a requirement id"):
        HostKeyRecord(
            document="claude_settings",
            path="statusLine",
            source=source,
            effect="unprobed",
            evidence="no probe yet",
        )


def test_surf_168_record_refuses_a_probed_effect_without_a_probe_node() -> None:
    with pytest.raises(ValueError, match="cites no probe node"):
        HostKeyRecord(
            document="claude_settings",
            path="statusLine",
            source="SURF-120",
            effect="honoured",
            evidence="seen once by hand",
        )


def test_surf_168_record_refuses_an_unprobed_effect_citing_a_probe_node() -> None:
    with pytest.raises(ValueError, match="cites a probe node"):
        HostKeyRecord(
            document="claude_settings",
            path="statusLine",
            source="SURF-120",
            effect="unprobed",
            evidence=f"{PROBE_MODULE}::test_surf_168_claude_status_line_runs",
        )


# ---- require_recorded ---------------------------------------------------------------------


def test_surf_168_require_recorded_accepts_no_paths() -> None:
    require_recorded("claude_settings", ())


def test_surf_168_require_recorded_accepts_every_writable_record() -> None:
    for record in HOST_KEYS:
        if record.effect in WRITABLE_EFFECTS:
            require_recorded(record.document, (record.path,))


def test_surf_168_require_recorded_refuses_a_key_with_no_record() -> None:
    with pytest.raises(UnrecordedHostKeyError, match=r"env\.INVENTED \(no record\)"):
        require_recorded("claude_settings", ("statusLine", "env.INVENTED"))


def test_surf_168_require_recorded_refuses_a_key_recorded_in_another_document() -> None:
    with pytest.raises(UnrecordedHostKeyError, match="no record"):
        require_recorded("codex_config", ("statusLine",))


@pytest.mark.parametrize(
    ("document", "path", "effect"),
    [
        ("codex_config", "plugins.eawf.enabled", "no_effect"),
        ("claude_settings", "env.CLAUDE_CODE_SUBAGENT_MODEL", "unprobed"),
    ],
)
def test_surf_168_require_recorded_refuses_an_unwritable_effect(
    document: host_keys.HostDocument, path: str, effect: str
) -> None:
    with pytest.raises(UnrecordedHostKeyError, match=rf"{path} \({effect}"):
        require_recorded(document, (path,))


def test_surf_168_require_recorded_names_every_refused_key() -> None:
    with pytest.raises(UnrecordedHostKeyError) as caught:
        require_recorded("codex_config", ("a.one", "agents.max_depth", "b.two"))
    assert "a.one" in str(caught.value)
    assert "b.two" in str(caught.value)
    assert "agents.max_depth" not in str(caught.value)


# ---- the writers ----------------------------------------------------------------------------


def test_surf_168_codex_block_refuses_an_unrecorded_fan_out_key() -> None:
    with pytest.raises(UnrecordedHostKeyError, match=r"agents\.invented"):
        codex_install._render_enabled_block({"agents.invented": 1})


def test_surf_168_codex_block_declares_no_plugin(tmp_path: Path) -> None:
    codex_install.install_plugin(tmp_path, home=tmp_path / "home")
    text = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[plugins" not in text
    assert "[agents]" in text


def test_surf_168_claude_settings_refuse_an_unrecorded_key_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        claude_install,
        "fan_out_values",
        lambda runtime, *, governor: {"env.CLAUDE_CODE_SUBAGENT_MODEL": "haiku"},
    )
    with pytest.raises(UnrecordedHostKeyError, match="CLAUDE_CODE_SUBAGENT_MODEL"):
        claude_install.install_plugin(tmp_path)
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_surf_168_claude_settings_record_the_keys_they_write(tmp_path: Path) -> None:
    payload = claude_install._patch_settings_json(
        tmp_path / "settings.json", {"version": "x"}, governor=DEFAULT_GOVERNOR
    )
    written = json.loads(payload)
    paths = [
        *(key for key in written if key not in {"hooks", "env"}),
        *(f"hooks.{event}" for event in written["hooks"]),
        *(f"env.{name}" for name in written["env"]),
    ]
    require_recorded("claude_settings", paths)


def test_surf_168_statusline_patch_is_recorded() -> None:
    assert "statusLine" in patch_settings({})


def test_surf_168_claude_mcp_entry_carries_no_transport(tmp_path: Path) -> None:
    install_runtime_entry(server=_server(), runtime="claude", target_dir=tmp_path, force=False)
    entry = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["demo"]
    assert sorted(entry) == ["__eawf_managed_at", "__eawf_owner", "args", "command", "env"]


def test_surf_168_opencode_mcp_entry_is_refused_before_writing(tmp_path: Path) -> None:
    with pytest.raises(UnrecordedHostKeyError, match="unprobed"):
        install_runtime_entry(
            server=_server(), runtime="opencode", target_dir=tmp_path, force=False
        )
    assert not (tmp_path / "opencode.json").exists()
