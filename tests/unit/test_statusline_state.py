"""Tests for the ``state`` statusline module (Phase 4 W06)."""

from __future__ import annotations

from pathlib import Path

import orjson

from eawf.runtimes.claude.statusline_modules import state as state_module


def _write_state(tmp_path: Path, payload: dict[str, object]) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state_path.write_bytes(orjson.dumps(payload))
    return state_path


def test_state_module_reads_active_wave_first(tmp_path: Path) -> None:
    state_path = _write_state(
        tmp_path,
        {
            "current": {
                "phase_id": "P04",
                "iter_id": "P04-I01",
                "active_wave_ids": ["P04-I01-W06"],
            }
        },
    )
    seg = state_module.build({}, state_path)
    assert seg.module == "state"
    assert seg.text == "state:P04-I01-W06"
    assert seg.status == "ok"


def test_state_module_falls_back_to_iter_then_phase(tmp_path: Path) -> None:
    state_path = _write_state(
        tmp_path, {"current": {"phase_id": "P04", "iter_id": "P04-I01", "active_wave_ids": []}}
    )
    seg = state_module.build({}, state_path)
    assert seg.text == "state:P04-I01"

    state_path = _write_state(tmp_path / "phase-only", {"current": {"phase_id": "P04"}})
    seg = state_module.build({}, state_path)
    assert seg.text == "state:P04"


def test_state_module_missing_state_path_returns_question(tmp_path: Path) -> None:
    seg = state_module.build({}, None)
    assert seg.text == "state:?"
    assert seg.status == "missing"

    seg = state_module.build({}, tmp_path / "nope" / "state.json")
    assert seg.text == "state:?"
    assert seg.status == "missing"


def test_state_module_malformed_payload_returns_question(tmp_path: Path) -> None:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_bytes(b"not-json")
    seg = state_module.build({}, state_path)
    assert seg.text == "state:?"
    assert seg.status == "missing"


def test_state_module_payload_without_current_returns_question(tmp_path: Path) -> None:
    state_path = _write_state(tmp_path, {"schema_version": "1.0"})
    seg = state_module.build({}, state_path)
    assert seg.text == "state:?"
    assert seg.status == "missing"
