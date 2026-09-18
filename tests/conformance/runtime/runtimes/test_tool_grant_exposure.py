"""RUN-008: the grant a Run holds is the grant its provider can see.

Three lanes, one contract. Each of Claude Code, Codex and opencode takes
MCP servers and tool grants in a different shape, so this suite asserts
the shape each one actually produces rather than a shared abstraction
over all three: the file Claude reads, the ``-c`` overrides Codex takes,
the document opencode loads. What is asserted identically across them is
the outcome -- the per-Run stdio server is the program the lane starts,
the tools it publishes are the capsule's resolved grant, and every
ambient vendor tool is denied.

The lane is bound to the live adapter by its own id: each renderer's
``RUNTIME_ID`` is asserted equal to the adapter class's ``id``, so a
configuration rendered here is the configuration the adapter that spawns
that lane is configured by, not a string that merely resembles one.

The empty-grant case is the one that matters most and is therefore
proved twice: the lane configuration exposes no tool at all, and the
server that configuration starts lists none either. A Run granted
nothing sees nothing -- not the vendor's file tools, not its shell, and
not the catalog the server happens to know about.

Nothing here starts a provider, opens a socket or writes outside
``tmp_path``. The renderers are pure over a capsule and a directory, and
the one server driven is driven in process with a transport that refuses
to be reached.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.runtime.mcp.semantic_stdio import (
    SERVER_NAME,
    RunServerBinding,
    RunServerConfig,
    SemanticStdioServer,
    ServerTableError,
    ambient_tool_denial,
    materialize_run_server_config,
    mcp_tool_name,
    run_server_config_for,
)
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter
from eawf.runtime.runtimes.claude.mcp_config import MCP_CONFIG_FILENAME
from eawf.runtime.runtimes.claude.mcp_config import RUNTIME_ID as CLAUDE_LANE
from eawf.runtime.runtimes.claude.mcp_config import render_run_server_config as render_claude
from eawf.runtime.runtimes.codex.adapter import CodexAdapter, codex_tool_config_key
from eawf.runtime.runtimes.codex.mcp_config import RUNTIME_ID as CODEX_LANE
from eawf.runtime.runtimes.codex.mcp_config import SERVER_CONFIG_KEY
from eawf.runtime.runtimes.codex.mcp_config import render_run_server_config as render_codex
from eawf.runtime.runtimes.opencode.adapter import OpenCodeAdapter
from eawf.runtime.runtimes.opencode.mcp_config import CONFIG_ENV_VAR, CONFIG_FILENAME
from eawf.runtime.runtimes.opencode.mcp_config import RUNTIME_ID as OPENCODE_LANE
from eawf.runtime.runtimes.opencode.mcp_config import render_run_server_config as render_opencode
from eawf.runtime.sandbox.policy import TOOL_UNIVERSE

pytestmark = pytest.mark.conformance


SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
RUN_URN: Final = f"{SLOT}/run/RUN-00000010"
TASK_URN: Final = f"{SLOT}/task/EAWF-0042"

#: The grant under test: two tools, so an exposure that leaked the whole
#: catalog and one that leaked none are both visible.
GRANTS: Final = ("budget_status", "attach_evidence")

#: The three lanes, in the order the selector resolves them.
LANES: Final = (CLAUDE_LANE, CODEX_LANE, OPENCODE_LANE)

#: The argv a lane is configured to start the per-Run server with.
SERVER_COMMAND: Final = ("eawf", "mcp", "serve", "--binding", "/dev/null")


def digest(char: str) -> str:
    """Return a well-formed digest whose body is one repeated character."""
    return f"sha256:{char * 64}"


def seal_capsule(*, grants: tuple[str, ...] = GRANTS) -> AuthorityCapsule:
    """Return a sealed capsule granting *grants* and nothing else."""
    return AuthorityCapsule.seal(
        {
            "run_ref": RUN_URN,
            "scope_ref": TASK_URN,
            "scope_digest": digest("a"),
            "agent_role": AgentSessionRole.EXECUTOR.value,
            "purpose": RunPurpose.IMPLEMENT.value,
            "authority": {"state": "read_only", "workspace": "scoped_write"},
            "tool_grants": grants,
            "budget": {"wall_seconds": 3600, "output_bytes": 1_048_576},
            "criteria_digest": digest("b"),
            "policy_digest": digest("c"),
            "compiled_spec_digest": digest("d"),
            "report_schema_ref": "schema://executor-report/v1",
            "stop_conditions": ("budget_exhausted",),
        }
    )


def make_binding(tmp_path: Path, capsule: AuthorityCapsule) -> RunServerBinding:
    """Write the capsule and return the binding that names it."""
    capsule_path = tmp_path / "capsule.json"
    capsule_path.write_text(json.dumps(capsule.model_dump(mode="json")), encoding="utf-8")
    return RunServerBinding(
        run_ref=RUN_URN, repo_root=str(tmp_path), capsule_path=str(capsule_path)
    )


def render(lane: str, tmp_path: Path, *, grants: tuple[str, ...] = GRANTS) -> RunServerConfig:
    """Return one lane's configuration of a Run granted *grants*."""
    capsule = seal_capsule(grants=grants)
    return run_server_config_for(
        lane,
        binding=make_binding(tmp_path, capsule),
        config_dir=tmp_path / "config",
        server_command=SERVER_COMMAND,
    )


