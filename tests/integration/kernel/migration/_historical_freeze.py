"""Freezing the P30/I26 corpus out of this repository's own history.

The built corpora cover shapes. This one covers *reality*: real ids, real
statuses, the real distribution of closed backlog prose the resolution
classifier has to be total over, and the schema signature of a revision
four collections older than the current one. No builder produces that,
because a builder writes what its author expected.

Two reductions are applied to the revision, and both are declared here
rather than done by hand, so a reader can tell exactly what the frozen
corpus is and is not.

**A slice.** The lifecycle collections are cut to the P30-I26 subtree.
The cut is reference-closed: the iter keeps its waves, and the phase's
``iter_ids`` is dropped rather than left pointing at twenty-five iters
the slice does not hold. A slice that kept the pointer would mint
canonical Batch references to records nobody imported, which is the
corrupt-reference corpus, not this one.

**A projection.** Every row keeps the fields :data:`PROJECTED_FIELDS`
names and drops the rest. The dropped fields are payload prose -- wave
criteria, gate bodies, descriptions, dispatch histories -- which is
where the byte weight and the leak risk both live, and which no rule in
the importer resolves against.

What survives both reductions is the part the cutover reasons over. The
counts before and after are recorded in the fixture's provenance file so
the reduction is auditable rather than asserted.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)


#: The revision the corpus is frozen at: the commit that closed P30-I26
#: together with phase P30. Freezing at the close rather than mid-iter is
#: what makes the lifecycle rows terminal and the slice self-consistent.
SOURCE_REVISION: Final = "9edef45aed746a153edbba8f351209ef5fc20793"  # pragma: allowlist secret

#: The iter the slice is taken around, and the phase that holds it.
SLICE_ITER_ID: Final = "P30-I26"
SLICE_PHASE_ID: Final = "P30"

#: The collections the source revision did not carry at all. Epoch 1
#: gained them later, so the frozen document backfills each as an empty
#: container: the census is total over the declared collections, and an
#: absent key would refuse the corpus before a single row was read. Empty
#: is the accurate statement here -- the revision recorded no such rows
#: because the feature did not exist yet.
BACKFILLED_COLLECTIONS: Final[tuple[str, ...]] = (
    "close_attempts",
    "wave_dependency_barriers",
    "wave_dependency_bindings",
    "wave_integrations",
)

#: The fields each collection keeps. A collection absent from this table
#: is copied whole, which is only the small pointer and metadata blocks.
PROJECTED_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "phases": ("id", "scope_id", "status", "title", "opened_at", "closed_at", "audit_id"),
    "iters": (
        "id",
        "phase_id",
        "status",
        "title",
        "opened_at",
        "closed_at",
        "audit_id",
        "wave_ids",
    ),
    "waves": (
        "id",
        "iter_id",
        "status",
        "title",
        "opened_at",
        "closed_at",
        "claimed_at",
        "agent_role",
        "effort_bucket",
        "claim_session_id",
        "worktree_id",
        "commit",
        "deps",
        "blocks",
    ),
    "backlog": (
        "id",
        "scope_id",
        "status",
        "title",
        "created_at",
        "closed_at",
        "priority",
        "resolution",
        "commit",
    ),
    "agent_sessions": ("id", "role", "runtime", "scope_id", "status", "started_at", "ended_at"),
    "worktrees": (
        "id",
        "wave_id",
        "branch",
        "base_branch",
        "path",
        "status",
        "created_at",
        "merged_commit",
    ),
    "estimates": (
        "id",
        "scope_id",
        "display",
        "confidence",
        "reference_class",
        "updated_at",
        "current_store_record_id",
    ),
    "actuals": (
        "id",
        "scope_id",
        "status",
        "updated_at",
        "elapsed_eu",
        "calibration_excluded",
        "current_store_record_id",
    ),
    "audits": ("id", "scope_id", "kind", "status", "verdict", "created_at"),
    "decisions": ("id", "scope_id", "status", "title", "rationale", "created_at"),
    "incidents": ("id", "scope_id", "status", "title", "severity", "cause", "opened_at"),
    "artifacts": ("id", "kind", "uri", "urn", "created_at"),
    "memory_index": (
        "id",
        "scope_id",
        "status",
        "tier",
        "summary",
        "confidence",
        "review_due",
        "store_record_id",
    ),
    "goals": ("id", "scope_id", "status", "title", "summary", "created_at"),
    "sandbox_policies": ("id", "scope_id", "scope_kind", "granted_at"),
}

#: The lifecycle collections the slice cuts, and how each row is selected.
#: ``phases`` keeps one row, ``iters`` keeps one, and ``waves`` keeps the
#: waves of that iter.
SLICED_COLLECTIONS: Final[tuple[str, ...]] = ("phases", "iters", "waves")

#: The measurement collections, which key by the scope they measure and
#: are therefore cut to the scopes the slice still holds.
MEASUREMENT_COLLECTIONS: Final[tuple[str, ...]] = ("estimates", "actuals")

#: The phase field the slice drops, named separately because dropping it
#: is the one edit that changes what the importer would resolve.
PHASE_BATCH_POINTER: Final = "iter_ids"


def read_revision_document(revision: str, *, repo_root: Path) -> dict[str, Any]:
    """Return the epoch-1 state document as of ``revision``.

    Args:
        revision: The commit to read at.
        repo_root: The repository to read it from.

    Returns:
        The decoded document.

    Raises:
        subprocess.CalledProcessError: When the revision is unreachable.
        json.JSONDecodeError: When the blob is not JSON.
    """
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{revision}:.ea/state.json"],
        capture_output=True,
        text=True,
        check=True,
    )
    decoded: dict[str, Any] = json.loads(completed.stdout)
    return decoded


def _project_row(*, collection: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Return one row reduced to the fields ``collection`` keeps."""
    fields = PROJECTED_FIELDS.get(collection)
    if fields is None:
        return dict(row)
    return {field: row[field] for field in fields if field in row and row[field] is not None}


