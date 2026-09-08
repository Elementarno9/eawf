"""Alias resolution is total, injective, chain-free, and read-only."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.identity.alias import (
    LegacyAliasEntry,
    LegacyAliasIndex,
    LegacyAliasKey,
    check_alias_invariants,
)
from eawf.kernel.identity.errors import IdentityError, IdentityRejection

WORKSPACE = "WSP-DEFAULT"
PROJECT = "PRJ-EAWF"
REPOSITORY = "REP-EAWF"

TASK_URN = f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/task/EAWF-0001"
OTHER_TASK_URN = f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/task/EAWF-0002"
LEGACY_RECORD_URN = f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/legacy-record/LGR-0001"
CLAIM_RUNG_URN = f"eawf://{WORKSPACE}/{PROJECT}/_/claim/CLM-0004#rung-2"


def make_key(source_id: str = "P13-I04-W01") -> LegacyAliasKey:
    """Return an alias key for *source_id* under a fixed source schema."""
    return LegacyAliasKey(
        source_schema_version="1.20",
        source_kind="wave",
        source_id=source_id,
        source_project_code="EAWF",
    )


def test_legacy_alias_index_resolves_a_native_target() -> None:
    index = LegacyAliasIndex.build([LegacyAliasEntry(key=make_key(), target=TASK_URN)])
    assert index.resolve(make_key()) == TASK_URN


def test_legacy_alias_index_resolves_a_legacy_record_target() -> None:
    entry = LegacyAliasEntry(key=make_key(), target=LEGACY_RECORD_URN)
    assert LegacyAliasIndex.build([entry]).resolve(make_key()) == LEGACY_RECORD_URN


def test_legacy_alias_index_is_empty_by_default() -> None:
    assert LegacyAliasIndex().entries == ()


def test_legacy_alias_index_resolve_reports_an_unknown_source_row() -> None:
    with pytest.raises(IdentityError) as excinfo:
        LegacyAliasIndex().resolve(make_key())
    assert excinfo.value.code is IdentityRejection.IDENTITY_NOT_FOUND


def test_legacy_alias_index_build_rejects_a_duplicate_key() -> None:
    entries = [
        LegacyAliasEntry(key=make_key(), target=TASK_URN),
        LegacyAliasEntry(key=make_key(), target=OTHER_TASK_URN),
    ]
    with pytest.raises(IdentityError) as excinfo:
        LegacyAliasIndex.build(entries)
    assert excinfo.value.code is IdentityRejection.ALIAS_KEY_DUPLICATE


def test_legacy_alias_index_model_rejects_a_duplicate_key() -> None:
    entries = (
        LegacyAliasEntry(key=make_key(), target=TASK_URN),
        LegacyAliasEntry(key=make_key(), target=OTHER_TASK_URN),
    )
    with pytest.raises(ValidationError, match=IdentityRejection.ALIAS_KEY_DUPLICATE.value):
        LegacyAliasIndex(entries=entries)


def test_legacy_alias_index_rejects_two_sources_resolving_to_one_record() -> None:
    entries = [
        LegacyAliasEntry(key=make_key("P13-I04-W01"), target=TASK_URN),
        LegacyAliasEntry(key=make_key("P13-I04-W02"), target=TASK_URN),
    ]
    with pytest.raises(IdentityError) as excinfo:
        LegacyAliasIndex.build(entries)
    assert excinfo.value.code is IdentityRejection.ALIAS_TARGET_NOT_INJECTIVE


def test_legacy_alias_index_rejects_an_alias_to_alias_chain() -> None:
    entries = [
        LegacyAliasEntry(key=make_key("P13-I04-W01"), source_urn=TASK_URN, target=OTHER_TASK_URN),
        LegacyAliasEntry(key=make_key("P13-I04-W02"), target=TASK_URN),
    ]
    with pytest.raises(IdentityError) as excinfo:
        LegacyAliasIndex.build(entries)
    assert excinfo.value.code is IdentityRejection.ALIAS_CHAIN_FORBIDDEN


def test_legacy_alias_entry_rejects_a_self_referencing_chain() -> None:
    with pytest.raises(ValidationError, match=IdentityRejection.ALIAS_CHAIN_FORBIDDEN.value):
        LegacyAliasEntry(key=make_key(), source_urn=TASK_URN, target=TASK_URN)


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("urn:eawf:v1:wave:P13-I04-W01", id="epoch-one-urn"),
        pytest.param("P13-I04-W01", id="bare-source-id"),
        pytest.param("", id="empty"),
        pytest.param(f"{TASK_URN},{OTHER_TASK_URN}", id="two-urns"),
        pytest.param(
            f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/task/TSK-0042", id="design-spelling"
        ),
    ],
)
def test_legacy_alias_entry_rejects_a_value_that_is_not_one_record_urn(target: str) -> None:
    with pytest.raises(ValidationError, match=IdentityRejection.ALIAS_TARGET_INVALID.value):
        LegacyAliasEntry(key=make_key(), target=target)


def test_legacy_alias_entry_rejects_a_value_selecting_a_rung() -> None:
    with pytest.raises(ValidationError, match="selects a rung, not a record"):
        LegacyAliasEntry(key=make_key(), target=CLAIM_RUNG_URN)


def test_legacy_alias_entry_rejects_an_unqualified_source_urn() -> None:
    with pytest.raises(ValidationError, match=IdentityRejection.ALIAS_TARGET_INVALID.value):
        LegacyAliasEntry(key=make_key(), source_urn="urn:eawf:v1:wave:X", target=TASK_URN)


def test_legacy_alias_entry_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        LegacyAliasEntry(key=make_key(), target=TASK_URN, disposition="native_conversion")


def test_legacy_alias_key_rejects_an_empty_source_id() -> None:
    with pytest.raises(ValidationError, match="source_id"):
        LegacyAliasKey(
            source_schema_version="1.20",
            source_kind="wave",
            source_id="",
            source_project_code="EAWF",
        )


def test_legacy_alias_key_rejects_a_lowercase_project_code() -> None:
    with pytest.raises(ValidationError, match="source_project_code"):
        LegacyAliasKey(
            source_schema_version="1.20",
            source_kind="wave",
            source_id="P13-I04-W01",
            source_project_code="eawf",
        )


def test_legacy_alias_key_rejects_a_missing_field() -> None:
    with pytest.raises(ValidationError, match="source_project_code"):
        LegacyAliasKey(
            source_schema_version="1.20",
            source_kind="wave",
            source_id="P13-I04-W01",
        )


def test_legacy_alias_key_separates_two_schema_versions_of_one_source_id() -> None:
    older = LegacyAliasKey(
        source_schema_version="1.19",
        source_kind="wave",
        source_id="P13-I04-W01",
        source_project_code="EAWF",
    )
    index = LegacyAliasIndex.build(
        [
            LegacyAliasEntry(key=older, target=TASK_URN),
            LegacyAliasEntry(key=make_key(), target=OTHER_TASK_URN),
        ]
    )
    assert index.resolve(older) == TASK_URN
    assert index.resolve(make_key()) == OTHER_TASK_URN


def test_legacy_alias_index_add_returns_a_new_index() -> None:
    index = LegacyAliasIndex.build([LegacyAliasEntry(key=make_key(), target=TASK_URN)])
    grown = index.add(LegacyAliasEntry(key=make_key("P13-I04-W02"), target=OTHER_TASK_URN))
    assert len(index.entries) == 1
    assert len(grown.entries) == 2


def test_legacy_alias_index_add_rejects_a_duplicate_key() -> None:
    index = LegacyAliasIndex.build([LegacyAliasEntry(key=make_key(), target=TASK_URN)])
    with pytest.raises(IdentityError) as excinfo:
        index.add(LegacyAliasEntry(key=make_key(), target=OTHER_TASK_URN))
    assert excinfo.value.code is IdentityRejection.ALIAS_KEY_DUPLICATE


def test_legacy_alias_index_as_mapping_is_injective() -> None:
    index = LegacyAliasIndex.build(
        [
            LegacyAliasEntry(key=make_key("P13-I04-W01"), target=TASK_URN),
            LegacyAliasEntry(key=make_key("P13-I04-W02"), target=OTHER_TASK_URN),
        ]
    )
    mapping = index.as_mapping()
    assert len(set(mapping.values())) == len(mapping)


def test_legacy_alias_index_assert_mutable_refuses_a_native_write() -> None:
    index = LegacyAliasIndex.build([LegacyAliasEntry(key=make_key(), target=TASK_URN)])
    with pytest.raises(IdentityError) as excinfo:
        index.assert_mutable(make_key())
    assert excinfo.value.code is IdentityRejection.LEGACY_IDENTITY_READ_ONLY
    assert excinfo.value.canonical_history_link == TASK_URN


def test_legacy_alias_index_assert_mutable_admits_a_non_alias_key() -> None:
    index = LegacyAliasIndex.build([LegacyAliasEntry(key=make_key(), target=TASK_URN)])
    assert index.assert_mutable(make_key("P13-I04-W99")) is None


def test_check_alias_invariants_admits_an_empty_corpus() -> None:
    assert check_alias_invariants([]) is None