def flag_value(config: RunServerConfig, flag: str) -> str:
    """Return the value Claude's *flag* carries in the rendered argv."""
    flags = list(config.argv_flags)
    return flags[flags.index(flag) + 1]


def codex_overrides(config: RunServerConfig) -> dict[str, str]:
    """Return the Codex ``-c key=value`` overrides as a mapping."""
    flags = list(config.argv_flags)
    pairs = [flags[index + 1] for index, token in enumerate(flags) if token == "-c"]
    return dict(pair.split("=", 1) for pair in pairs)


def opencode_document(config: RunServerConfig, tmp_path: Path) -> dict[str, Any]:
    """Return the opencode document the lane writes, read back from disk."""
    materialize_run_server_config(config)
    body: dict[str, Any] = json.loads(
        (tmp_path / "config" / CONFIG_FILENAME).read_text(encoding="utf-8")
    )
    return body


def denied_ambient(config: RunServerConfig, tmp_path: Path) -> set[str]:
    """Return the ambient tool names the lane's own mechanism refuses.

    Each lane spells a denial differently, so the extraction is per lane
    and the assertion over it is shared: whatever the spelling, the set
    must cover the whole ambient universe.
    """
    if config.runtime_id == CLAUDE_LANE:
        return set(flag_value(config, "--disallowedTools").split(" "))
    if config.runtime_id == CODEX_LANE:
        return {key for key, value in codex_overrides(config).items() if value == "false"}
    document = opencode_document(config, tmp_path)
    return {name for name, granted in document["tools"].items() if granted is False}


def ambient_names(lane: str) -> set[str]:
    """Return the ambient universe as *lane* spells it."""
    if lane == CODEX_LANE:
        return {codex_tool_config_key(tool) for tool in TOOL_UNIVERSE}
    return set(TOOL_UNIVERSE)


# ---------------------------------------------------------------------------
# The lane a configuration is rendered for is a lane an adapter drives
# ---------------------------------------------------------------------------


def test_each_renderer_names_the_lane_its_live_adapter_declares() -> None:
    """A configuration for a lane nothing spawns would configure nothing."""
    assert ClaudeAdapter.id == CLAUDE_LANE
    assert CodexAdapter.id == CODEX_LANE
    assert OpenCodeAdapter.id == OPENCODE_LANE


def test_a_lane_no_adapter_drives_renders_no_configuration(tmp_path: Path) -> None:
    capsule = seal_capsule()

    with pytest.raises(ServerTableError, match="aider"):
        run_server_config_for(
            "aider",
            binding=make_binding(tmp_path, capsule),
            config_dir=tmp_path,
            server_command=SERVER_COMMAND,
        )


@pytest.mark.parametrize(
    "renderer", [render_claude, render_codex, render_opencode], ids=list(LANES)
)
def test_a_renderer_refuses_a_server_with_no_program(tmp_path: Path, renderer: Any) -> None:
    capsule = seal_capsule()

    with pytest.raises(ValueError, match="program"):
        renderer(make_binding(tmp_path, capsule), config_dir=tmp_path, server_command=())


# ---------------------------------------------------------------------------
# The positive grant reaches each lane
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", LANES)
def test_the_lane_exposes_exactly_the_runs_granted_tools(lane: str, tmp_path: Path) -> None:
    config = render(lane, tmp_path)

    assert config.runtime_id == lane
    assert config.exposed_tools == tuple(mcp_tool_name(SemanticToolId(tool)) for tool in GRANTS)


@pytest.mark.parametrize("lane", LANES)
def test_the_lane_exposes_no_catalog_tool_the_run_was_not_granted(
    lane: str, tmp_path: Path
) -> None:
    config = render(lane, tmp_path)

    ungranted = {tool for tool in SemanticToolId if tool.value not in GRANTS}
    assert not {mcp_tool_name(tool) for tool in ungranted} & set(config.exposed_tools)


@pytest.mark.parametrize("lane", LANES)
def test_the_lane_starts_the_per_run_server_and_nothing_else(lane: str, tmp_path: Path) -> None:
    """The registered program is the eawf stdio server, argv and all."""
    config = render(lane, tmp_path)
    rendered = json.dumps(
        {
            "flags": list(config.argv_flags),
            "files": dict(config.files),
        }
    )

    assert SERVER_NAME.replace("-", "_") in rendered or SERVER_NAME in rendered
    for token in SERVER_COMMAND:
        assert token in rendered