def _sliced_lifecycle(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the three lifecycle collections cut to the P30-I26 subtree.

    Returns:
        The cut collections. The phase keeps every field but its batch
        pointer; the iter keeps its wave pointer, because every wave it
        names survives the cut.
    """
    phases = dict(document["phases"])
    iters = dict(document["iters"])
    waves = dict(document["waves"])
    kept_waves = {
        wave_id: row for wave_id, row in waves.items() if row.get("iter_id") == SLICE_ITER_ID
    }
    phase_row = _project_row(collection="phases", row=phases[SLICE_PHASE_ID])
    phase_row.pop(PHASE_BATCH_POINTER, None)
    iter_row = _project_row(collection="iters", row=iters[SLICE_ITER_ID])
    iter_row["wave_ids"] = sorted(kept_waves)
    return {
        "phases": {SLICE_PHASE_ID: phase_row},
        "iters": {SLICE_ITER_ID: iter_row},
        "waves": {
            wave_id: _project_row(collection="waves", row=row)
            for wave_id, row in sorted(kept_waves.items())
        },
    }


def freeze_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the frozen corpus document for one source revision.

    Args:
        document: The decoded epoch-1 document at the source revision.

    Returns:
        The sliced, projected, backfilled document.
    """
    frozen: dict[str, Any] = {}
    lifecycle = _sliced_lifecycle(document)
    scopes = frozenset(lifecycle["waves"]) | {SLICE_ITER_ID, SLICE_PHASE_ID}
    for collection, value in document.items():
        if collection in lifecycle:
            frozen[collection] = lifecycle[collection]
        elif not isinstance(value, dict):
            frozen[collection] = value
        elif collection in MEASUREMENT_COLLECTIONS:
            frozen[collection] = {
                key: _project_row(collection=collection, row=row)
                for key, row in sorted(value.items())
                if key in scopes
            }
        elif collection in PROJECTED_FIELDS:
            frozen[collection] = {
                key: _project_row(collection=collection, row=row)
                for key, row in sorted(value.items())
            }
        else:
            frozen[collection] = value
    frozen.update({key: {} for key in BACKFILLED_COLLECTIONS})
    return frozen


def row_counts(document: Mapping[str, Any]) -> dict[str, int]:
    """Return the row count of every mapping-valued collection."""
    return {
        collection: len(value)
        for collection, value in sorted(document.items())
        if isinstance(value, dict)
    }


def read_revision_ledger(*, ledger: str, revision: str, repo_root: Path) -> str:
    """Return one store ledger's raw text as of ``revision``.

    Args:
        ledger: The ledger file stem, such as ``audit``.
        revision: The commit to read at.
        repo_root: The repository to read it from.

    Returns:
        The ledger's bytes, decoded as UTF-8.

    Raises:
        subprocess.CalledProcessError: When the path is absent at that
            revision.
    """
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{revision}:.ea/store/{ledger}.jsonl"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def write_frozen_snapshot(root: Path, *, repo_root: Path) -> dict[str, Any]:
    """Freeze the pinned revision into a snapshot tree under ``root``.

    Args:
        root: The snapshot directory to write. Created when absent.
        repo_root: The repository holding the pinned revision.

    Returns:
        The provenance record: the revision, the slice, what was
        backfilled, and the row counts before and after the reduction.

    Raises:
        subprocess.CalledProcessError: When the revision is unreachable.
    """
    from tests.integration.kernel.migration._corpus_builders import (
        AUDIT_LEDGER_FILENAME,
        CONFIG_BODY,
        CONFIG_DIRNAME,
        CONFIG_FILENAME,
        DECISION_LEDGER_FILENAME,
        DOCUMENT_FILENAME,
        REGISTRY_FILENAME,
        STORE_DIRNAME,
        TELEMETRY_BODY,
        TELEMETRY_FILENAME,
    )
    from tests.integration.kernel.migration._corpus_shapes import default_registry

    source = read_revision_document(SOURCE_REVISION, repo_root=repo_root)
    frozen = freeze_document(source)
    (root / STORE_DIRNAME).mkdir(parents=True, exist_ok=True)
    (root / CONFIG_DIRNAME).mkdir(parents=True, exist_ok=True)
    _write_json(root / DOCUMENT_FILENAME, frozen)
    _write_json(root / REGISTRY_FILENAME, default_registry().document())
    _write_json(root / TELEMETRY_FILENAME, TELEMETRY_BODY)
    (root / CONFIG_DIRNAME / CONFIG_FILENAME).write_text(CONFIG_BODY, encoding="utf-8")
    for ledger, filename in (
        ("audit", AUDIT_LEDGER_FILENAME),
        ("decision", DECISION_LEDGER_FILENAME),
    ):
        (root / STORE_DIRNAME / filename).write_text(
            read_revision_ledger(ledger=ledger, revision=SOURCE_REVISION, repo_root=repo_root),
            encoding="utf-8",
        )
    return {
        "source_revision": SOURCE_REVISION,
        "slice": {"phase_id": SLICE_PHASE_ID, "iter_id": SLICE_ITER_ID},
        "backfilled_collections": list(BACKFILLED_COLLECTIONS),
        "dropped_phase_field": PHASE_BATCH_POINTER,
        "row_counts_at_revision": row_counts(source),
        "row_counts_frozen": row_counts(frozen),
    }


def _write_json(path: Path, payload: Any) -> None:
    """Write ``payload`` as sorted, newline-terminated JSON."""
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")


__all__ = [
    "BACKFILLED_COLLECTIONS",
    "MEASUREMENT_COLLECTIONS",
    "PHASE_BATCH_POINTER",
    "PROJECTED_FIELDS",
    "SLICED_COLLECTIONS",
    "SLICE_ITER_ID",
    "SLICE_PHASE_ID",
    "SOURCE_REVISION",
    "freeze_document",
    "read_revision_document",
    "read_revision_ledger",
    "row_counts",
    "write_frozen_snapshot",
]
