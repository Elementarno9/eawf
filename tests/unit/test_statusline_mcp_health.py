"""Tests for the ``mcp_health`` statusline module on trees without a generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude.statusline_modules import mcp_health


def test_no_state_path_names_why() -> None:
    seg = mcp_health.build({}, None)
    assert seg.text == "mcp:n/a(no-state)"
    assert seg.status == "missing"


@pytest.mark.parametrize(
    "content",
    [b"{}", b'{"mcp_servers": {"a": {"id": "a", "status": "up"}}}', b"{invalid"],
    ids=["empty", "servers-up", "malformed"],
)
def test_an_epoch1_tree_names_the_authority_gap_not_its_document(
    tmp_path: Path, content: bytes
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir()
    state_path.write_bytes(content)
    seg = mcp_health.build({}, state_path)
    assert seg.text == "mcp:n/a(epoch1-undeclared)"
    assert seg.status == "missing"
