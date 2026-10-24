"""Unit tests for :func:`eawf.surfaces.tui.console.onboarding.project_code`."""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.onboarding import project_code
from tests._epoch2_helpers import lay_epoch2_tree
from tests.integration._memory_native import QR_DOCUMENT


def _document_path(repo_root: Path) -> Path:
    authority = resolve_authority(repo_root / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def test_the_generation_names_its_project_code(tmp_path: Path) -> None:
    lay_epoch2_tree(tmp_path, project_code="QS")
    assert project_code(tmp_path) == "QS"


def test_a_tree_with_no_generation_records_no_code(tmp_path: Path) -> None:
    assert project_code(tmp_path) is None


def test_a_generation_without_a_project_row_records_no_code_whatever_the_frozen_document_says(
    tmp_path: Path,
) -> None:
    lay_epoch2_tree(tmp_path, state=QR_DOCUMENT)
    path = _document_path(tmp_path)
    document = read_document(path)
    document.pop(Epoch2Collection.PROJECT.value)
    write_document(path, document)
    assert project_code(tmp_path) is None


def test_a_project_row_that_does_not_validate_records_no_code(tmp_path: Path) -> None:
    lay_epoch2_tree(tmp_path)
    path = _document_path(tmp_path)
    document = read_document(path)
    document[Epoch2Collection.PROJECT.value] = {"QR": {"code": "QR"}}
    write_document(path, document)
    assert project_code(tmp_path) is None


def test_a_generation_that_is_not_json_records_no_code(tmp_path: Path) -> None:
    lay_epoch2_tree(tmp_path)
    _document_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert project_code(tmp_path) is None
