"""Where every epoch-2 collection's bytes are allowed to live.

Epoch 1 kept one document that every mutation rewrote in full, so a
collection's storage was whatever the writer happened to do. Epoch 2
declares it instead: each collection names exactly one tier, and the
table is compiled at import so a collection nobody assigned a tier to,
or one assigned two, is a startup failure rather than a byte that lands
somewhere plausible and wrong.

The five tiers differ in who may rewrite them and whether they are worth
keeping.

``document`` is the compare-and-swap tree of work in flight. It is
rewritten in full on every mutation, which is affordable only because
nothing terminal stays in it.

``ledger`` is append-only history. A line, once written, is never
rewritten, reordered or removed; a correction appends a line naming the
digest of the line it supersedes.

``local_store`` is machine-local and never committed: a clone starts
empty and restores it explicitly.

``firehose`` is the raw agent-output stream. It is never committed and
never compacted, because it is neither evidence nor state.

``derived`` is regenerable from the ledgers alone, so deleting it costs
nothing but the rebuild.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.identity import EntityKind


class StorageTier(StrEnum):
    """The five places an epoch-2 collection's bytes may live."""

    DOCUMENT = "document"
    LEDGER = "ledger"
    LOCAL_STORE = "local_store"
    FIREHOSE = "firehose"
    DERIVED = "derived"


class Epoch2Collection(StrEnum):
    """Every collection an epoch-2 tree holds, spelled as its storage name.

    A collection is a storage slot, not an entity: several entity kinds
    may share one, and a kind whose records are addressed under another
    collection has no row of its own.
    """

    WORKSPACE = "workspace"
    PROJECT = "project"
    REPOSITORY = "repository"
    TRACK = "track"
    CAMPAIGN = "campaign"
    CURRENT = "current"
    TRACK_OUTCOME = "track_outcome"
    SANDBOX_POLICY = "sandbox_policy"
    CAPABILITY = "capability"
    TOOL_AUTHORITY = "tool_authority"
    PERMISSION = "permission"
    PENDING_ACTION = "pending_action"
    OPEN_QUESTION = "open_question"
    MILESTONE = "milestone"
    BATCH = "batch"
    TASK = "task"
    RUN = "run"
    RELEASE = "release"
    EVIDENCE = "evidence"
    RECEIPT = "receipt"
    CLAIM = "claim"
    CAMPAIGN_FINDING = "campaign_finding"
    HYPOTHESIS = "hypothesis"
    AUDIT = "audit"
    DECISION = "decision"
    INCIDENT = "incident"
    ESTIMATE = "estimate"
    ACTUAL = "actual"
    ARTIFACT = "artifact"
    MEMORY = "memory"
    LEGACY = "legacy"
    TELEMETRY = "telemetry"
    DRAFT = "draft"
    EVENT = "event"
    INDEXES = "indexes"
    DISPATCH_PAUSED = "dispatch_paused"
    FLEET_RUN = "fleet_run"
    HEALTH_VIEW = "health_view"


class TierTableError(ValueError):
    """The tier table is not a total function over the collections."""


