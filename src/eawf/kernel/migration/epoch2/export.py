"""Reading epoch 1 out whole, without touching it.

The apply is the interesting verb and the dangerous one. This is its
counterweight: a verb that answers "what is in epoch 1" and has no write
path at all, so an operator deciding whether to cut over never has to run
something that could change the thing they are measuring.

The export is total over the declared collections rather than over the
ones that happen to hold rows. A collection reported only when it is
non-empty makes an empty collection indistinguishable from a collection
the exporter forgot, and the whole point of the census is that nothing
leaves the corpus unaccounted for. An empty collection therefore appears
with a row count and the drop proof that justifies its emptiness.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import Field

from eawf.kernel.migration.epoch2.census import SourceCensus
from eawf.kernel.migration.epoch2.dispositions import COLLECTION_DISPOSITION_INDEX
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot

logger = logging.getLogger(__name__)


#: The JSON-RPC method the read-only export is served under.
EPOCH2_EXPORT_METHOD: Final = "migration.epoch2.export"


class Epoch2ExportRequest(StrictMigrationModel):
    """One request for a read-only epoch-1 export.

    Attributes:
        snapshot_root: The staging directory holding the epoch-1 corpus.
            A staging root rather than a live tree, so the export cannot
            be pointed at a directory with a writer behind it.
    """

    snapshot_root: Annotated[str, Field(min_length=1)]


def export_epoch1(request: Epoch2ExportRequest) -> dict[str, Any]:
    """Return every declared epoch-1 collection, writing nothing.

    Args:
        request: The validated export request.

    Returns:
        The export envelope: the pinned source revision, one row per
        declared collection, and the reconciled audit population.

    Raises:
        MigrationCollectionOmittedError: A declared collection is absent.
        MigrationCollectionUnknownError: The source carries a collection
            no disposition declares.
        MigrationSourceUnreadableError: A declared read surface is
            missing or unparseable.
        MigrationDuplicateKeyError: A decoded object repeats a key.
    """
    snapshot = SourceSnapshot.read(Path(request.snapshot_root))
    census = SourceCensus.build(snapshot)
    rows = [
        {
            "source_collection": row.source_collection,
            "disposition": None if row.disposition is None else row.disposition.value,
            "target_collection": row.target_collection,
            "tier": row.tier.value,
            "shape": row.shape.value,
            "row_count": row.row_count,
            "proof_form": None if row.proof_form is None else row.proof_form.value,
        }
        for row in census.collections
    ]
    missing = sorted(set(COLLECTION_DISPOSITION_INDEX) - {row["source_collection"] for row in rows})
    logger.info(
        f"export_epoch1 collections={len(rows)} "
        f"source_digest={census.identity.snapshot_digest[:12]}"
    )
    return {
        "status": "ok",
        "source_digest": census.identity.snapshot_digest,
        "surface_count": len(census.identity.surfaces),
        "collection_count": len(rows),
        "declared_collection_count": len(COLLECTION_DISPOSITION_INDEX),
        "undeclared_collections": missing,
        "total_rows": sum(row["row_count"] or 0 for row in rows),
        "row_failure_count": len(census.row_failures),
        "collections": rows,
        "audits": census.audits.model_dump(mode="json"),
    }


def export_text(payload: dict[str, Any]) -> str:
    """Render one export envelope for a terminal.

    Args:
        payload: The export envelope.

    Returns:
        A header naming the revision, then one line per collection.
    """
    lines = [
        f"epoch1 export: {payload['collection_count']} collections, "
        f"{payload['total_rows']} rows, {payload['surface_count']} read surfaces",
        f"  source digest: {payload['source_digest']}",
    ]
    lines += [
        f"  {row['source_collection']}: {row['row_count']} row(s) -> "
        f"{row['target_collection']} ({row['tier']})"
        for row in payload["collections"]
    ]
    return "\n".join(lines)


__all__ = [
    "EPOCH2_EXPORT_METHOD",
    "Epoch2ExportRequest",
    "export_epoch1",
    "export_text",
]
