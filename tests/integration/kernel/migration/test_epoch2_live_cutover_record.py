"""The committed record of this repository's live epoch-2 cutover.

The live cut is irreversible past the first native mutation, so its proof
is the record it left behind: the manifest a pinned clone produced at the
frozen revision, the manifest the live apply produced, a re-run that
changed nothing, and the rollback marker. This module reads that record
through a closed model and checks it against itself and against the tree
it describes, so a record whose digests disagree, or a tree that no longer
resolves to the generation the record names, reds here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.state.epoch2.authority import resolve_authority

REPO_ROOT = Path(__file__).resolve().parents[4]
RECORD_PATH = (
    REPO_ROOT / ".ea" / "artifacts" / "evidence" / "2026-09-dev5-live-cutover" / "live-cutover.json"
)

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GenerationId = Annotated[str, Field(pattern=r"^gen-[0-9a-f]{16}$")]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Addressing(_Closed):
    workspace_key: str
    project_key: str
    repository_key: str
    default_track_key: str
    allowlist: str


class Backup(_Closed):
    ts: str
    digest: Digest


class ClonePlan(_Closed):
    manifest_digest: Digest
    approval_digest: Digest
    source_digest: Digest
    idempotence_digest: Digest
    target_rows: int


class LivePlan(ClonePlan):
    applicable: Literal[True]
    unresolved_row_count: Literal[0]


class Apply(_Closed):
    status: Literal["applied"]
    manifest_digest: Digest
    generation_id: GenerationId
    journal_rows: int
    rollback_boundary: Literal["marker_written"]
    target_rows: int


class Reapply(_Closed):
    status: Literal["already-selected"]
    manifest_digest: Digest
    generation_id: GenerationId
    journal_rows: Literal[0]


class Boundary(_Closed):
    boundary: Literal["marker_written"]
    generation_id: GenerationId
    simple_rollback_window: str
    canary_reversible_window: str


class LiveCutoverRecord(_Closed):
    """What the live cut reported, leg by leg, at the frozen revision."""

    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    addressing: Addressing
    backup: Backup
    clone: ClonePlan
    live_plan: LivePlan
    apply: Apply
    reapply: Reapply
    boundary: Boundary


class RecordMismatchError(ValueError):
    """Raised when the record's legs disagree with each other."""


def check_record(record: LiveCutoverRecord) -> None:
    """Raise when the live legs do not reproduce the pinned-clone run.

    Raises:
        RecordMismatchError: Naming the first leg that disagrees.
    """
    manifest = record.clone.manifest_digest
    for leg, digest in (
        ("live plan", record.live_plan.manifest_digest),
        ("apply", record.apply.manifest_digest),
        ("re-apply", record.reapply.manifest_digest),
    ):
        if digest != manifest:
            raise RecordMismatchError(
                f"{leg} manifest {digest} differs from the clone's {manifest}"
            )
    for field in ("approval_digest", "source_digest", "idempotence_digest", "target_rows"):
        if getattr(record.live_plan, field) != getattr(record.clone, field):
            raise RecordMismatchError(f"live plan {field} differs from the clone's")
    generations = {
        record.apply.generation_id,
        record.reapply.generation_id,
        record.boundary.generation_id,
    }
    if len(generations) != 1:
        raise RecordMismatchError(f"legs name different generations: {sorted(generations)}")


def _load(path: Path) -> LiveCutoverRecord:
    return LiveCutoverRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))


def test_live_manifest_matches_the_pinned_clone_and_the_rerun_is_a_no_op() -> None:
    record = _load(RECORD_PATH)

    check_record(record)

    assert record.apply.journal_rows > 0
    assert record.reapply.journal_rows == 0


def test_this_repository_resolves_to_the_recorded_generation() -> None:
    record = _load(RECORD_PATH)

    authority = resolve_authority(REPO_ROOT / ".ea")

    assert authority.epoch == 2
    assert authority.generation_id == record.apply.generation_id


def test_a_mismatched_clone_digest_reds(tmp_path: Path) -> None:
    raw = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
    raw["clone"]["manifest_digest"] = "0" * 64
    planted = tmp_path / "live-cutover.json"
    planted.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RecordMismatchError, match="differs from the clone's"):
        check_record(_load(planted))


def test_a_rerun_that_wrote_rows_is_refused(tmp_path: Path) -> None:
    raw = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
    raw["reapply"]["journal_rows"] = 3
    planted = tmp_path / "live-cutover.json"
    planted.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValidationError):
        _load(planted)
