"""Legacy references resolve by alias, and never by minting the referent.

A claim-session id is the largest population of dangling references the
epoch-1 corpus holds. The importer treats it as a string on the envelope:
it is carried verbatim, it additionally resolves to a legacy session when
the source really holds one, and it is never a canonical reference. That
is what makes it impossible for the population to dangle -- there is no
canonical edge to dangle -- without inventing a single session.

The same alias discipline indexes the records themselves: every imported
record is addressable by the source id it came from, and no two records
share one.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from eawf.kernel.identity.alias import LegacyAliasEntry
from eawf.kernel.identity.errors import IdentityError, IdentityRejection
from eawf.kernel.identity.keys import EntityKind
from eawf.kernel.migration.epoch2.lifecycle import (
    CLAIM_SESSION_LEGACY_FIELD,
    CLAIM_SESSION_RESOLVING_FIELD,
    CLAIM_SESSION_SOURCE_FIELD,
    EMPTY_CLAIM_IMPORTS_AS,
    map_wave_row,
)
from eawf.kernel.migration.epoch2.plan import LifecycleImportPlan
from eawf.kernel.migration.epoch2.runs import RunSource
from eawf.kernel.migration.epoch2.validation import (
    ImportAliasIndex,
    ImportedRow,
    ImportValidationReport,
    StagedImport,
    mint_identities,
)
from tests.property.kernel.migration.conftest import (
    ALIAS_COLLISION_SNAPSHOT,
    CUTOVER_IDENTITY,
    DANGLING_SESSION_ID,
    RESOLVING_SESSION_ID,
    sparse_history_index,
    validation_report,
)

_INDEX = sparse_history_index()


def _wave_row(**overrides: Any) -> dict[str, Any]:
    """One epoch-1 wave row, overridable field by field."""
    row: dict[str, Any] = {
        "id": "P09-I01-W01",
        "iter_id": "P09-I01",
        "status": "claimed",
        "title": "Land P09-I01-W01",
        "opened_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def _map(row: dict[str, Any]) -> Any:
    """Map one wave row against the sparse-history populations."""
    return map_wave_row(source_id=row["id"], row=row, criteria=(), index=_INDEX)


def test_claim_session_resolving_id_yields_a_legacy_session_ref() -> None:
    record = _map(_wave_row(claim_session_id=RESOLVING_SESSION_ID))

    assert record.legacy_session_ref == RESOLVING_SESSION_ID
    assert record.legacy_refs[CLAIM_SESSION_SOURCE_FIELD] == RESOLVING_SESSION_ID


def test_claim_session_resolving_id_mints_exactly_one_run() -> None:
    record = _map(_wave_row(claim_session_id=RESOLVING_SESSION_ID))

    assert [run.run_source for run in record.minted_runs] == [RunSource.RESOLVING_CLAIM]
    assert record.minted_runs[0].source_id == RESOLVING_SESSION_ID


def test_claim_session_unresolved_id_stands_as_a_string_with_no_session_minted() -> None:
    """The id is a fact the source recorded; the session it names is not."""
    record = _map(_wave_row(claim_session_id=DANGLING_SESSION_ID))

    assert record.legacy_refs[CLAIM_SESSION_SOURCE_FIELD] == DANGLING_SESSION_ID
    assert record.legacy_session_ref is None
    assert record.minted_runs == ()


def test_claim_session_empty_string_imports_as_an_absent_field() -> None:
    """An empty string is a field nobody filled, not an empty reference."""
    record = _map(_wave_row(claim_session_id=""))

    assert CLAIM_SESSION_SOURCE_FIELD not in record.legacy_refs
    assert record.legacy_session_ref is None
    assert EMPTY_CLAIM_IMPORTS_AS == "absent_field"


def test_claim_session_absent_field_imports_as_absent() -> None:
    record = _map(_wave_row())

    assert CLAIM_SESSION_SOURCE_FIELD not in record.legacy_refs
    assert record.legacy_session_ref is None


def test_claim_session_null_value_imports_as_absent() -> None:
    record = _map(_wave_row(claim_session_id=None))

    assert CLAIM_SESSION_SOURCE_FIELD not in record.legacy_refs
    assert record.legacy_session_ref is None


def test_claim_session_is_carried_on_the_envelope_not_as_a_canonical_field() -> None:
    """The record's own epoch-2 fields never hold the claimed session."""
    record = _map(_wave_row(claim_session_id=RESOLVING_SESSION_ID))

    assert CLAIM_SESSION_SOURCE_FIELD not in record.record
    assert f"legacy_refs.{CLAIM_SESSION_SOURCE_FIELD}" == CLAIM_SESSION_LEGACY_FIELD
    assert CLAIM_SESSION_RESOLVING_FIELD == "legacy_session_ref"


