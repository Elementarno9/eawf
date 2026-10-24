"""``ensure_gitignore_block`` writes the managed block whether or not one exists.

A tree from a release that predates the markers has a ``.gitignore`` with
no block, so the refresh-only writer left its machine-local paths
committable. These pin the append, the rewrite, idempotence and the
refusal on damaged markers, each keeping the operator's own lines.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.platform.install.gitignore_writer import ensure_gitignore_block, target_patterns
from eawf.platform.install.managed_block import ManagedBlockError

_BEGIN = "# BEGIN EAWF:gitignore\n"
_END = "# END EAWF:gitignore\n"
_OLD_BLOCK = f"{_BEGIN}CLAUDE.md\n.ea/local/\n/custom/state.json.lock\n{_END}"
_USER_HEAD = "# operator notes\nbuild/\n\n"
_USER_TAIL = "\n# after the block\n!keep.db\n"


def test_appends_a_block_to_a_file_without_one(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(_USER_HEAD, encoding="utf-8")

    plan = ensure_gitignore_block(tmp_path)

    text = gitignore.read_text(encoding="utf-8")
    assert text.startswith(_USER_HEAD)
    assert text.count(_BEGIN) == 1
    assert text.endswith(_END)
    assert ".ea/generations/restore/\n" in text
    assert plan.added == target_patterns(tmp_path.resolve())
    assert plan.stale is False


def test_appends_after_a_single_line_without_a_trailing_newline(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("build/", encoding="utf-8")

    ensure_gitignore_block(tmp_path)

    lines = gitignore.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "build/"
    assert _BEGIN.rstrip("\n") in lines


def test_writes_the_block_where_no_file_exists(tmp_path: Path) -> None:
    ensure_gitignore_block(tmp_path)

    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith(_BEGIN)
    assert text.endswith(_END)


def test_writes_the_block_into_an_empty_file(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_bytes(b"")

    ensure_gitignore_block(tmp_path)

    assert gitignore.read_text(encoding="utf-8").startswith(_BEGIN)


def test_rewrites_an_outdated_block_and_keeps_operator_lines(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(f"{_USER_HEAD}{_OLD_BLOCK}{_USER_TAIL}", encoding="utf-8")

    plan = ensure_gitignore_block(tmp_path)

    text = gitignore.read_text(encoding="utf-8")
    assert plan.stale is True
    assert text.startswith(_USER_HEAD)
    assert text.endswith(_USER_TAIL)
    assert text.count(_BEGIN) == 1
    assert "/custom/state.json.lock\n" in text
    assert ".ea/generations/journal.jsonl\n" in text


def test_is_idempotent_and_leaves_a_current_file_unwritten(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(_USER_HEAD, encoding="utf-8")
    ensure_gitignore_block(tmp_path)
    first = gitignore.read_bytes()
    mtime = gitignore.stat().st_mtime_ns

    again = ensure_gitignore_block(tmp_path)

    assert again.added == ()
    assert again.stale is False
    assert gitignore.read_bytes() == first
    assert gitignore.stat().st_mtime_ns == mtime


@pytest.mark.parametrize(
    "broken",
    [
        f"{_BEGIN}CLAUDE.md\n",
        f"{_END}{_BEGIN}",
        f"{_BEGIN}{_BEGIN}{_END}",
    ],
    ids=["unclosed", "reversed", "doubled"],
)
def test_refuses_damaged_markers_and_writes_nothing(tmp_path: Path, broken: str) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(broken, encoding="utf-8")

    with pytest.raises(ManagedBlockError):
        ensure_gitignore_block(tmp_path)

    assert gitignore.read_text(encoding="utf-8") == broken
