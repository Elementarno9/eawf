"""Every canonical public key allocates and validates; design spellings do not."""

from __future__ import annotations

import pytest

from eawf.kernel.identity.errors import IdentityError, IdentityRejection
from eawf.kernel.identity.keys import (
    KEY_ORDINAL_FAMILIES,
    EntityKeyAllocator,
    EntityKind,
    format_entity_key,
    project_code_of,
    validate_entity_key,
    validate_symbol_key,
)
from eawf.kernel.state.canonical_sequence import CanonicalSequenceError

WORKSPACE = "WSP-DEFAULT"
PROJECT_CODE = "EAWF"

# The 2026-08-13 design frames spell five keys the grammar does not mint.
# Each is refused by the family it claims to belong to, so a fixture or
# golden file carrying one reddens before it can be snapshotted.
DESIGN_SPELLINGS = [
    pytest.param(EntityKind.CAMPAIGN, "CMP-0001", None, id="campaign-CMP"),
    pytest.param(EntityKind.TASK, "TSK-0042", PROJECT_CODE, id="task-TSK"),
    pytest.param(EntityKind.RUN, "RUN-0001", None, id="run-narrow-ordinal"),
    pytest.param(EntityKind.RECEIPT, "EVT-0001", None, id="receipt-EVT"),
    pytest.param(EntityKind.PERMISSION, "TOOL-0401", None, id="permission-TOOL"),
]

ALLOCATABLE_KINDS = [pytest.param(kind, None, id=kind.value) for kind in KEY_ORDINAL_FAMILIES] + [
    pytest.param(EntityKind.TASK, PROJECT_CODE, id="task")
]


@pytest.mark.parametrize(("kind", "project_code"), ALLOCATABLE_KINDS)
def test_allocate_mints_a_key_its_own_grammar_accepts(
    kind: EntityKind, project_code: str | None
) -> None:
    allocator = EntityKeyAllocator(workspace_key=WORKSPACE, kind=kind, project_code=project_code)
    with allocator.transaction() as txn:
        key = txn.allocate()
    assert validate_entity_key(kind, key, project_code=project_code) == key
    assert allocator.allocated == 1


@pytest.mark.parametrize(("kind", "key", "project_code"), DESIGN_SPELLINGS)
def test_validate_entity_key_rejects_design_spelling(
    kind: EntityKind, key: str, project_code: str | None
) -> None:
    with pytest.raises(IdentityError) as excinfo:
        validate_entity_key(kind, key, project_code=project_code)
    assert excinfo.value.code is IdentityRejection.ENTITY_KEY_INVALID


def test_format_entity_key_pads_run_to_eight_digits() -> None:
    assert format_entity_key(EntityKind.RUN, 10) == "RUN-00000010"


def test_format_entity_key_pads_ordinal_family_to_four_digits() -> None:
    assert format_entity_key(EntityKind.MILESTONE, 30) == "MLS-0030"


def test_format_entity_key_prefixes_a_task_with_its_project_code() -> None:
    assert format_entity_key(EntityKind.TASK, 42, project_code=PROJECT_CODE) == "EAWF-0042"


def test_format_entity_key_accepts_the_first_ordinal() -> None:
    assert format_entity_key(EntityKind.CAMPAIGN, 1) == "CAM-0001"


def test_format_entity_key_accepts_the_widest_ordinal() -> None:
    assert format_entity_key(EntityKind.CAMPAIGN, 9999) == "CAM-9999"


def test_format_entity_key_rejects_one_past_the_widest_ordinal() -> None:
    with pytest.raises(IdentityError) as excinfo:
        format_entity_key(EntityKind.CAMPAIGN, 10_000)
    assert excinfo.value.code is IdentityRejection.KEY_SPACE_SATURATED


def test_format_entity_key_rejects_a_non_positive_ordinal() -> None:
    with pytest.raises(ValueError, match="ordinal must be positive"):
        format_entity_key(EntityKind.CAMPAIGN, 0)


def test_format_entity_key_rejects_a_task_without_a_project_code() -> None:
    with pytest.raises(IdentityError) as excinfo:
        format_entity_key(EntityKind.TASK, 1)
    assert excinfo.value.code is IdentityRejection.ENTITY_KEY_INVALID


@pytest.mark.parametrize(
    "kind",
    [EntityKind.TRACK, EntityKind.RELEASE, EntityKind.WORKSPACE],
    ids=lambda kind: kind.value,
)
def test_format_entity_key_rejects_a_supplied_key_family(kind: EntityKind) -> None:
    with pytest.raises(IdentityError) as excinfo:
        format_entity_key(kind, 1)
    assert excinfo.value.code is IdentityRejection.KIND_NOT_ALLOCATABLE


def test_validate_entity_key_accepts_an_operator_named_track() -> None:
    assert validate_entity_key(EntityKind.TRACK, "TRK-RUNTIME") == "TRK-RUNTIME"


def test_validate_entity_key_rejects_a_single_character_track_symbol() -> None:
    with pytest.raises(IdentityError) as excinfo:
        validate_entity_key(EntityKind.TRACK, "TRK-R")
    assert excinfo.value.code is IdentityRejection.ENTITY_KEY_INVALID


