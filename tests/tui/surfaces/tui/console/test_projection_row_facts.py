"""A projected row carries its title and the record it is filed under, through every read.

The native frames nest a Milestone under its Track and name the Task a Run runs, so a
projected row states two facts beyond its status: its own title and the key of the record
it is filed under. Both are read where the document stores them -- a native record's own
fields, an imported record's converted fields -- and both survive a replay, because a
patch moves a status and a revision and nothing else. A row the cutover converted whole
into a native collection, such as a sandbox policy, is readable too.

These are the projection half of UI-020, UI-045, UI-066 and UI-067: the frames those rows
state draw from what this module proves the projection carries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.compute import (
    KeyedPatch,
    PatchEntry,
    build_route_projection,
    row_document,
)
from eawf.kernel.projection.connection import apply_patches
from eawf.kernel.projection.read_models import ReadModelKind
from eawf.kernel.store.tiers import Epoch2Collection

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
ROOT = "eawf://EAWF/EAWF/EAWF"
DIGEST = "sha256:" + "a" * 64


def _project(route: str, document: dict[str, Any], cursor: int = 5) -> Any:
    return build_route_projection(
        route=route, document=document, cursor=cursor, scope_id="EAWF", generated_at=AT
    )


def _native(kind: str, key: str, **extra: Any) -> dict[str, Any]:
    return {"urn": f"{ROOT}/{kind}/{key}", "revision": 2, "status": "ACTIVE", **extra}


@pytest.mark.parametrize(
    ("route", "kind", "extra", "title", "parent"),
    [
        (
            "scope.home",
            "milestone",
            {"title": "Cut rc1", "primary_track_ref": f"{ROOT}/track/TRK-1"},
            "Cut rc1",
            "TRK-1",
        ),
        ("scope.home", "batch", {"milestone_ref": f"{ROOT}/milestone/MLS-1"}, None, "MLS-1"),
        (
            "backlog",
            "task",
            {"intent": "Draw the frames", "batch_ref": f"{ROOT}/batch/BAT-1"},
            "Draw the frames",
            "BAT-1",
        ),
        ("activity", "run", {"scope": {"task_ref": f"{ROOT}/task/TSK-1"}}, None, "TSK-1"),
        ("scope.home", "track", {"title": "  Core  "}, "Core", None),
    ],
)
def test_a_native_row_carries_its_title_and_parent(
    route: str, kind: str, extra: dict[str, Any], title: str | None, parent: str | None
) -> None:
    row = _project(route, {kind: {"K-1": _native(kind, "K-1", **extra)}}).rows[0]
    assert (row.title, row.parent_key) == (title, parent)


@pytest.mark.parametrize(
    "extra",
    [
        {"title": "", "primary_track_ref": ""},
        {"title": "   ", "primary_track_ref": None},
        {"title": 7, "primary_track_ref": 7},
        {},
    ],
)
def test_a_blank_or_mistyped_fact_is_absent_never_invented(extra: dict[str, Any]) -> None:
    row = _project("scope.home", {"milestone": {"M-1": _native("milestone", "M-1", **extra)}}).rows[
        0
    ]
    assert (row.title, row.parent_key) == (None, None)


def test_a_run_whose_scope_is_not_an_object_names_no_task() -> None:
    row = _project("activity", {"run": {"R-1": _native("run", "R-1", scope="task")}}).rows[0]
    assert row.parent_key is None


def _converted(target: str = "sandbox_policy") -> dict[str, Any]:
    return {
        "payload": {
            "origin": {
                "confidence": "exact",
                "kind": "legacy",
                "mapping_basis": "mechanical",
                "source_digest": DIGEST,
                "source_id": "POL-1",
                "source_kind": "sandbox_policies",
                "source_schema_version": "1.20",
                "source_urn": None,
            },
            "payload": {"id": "POL-1"},
            "payload_digest": DIGEST,
            "record_key": "POL-1",
            "source_collection": "sandbox_policies",
            "target": target,
            "urn": None,
        },
        "recorded_at": "2026-09-26T21:14:53+00:00",
        "status": "imported",
    }


def test_a_record_the_cutover_converted_whole_is_readable() -> None:
    row = _project("sandbox.log", {"sandbox_policy": {"POL-1": _converted()}}).rows[0]
    assert (row.key, row.urn, row.revision) == ("POL-1", "legacy:sandbox_policy/POL-1", 1)
    assert row.status.value == "imported"


def test_a_converted_record_of_another_collection_is_refused() -> None:
    with pytest.raises(ValueError, match="is not a sandbox_policy record"):
        _project("sandbox.log", {"sandbox_policy": {"POL-1": _converted("decision")}})


def test_a_converted_record_that_does_not_validate_is_refused() -> None:
    broken = _converted()
    broken["payload"]["payload_digest"] = "not-a-digest"
    with pytest.raises(ValueError, match="does not validate as an imported native record"):
        _project("sandbox.log", {"sandbox_policy": {"POL-1": broken}})


@pytest.mark.parametrize(
    ("kind", "extra"),
    [
        ("milestone", {"title": "Cut rc1", "primary_track_ref": "TRK-1"}),
        ("run", {"scope": {"task_ref": "TSK-1"}}),
        ("task", {"intent": "Draw", "batch_ref": "BAT-1"}),
        ("track", {}),
    ],
)
def test_row_document_spells_a_row_back_as_the_row_it_was_read_from(
    kind: str, extra: dict[str, Any]
) -> None:
    route = {"milestone": "scope.home", "run": "activity", "task": "backlog", "track": "track"}[
        kind
    ]
    row = _project(route, {kind: {"K-1": _native(kind, "K-1", **extra)}}).rows[0]
    again = _project(route, {kind: {"K-1": row_document(row)}}).rows[0]
    assert again == row


def test_a_replayed_patch_keeps_the_title_and_parent_and_digests_as_a_clean_read() -> None:
    milestone = _native(
        "milestone", "MLS-1", title="Cut rc1", primary_track_ref=f"{ROOT}/track/TRK-1"
    )
    held = _project("scope.home", {"milestone": {"MLS-1": milestone}})
    patch = KeyedPatch(
        schema_version="1.0",
        projection_kind=ReadModelKind(held.header.projection_kind),
        routes=("scope.home",),
        scope_id="EAWF",
        canonical_sequence=6,
        entries=(
            PatchEntry(
                key="MLS-1",
                urn=f"{ROOT}/milestone/MLS-1",
                collection=Epoch2Collection.MILESTONE,
                revision=3,
                status="COMPLETED",
            ),
        ),
    )
    replayed = apply_patches(held, [patch], cursor=6, scope_id="EAWF", generated_at=AT)
    row = replayed.rows[0]
    assert (row.title, row.parent_key, row.status.value, row.revision) == (
        "Cut rc1",
        "TRK-1",
        "COMPLETED",
        3,
    )
    clean = _project(
        "scope.home",
        {"milestone": {"MLS-1": {**milestone, "revision": 3, "status": "COMPLETED"}}},
        6,
    )
    assert replayed.digest == clean.digest


@pytest.mark.parametrize(("moved", "drawn"), [("2026-10-20", "2026-10-20"), (None, None)])
def test_a_patch_carrying_a_target_date_moves_or_clears_the_held_one(
    moved: str | None, drawn: str | None
) -> None:
    milestone = _native("milestone", "MLS-1", target_date="2026-10-05")
    held = _project("scope.home", {"milestone": {"MLS-1": milestone}})
    patch = KeyedPatch(
        schema_version="1.0",
        projection_kind=ReadModelKind(held.header.projection_kind),
        routes=("scope.home",),
        scope_id="EAWF",
        canonical_sequence=6,
        entries=(
            PatchEntry(
                key="MLS-1",
                urn=f"{ROOT}/milestone/MLS-1",
                collection=Epoch2Collection.MILESTONE,
                revision=3,
                status="ACTIVE",
                facts={"target_date": moved},
            ),
        ),
    )
    row = apply_patches(held, [patch], cursor=6, scope_id="EAWF", generated_at=AT).rows[0]
    assert row.facts.get("target_date") == drawn


def test_a_patch_for_a_row_the_client_never_held_states_no_title() -> None:
    held = _project("scope.home", {})
    patch = KeyedPatch(
        schema_version="1.0",
        projection_kind=ReadModelKind(held.header.projection_kind),
        routes=("scope.home",),
        scope_id="EAWF",
        canonical_sequence=6,
        entries=(
            PatchEntry(
                key="MLS-9",
                urn=f"{ROOT}/milestone/MLS-9",
                collection=Epoch2Collection.MILESTONE,
                revision=1,
                status="PLANNED",
            ),
        ),
    )
    row = apply_patches(held, [patch], cursor=6, scope_id="EAWF", generated_at=AT).rows[0]
    assert (row.key, row.title, row.parent_key) == ("MLS-9", None, None)


def test_home_binds_the_batches_its_progress_is_counted_from() -> None:
    projection = _project(
        "scope.home",
        {"batch": {"BAT-1": _native("batch", "BAT-1", milestone_ref=f"{ROOT}/milestone/MLS-1")}},
    )
    assert [row.collection for row in projection.rows] == [Epoch2Collection.BATCH]
