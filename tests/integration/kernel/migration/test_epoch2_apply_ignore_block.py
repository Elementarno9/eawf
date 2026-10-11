"""The cutover writes the repository's managed ``.gitignore`` block.

A tree initialised by an older release carries the block that release
wrote, which knows nothing of the generation tree the apply is about to
create, and a tree from a release that predates the markers carries no
block at all. Writing it as part of the apply means the first status
after a cutover lists only what a clone needs; the operator's own lines
around the block are never touched, and a repeat apply changes nothing.
"""

from __future__ import annotations

from pathlib import Path

from tests.integration.kernel.migration._cutover_harness import (
    APPLIED_AT,
    apply_once,
    declared_canary,
    staged_corpus,
)

_OLD_BLOCK = "# BEGIN EAWF:gitignore\nCLAUDE.md\n.ea/local/\n# END EAWF:gitignore\n"
_USER_HEAD = "build/\n\n"
_USER_TAIL = "\n!keep.db\n"
_BEGIN = "# BEGIN EAWF:gitignore\n"
_END = "# END EAWF:gitignore\n"
_MACHINE_LOCAL = (
    ".ea/generations/gen-*/local/\n",
    ".ea/generations/gen-*/indexes/\n",
    ".ea/generations/journal.jsonl\n",
    ".ea/generations/restore/\n",
)


def test_apply_rewrites_an_outdated_block_and_keeps_operator_lines(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(f"{_USER_HEAD}{_OLD_BLOCK}{_USER_TAIL}", encoding="utf-8")
    corpus = staged_corpus(tmp_path)
    target_root = declared_canary(tmp_path / ".ea")

    apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)

    text = gitignore.read_text(encoding="utf-8")
    assert text.startswith(_USER_HEAD)
    assert text.endswith(_USER_TAIL)
    assert ".ea/generations/journal.jsonl\n" in text
    assert ".ea/generations/restore/\n" in text

    refreshed = gitignore.read_bytes()
    again = apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)

    assert again.applied is False
    assert gitignore.read_bytes() == refreshed


def test_apply_appends_the_block_to_an_unmarked_gitignore(tmp_path: Path) -> None:
    """A pre-marker tree gains the block after its own lines, exactly once."""
    gitignore = tmp_path / ".gitignore"
    operator = "build/\n.venv/\n"
    gitignore.write_text(operator, encoding="utf-8")
    corpus = staged_corpus(tmp_path)
    target_root = declared_canary(tmp_path / ".ea")

    apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)

    text = gitignore.read_text(encoding="utf-8")
    assert text.startswith(operator)
    assert text.count(_BEGIN) == 1
    assert text.count(_END) == 1
    block = text[text.index(_BEGIN) :]
    for line in _MACHINE_LOCAL:
        assert line in block

    written = gitignore.read_bytes()
    again = apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)

    assert again.applied is False
    assert gitignore.read_bytes() == written


def test_apply_creates_the_block_when_the_repository_has_no_gitignore(tmp_path: Path) -> None:
    """No file at all is the empty unmarked case: the block alone is written."""
    corpus = staged_corpus(tmp_path)
    target_root = declared_canary(tmp_path / ".ea")

    apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)

    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith(_BEGIN)
    assert text.endswith(_END)
    for line in _MACHINE_LOCAL:
        assert line in text


def test_apply_keeps_an_up_to_date_marked_block_byte_for_byte(tmp_path: Path) -> None:
    """A block this release already wrote is left exactly as it is."""
    corpus = staged_corpus(tmp_path)
    target_root = declared_canary(tmp_path / ".ea")
    gitignore = tmp_path / ".gitignore"
    apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)
    gitignore.write_bytes(b"dist/\n\n" + gitignore.read_bytes() + b"\n*.tmp\n")
    before = gitignore.read_bytes()
    mtime = gitignore.stat().st_mtime_ns

    apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)

    assert gitignore.read_bytes() == before
    assert gitignore.stat().st_mtime_ns == mtime


def test_apply_leaves_a_gitignore_with_damaged_markers_untouched(tmp_path: Path) -> None:
    """Two begin markers are a hand-repair case; the cutover still lands."""
    gitignore = tmp_path / ".gitignore"
    damaged = f"{_BEGIN}CLAUDE.md\n{_BEGIN}{_END}"
    gitignore.write_text(damaged, encoding="utf-8")
    corpus = staged_corpus(tmp_path)
    target_root = declared_canary(tmp_path / ".ea")

    result = apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)

    assert result.applied is True
    assert gitignore.read_text(encoding="utf-8") == damaged