@pytest.mark.parametrize("key", ["REL-0.7.0", "REL-0.7.0.dev1", "REL-1.0.0rc2"])
def test_validate_entity_key_accepts_a_normalised_release_version(key: str) -> None:
    assert validate_entity_key(EntityKind.RELEASE, key) == key


def test_validate_entity_key_rejects_an_unnormalised_release_version() -> None:
    with pytest.raises(IdentityError) as excinfo:
        validate_entity_key(EntityKind.RELEASE, "REL-v0.7")
    assert excinfo.value.code is IdentityRejection.ENTITY_KEY_INVALID


def test_validate_entity_key_accepts_a_container_symbol() -> None:
    assert validate_entity_key(EntityKind.PROJECT, "PRJ-EAWF") == "PRJ-EAWF"


def test_validate_entity_key_rejects_a_lowercase_container_symbol() -> None:
    with pytest.raises(IdentityError) as excinfo:
        validate_entity_key(EntityKind.PROJECT, "prj-eawf")
    assert excinfo.value.code is IdentityRejection.SYMBOL_KEY_INVALID


def test_validate_symbol_key_rejects_the_empty_string() -> None:
    with pytest.raises(IdentityError) as excinfo:
        validate_symbol_key("", slot="workspace")
    assert excinfo.value.code is IdentityRejection.SYMBOL_KEY_INVALID


def test_validate_symbol_key_rejects_a_single_character_symbol() -> None:
    with pytest.raises(IdentityError) as excinfo:
        validate_symbol_key("W", slot="workspace")
    assert excinfo.value.code is IdentityRejection.SYMBOL_KEY_INVALID


def test_validate_symbol_key_accepts_the_longest_symbol() -> None:
    assert validate_symbol_key("W" * 16, slot="workspace") == "W" * 16


def test_validate_symbol_key_rejects_one_character_past_the_longest() -> None:
    with pytest.raises(IdentityError) as excinfo:
        validate_symbol_key("W" * 17, slot="workspace")
    assert excinfo.value.code is IdentityRejection.SYMBOL_KEY_INVALID


def test_project_code_of_strips_the_registry_prefix() -> None:
    assert project_code_of("PRJ-EAWF") == PROJECT_CODE


def test_project_code_of_passes_through_an_unprefixed_key() -> None:
    assert project_code_of(PROJECT_CODE) == PROJECT_CODE


def test_entity_key_allocator_is_monotonic_across_transactions() -> None:
    allocator = EntityKeyAllocator(workspace_key=WORKSPACE, kind=EntityKind.MILESTONE)
    minted: list[str] = []
    for _ in range(3):
        with allocator.transaction() as txn:
            minted.append(txn.allocate())
    assert minted == ["MLS-0001", "MLS-0002", "MLS-0003"]
    assert allocator.allocated == 3


def test_entity_key_allocator_burns_a_rolled_back_key() -> None:
    allocator = EntityKeyAllocator(workspace_key=WORKSPACE, kind=EntityKind.MILESTONE)
    burned: list[str] = []
    with pytest.raises(RuntimeError, match="import aborted"), allocator.transaction() as txn:
        burned.append(txn.allocate())
        raise RuntimeError("import aborted")
    assert allocator.allocated == 0
    with allocator.transaction() as txn:
        reissued = txn.allocate()
    assert reissued not in burned
    assert reissued == "MLS-0002"


def test_entity_key_allocator_resumes_from_a_committed_count() -> None:
    allocator = EntityKeyAllocator(workspace_key=WORKSPACE, kind=EntityKind.RECEIPT, allocated=7)
    with allocator.transaction() as txn:
        assert txn.allocate() == "RCP-0008"


def test_entity_key_allocator_aborts_the_transaction_when_the_space_saturates() -> None:
    allocator = EntityKeyAllocator(
        workspace_key=WORKSPACE, kind=EntityKind.CAMPAIGN, allocated=9_998
    )
    with allocator.transaction() as txn:
        assert txn.allocate() == "CAM-9999"
    with pytest.raises(CanonicalSequenceError), allocator.transaction() as txn:
        txn.allocate()
    assert allocator.allocated == 9_999


def test_entity_key_allocator_rejects_an_empty_workspace_key() -> None:
    with pytest.raises(ValueError, match="workspace_key must be non-empty"):
        EntityKeyAllocator(workspace_key="", kind=EntityKind.MILESTONE)


def test_entity_key_allocator_rejects_a_supplied_key_family() -> None:
    with pytest.raises(IdentityError) as excinfo:
        EntityKeyAllocator(workspace_key=WORKSPACE, kind=EntityKind.TRACK)
    assert excinfo.value.code is IdentityRejection.KIND_NOT_ALLOCATABLE


def test_entity_key_allocator_rejects_a_negative_committed_count() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        EntityKeyAllocator(workspace_key=WORKSPACE, kind=EntityKind.MILESTONE, allocated=-1)


def test_entity_key_allocators_of_two_workspaces_are_independent() -> None:
    first = EntityKeyAllocator(workspace_key="WSP-ONE", kind=EntityKind.BATCH)
    second = EntityKeyAllocator(workspace_key="WSP-TWO", kind=EntityKind.BATCH)
    with first.transaction() as txn:
        txn.allocate()
    with second.transaction() as txn:
        assert txn.allocate() == "BAT-0001"
