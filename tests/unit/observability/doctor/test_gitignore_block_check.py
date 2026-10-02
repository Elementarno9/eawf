"""The doctor check that flags a managed ``.gitignore`` block an older release wrote."""

from __future__ import annotations

from pathlib import Path

from eawf.observability.doctor.checks_gitignore import CHECK_NAME, check_gitignore_block
from eawf.platform.install.gitignore_writer import write_gitignore

_OLD_BLOCK = "# BEGIN EAWF:gitignore\nCLAUDE.md\n.ea/local/\n# END EAWF:gitignore\n"


def test_stale_block_warns_and_names_the_fix(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(_OLD_BLOCK, encoding="utf-8")

    result = check_gitignore_block(workspace=tmp_path)

    assert result.name == CHECK_NAME
    assert result.status == "warn"
    assert result.detail is not None
    assert "eawf sync" in result.detail
    assert ".ea/generations/journal.jsonl" in result.detail


def test_current_block_is_ok(tmp_path: Path) -> None:
    write_gitignore(tmp_path)

    assert check_gitignore_block(workspace=tmp_path).status == "ok"


def test_file_without_a_block_is_ok(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")

    assert check_gitignore_block(workspace=tmp_path).status == "ok"


def test_missing_gitignore_is_ok(tmp_path: Path) -> None:
    assert check_gitignore_block(workspace=tmp_path).status == "ok"


def test_no_workspace_anchor_is_ok() -> None:
    assert check_gitignore_block(workspace=None).status == "ok"


def test_unpaired_markers_warn_without_a_sync_hint(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("# BEGIN EAWF:gitignore\nCLAUDE.md\n", encoding="utf-8")

    result = check_gitignore_block(workspace=tmp_path)

    assert result.status == "warn"
    assert result.detail is not None
    assert "by hand" in result.detail
