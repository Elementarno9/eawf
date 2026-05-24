"""Tests for the ``memory`` statusline module (Phase 4 W06)."""

from __future__ import annotations

from pathlib import Path

import orjson

from eawf.runtime.runtimes.claude.statusline_modules import memory as memory_module


def _seed_state(tmp_path: Path, payload: dict[str, object]) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_bytes(orjson.dumps(payload))
    return state_path


def test_no_state_path_returns_dash() -> None:
    seg = memory_module.build({}, None)
    assert seg.text == "mem:-"
    assert seg.status == "missing"


def test_empty_index_returns_dash(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path, {})
    seg = memory_module.build({}, state_path)
    assert seg.text == "mem:-"
    assert seg.status == "missing"


def test_index_count_renders_entries(tmp_path: Path) -> None:
    state_path = _seed_state(
        tmp_path,
        {
            "memory_index": {
                "m1": {"id": "m1", "summary": "x"},
                "m2": {"id": "m2", "summary": "y"},
                "m3": {"id": "m3", "summary": "z"},
            }
        },
    )
    seg = memory_module.build({}, state_path)
    assert seg.module == "memory"
    assert seg.text.startswith("mem:3@")
    assert seg.text.endswith("B")
    assert seg.status == "ok"


def test_size_reflects_jsonl_bytes(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path, {"memory_index": {"m1": {"id": "m1"}}})
    store_dir = tmp_path / ".ea" / "store"
    store_dir.mkdir()
    (store_dir / "memory.jsonl").write_bytes(b"x" * 2048)
    seg = memory_module.build({}, state_path)
    assert seg.text == "mem:1@2KiB"


def test_malformed_state_returns_dash(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir()
    state_path.write_bytes(b"not-json")
    seg = memory_module.build({}, state_path)
    assert seg.text == "mem:-"
    assert seg.status == "missing"
