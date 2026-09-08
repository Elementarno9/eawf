"""The disposition table: what happens to every epoch-1 collection.

Every source collection receives exactly one disposition, and the table
is total over the epoch-1 document. Totality is the point: a collection
with no row here is a collection nobody decided the fate of, and the
census fails rather than letting it drift through the cutover unimported.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)


class Disposition(StrEnum):
    """The five fates an epoch-1 collection or row can receive."""

    NATIVE_CONVERSION = "native_conversion"
    IMMUTABLE_LEGACY_RECORD = "immutable_legacy_record"
    SPLIT_CONVERSION = "split_conversion"
    DERIVED_PROJECTION = "derived_projection"
    EXPLICIT_DROP = "explicit_drop"


class StorageTier(StrEnum):
    """Where an imported record is written.

    ``DOCUMENT_OR_LEDGER`` is the tier of a lifecycle collection whose
    rows split by terminality: work in flight stays in the document and
    a terminal row lands directly in its ledger, so the cutover never
    reconstructs the monolith it replaces.
    """

    DOCUMENT = "document"
    LEDGER = "ledger"
    DOCUMENT_OR_LEDGER = "document|ledger"
    DERIVED = "derived"
    METADATA = "metadata"
    NONE = "-"


class DropProofForm(StrEnum):
    """The two proofs an ``explicit_drop`` admits, never merged.

    They assert different things about the source. ``ZERO_ROWS`` says a
    container exists and holds nothing. ``NULL_NOT_EMPTY`` says the key
    exists and no container was ever constructed, so there is nothing to
    count -- reporting ``0 rows`` for it would assert a container the
    source does not have.
    """

    ZERO_ROWS = "zero_rows"
    NULL_NOT_EMPTY = "null_not_empty"


class CollectionDisposition(StrictMigrationModel):
    """The default disposition of one epoch-1 top-level collection."""

    source_collection: Annotated[str, Field(min_length=1, max_length=64)]
    disposition: Disposition | None
    target_collection: Annotated[str, Field(min_length=1, max_length=64)]
    tier: StorageTier
    note: Annotated[str, Field(max_length=300)]


def _row(
    source_collection: str,
    disposition: Disposition | None,
    target_collection: str,
    tier: StorageTier,
    note: str,
) -> CollectionDisposition:
    """Build one disposition row (positional to keep the table readable)."""
    return CollectionDisposition(
        source_collection=source_collection,
        disposition=disposition,
        target_collection=target_collection,
        tier=tier,
        note=note,
    )


_N = Disposition.NATIVE_CONVERSION
_L = Disposition.IMMUTABLE_LEGACY_RECORD
_S = Disposition.SPLIT_CONVERSION
_D = Disposition.DERIVED_PROJECTION
_X = Disposition.EXPLICIT_DROP

# A ``disposition`` of None marks a document-metadata key that carries no
# rows and is replaced wholesale by the target schema header.
COLLECTION_DISPOSITIONS: tuple[CollectionDisposition, ...] = (
    _row(
        "phases",
        _N,
        "milestone",
        StorageTier.DOCUMENT_OR_LEDGER,
        "imported Milestone; acceptance never inferred",
    ),
    _row(
        "iters",
        _N,
        "batch",
        StorageTier.DOCUMENT_OR_LEDGER,
        "imported DeliveryBatch with legacy_slice; merge never inferred",
    ),
    _row(
        "waves",
        _N,
        "task",
        StorageTier.DOCUMENT_OR_LEDGER,
        "imported Task; integration never inferred; attempt entries mint Runs",
    ),
    _row(
        "backlog",
        _N,
        "task",
        StorageTier.DOCUMENT_OR_LEDGER,
        "DRAFT head of Task; closed rows classified by resolution",
    ),
    _row(
        "agent_sessions",
        _S,
        "run|legacy",
        StorageTier.LEDGER,
        "Run plus report when a claim resolves, else immutable legacy record",
    ),
    _row(
        "worktrees",
        _L,
        "legacy",
        StorageTier.LEDGER,
        "operational record; never an active native lease",
    ),
    _row(
        "estimates",
        _N,
        "estimate",
        StorageTier.LEDGER,
        "re-pointed at the imported Task; never recalibrated",
    ),
    _row(
        "actuals",
        _N,
        "actual",
        StorageTier.LEDGER,
        "re-pointed at the imported Task; quality marker verbatim",
    ),
    _row(
        "audits", _N, "audit", StorageTier.LEDGER, "reconciled against the audit store as a union"
    ),
    _row("decisions", _N, "decision", StorageTier.LEDGER, "already a ledger kind in epoch 1"),
    _row("incidents", _N, "incident", StorageTier.LEDGER, "already a ledger kind in epoch 1"),
    # The two tier rows the epoch-1 corpus needs before the tier table is
    # total; without them the corpus fails tier validation at cutover
    # rather than at review.
    _row(
        "artifacts",
        _N,
        "artifact",
        StorageTier.LEDGER,
        "byte-identical artifact plus reindexed aliases; the index over it is derived",
    ),
    _row(
        "memory_index",
        _N,
        "memory",
        StorageTier.LEDGER,
        "append-only projection of a durable note; superseded, never edited",
    ),
    _row(
        "goals",
        _N,
        "track_outcome",
        StorageTier.DOCUMENT,
        "Track outcome metric; Track ownership never inferred",
    ),
    _row(
        "sandbox_policies",
        _N,
        "sandbox_policy",
        StorageTier.DOCUMENT,
        "SandboxPolicy plus its policy references",
    ),
    _row("project", _N, "project", StorageTier.DOCUMENT, "Project entity"),
    _row("current", _D, "current", StorageTier.DOCUMENT, "live pointers over work in flight"),
    _row(
        "dispatch_paused", _D, "dispatch_paused", StorageTier.DERIVED, "recipe, version and digest"
    ),
    _row("fleet_run", _D, "fleet_run", StorageTier.DERIVED, "recipe, version and digest"),
    _row(
        "health",
        _D,
        "health_view",
        StorageTier.DERIVED,
        "legacy ok/warn/fail map onto passed/warn/failed",
    ),
    _row("indexes", _D, "indexes", StorageTier.DERIVED, "rebuilt from the ledgers on first use"),
    _row(
        "plugins",
        _L,
        "legacy",
        StorageTier.LEDGER,
        "installed identity plus source digest; certification never inferred",
    ),
    _row(
        "wave_integrations",
        _X,
        "-",
        StorageTier.NONE,
        "no epoch-2 successor; drop requires a proof form",
    ),
    _row(
        "wave_dependency_bindings",
        _X,
        "-",
        StorageTier.NONE,
        "no epoch-2 successor; drop requires a proof form",
    ),
    _row(
        "wave_dependency_barriers",
        _X,
        "-",
        StorageTier.NONE,
        "no epoch-2 successor; drop requires a proof form",
    ),
    _row(
        "close_attempts",
        _X,
        "-",
        StorageTier.NONE,
        "no epoch-2 successor; drop requires a proof form",
    ),
    _row(
        "tracks",
        _N,
        "track",
        StorageTier.DOCUMENT,
        "PLANNED and DEFERRED are dropped by census, never folded into ACTIVE",
    ),
    _row(
        "outcomes",
        _N,
        "track_outcome",
        StorageTier.DOCUMENT,
        "outcome metrics where semantics match",
    ),
    _row(
        "claims",
        _N,
        "claim",
        StorageTier.LEDGER,
        "evidence rungs carried; a certifying rung never invented",
    ),
    _row("hypotheses", _N, "hypothesis", StorageTier.LEDGER, "payload, links and provenance"),
    _row(
        "open_questions", _N, "open_question", StorageTier.DOCUMENT, "payload, links and provenance"
    ),
    _row("mcp_servers", _N, "capability", StorageTier.DOCUMENT, "narrowed to registered entries"),
    _row(
        "mcp_grants",
        _N,
        "tool_authority",
        StorageTier.DOCUMENT,
        "never a grant wider than the source recorded",
    ),
    _row(
        "workspace",
        _N,
        "workspace",
        StorageTier.DOCUMENT,
        "resolved from the registry as a cutover precondition",
    ),
    _row(
        "schema_version", None, "-", StorageTier.METADATA, "replaced by the target schema version"
    ),
    _row("scope_kind", None, "-", StorageTier.METADATA, "document metadata"),
    _row("updated_at", None, "-", StorageTier.METADATA, "document metadata"),
    _row("urn", None, "-", StorageTier.METADATA, "document metadata"),
)


COLLECTION_DISPOSITION_INDEX: dict[str, CollectionDisposition] = {
    row.source_collection: row for row in COLLECTION_DISPOSITIONS
}


def drop_proof_form(value: Any) -> DropProofForm:
    """Return the ``explicit_drop`` proof form the source value carries.

    Args:
        value: The raw JSON value the source held under the collection
            key -- ``None`` for a key that serialized as JSON ``null``,
            or an empty container.

    Returns:
        :attr:`DropProofForm.NULL_NOT_EMPTY` when the source held
        ``null``, :attr:`DropProofForm.ZERO_ROWS` when it held a present
        but empty container.

    Raises:
        ValueError: When ``value`` is a non-empty container, which has
            rows to import and therefore cannot reach ``explicit_drop``,
            or a scalar, which is neither proof form.
    """
    if value is None:
        return DropProofForm.NULL_NOT_EMPTY
    if isinstance(value, (dict, list, tuple, set)):
        if len(value) == 0:
            return DropProofForm.ZERO_ROWS
        raise ValueError(
            f"explicit_drop needs a proof form, but the source holds {len(value)} rows"
        )
    raise ValueError(f"explicit_drop admits no proof form for a {type(value).__name__} value")


def disposition_rule_payload() -> dict[str, Any]:
    """Return the digestable form of the disposition and drop-proof tables."""
    return {
        "collections": [row.model_dump(mode="json") for row in COLLECTION_DISPOSITIONS],
        "dispositions": sorted(item.value for item in Disposition),
        "drop_proof_forms": sorted(item.value for item in DropProofForm),
    }
