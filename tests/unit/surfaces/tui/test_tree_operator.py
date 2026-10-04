"""A console launched without ``--actor`` acts as the operator the tree's Tracks name.

The tree is a temporary epoch-2 canary whose selected generation document is written
directly, so the one thing under test is the read of the Tracks' owners.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    GENERATIONS_DIRNAME,
    MARKER_FILENAME,
)
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import RootAuthority, resolve_authority
from eawf.surfaces.tui.console.operations import Operator
from eawf.surfaces.tui.launch import tree_operator

_GENERATION_ID = "gen-" + "ab" * 8
_DIGEST = "cd" * 32


def _authority(root: Path) -> RootAuthority:
    """Declare ``root`` an epoch-2 canary tree and resolve it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / CANARY_DECLARATION_FILENAME).write_text(
        json.dumps({"disposable": True, "declared_by": "test", "purpose": "tree operator"})
    )
    (root / GENERATIONS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (root / GENERATIONS_DIRNAME / MARKER_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "epoch": 2,
                "generation_id": _GENERATION_ID,
                "manifest_digest": _DIGEST,
                "generation_digest": _DIGEST,
                "written_at": "2026-09-26T00:00:00Z",
            }
        )
    )
    authority = resolve_authority(root)
    assert authority.epoch == 2
    return authority


def _write(authority: RootAuthority, document: Any) -> None:
    assert authority.target is not None and authority.generation_id is not None
    folder = authority.target.generation_path(authority.generation_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / GENERATION_DOCUMENT).write_text(json.dumps(document))


def _track(owner: str, kind: str = "operator") -> dict[str, Any]:
    return {"policy": {"ownership_principal": {"principal_kind": kind, "principal_id": owner}}}


def test_one_owning_operator_is_who_the_console_acts_as(tmp_path: Path) -> None:
    authority = _authority(tmp_path / ".ea")
    _write(authority, {"track": {"TRK-A": _track("OP-0001"), "TRK-B": _track("OP-0001")}})

    assert tree_operator(authority) == Operator(principal="OP-0001")


def test_a_single_track_names_its_owner(tmp_path: Path) -> None:
    authority = _authority(tmp_path / ".ea")
    _write(authority, {"track": {"TRK-A": _track("OP-0002")}})

    assert tree_operator(authority) == Operator(principal="OP-0002")


@pytest.mark.parametrize(
    "tracks",
    [
        {},
        {"TRK-A": _track("OP-0001"), "TRK-B": _track("OP-0002")},
        {"TRK-A": _track("TEAM-CORE", kind="team")},
    ],
    ids=["no-track", "two-operators", "team-owner"],
)
def test_no_single_owning_operator_names_no_one(tmp_path: Path, tracks: dict[str, Any]) -> None:
    authority = _authority(tmp_path / ".ea")
    _write(authority, {"track": tracks})

    assert tree_operator(authority) is None


def test_a_document_with_no_track_collection_names_no_one(tmp_path: Path) -> None:
    authority = _authority(tmp_path / ".ea")
    _write(authority, {"task": {}})

    assert tree_operator(authority) is None


def test_a_missing_document_names_no_one(tmp_path: Path) -> None:
    assert tree_operator(_authority(tmp_path / ".ea")) is None


def test_a_document_that_is_not_an_object_names_no_one(tmp_path: Path) -> None:
    authority = _authority(tmp_path / ".ea")
    _write(authority, ["not", "an", "object"])

    assert tree_operator(authority) is None


def test_a_track_with_no_policy_names_no_one(tmp_path: Path) -> None:
    authority = _authority(tmp_path / ".ea")
    _write(authority, {"track": {"TRK-A": {"title": "no policy"}}})

    assert tree_operator(authority) is None


def test_a_malformed_principal_key_names_no_one(tmp_path: Path) -> None:
    authority = _authority(tmp_path / ".ea")
    _write(authority, {"track": {"TRK-A": _track("someone@example.test")}})

    assert tree_operator(authority) is None