@given(
    claim_id=st.text(min_size=1, max_size=32).filter(lambda value: value not in _INDEX.session_ids)
)
def test_claim_session_never_mints_a_session_the_source_lacks(claim_id: str) -> None:
    """No unresolved string, however shaped, produces a session or a run."""
    record = _map(_wave_row(claim_session_id=claim_id))

    assert record.legacy_refs[CLAIM_SESSION_SOURCE_FIELD] == claim_id
    assert record.legacy_session_ref is None
    assert record.minted_runs == ()


@given(claim_id=st.sampled_from([RESOLVING_SESSION_ID]))
def test_claim_session_resolution_is_membership_not_similarity(claim_id: str) -> None:
    """Only exact membership resolves; a near-miss stays a bare string."""
    resolving = _map(_wave_row(claim_session_id=claim_id))
    near_miss = _map(_wave_row(claim_session_id=claim_id.lower()))

    assert resolving.legacy_session_ref == claim_id
    assert near_miss.legacy_session_ref is None


def test_record_for_indexes_every_record_by_its_source_id(
    sparse_plan: LifecycleImportPlan,
) -> None:
    for record in sparse_plan.records:
        source_id = record.origin.source_id
        assert source_id is not None
        assert sparse_plan.record_for(source_id) is record


def test_record_for_rejects_a_source_id_the_corpus_never_held(
    sparse_plan: LifecycleImportPlan,
) -> None:
    with pytest.raises(KeyError):
        sparse_plan.record_for("")


# --- The alias index over a whole staged import ---
#
# The mappers above make one row resolvable. These make the corpus
# resolvable: every source row keyed once, every key landing on a record
# of its own, and a write addressed by any of them refused. The
# alias-collision corpus is the counter-example -- one identifier naming
# both a wave and a backlog row -- because a promotion that keeps its id
# is the one shape that really does collapse two rows onto one Task.

#: A record address no corpus mints, used as the target of a chained
#: entry so the chain is the only invariant the entry breaks.
UNUSED_RECORD_URN = "eawf://WSP-DEFAULT/PRJ-DEMO/REP-DEMO/legacy-record/LGR-9999"

#: A source row the full-shape corpus holds, and one it does not.
FULL_CORPUS_WAVE = "P01-I01-W01"
ABSENT_WAVE = "P99-I99-W99"

#: The identifier the alias-collision corpus gives to both a wave and a
#: backlog row, and the two addresses that collide on it.
COLLIDING_IDENTIFIER = "P01-I01-W01"
COLLIDING_ADDRESSES = ("waves/P01-I01-W01", "backlog/P01-I01-W01")


def _staged_row(**overrides: Any) -> ImportedRow:
    """One reduced import row, overridable field by field."""
    row: dict[str, Any] = {
        "address": "waves/P01-I01-W01",
        "source_kind": "waves",
        "source_id": "P01-I01-W01",
        "entity_kind": EntityKind.TASK,
        "identifier": "P01-I01-W01",
    }
    row.update(overrides)
    return ImportedRow(**row)


