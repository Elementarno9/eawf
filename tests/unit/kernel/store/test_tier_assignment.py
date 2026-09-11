"""The tier table is total, single-valued, and agrees with the importer.

Totality is asserted against a fixture that spells the table a second
time. A test that rebuilt the expectation from the table under test would
pass no matter what the table said, so the fixture is written out by hand
and a row that changes tier has to change in both places.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.identity import EntityKind
from eawf.kernel.migration.epoch2.dispositions import COLLECTION_DISPOSITIONS
from eawf.kernel.migration.epoch2.dispositions import StorageTier as MigrationTier
from eawf.kernel.store.paths import index_path, is_epoch2_ledger_path, ledger_path
from eawf.kernel.store.tiers import (
    ENTITY_COLLECTIONS,
    TIER_ASSIGNMENTS,
    TIER_TABLE,
    Epoch2Collection,
    StorageTier,
    TierAssignment,
    TierTableError,
    compile_tier_table,
    tier_for,
)

pytestmark = pytest.mark.unit


#: The tier of every epoch-2 collection, spelled independently of the
#: table under test.
TIER_FIXTURE: dict[str, str] = {
    "workspace": "document",
    "project": "document",
    "repository": "document",
    "track": "document",
    "campaign": "document",
    "current": "document",
    "track_outcome": "document",
    "sandbox_policy": "document",
    "capability": "document",
    "tool_authority": "document",
    "permission": "document",
    "pending_action": "document",
    "open_question": "document",
    "milestone": "ledger",
    "batch": "ledger",
    "task": "ledger",
    "run": "ledger",
    "release": "ledger",
    "evidence": "ledger",
    "receipt": "ledger",
    "claim": "ledger",
    "campaign_finding": "ledger",
    "hypothesis": "ledger",
    "audit": "ledger",
    "decision": "ledger",
    "incident": "ledger",
    "estimate": "ledger",
    "actual": "ledger",
    "artifact": "ledger",
    "memory": "ledger",
    "legacy": "ledger",
    "telemetry": "local_store",
    "draft": "local_store",
    "event": "firehose",
    "indexes": "derived",
    "dispatch_paused": "derived",
    "fleet_run": "derived",
    "health_view": "derived",
}

#: The one epoch-2 tier each migration tier resolves to. The importer's
#: ``document|ledger`` rows are the lifecycle collections, whose terminal
#: records leave the document, so they resolve to the ledger.
_MIGRATION_TIER_RESOLUTION: dict[MigrationTier, StorageTier] = {
    MigrationTier.DOCUMENT: StorageTier.DOCUMENT,
    MigrationTier.LEDGER: StorageTier.LEDGER,
    MigrationTier.DOCUMENT_OR_LEDGER: StorageTier.LEDGER,
    MigrationTier.DERIVED: StorageTier.DERIVED,
}


def _row(collection: Epoch2Collection, tier: StorageTier) -> TierAssignment:
    return TierAssignment(collection=collection, tier=tier, note="fixture row")


def test_tier_table_is_total_against_the_fixture() -> None:
    """Every collection resolves, and to the tier the fixture declares."""
    resolved = {collection.value: tier.value for collection, tier in TIER_TABLE.items()}
    assert resolved == TIER_FIXTURE


def test_tier_table_covers_every_declared_collection() -> None:
    """No enum member is missing a row and no row names a stranger."""
    assert set(TIER_TABLE) == set(Epoch2Collection)
    assert len(TIER_ASSIGNMENTS) == len(Epoch2Collection)


def test_every_tier_has_at_least_one_collection() -> None:
    """A tier nothing lands in is a tier nobody would maintain."""
    assert set(TIER_TABLE.values()) == set(StorageTier)


def test_tier_for_resolves_each_collection_once() -> None:
    """``tier_for`` is a function: one call, one tier, no ambiguity."""
    for collection in Epoch2Collection:
        assert tier_for(collection) is TIER_TABLE[collection]


def test_assignment_without_a_tier_fails_schema_validation() -> None:
    """A row that omits the tier is refused, never defaulted."""
    with pytest.raises(ValidationError, match="tier"):
        TierAssignment(collection=Epoch2Collection.TASK, note="no tier here")  # type: ignore[call-arg]


def test_assignment_with_a_null_tier_fails_schema_validation() -> None:
    """An explicit null tier is refused too."""
    with pytest.raises(ValidationError):
        TierAssignment.model_validate(
            {"collection": "task", "tier": None, "note": "explicitly untiered"}
        )


def test_assignment_with_an_unknown_tier_fails_schema_validation() -> None:
    """A tier outside the closed five is refused."""
    with pytest.raises(ValidationError):
        TierAssignment.model_validate(
            {"collection": "task", "tier": "somewhere_else", "note": "invented tier"}
        )


def test_assignment_with_an_unknown_collection_fails_schema_validation() -> None:
    """A collection outside the closed set is refused."""
    with pytest.raises(ValidationError):
        TierAssignment.model_validate(
            {"collection": "sprints", "tier": "ledger", "note": "invented collection"}
        )


def test_assignment_rejects_an_unknown_key() -> None:
    """The row model forbids extras like every other strict model."""
    with pytest.raises(ValidationError):
        TierAssignment.model_validate(
            {"collection": "task", "tier": "ledger", "note": "n", "committed": True}
        )


def test_assignment_rejects_an_empty_note() -> None:
    """A row with no stated reason is refused."""
    with pytest.raises(ValidationError):
        TierAssignment.model_validate({"collection": "task", "tier": "ledger", "note": ""})


def test_collection_in_two_tiers_fails_compilation() -> None:
    """The same collection at two tiers has no single home."""
    rows = (
        _row(Epoch2Collection.TASK, StorageTier.LEDGER),
        _row(Epoch2Collection.TASK, StorageTier.DOCUMENT),
    )
    with pytest.raises(TierTableError, match="declared twice"):
        compile_tier_table(rows, collections=(Epoch2Collection.TASK,))


def test_collection_declared_twice_at_one_tier_fails_compilation() -> None:
    """A duplicated row is a defect even when the two agree."""
    rows = (
        _row(Epoch2Collection.TASK, StorageTier.LEDGER),
        _row(Epoch2Collection.TASK, StorageTier.LEDGER),
    )
    with pytest.raises(TierTableError, match="declared twice"):
        compile_tier_table(rows, collections=(Epoch2Collection.TASK,))


def test_collection_with_no_row_fails_compilation() -> None:
    """A collection nobody assigned a tier to fails the table, not a write."""
    rows = (_row(Epoch2Collection.TASK, StorageTier.LEDGER),)
    with pytest.raises(TierTableError, match="no tier declared for run"):
        compile_tier_table(rows, collections=(Epoch2Collection.TASK, Epoch2Collection.RUN))


def test_empty_table_over_no_collections_compiles() -> None:
    """The empty table is total over the empty collection set."""
    assert dict(compile_tier_table((), collections=())) == {}


def test_empty_table_over_the_real_collections_fails_compilation() -> None:
    """Declaring nothing is not a way to satisfy totality."""
    with pytest.raises(TierTableError, match="no tier declared for"):
        compile_tier_table(())


def test_single_row_table_compiles_over_its_own_collection() -> None:
    """The one-row boundary case is a total table."""
    table = compile_tier_table(
        (_row(Epoch2Collection.EVENT, StorageTier.FIREHOSE),),
        collections=(Epoch2Collection.EVENT,),
    )
    assert table == {Epoch2Collection.EVENT: StorageTier.FIREHOSE}


def test_compiled_table_is_read_only() -> None:
    """A caller cannot retier a collection by mutating the map."""
    with pytest.raises(TypeError):
        TIER_TABLE[Epoch2Collection.TASK] = StorageTier.DOCUMENT  # type: ignore[index]


def test_every_entity_kind_resolves_to_a_collection() -> None:
    """A record of any addressable kind has a declared place to live."""
    assert set(ENTITY_COLLECTIONS) == set(EntityKind)
    for kind in EntityKind:
        assert tier_for(ENTITY_COLLECTIONS[kind]) in set(StorageTier)


def test_question_records_live_in_the_open_question_collection() -> None:
    """The one entity kind whose collection is not its own name."""
    assert ENTITY_COLLECTIONS[EntityKind.QUESTION] is Epoch2Collection.OPEN_QUESTION
    assert ENTITY_COLLECTIONS[EntityKind.LEGACY_RECORD] is Epoch2Collection.LEGACY


def test_entity_collections_map_is_read_only() -> None:
    """A caller cannot reroute a kind by mutating the map."""
    with pytest.raises(TypeError):
        ENTITY_COLLECTIONS[EntityKind.TASK] = Epoch2Collection.LEGACY  # type: ignore[index]


def test_tier_for_rejects_a_non_collection() -> None:
    """A name outside the closed set has no tier to fall back on."""
    with pytest.raises(KeyError):
        tier_for("sprints")  # type: ignore[arg-type]


def test_every_importer_target_collection_has_a_tier() -> None:
    """The importer's destinations are all declared epoch-2 collections."""
    for row in COLLECTION_DISPOSITIONS:
        if row.target_collection == "-":
            continue
        for target in row.target_collection.split("|"):
            assert tier_for(Epoch2Collection(target)) in set(StorageTier)


