"""Strict validation for the ``agents.extra_tools`` config section.

The leaf grants extra runtime tool names to rendered subagents without eawf
naming any specific tool or MCP server. Covered here:

- :class:`~eawf.kernel.config.schema.AgentsConfig` — the strict section
  model. An empty body defaults cleanly, a known role key and the ``"*"``
  wildcard are accepted, and an unknown role key, a non-list value, a blank
  tool name, or an unknown ``agents.*`` key all raise
  :class:`pydantic.ValidationError`.
- The built-in defaults ship the section empty, so an unconfigured repo
  renders the declared allowlists unchanged.
- The leaf catalog (:data:`LEAF_KEY_REGISTRY`) resolves ``agents.extra_tools``
  with its empty-mapping default and its declared writable layers.
- :func:`~eawf.kernel.config.layered.resolve_agent_extra_tools` folds the
  merged layers into the ``role -> extra tools`` map the renderers consume.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.config.defaults import built_in_defaults
from eawf.kernel.config.layered import resolve_agent_extra_tools
from eawf.kernel.config.registry import LEAF_KEY_REGISTRY, is_known_leaf_key, leaf_key_lookup
from eawf.kernel.config.schema import ALL_ROLES, AgentsConfig
from eawf.kernel.state.enums import AgentSessionRole


def test_empty_body_defaults_to_no_grant() -> None:
    assert AgentsConfig.model_validate({}).extra_tools == {}


def test_wildcard_key_is_accepted() -> None:
    config = AgentsConfig.model_validate({"extra_tools": {ALL_ROLES: ["SomeTool"]}})
    assert config.extra_tools[ALL_ROLES] == ["SomeTool"]


@pytest.mark.parametrize("role", sorted(r.value for r in AgentSessionRole))
def test_every_canonical_role_key_is_accepted(role: str) -> None:
    """Including ``domain-specialist``, whose hyphen is not an identifier."""
    config = AgentsConfig.model_validate({"extra_tools": {role: ["SomeTool"]}})
    assert config.extra_tools[role] == ["SomeTool"]


def test_empty_tool_list_is_accepted() -> None:
    """An explicitly empty grant is a legitimate way to spell "nothing"."""
    assert AgentsConfig.model_validate({"extra_tools": {"researcher": []}}).extra_tools == {
        "researcher": []
    }


def test_unknown_role_key_rejected() -> None:
    with pytest.raises(ValidationError, match=r"unknown agents\.extra_tools role key"):
        AgentsConfig.model_validate({"extra_tools": {"reseacher": ["SomeTool"]}})


def test_non_list_value_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentsConfig.model_validate({"extra_tools": {"researcher": "SomeTool"}})


def test_blank_tool_name_rejected() -> None:
    with pytest.raises(ValidationError, match="blank tool name"):
        AgentsConfig.model_validate({"extra_tools": {"researcher": ["Read", "  "]}})


def test_unknown_section_key_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentsConfig.model_validate({"extra_tools": {}, "extra_skills": {}})


def test_built_in_defaults_ship_the_section_empty() -> None:
    assert built_in_defaults()["agents"] == {"extra_tools": {}}


def test_leaf_catalog_declares_the_key() -> None:
    entry = leaf_key_lookup("agents.extra_tools")
    assert entry.type == "mapping"
    assert entry.default == {}
    assert entry.domain == "agents"
    assert is_known_leaf_key("agents.extra_tools")
    assert LEAF_KEY_REGISTRY["agents.extra_tools"].writable_layers == (
        "global",
        "workspace",
        "repo",
    )


def test_leaf_catalog_binds_the_resolver_as_consumer() -> None:
    entry = leaf_key_lookup("agents.extra_tools")
    assert entry.consumer == "eawf.kernel.config.layered.resolve_agent_extra_tools"
    assert entry.reserved is False


def _write_repo_layer(repo: Path, body: dict[str, object]) -> None:
    (repo / ".ea").mkdir(parents=True, exist_ok=True)
    (repo / ".ea" / "config.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def test_resolve_reads_the_repo_layer(tmp_path: Path) -> None:
    _write_repo_layer(
        tmp_path,
        {"schema_version": "1.0", "agents": {"extra_tools": {"researcher": ["SomeTool"]}}},
    )
    assert resolve_agent_extra_tools(tmp_path)["researcher"] == ("SomeTool",)


def test_resolve_drops_empty_grants(tmp_path: Path) -> None:
    """A role configured with an empty list contributes no map entry."""
    _write_repo_layer(
        tmp_path,
        {
            "schema_version": "1.0",
            "agents": {"extra_tools": {"researcher": [], "auditor": ["SomeTool"]}},
        },
    )
    resolved = resolve_agent_extra_tools(tmp_path)
    assert "researcher" not in resolved
    assert resolved["auditor"] == ("SomeTool",)


def test_resolve_raises_on_a_malformed_repo_layer(tmp_path: Path) -> None:
    """A misspelled role fails at the loader, not silently at agent-spawn time."""
    _write_repo_layer(
        tmp_path,
        {"schema_version": "1.0", "agents": {"extra_tools": {"reseacher": ["SomeTool"]}}},
    )
    with pytest.raises(ValidationError, match=r"unknown agents\.extra_tools role key"):
        resolve_agent_extra_tools(tmp_path)