def _staged(*rows: ImportedRow) -> StagedImport:
    """A reduced import over ``rows`` under the fixture's source identity."""
    return StagedImport(source_schema_version="1.19", project_code="DEMO", rows=rows)


def test_corpus_alias_index_keys_every_source_row_exactly_once(
    full_report: ImportValidationReport,
) -> None:
    census = full_report.aliases.census

    assert census.source_rows > 0
    assert census.aliased_rows == census.source_rows
    assert len({entry.key for entry in full_report.aliases.entries}) == census.aliased_rows


def test_corpus_alias_index_resolves_injectively_with_no_collisions(
    full_report: ImportValidationReport,
) -> None:
    """One source row, one record: the counts agree and nothing collides."""
    census = full_report.aliases.census

    assert census.collisions == ()
    assert census.minted_records == census.aliased_rows
    assert census.is_injective


def test_corpus_alias_index_resolves_a_source_row_to_its_own_record(
    full_report: ImportValidationReport,
) -> None:
    index = full_report.aliases.resolvable_index()
    key = full_report.aliases.key_for(source_kind="waves", source_id=FULL_CORPUS_WAVE)

    assert index.resolve(key).startswith("eawf://WSP-DEFAULT/PRJ-DEMO/REP-DEMO/task/")


def test_corpus_alias_index_refuses_a_source_row_the_corpus_never_held(
    full_report: ImportValidationReport,
) -> None:
    key = full_report.aliases.key_for(source_kind="waves", source_id=ABSENT_WAVE)

    with pytest.raises(IdentityError) as excinfo:
        full_report.aliases.resolvable_index().resolve(key)
    assert excinfo.value.code is IdentityRejection.IDENTITY_NOT_FOUND


def test_corpus_alias_index_reports_a_collision_before_it_refuses_one() -> None:
    """The census names both colliding rows; refusing them is a later step."""
    census = validation_report(ALIAS_COLLISION_SNAPSHOT).aliases.census

    assert not census.is_injective
    assert census.minted_records == census.aliased_rows - 1
    assert [row.source_addresses for row in census.collisions] == [COLLIDING_ADDRESSES]


def test_corpus_alias_index_refuses_a_colliding_corpus() -> None:
    report = validation_report(ALIAS_COLLISION_SNAPSHOT)

    with pytest.raises(IdentityError) as excinfo:
        report.require_clean()
    assert excinfo.value.code is IdentityRejection.ALIAS_TARGET_NOT_INJECTIVE
    assert COLLIDING_IDENTIFIER in str(excinfo.value)


def test_corpus_alias_index_refuses_an_alias_chain(
    full_report: ImportValidationReport,
) -> None:
    """A target that is another entry's source address needs a second hop."""
    entries = full_report.aliases.entries
    chained = LegacyAliasEntry(
        key=full_report.aliases.key_for(source_kind="waves", source_id=ABSENT_WAVE),
        target=UNUSED_RECORD_URN,
        source_urn=entries[0].target,
    )
    chaining = full_report.aliases.model_copy(update={"entries": (*entries, chained)})

    with pytest.raises(IdentityError) as excinfo:
        chaining.resolvable_index()
    assert excinfo.value.code is IdentityRejection.ALIAS_CHAIN_FORBIDDEN


def test_corpus_alias_index_refuses_a_native_mutation_addressed_by_an_alias(
    full_report: ImportValidationReport,
) -> None:
    """History resolves, and the refusal names the record to read instead."""
    with pytest.raises(IdentityError) as excinfo:
        full_report.aliases.refuse_mutation(source_kind="waves", source_id=FULL_CORPUS_WAVE)

    assert excinfo.value.code is IdentityRejection.LEGACY_IDENTITY_READ_ONLY
    assert excinfo.value.canonical_history_link is not None
    assert excinfo.value.canonical_history_link.startswith("eawf://")


