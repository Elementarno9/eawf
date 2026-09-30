"""Tests for the ``memory`` statusline module on trees without a generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude.statusline_modules import memory as memory_module


def test_no_state_path_names_why() -> None:
    seg = memory_module.build({}, None)
    assert seg.text == "mem:n/a(no-state)"
    assert seg.status == "missing"


@pytest.mark.parametrize(
    "content",
    [b"{}", b'{"memory_index": {"m1": {"id": "m1"}}}', b"not-json"],
    ids=["empty", "indexed", "malformed"],
)
def test_an_epoch1_tree_names_the_authority_gap_not_its_index(
    tmp_path: Path, content: bytes
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir()
    state_path.write_bytes(content)
    store_dir = tmp_path / ".ea" / "store"
    store_dir.mkdir()
    (store_dir / "memory.jsonl").write_bytes(b"x" * 2048)
    seg = memory_module.build({}, state_path)
    assert seg.text == "mem:n/a(epoch1-undeclared)"
    assert seg.status == "missing"
