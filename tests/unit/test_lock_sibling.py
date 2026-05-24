from __future__ import annotations

from pathlib import Path

from eawf.runtime.lock import sibling


def test_sibling_path_in_same_dir(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    assert sibling.lock_path(target) == tmp_path / "state.json.lock"


def test_sibling_path_for_jsonl(tmp_path: Path) -> None:
    target = tmp_path / "stores" / "memory.jsonl"
    target.parent.mkdir()
    assert sibling.lock_path(target) == tmp_path / "stores" / "memory.jsonl.lock"


def test_sibling_path_accepts_string(tmp_path: Path) -> None:
    target = str(tmp_path / "state.json")
    result = sibling.lock_path(Path(target))
    assert result == tmp_path / "state.json.lock"


def test_sibling_path_has_lock_suffix(tmp_path: Path) -> None:
    target = tmp_path / "anything.txt"
    result = sibling.lock_path(target)
    assert result.name == "anything.txt.lock"


def test_sibling_path_preserves_parent(tmp_path: Path) -> None:
    sub = tmp_path / "deep" / "nested"
    sub.mkdir(parents=True)
    target = sub / "data.json"
    result = sibling.lock_path(target)
    assert result.parent == sub


def test_sibling_path_no_extension_file(tmp_path: Path) -> None:
    target = tmp_path / "lockable"
    result = sibling.lock_path(target)
    assert result == tmp_path / "lockable.lock"


def test_sibling_path_multiple_dots_in_name(tmp_path: Path) -> None:
    target = tmp_path / "state.v2.json"
    result = sibling.lock_path(target)
    assert result == tmp_path / "state.v2.json.lock"
