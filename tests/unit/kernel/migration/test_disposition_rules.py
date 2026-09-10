"""The disposition table, the drop-proof forms and the versioned rule registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.migration.epoch2.allowlist import LegacySymbolAllowlist
from eawf.kernel.migration.epoch2.corpus import Epoch1BacklogCorpus
from eawf.kernel.migration.epoch2.dispositions import (
    COLLECTION_DISPOSITION_INDEX,
    COLLECTION_DISPOSITIONS,
    Disposition,
    DropProofForm,
    StorageTier,
    disposition_rule_payload,
    drop_proof_form,
)
from eawf.kernel.migration.epoch2.registry import mapping_rule_index, mapping_rule_versions
from eawf.kernel.migration.epoch2.rules import (
    RULE_IMPLEMENTATION_VERSION,
    MappingRuleVersion,
    build_rule_version,
    canonical_json,
    index_rule_versions,
    rule_digest,
)

EXPECTED_RULE_IDS = (
    "DOM-004",
    "DOM-017",
    "DOM-018",
    "DOM-019",
    "DOM-041",
    "DOM-043",
    "DOM-044",
    "DOM-045",
)


def test_collection_dispositions_declare_five_dispositions() -> None:
    assert {item.value for item in Disposition} == {
        "native_conversion",
        "immutable_legacy_record",
        "split_conversion",
        "derived_projection",
        "explicit_drop",
    }


def test_collection_dispositions_have_unique_source_collections() -> None:
    names = [row.source_collection for row in COLLECTION_DISPOSITIONS]
    assert len(names) == len(set(names))
    assert len(COLLECTION_DISPOSITION_INDEX) == len(COLLECTION_DISPOSITIONS)


def test_collection_dispositions_declare_the_two_added_tier_rows() -> None:
    """Without a declared tier these two collections fail tier validation at cutover."""
    assert COLLECTION_DISPOSITION_INDEX["artifacts"].tier is StorageTier.LEDGER
    assert COLLECTION_DISPOSITION_INDEX["memory_index"].tier is StorageTier.LEDGER


def test_collection_dispositions_never_leave_a_tier_undeclared() -> None:
    for row in COLLECTION_DISPOSITIONS:
        assert isinstance(row.tier, StorageTier)
        if row.disposition is Disposition.EXPLICIT_DROP:
            assert row.tier is StorageTier.NONE


def test_drop_proof_form_null_source_is_null_not_empty() -> None:
    """A JSON null collection has no container, so it can never report zero rows."""
    assert drop_proof_form(None) is DropProofForm.NULL_NOT_EMPTY


@pytest.mark.parametrize("empty", [{}, [], (), set()])
def test_drop_proof_form_empty_container_is_zero_rows(empty: object) -> None:
    assert drop_proof_form(empty) is DropProofForm.ZERO_ROWS


def test_drop_proof_form_non_empty_container_raises() -> None:
    with pytest.raises(ValueError, match="holds 1 rows"):
        drop_proof_form({"B001": {}})


def test_drop_proof_form_scalar_raises() -> None:
    with pytest.raises(ValueError, match="admits no proof form"):
        drop_proof_form("1.20")


def test_disposition_rule_payload_is_json_serializable() -> None:
    payload = disposition_rule_payload()
    assert json.loads(canonical_json(payload).decode("utf-8")) == payload
    assert payload["drop_proof_forms"] == ["null_not_empty", "zero_rows"]


def test_canonical_json_sorts_keys_so_equal_tables_digest_equally() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert rule_digest({"b": 1, "a": 2}) == rule_digest({"a": 2, "b": 1})


def test_canonical_json_on_an_empty_payload() -> None:
    assert canonical_json({}) == b"{}"


def test_canonical_json_rejects_an_unencodable_value() -> None:
    with pytest.raises(TypeError):
        canonical_json({"path": Path("/tmp")})


def test_build_rule_version_pins_the_implementation_version() -> None:
    version = build_rule_version(
        rule_id="DOM-004", source_kind="document", title="t", payload={"a": 1}
    )
    assert version.implementation_version == RULE_IMPLEMENTATION_VERSION
    assert version.rule_digest == rule_digest({"a": 1})


def test_build_rule_version_rejects_a_malformed_rule_id() -> None:
    with pytest.raises(ValidationError):
        build_rule_version(rule_id="dom4", source_kind="document", title="t", payload={})


def test_build_rule_version_rejects_an_empty_source_kind() -> None:
    with pytest.raises(ValidationError):
        build_rule_version(rule_id="DOM-004", source_kind="", title="t", payload={})


def test_mapping_rule_version_is_frozen() -> None:
    version = build_rule_version(rule_id="DOM-004", source_kind="document", title="t", payload={})
    with pytest.raises(ValidationError):
        version.rule_id = "DOM-005"  # type: ignore[misc]


def test_index_rule_versions_on_an_empty_tuple() -> None:
    assert index_rule_versions(()) == {}


def test_index_rule_versions_rejects_a_duplicate_rule_id() -> None:
    version = build_rule_version(rule_id="DOM-004", source_kind="document", title="t", payload={})
    with pytest.raises(ValueError, match="duplicate mapping rule id"):
        index_rule_versions((version, version))


def test_mapping_rule_versions_publishes_every_rule_once(
    allowlist: LegacySymbolAllowlist,
) -> None:
    versions = mapping_rule_versions(allowlist)
    assert tuple(version.rule_id for version in versions) == EXPECTED_RULE_IDS
    assert all(isinstance(version, MappingRuleVersion) for version in versions)


def test_mapping_rule_index_keys_by_rule_id(allowlist: LegacySymbolAllowlist) -> None:
    index = mapping_rule_index(allowlist)
    assert set(index) == set(EXPECTED_RULE_IDS)
    assert index["DOM-044"].source_kind == "backlog"


def test_mapping_rule_index_digests_are_stable_across_calls(
    allowlist: LegacySymbolAllowlist,
) -> None:
    first = {key: value.rule_digest for key, value in mapping_rule_index(allowlist).items()}
    second = {key: value.rule_digest for key, value in mapping_rule_index(allowlist).items()}
    assert first == second


def test_epoch1_backlog_corpus_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Epoch1BacklogCorpus.load(tmp_path / "absent.json")


def test_epoch1_backlog_corpus_load_rejects_an_unknown_key(tmp_path: Path) -> None:
    path = tmp_path / "corpus.json"
    path.write_text(
        json.dumps(
            {
                "source_revision": "ae04a5c1",
                "source_schema_version": "1.19",
                "backlog": {},
                "wave_ids": [],
                "audit_document_ids": [],
                "audit_ledger_ids": [],
                "surprise": 1,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        Epoch1BacklogCorpus.load(path)


def test_epoch1_backlog_corpus_load_rejects_a_non_revision_source(tmp_path: Path) -> None:
    path = tmp_path / "corpus.json"
    path.write_text(
        json.dumps(
            {
                "source_revision": "HEAD",
                "source_schema_version": "1.19",
                "backlog": {},
                "wave_ids": [],
                "audit_document_ids": [],
                "audit_ledger_ids": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        Epoch1BacklogCorpus.load(path)


def test_epoch1_backlog_corpus_audit_union_counts_each_id_once() -> None:
    from tests.unit.kernel.migration.conftest import EPOCH1_FULL_CORPUS_PATH

    corpus = Epoch1BacklogCorpus.load(EPOCH1_FULL_CORPUS_PATH)
    union = corpus.audit_union_ids()
    assert len(corpus.audit_document_ids) == 77
    assert len(corpus.audit_ledger_ids) == 101
    assert len(union) == 101
    assert union >= frozenset(corpus.audit_document_ids)
