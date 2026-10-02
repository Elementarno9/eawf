"""Unit tests for :func:`eawf.runtime.daemon.methods.memory.project_subject`'s frozen fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.daemon.epoch2_root import attach_root_context
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.memory import project_subject
from tests._epoch2_helpers import lay_epoch2_tree
from tests.integration._memory_native import QR_DOCUMENT

pytestmark = pytest.mark.unit


def _subject(tmp_path: Path, frozen: dict[str, Any]) -> str:
    state_path = lay_epoch2_tree(tmp_path, state=frozen)
    context = attach_root_context({}, tree_root=state_path.parent, daemon_wal_dir=tmp_path / "wal")
    return project_subject(context)


def test_the_frozen_document_names_the_project(tmp_path: Path) -> None:
    assert _subject(tmp_path, QR_DOCUMENT).endswith("/project/QR")


def test_a_frozen_document_that_does_not_validate_is_refused(tmp_path: Path) -> None:
    frozen = {**QR_DOCUMENT, "project": {**QR_DOCUMENT["project"], "smuggled": "x"}}
    with pytest.raises(DaemonValidationError, match="project_unresolved"):
        _subject(tmp_path, frozen)


def test_a_frozen_document_without_a_project_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DaemonValidationError, match="project_unresolved"):
        _subject(tmp_path, {**QR_DOCUMENT, "project": None})
