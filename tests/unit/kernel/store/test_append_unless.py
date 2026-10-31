"""A conditional append asks whether its line is already said under the lock it writes under."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.store.append import append_json_line, append_json_line_unless
from eawf.runtime.lock import portalock
from eawf.surfaces.cli.errors import StateConflict

pytestmark = pytest.mark.unit


def test_a_line_not_held_is_appended_into_a_new_file(tmp_path: Path) -> None:
    """Boundary: the file and its directory do not exist yet."""
    path = tmp_path / "local" / "rows.jsonl"

    assert append_json_line_unless(path, '{"a": 1}', lambda: False) is True
    assert path.read_text(encoding="utf-8") == '{"a": 1}\n'


def test_a_held_line_is_not_appended(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    append_json_line(path, '{"a": 1}')

    assert append_json_line_unless(path, '{"a": 1}', lambda: True) is False
    assert path.read_text(encoding="utf-8") == '{"a": 1}\n'


def test_the_check_runs_while_the_lock_is_held(tmp_path: Path) -> None:
    """A second taker of the lock, asked during the check, times out."""
    path = tmp_path / "rows.jsonl"
    contended: list[bool] = []

    def held() -> bool:
        try:
            with portalock.acquire(path, timeout=0.1):
                contended.append(False)
        except portalock.LockTimeout:
            contended.append(True)
        return False

    assert append_json_line_unless(path, '{"a": 1}', held) is True
    assert contended == [True]


def test_a_lock_that_cannot_be_taken_is_a_state_conflict(tmp_path: Path) -> None:
    """Error path: the check is never asked and nothing is written."""
    path = tmp_path / "rows.jsonl"
    asked: list[int] = []

    def held() -> bool:
        asked.append(1)
        return False

    with portalock.acquire(path, timeout=1.0), pytest.raises(StateConflict):
        append_json_line_unless(path, '{"a": 1}', held, timeout=0.1)
    assert asked == []
    assert not path.exists()


def test_an_error_in_the_check_writes_nothing(tmp_path: Path) -> None:
    """Error path: a check that raises propagates and leaves the file alone."""
    path = tmp_path / "rows.jsonl"

    def held() -> bool:
        raise ValueError("corrupt line")

    with pytest.raises(ValueError, match="corrupt line"):
        append_json_line_unless(path, '{"a": 1}', held)
    assert not path.exists()