def test_importer_tiers_agree_with_the_epoch2_table() -> None:
    """Where the importer names a tier, the epoch-2 table names the same one."""
    for row in COLLECTION_DISPOSITIONS:
        expected = _MIGRATION_TIER_RESOLUTION.get(row.tier)
        if expected is None or row.target_collection == "-":
            continue
        for target in row.target_collection.split("|"):
            assert tier_for(Epoch2Collection(target)) is expected, row.source_collection


def test_ledger_path_is_fenced_off_from_the_store_directory() -> None:
    """Ledgers live under ``ledger/`` so no in-place rewriter reaches them."""
    state_path = Path("/tmp/tree/.ea/state.json")
    ledger = ledger_path(state_path, Epoch2Collection.TASK)
    assert ledger.name == "task.jsonl"
    assert ledger.parent.name == "ledger"
    assert is_epoch2_ledger_path(ledger)
    assert not is_epoch2_ledger_path(state_path.parent / "store" / "audit.jsonl")


def test_index_path_sits_under_the_indexes_directory() -> None:
    """The derived index of a ledger is regenerable and separately located."""
    index = index_path(Path("/tmp/tree/.ea/state.json"), Epoch2Collection.TASK)
    assert index.name == "task.index.json"
    assert index.parent.name == "indexes"


def test_ledger_path_rejects_a_non_ledger_collection() -> None:
    """A document collection has no append-only file."""
    with pytest.raises(ValueError, match="not ledger"):
        ledger_path(Path("/tmp/tree/.ea/state.json"), Epoch2Collection.TRACK)


def test_index_path_rejects_a_non_ledger_collection() -> None:
    """A derived collection has no ledger offsets to index."""
    with pytest.raises(ValueError, match="no offset index"):
        index_path(Path("/tmp/tree/.ea/state.json"), Epoch2Collection.HEALTH_VIEW)