def test_corpus_alias_index_admits_a_mutation_addressed_by_a_native_key(
    full_report: ImportValidationReport,
) -> None:
    """A key the import never wrote is not history, so it may be written."""
    full_report.aliases.refuse_mutation(source_kind="waves", source_id=ABSENT_WAVE)

    key = full_report.aliases.key_for(source_kind="waves", source_id=ABSENT_WAVE)
    assert key not in {entry.key for entry in full_report.aliases.entries}


def test_corpus_alias_index_rejects_an_empty_source_id(
    full_report: ImportValidationReport,
) -> None:
    with pytest.raises(ValidationError):
        full_report.aliases.key_for(source_kind="waves", source_id="")


def test_mint_identities_over_an_import_with_no_rows_mints_nothing() -> None:
    assert mint_identities(staged=_staged(), identity=CUTOVER_IDENTITY) == ()


def test_mint_identities_over_one_row_mints_the_first_key_of_its_family() -> None:
    minted = mint_identities(staged=_staged(_staged_row()), identity=CUTOVER_IDENTITY)

    assert [row.entity_key for row in minted] == ["DEMO-0001"]
    assert minted[0].urn == "eawf://WSP-DEFAULT/PRJ-DEMO/REP-DEMO/task/DEMO-0001"


def test_mint_identities_gives_two_distinct_identifiers_two_records() -> None:
    minted = mint_identities(
        staged=_staged(
            _staged_row(),
            _staged_row(
                address="waves/P01-I01-W02",
                source_id="P01-I01-W02",
                identifier="P01-I01-W02",
            ),
        ),
        identity=CUTOVER_IDENTITY,
    )

    assert [row.entity_key for row in minted] == ["DEMO-0001", "DEMO-0002"]


def test_mint_identities_gives_two_rows_naming_one_identifier_one_record() -> None:
    """Promotion never changes identity, so the two rows are one Task."""
    minted = mint_identities(
        staged=_staged(
            _staged_row(),
            _staged_row(address="backlog/P01-I01-W01", source_kind="backlog"),
        ),
        identity=CUTOVER_IDENTITY,
    )

    assert minted[0] is minted[1]
    assert minted[0].entity_key == "DEMO-0001"


def test_mint_identities_refuses_a_kind_whose_keys_are_supplied_not_minted() -> None:
    """A track key is an operator-chosen symbol; no ordinal stands in for it."""
    with pytest.raises(IdentityError) as excinfo:
        mint_identities(
            staged=_staged(_staged_row(entity_kind=EntityKind.TRACK)),
            identity=CUTOVER_IDENTITY,
        )
    assert excinfo.value.code is IdentityRejection.KIND_NOT_ALLOCATABLE


#: A two-row import the read-only property test drives, so each example
#: costs one small index rather than a rebuild over the whole corpus.
_SMALL_ALIASES = ImportAliasIndex.build(
    staged=_staged(
        _staged_row(),
        _staged_row(address="waves/P01-I01-W02", source_id="P01-I01-W02", identifier="P01-I01-W02"),
    ),
    identity=CUTOVER_IDENTITY,
)
_SMALL_SOURCE_IDS = frozenset({"P01-I01-W01", "P01-I01-W02"})


@given(
    source_id=st.text(min_size=1, max_size=24).filter(lambda value: value not in _SMALL_SOURCE_IDS)
)
def test_corpus_alias_index_admits_every_key_the_import_never_wrote(source_id: str) -> None:
    """The guard refuses exactly the keys the index can resolve, and no others."""
    _SMALL_ALIASES.refuse_mutation(source_kind="waves", source_id=source_id)
    key = _SMALL_ALIASES.key_for(source_kind="waves", source_id=source_id)

    with pytest.raises(IdentityError) as excinfo:
        _SMALL_ALIASES.resolvable_index().resolve(key)
    assert excinfo.value.code is IdentityRejection.IDENTITY_NOT_FOUND