class TierAssignment(BaseModel):
    """One collection's declared tier, with the reason it is that tier.

    ``tier`` is required and has no default: a row that omits it is a
    collection nobody decided the storage of, and it fails validation
    rather than defaulting into the document the tiering exists to keep
    small.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    collection: Epoch2Collection
    tier: StorageTier
    note: Annotated[str, Field(min_length=1, max_length=200)]


def _at(collection: Epoch2Collection, tier: StorageTier, note: str) -> TierAssignment:
    """Build one tier row (positional to keep the table readable)."""
    return TierAssignment(collection=collection, tier=tier, note=note)


_DOC: Final = StorageTier.DOCUMENT
_LED: Final = StorageTier.LEDGER
_LOC: Final = StorageTier.LOCAL_STORE
_FIRE: Final = StorageTier.FIREHOSE
_DER: Final = StorageTier.DERIVED

TIER_ASSIGNMENTS: Final[tuple[TierAssignment, ...]] = (
    _at(Epoch2Collection.WORKSPACE, _DOC, "one row per tree; never terminates"),
    _at(Epoch2Collection.PROJECT, _DOC, "one row per tree; never terminates"),
    _at(Epoch2Collection.REPOSITORY, _DOC, "integration target; never terminates"),
    _at(Epoch2Collection.TRACK, _DOC, "a Track has no completion, so it never compacts"),
    _at(Epoch2Collection.CAMPAIGN, _DOC, "research aggregate read while it runs"),
    _at(Epoch2Collection.CURRENT, _DOC, "live pointers over work in flight"),
    _at(Epoch2Collection.TRACK_OUTCOME, _DOC, "outcome metric read on every render"),
    _at(Epoch2Collection.SANDBOX_POLICY, _DOC, "policy consulted before every dispatch"),
    _at(Epoch2Collection.CAPABILITY, _DOC, "registered server; the register is the state"),
    _at(Epoch2Collection.TOOL_AUTHORITY, _DOC, "grant consulted before every tool call"),
    _at(Epoch2Collection.PERMISSION, _DOC, "grant consulted before every mutation"),
    _at(Epoch2Collection.PENDING_ACTION, _DOC, "queued by definition; leaves on resolution"),
    _at(Epoch2Collection.OPEN_QUESTION, _DOC, "open by definition; answered questions compact"),
    _at(Epoch2Collection.MILESTONE, _LED, "accepted or abandoned Milestones leave the document"),
    _at(Epoch2Collection.BATCH, _LED, "merged or abandoned Batches leave the document"),
    _at(Epoch2Collection.TASK, _LED, "the collection that made the epoch-1 document 5.9 MB"),
    _at(Epoch2Collection.RUN, _LED, "a finished Run is history, never a live lease"),
    _at(Epoch2Collection.RELEASE, _LED, "published releases are the record of what shipped"),
    _at(Epoch2Collection.EVIDENCE, _LED, "an observation is immutable once recorded"),
    _at(Epoch2Collection.RECEIPT, _LED, "a gate receipt proves one run and never changes"),
    _at(Epoch2Collection.CLAIM, _LED, "a claim plus the rungs that certified it"),
    _at(Epoch2Collection.CAMPAIGN_FINDING, _LED, "a finding is immutable once published"),
    _at(Epoch2Collection.HYPOTHESIS, _LED, "payload, links and provenance, appended"),
    _at(Epoch2Collection.AUDIT, _LED, "already an append-only kind in epoch 1"),
    _at(Epoch2Collection.DECISION, _LED, "already an append-only kind in epoch 1"),
    _at(Epoch2Collection.INCIDENT, _LED, "already an append-only kind in epoch 1"),
    _at(Epoch2Collection.ESTIMATE, _LED, "an estimate is a dated statement, never edited"),
    _at(Epoch2Collection.ACTUAL, _LED, "a measurement is a dated statement, never edited"),
    _at(Epoch2Collection.ARTIFACT, _LED, "artifact rows verbatim; the index over them is derived"),
    _at(Epoch2Collection.MEMORY, _LED, "append-only projection of a durable note"),
    _at(Epoch2Collection.LEGACY, _LED, "epoch-1 rows with no native successor, kept verbatim"),
    _at(Epoch2Collection.TELEMETRY, _LOC, "machine-local measurement history; restored explicitly"),
    _at(Epoch2Collection.DRAFT, _LOC, "machine-local working drafts, never committed"),
    _at(Epoch2Collection.EVENT, _FIRE, "raw agent output; neither evidence nor state"),
    _at(Epoch2Collection.INDEXES, _DER, "offset indexes rebuilt from the ledgers"),
    _at(Epoch2Collection.DISPATCH_PAUSED, _DER, "recipe, version and digest"),
    _at(Epoch2Collection.FLEET_RUN, _DER, "recipe, version and digest"),
    _at(Epoch2Collection.HEALTH_VIEW, _DER, "projection over runs and receipts"),
)


def compile_tier_table(
    assignments: Iterable[TierAssignment],
    *,
    collections: Iterable[Epoch2Collection] = Epoch2Collection,
) -> Mapping[Epoch2Collection, StorageTier]:
    """Compile *assignments* into a total, single-valued collection-to-tier map.

    Args:
        assignments: The declared rows, in table order.
        collections: The collections the table must cover.

    Returns:
        A read-only mapping from every collection to its one tier.

    Raises:
        TierTableError: A collection is declared twice, so its tier is
            ambiguous, or a collection has no row at all, so its bytes
            have no declared home.
    """
    table: dict[Epoch2Collection, StorageTier] = {}
    for assignment in assignments:
        declared = table.get(assignment.collection)
        if declared is not None:
            raise TierTableError(
                f"collection {assignment.collection.value!r} is declared twice: "
                f"{declared.value} then {assignment.tier.value}"
            )
        table[assignment.collection] = assignment.tier
    missing = sorted(item.value for item in collections if item not in table)
    if missing:
        raise TierTableError(f"no tier declared for {', '.join(missing)}")
    return MappingProxyType(table)


TIER_TABLE: Final[Mapping[Epoch2Collection, StorageTier]] = compile_tier_table(TIER_ASSIGNMENTS)

#: The collections whose bytes are append-only history, in table order.
LEDGER_COLLECTIONS: Final[tuple[Epoch2Collection, ...]] = tuple(
    collection for collection, tier in TIER_TABLE.items() if tier is StorageTier.LEDGER
)


def _compile_entity_collections(
    mapping: Mapping[EntityKind, Epoch2Collection],
) -> Mapping[EntityKind, Epoch2Collection]:
    """Return *mapping* after checking it covers every entity kind.

    Args:
        mapping: The declared kind-to-collection routes.

    Returns:
        A read-only mapping from every entity kind to its collection.

    Raises:
        TierTableError: An entity kind has no collection, so its records
            would have no declared tier.
    """
    missing = sorted(kind.value for kind in EntityKind if kind not in mapping)
    if missing:
        raise TierTableError(f"no collection declared for entity kind {', '.join(missing)}")
    return MappingProxyType(dict(mapping))


#: Which collection holds the records of each addressable entity kind.
ENTITY_COLLECTIONS: Final[Mapping[EntityKind, Epoch2Collection]] = _compile_entity_collections(
    {
        EntityKind.WORKSPACE: Epoch2Collection.WORKSPACE,
        EntityKind.PROJECT: Epoch2Collection.PROJECT,
        EntityKind.REPOSITORY: Epoch2Collection.REPOSITORY,
        EntityKind.TRACK: Epoch2Collection.TRACK,
        EntityKind.CAMPAIGN: Epoch2Collection.CAMPAIGN,
        EntityKind.MILESTONE: Epoch2Collection.MILESTONE,
        EntityKind.BATCH: Epoch2Collection.BATCH,
        EntityKind.TASK: Epoch2Collection.TASK,
        EntityKind.RUN: Epoch2Collection.RUN,
        EntityKind.RELEASE: Epoch2Collection.RELEASE,
        EntityKind.EVIDENCE: Epoch2Collection.EVIDENCE,
        EntityKind.CAMPAIGN_FINDING: Epoch2Collection.CAMPAIGN_FINDING,
        EntityKind.CLAIM: Epoch2Collection.CLAIM,
        EntityKind.QUESTION: Epoch2Collection.OPEN_QUESTION,
        EntityKind.RECEIPT: Epoch2Collection.RECEIPT,
        EntityKind.PENDING_ACTION: Epoch2Collection.PENDING_ACTION,
        EntityKind.PERMISSION: Epoch2Collection.PERMISSION,
        EntityKind.LEGACY_RECORD: Epoch2Collection.LEGACY,
    }
)


def tier_for(collection: Epoch2Collection) -> StorageTier:
    """Return the one tier *collection* is declared at.

    Args:
        collection: The collection to resolve.

    Returns:
        The declared tier.

    Raises:
        KeyError: The collection has no row, which the compiled table
            makes unreachable for a declared member.
    """
    return TIER_TABLE[collection]


__all__ = [
    "ENTITY_COLLECTIONS",
    "LEDGER_COLLECTIONS",
    "TIER_ASSIGNMENTS",
    "TIER_TABLE",
    "Epoch2Collection",
    "StorageTier",
    "TierAssignment",
    "TierTableError",
    "compile_tier_table",
    "tier_for",
]
