"""The epoch-1 write fence at the state-write chokepoint and the JSON writer."""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME, MARKER_FILENAME
from eawf.kernel.state.io import (
    LEGACY_OPERATION_REMOVED,
    LegacyOperationRemovedError,
    StateValidationError,
    epoch_marker_present,
    refuse_legacy_write,
    write_state_unlocked,
)
from eawf.kernel.state.writer import atomic_write_json, atomic_write_json_locked

pytestmark = pytest.mark.unit


def _mark(ea_dir: Path) -> None:
    marker = ea_dir / GENERATIONS_DIRNAME / MARKER_FILENAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"{}")


@pytest.fixture
def ea_dir(tmp_path: Path) -> Path:
    root = tmp_path / ".ea"
    root.mkdir()
    (root / "state.json").write_bytes(orjson.dumps({"a": 1}))
    return root


def test_epoch_marker_present_false_without_marker(ea_dir: Path) -> None:
    assert epoch_marker_present(ea_dir) is False


def test_epoch_marker_present_false_for_missing_dir(tmp_path: Path) -> None:
    assert epoch_marker_present(tmp_path / "absent") is False


def test_epoch_marker_present_false_with_only_generations_dir(ea_dir: Path) -> None:
    (ea_dir / GENERATIONS_DIRNAME).mkdir()
    assert epoch_marker_present(ea_dir) is False


def test_epoch_marker_present_true_with_marker(ea_dir: Path) -> None:
    _mark(ea_dir)
    assert epoch_marker_present(ea_dir) is True


def test_epoch_marker_present_true_for_unparseable_marker(ea_dir: Path) -> None:
    """Presence is the question; a torn marker still freezes the tree."""
    _mark(ea_dir)
    (ea_dir / GENERATIONS_DIRNAME / MARKER_FILENAME).write_bytes(b"{not json")
    assert epoch_marker_present(ea_dir) is True


def test_refuse_legacy_write_passes_an_unmarked_tree(ea_dir: Path) -> None:
    refuse_legacy_write(ea_dir / "state.json")


def test_refuse_legacy_write_ignores_other_files_on_a_marked_tree(ea_dir: Path) -> None:
    _mark(ea_dir)
    refuse_legacy_write(ea_dir / "registry.json")
    refuse_legacy_write(ea_dir / "state.json.bak")


def test_refuse_legacy_write_raises_on_a_marked_tree(ea_dir: Path) -> None:
    _mark(ea_dir)
    with pytest.raises(LegacyOperationRemovedError) as caught:
        refuse_legacy_write(ea_dir / "state.json")
    assert caught.value.code == LEGACY_OPERATION_REMOVED
    assert str(caught.value).startswith(LEGACY_OPERATION_REMOVED)
    assert str(ea_dir.parent) not in str(caught.value)


def test_refuse_legacy_write_error_is_a_state_validation_error(ea_dir: Path) -> None:
    """Callers that map a refused state write onto the validation bucket keep working."""
    _mark(ea_dir)
    with pytest.raises(StateValidationError):
        refuse_legacy_write(ea_dir / "state.json")
    with pytest.raises(ValueError, match=LEGACY_OPERATION_REMOVED):
        refuse_legacy_write(ea_dir / "state.json")


def test_refuse_legacy_write_ignores_a_nested_generation_state(ea_dir: Path) -> None:
    """A ``state.json`` below ``generations/`` is not the tree's epoch-1 document."""
    _mark(ea_dir)
    refuse_legacy_write(ea_dir / GENERATIONS_DIRNAME / "G-1" / "state.json")


def test_write_state_unlocked_refused_leaves_state_byte_identical(ea_dir: Path) -> None:
    _mark(ea_dir)
    before = (ea_dir / "state.json").read_bytes()
    with pytest.raises(LegacyOperationRemovedError):
        write_state_unlocked(ea_dir / "state.json", {"a": 2})
    assert (ea_dir / "state.json").read_bytes() == before
    assert sorted(p.name for p in ea_dir.iterdir()) == [GENERATIONS_DIRNAME, "state.json"]


def test_write_state_unlocked_writes_an_unmarked_tree(ea_dir: Path) -> None:
    write_state_unlocked(ea_dir / "state.json", {"a": 2})
    assert orjson.loads((ea_dir / "state.json").read_bytes()) == {"a": 2}


@pytest.mark.parametrize("writer", [atomic_write_json, atomic_write_json_locked])
def test_atomic_write_json_refuses_state_on_a_marked_tree(ea_dir: Path, writer: object) -> None:
    _mark(ea_dir)
    before = (ea_dir / "state.json").read_bytes()
    with pytest.raises(LegacyOperationRemovedError):
        writer(ea_dir / "state.json", {"a": 2})  # type: ignore[operator]
    assert (ea_dir / "state.json").read_bytes() == before


def test_atomic_write_json_writes_other_files_on_a_marked_tree(ea_dir: Path) -> None:
    _mark(ea_dir)
    atomic_write_json(ea_dir / "registry.json", {"b": 1})
    assert orjson.loads((ea_dir / "registry.json").read_bytes()) == {"b": 1}
