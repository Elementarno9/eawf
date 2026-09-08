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

from eawf.kernel.migration.epoch2.lifecycle import (
    CLAIM_SESSION_LEGACY_FIELD,
    CLAIM_SESSION_RESOLVING_FIELD,
    CLAIM_SESSION_SOURCE_FIELD,
    EMPTY_CLAIM_IMPORTS_AS,
    RunSource,
    map_wave_row,
)
from eawf.kernel.migration.epoch2.plan import LifecycleImportPlan
from tests.property.kernel.migration.conftest import (
    DANGLING_SESSION_ID,
    RESOLVING_SESSION_ID,
    sparse_history_index,
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
