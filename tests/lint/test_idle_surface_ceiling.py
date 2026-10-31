"""The idle-surface ceiling row of the idle-contract gate.

``tools/idle_surface_ceiling.json`` records how many public functions under
``src/eawf`` may sit with no production caller. The gate fails when the live
count rises above it, and the recorded value is held equal to the live count
so the ceiling only ever ratchets down.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.idle_contract_gate import GateFailure, check_idle_surface_ceiling
from tools.idle_contract_wiring import IDLE_SURFACE_CEILING_PATH, IdleSurfaceCeiling
from tools.idle_surface_report import find_idle_functions

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _live_idle_count() -> int:
    return len(
        find_idle_functions(_REPO_ROOT / "src" / "eawf", caller_roots=[_REPO_ROOT / "tools"])
    )


def _ceiling_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "idle_surface_ceiling.json"
    path.write_text(body, encoding="utf-8")
    return path


def test_recorded_ceiling_equals_the_live_idle_count() -> None:
    """The recorded ceiling is the measured count, with no slack to absorb a new idle function."""
    recorded = IdleSurfaceCeiling.model_validate_json(
        IDLE_SURFACE_CEILING_PATH.read_bytes()
    ).ceiling
    live = _live_idle_count()
    assert recorded == live, (
        f"{IDLE_SURFACE_CEILING_PATH.name} records {recorded} but {live} public functions "
        f"are idle; set the ceiling to {live}"
    )


def test_gate_passes_on_the_live_tree() -> None:
    result = check_idle_surface_ceiling()
    assert result.passed, result.message


@pytest.mark.parametrize("ceiling", [0, 1, 182])
def test_count_equal_to_the_ceiling_passes(tmp_path: Path, ceiling: int) -> None:
    path = _ceiling_file(tmp_path, json.dumps({"ceiling": ceiling}))
    result = check_idle_surface_ceiling(idle_count=ceiling, ceiling_path=path)
    assert result.passed
    assert result.failure is None
    assert "equals the ceiling" in result.message


@pytest.mark.parametrize(("count", "ceiling"), [(1, 0), (183, 182), (10_000, 182)])
def test_count_above_the_ceiling_fails_naming_both(
    tmp_path: Path, count: int, ceiling: int
) -> None:
    path = _ceiling_file(tmp_path, json.dumps({"ceiling": ceiling}))
    result = check_idle_surface_ceiling(idle_count=count, ceiling_path=path)
    assert not result.passed
    assert result.failure is GateFailure.IDLE_SURFACE_CEILING_EXCEEDED
    assert f"idle surface {count} exceeds the recorded ceiling {ceiling}" in result.message


def test_live_count_fails_against_a_ceiling_one_below_it(tmp_path: Path) -> None:
    """The gate reds on the real tree the moment the ceiling sits one under the measurement."""
    live = _live_idle_count()
    path = _ceiling_file(tmp_path, json.dumps({"ceiling": live - 1}))
    result = check_idle_surface_ceiling(ceiling_path=path)
    assert result.failure is GateFailure.IDLE_SURFACE_CEILING_EXCEEDED
    assert f"{live} exceeds the recorded ceiling {live - 1}" in result.message


@pytest.mark.parametrize(("count", "ceiling"), [(0, 1), (181, 182)])
def test_count_below_the_ceiling_passes_and_asks_to_lower_it(
    tmp_path: Path, count: int, ceiling: int
) -> None:
    path = _ceiling_file(tmp_path, json.dumps({"ceiling": ceiling}))
    result = check_idle_surface_ceiling(idle_count=count, ceiling_path=path)
    assert result.passed
    assert f"lower idle_surface_ceiling.json to {count}" in result.message


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        "{}",
        "[182]",
        '{"ceiling": -1}',
        '{"ceiling": "182"}',
        '{"ceiling": 182.0}',
        '{"ceiling": null}',
        '{"ceiling": 182, "headroom": 3}',
    ],
    ids=[
        "empty",
        "not-json",
        "missing-key",
        "wrong-shape",
        "negative",
        "string",
        "float",
        "null",
        "extra-key",
    ],
)
def test_malformed_ceiling_file_is_refused(tmp_path: Path, body: str) -> None:
    path = _ceiling_file(tmp_path, body)
    result = check_idle_surface_ceiling(idle_count=0, ceiling_path=path)
    assert not result.passed
    assert result.failure is GateFailure.IDLE_SURFACE_CEILING_INVALID
    assert "malformed" in result.message


def test_missing_ceiling_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        check_idle_surface_ceiling(idle_count=0, ceiling_path=tmp_path / "absent.json")
