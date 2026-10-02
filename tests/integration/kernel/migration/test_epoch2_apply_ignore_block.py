"""The cutover refreshes the repository's managed ``.gitignore`` block.

A tree initialised by an older release carries the block that release
wrote, which knows nothing of the generation tree the apply is about to
create. Refreshing it as part of the apply means the first ``git status``
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


def test_apply_writes_no_gitignore_into_a_repository_without_a_block(tmp_path: Path) -> None:
    corpus = staged_corpus(tmp_path)
    target_root = declared_canary(tmp_path / ".ea")

    apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)

    assert not (tmp_path / ".gitignore").exists()
