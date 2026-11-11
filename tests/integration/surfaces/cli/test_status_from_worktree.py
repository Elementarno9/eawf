"""``eawf status`` run in a linked worktree reports the repository's state, not its copy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.task import Task, TaskPriority, TaskStatus
from eawf.kernel.store.compaction import read_document, write_document
from tests._linked_worktree import repo_with_linked_worktree

pytestmark = pytest.mark.integration

TRACK_URN: Final = "eawf://QR/QR/QR/track/TRK-0001"


def _native_task(key: str) -> dict[str, Any]:
    at = datetime(2026, 10, 1, tzinfo=UTC).isoformat()
    row: dict[str, Any] = Task.model_validate(
        {
            "uid": str(uuid4()),
            "key": key,
            "urn": f"eawf://QR/QR/QR/task/{key}",
            "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
            "revision": 1,
            "created_at": at,
            "updated_at": at,
            "priority": TaskPriority.P1.value,
            "intent": f"deliver {key}",
            "contract_revision": 1,
            "status": TaskStatus.DRAFT.value,
        }
    ).model_dump(mode="json")
    return row


def _advance_main(main: Path) -> None:
    """Activate a Milestone and file two Tasks in the main checkout's tree only."""
    authority = resolve_authority(main / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(document_path)
    document["track"] = {"TRK-0001": {"urn": TRACK_URN, "revision": 1, "status": "ACTIVE"}}
    document["milestone"] = {
        "MLS-0001": {
            "urn": "eawf://QR/QR/QR/milestone/MLS-0001",
            "revision": 1,
            "status": "ACTIVE",
            "primary_track_ref": TRACK_URN,
        }
    }
    document["task"] = {key: _native_task(key) for key in ("QR-0001", "QR-0002")}
    write_document(document_path, document)


def _status(cwd: Path) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if key != "EA_STATE"}
    completed = subprocess.run(
        [sys.executable, "-m", "eawf", "status", "--json"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    payload: dict[str, Any] = json.loads(completed.stdout)
    return payload


def test_status_same_from_worktree(tmp_path: Path) -> None:
    main, linked = repo_with_linked_worktree(tmp_path / "repo")
    _advance_main(main)

    from_main = _status(main)["native"]
    from_linked = _status(linked)["native"]

    assert (from_main["milestone_key"], from_main["open_task_count"]) == ("MLS-0001", 2)
    assert (from_linked["milestone_key"], from_linked["open_task_count"]) == ("MLS-0001", 2)
    assert from_linked == from_main
