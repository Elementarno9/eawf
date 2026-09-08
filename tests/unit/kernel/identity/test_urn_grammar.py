"""The qualified URN parses, round-trips, and refuses the three grammar defects."""

from __future__ import annotations

import pytest

from eawf.kernel.identity.errors import IdentityError, IdentityRejection
from eawf.kernel.identity.keys import ENTITY_SCOPES, EntityKind, EntityScope
from eawf.kernel.identity.urn import (
    RUNG_MAX,
    RUNG_MIN,
    QualifiedUrn,
    format_qualified_urn,
    parse_qualified_urn,
)

WORKSPACE = "WSP-DEFAULT"
PROJECT = "PRJ-EAWF"
REPOSITORY = "REP-EAWF"

REPOSITORY_LEVEL = [
    pytest.param(f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/milestone/MLS-0030", id="milestone"),
    pytest.param(f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/task/EAWF-0001", id="task"),
    pytest.param(f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/run/RUN-00000010", id="run"),
    pytest.param(f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/track/TRK-RUNTIME", id="track"),
    pytest.param(f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/batch/BAT-0001", id="batch"),
]

WORKSPACE_OR_PROJECT_LEVEL = [
    pytest.param(f"eawf://{WORKSPACE}/{PROJECT}/_/release/REL-0.7.0", id="release"),
    pytest.param(f"eawf://{WORKSPACE}/{PROJECT}/_/claim/CLM-0004", id="claim"),
    pytest.param(f"eawf://{WORKSPACE}/{PROJECT}/_/question/QST-0007", id="question"),
    pytest.param(f"eawf://{WORKSPACE}/{PROJECT}/_/project/{PROJECT}", id="project"),
    pytest.param(f"eawf://{WORKSPACE}/{PROJECT}/_/workspace/{WORKSPACE}", id="workspace"),
]


@pytest.mark.parametrize("raw", REPOSITORY_LEVEL + WORKSPACE_OR_PROJECT_LEVEL)
def test_parse_qualified_urn_round_trips(raw: str) -> None:
    assert str(parse_qualified_urn(raw)) == raw


@pytest.mark.parametrize("raw", REPOSITORY_LEVEL)
def test_parse_qualified_urn_keeps_the_repository_of_a_repository_level_kind(raw: str) -> None:
    assert parse_qualified_urn(raw).repository_key == REPOSITORY


@pytest.mark.parametrize("raw", WORKSPACE_OR_PROJECT_LEVEL)
def test_parse_qualified_urn_reads_the_reserved_slot_as_absent(raw: str) -> None:
    parsed = parse_qualified_urn(raw)
    assert parsed.repository_key is None
    assert parsed.repository_slot == "_"
    assert ENTITY_SCOPES[parsed.kind] in {EntityScope.WORKSPACE, EntityScope.PROJECT}


def test_parse_qualified_urn_rejects_the_reserved_slot_on_a_repository_level_kind() -> None:
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(f"eawf://{WORKSPACE}/{PROJECT}/_/milestone/MLS-0030")
    assert excinfo.value.code is IdentityRejection.REPOSITORY_SLOT_INVALID


def test_parse_qualified_urn_rejects_a_repository_on_a_workspace_level_kind() -> None:
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/release/REL-0.7.0")
    assert excinfo.value.code is IdentityRejection.REPOSITORY_SLOT_INVALID


def test_parse_qualified_urn_accepts_an_agreeing_discriminator() -> None:
    raw = f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/milestone/MLS-0030"
    assert parse_qualified_urn(raw, expected_kind=EntityKind.MILESTONE).entity_key == "MLS-0030"


def test_parse_qualified_urn_rejects_a_disagreeing_discriminator() -> None:
    raw = f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/milestone/MLS-0030"
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(raw, expected_kind=EntityKind.TASK)
    assert excinfo.value.code is IdentityRejection.IDENTITY_KIND_MISMATCH


@pytest.mark.parametrize("rung", range(RUNG_MIN, RUNG_MAX + 1))
def test_parse_qualified_urn_accepts_every_ladder_rung(rung: int) -> None:
    raw = f"eawf://{WORKSPACE}/{PROJECT}/_/claim/CLM-0004#rung-{rung}"
    assert parse_qualified_urn(raw).rung == rung


@pytest.mark.parametrize("rung", [RUNG_MIN - 1, RUNG_MAX + 1])
def test_parse_qualified_urn_rejects_a_rung_outside_the_ladder(rung: int) -> None:
    raw = f"eawf://{WORKSPACE}/{PROJECT}/_/claim/CLM-0004#rung-{rung}"
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(raw)
    assert excinfo.value.code is IdentityRejection.IDENTITY_KIND_MISMATCH


def test_parse_qualified_urn_rejects_a_rung_on_a_non_claim_kind() -> None:
    raw = f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/milestone/MLS-0030#rung-2"
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(raw)
    assert excinfo.value.code is IdentityRejection.IDENTITY_KIND_MISMATCH


def test_parse_qualified_urn_rejects_an_unaddressable_fragment() -> None:
    raw = f"eawf://{WORKSPACE}/{PROJECT}/_/claim/CLM-0004#step-2"
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(raw)
    assert excinfo.value.code is IdentityRejection.URN_MALFORMED


def test_parse_qualified_urn_rejects_the_epoch_one_urn_form() -> None:
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn("urn:eawf:v1:wave:P32-I01-W03")
    assert excinfo.value.code is IdentityRejection.URN_MALFORMED


def test_parse_qualified_urn_rejects_the_empty_string() -> None:
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn("")
    assert excinfo.value.code is IdentityRejection.URN_MALFORMED


def test_parse_qualified_urn_rejects_a_missing_entity_segment() -> None:
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/milestone")
    assert excinfo.value.code is IdentityRejection.URN_MALFORMED


def test_parse_qualified_urn_rejects_a_sixth_segment() -> None:
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/milestone/MLS-0030/extra")
    assert excinfo.value.code is IdentityRejection.URN_MALFORMED


def test_parse_qualified_urn_rejects_an_unknown_kind_token() -> None:
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/sprint/SPR-0001")
    assert excinfo.value.code is IdentityRejection.URN_UNKNOWN_KIND


def test_parse_qualified_urn_rejects_a_lowercase_workspace_slot() -> None:
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(f"eawf://wsp-default/{PROJECT}/{REPOSITORY}/milestone/MLS-0030")
    assert excinfo.value.code is IdentityRejection.SYMBOL_KEY_INVALID


def test_parse_qualified_urn_rejects_a_task_key_of_another_project() -> None:
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/task/OTHER-0001")
    assert excinfo.value.code is IdentityRejection.ENTITY_KEY_INVALID


def test_parse_qualified_urn_rejects_a_container_addressing_another_container() -> None:
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(f"eawf://{WORKSPACE}/{PROJECT}/_/workspace/WSP-OTHER")
    assert excinfo.value.code is IdentityRejection.ENTITY_KEY_INVALID


def test_format_qualified_urn_renders_a_repository_level_entity() -> None:
    assert (
        format_qualified_urn(
            workspace_key=WORKSPACE,
            project_key=PROJECT,
            repository_key=REPOSITORY,
            kind=EntityKind.MILESTONE,
            entity_key="MLS-0030",
        )
        == f"eawf://{WORKSPACE}/{PROJECT}/{REPOSITORY}/milestone/MLS-0030"
    )


def test_format_qualified_urn_renders_the_reserved_slot_and_a_rung() -> None:
    assert (
        format_qualified_urn(
            workspace_key=WORKSPACE,
            project_key=PROJECT,
            repository_key=None,
            kind=EntityKind.CLAIM,
            entity_key="CLM-0004",
            rung=2,
        )
        == f"eawf://{WORKSPACE}/{PROJECT}/_/claim/CLM-0004#rung-2"
    )


def test_format_qualified_urn_rejects_a_rung_on_a_non_claim_kind() -> None:
    with pytest.raises(IdentityError) as excinfo:
        format_qualified_urn(
            workspace_key=WORKSPACE,
            project_key=PROJECT,
            repository_key=REPOSITORY,
            kind=EntityKind.TASK,
            entity_key="EAWF-0001",
            rung=1,
        )
    assert excinfo.value.code is IdentityRejection.IDENTITY_KIND_MISMATCH


def test_qualified_urn_validates_on_direct_construction() -> None:
    with pytest.raises(IdentityError) as excinfo:
        QualifiedUrn(
            workspace_key=WORKSPACE,
            project_key=PROJECT,
            repository_key=None,
            kind=EntityKind.MILESTONE,
            entity_key="MLS-0030",
        )
    assert excinfo.value.code is IdentityRejection.REPOSITORY_SLOT_INVALID
