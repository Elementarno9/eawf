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


SINGLE_KEY = [
    pytest.param("eawf://EAWF/task/EAWF-0042", "EAWF", id="task"),
    pytest.param("eawf://EAWF/repository/EAWF", "EAWF", id="repository"),
    pytest.param("eawf://EAWF/workspace/EAWF", None, id="workspace"),
    pytest.param("eawf://EAWF/claim/CLM-0001#rung-2", None, id="claim-rung"),
    pytest.param("eawf://E1/batch/BAT-0001", "E1", id="shortest-key"),
]


@pytest.mark.parametrize(("raw", "repository"), SINGLE_KEY)
def test_parse_qualified_urn_reads_the_single_key_form_and_renders_it_back(
    raw: str, repository: str | None
) -> None:
    parsed = parse_qualified_urn(raw)
    assert parsed.single_key is True
    key = raw.removeprefix("eawf://").split("/", 1)[0]
    assert (parsed.workspace_key, parsed.project_key, parsed.repository_key) == (
        key,
        key,
        repository,
    )
    assert str(parsed) == raw


def test_parse_qualified_urn_keeps_reading_the_three_slot_form() -> None:
    parsed = parse_qualified_urn("eawf://EAWF/EAWF/EAWF/task/EAWF-0042")
    assert parsed.single_key is False
    assert str(parsed) == "eawf://EAWF/EAWF/EAWF/task/EAWF-0042"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        pytest.param("eawf://EAWF/task", IdentityRejection.URN_MALFORMED, id="two-segments"),
        pytest.param("eawf://EAWF/Task/EAWF-0042", IdentityRejection.URN_MALFORMED, id="kind-case"),
        pytest.param("eawf://EAWF/widget/W-1", IdentityRejection.URN_UNKNOWN_KIND, id="kind"),
        pytest.param("eawf://EAWF/task/DEMO-0042", None, id="task-of-another-project"),
        pytest.param("eawf://EAWF/workspace/OTHER", None, id="container-elsewhere"),
        pytest.param("eawf://EAWF/task/EAWF-0042#rung-1", None, id="rung-off-claim"),
    ],
)
def test_parse_qualified_urn_refuses_a_broken_single_key_urn(
    raw: str, code: IdentityRejection | None
) -> None:
    with pytest.raises(IdentityError) as excinfo:
        parse_qualified_urn(raw)
    if code is not None:
        assert excinfo.value.code is code


def test_a_single_key_urn_refuses_slots_that_disagree() -> None:
    with pytest.raises(IdentityError) as excinfo:
        QualifiedUrn(
            workspace_key=WORKSPACE,
            project_key=PROJECT,
            repository_key=REPOSITORY,
            kind=EntityKind.MILESTONE,
            entity_key="MLS-0030",
            single_key=True,
        )
    assert excinfo.value.code is IdentityRejection.URN_MALFORMED


def test_format_qualified_urn_renders_the_single_key_form() -> None:
    rendered = format_qualified_urn(
        workspace_key="EAWF",
        project_key="EAWF",
        repository_key="EAWF",
        kind=EntityKind.TASK,
        entity_key="EAWF-0042",
        single_key=True,
    )
    assert rendered == "eawf://EAWF/task/EAWF-0042"