def test_claude_registers_the_server_in_a_strict_mcp_document(tmp_path: Path) -> None:
    config = render(CLAUDE_LANE, tmp_path)
    written = materialize_run_server_config(config)
    document = json.loads((tmp_path / "config" / MCP_CONFIG_FILENAME).read_text("utf-8"))

    assert written == (str(tmp_path / "config" / MCP_CONFIG_FILENAME),)
    assert document["mcpServers"][SERVER_NAME]["command"] == SERVER_COMMAND[0]
    assert document["mcpServers"][SERVER_NAME]["args"] == list(SERVER_COMMAND[1:])
    assert "--strict-mcp-config" in config.argv_flags
    assert flag_value(config, "--allowedTools").split(" ") == list(config.exposed_tools)


def test_codex_registers_the_server_without_touching_a_config_file(tmp_path: Path) -> None:
    config = render(CODEX_LANE, tmp_path)
    overrides = codex_overrides(config)

    assert config.files == {}
    assert materialize_run_server_config(config) == ()
    assert overrides[f"{SERVER_CONFIG_KEY}.command"] == json.dumps(SERVER_COMMAND[0])
    assert overrides[f"{SERVER_CONFIG_KEY}.args"] == json.dumps(list(SERVER_COMMAND[1:]))
    assert overrides[f"{SERVER_CONFIG_KEY}.enabled"] == "true"


def test_opencode_registers_the_server_in_a_document_it_is_pointed_at(tmp_path: Path) -> None:
    config = render(OPENCODE_LANE, tmp_path)
    document = opencode_document(config, tmp_path)
    path = tmp_path / "config" / CONFIG_FILENAME

    assert config.env == {CONFIG_ENV_VAR: str(path)}
    assert document["mcp"][SERVER_NAME]["command"] == list(SERVER_COMMAND)
    assert document["mcp"][SERVER_NAME]["enabled"] is True
    assert document["mcp"][SERVER_NAME]["type"] == "local"


# ---------------------------------------------------------------------------
# An empty grant exposes nothing, ambient tools least of all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", LANES)
def test_an_empty_grant_exposes_no_tool_on_any_lane(lane: str, tmp_path: Path) -> None:
    config = render(lane, tmp_path, grants=())

    assert config.exposed_tools == ()


def test_an_empty_grant_leaves_claude_with_no_allow_flag(tmp_path: Path) -> None:
    """An allow flag with an empty value reads as a grant nobody made."""
    config = render(CLAUDE_LANE, tmp_path, grants=())

    assert "--allowedTools" not in config.argv_flags
    assert "--strict-mcp-config" in config.argv_flags


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("grants", [GRANTS, ()], ids=["granted", "empty"])
def test_every_ambient_provider_tool_is_denied_whatever_the_grant(
    lane: str, grants: tuple[str, ...], tmp_path: Path
) -> None:
    """A checked grant beside an unchecked shell is not a boundary."""
    config = render(lane, tmp_path, grants=grants)

    assert ambient_names(lane) <= denied_ambient(config, tmp_path)


def test_the_ambient_universe_is_the_sandbox_universe() -> None:
    """One universe, so a tool added to the sandbox set is denied here too."""
    assert set(ambient_tool_denial()) == set(TOOL_UNIVERSE)
    assert ambient_tool_denial()


@pytest.mark.parametrize("lane", LANES)
def test_the_server_a_lane_starts_lists_the_same_grant_it_advertises(
    lane: str, tmp_path: Path
) -> None:
    """The configuration and the server derive from one capsule."""
    capsule = seal_capsule()
    binding = make_binding(tmp_path, capsule)
    config = run_server_config_for(
        lane, binding=binding, config_dir=tmp_path / "config", server_command=SERVER_COMMAND
    )
    server = SemanticStdioServer(binding=binding, capsule=capsule, transport=UnreachedTransport())

    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert listed is not None
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert [mcp_tool_name(SemanticToolId(name)) for name in names] == list(config.exposed_tools)


@pytest.mark.parametrize("lane", LANES)
def test_the_server_a_lane_starts_lists_nothing_for_an_empty_grant(
    lane: str, tmp_path: Path
) -> None:
    capsule = seal_capsule(grants=())
    binding = make_binding(tmp_path, capsule)
    config = run_server_config_for(
        lane, binding=binding, config_dir=tmp_path / "config", server_command=SERVER_COMMAND
    )
    server = SemanticStdioServer(binding=binding, capsule=capsule, transport=UnreachedTransport())

    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert listed is not None
    assert listed["result"]["tools"] == []
    assert config.exposed_tools == ()


class UnreachedTransport:
    """A transport no case here is allowed to reach."""

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Fail loudly: listing tools must forward nothing.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError(f"{method} was forwarded by a listing case")
