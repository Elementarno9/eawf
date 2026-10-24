"""Unit tests for :func:`eawf.runtime.daemon.methods.memory.project_subject`.

The subject is the one project row of the selected generation; the frozen
epoch-1 document beside it is never consulted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, attach_root_context
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.memory import project_subject
from tests._epoch2_helpers import lay_epoch2_tree
from tests.integration._memory_native import QR_DOCUMENT

pytestmark = pytest.mark.unit

_PROJECT = Epoch2Collection.PROJECT.value


def _context(tmp_path: Path, *, state: dict[str, object] | None = None) -> Epoch2RootContext:
    state_path = lay_epoch2_tree(tmp_path, state=state)
    return attach_root_context({}, tree_root=state_path.parent, daemon_wal_dir=tmp_path / "wal")


def _rewrite_projects(tmp_path: Path, projects: dict[str, object] | None) -> None:
    authority = resolve_authority(tmp_path / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(path)
    if projects is None:
        document.pop(_PROJECT)
    else:
        document[_PROJECT] = projects
    write_document(path, document)


def test_the_born_generation_names_the_project(tmp_path: Path) -> None:
    assert project_subject(_context(tmp_path)).endswith("/project/QR")


def test_a_generation_without_a_project_row_is_refused_whatever_the_frozen_document_says(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, state=QR_DOCUMENT)
    _rewrite_projects(tmp_path, None)
    with pytest.raises(DaemonValidationError, match="project_unresolved"):
        project_subject(context)


def test_a_generation_with_two_project_rows_is_refused(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _rewrite_projects(tmp_path, {"QR": {"code": "QR"}, "QS": {"code": "QS"}})
    with pytest.raises(DaemonValidationError, match="project_unresolved"):
        project_subject(context)
