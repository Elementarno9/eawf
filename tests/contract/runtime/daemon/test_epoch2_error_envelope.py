"""Every native domain answer is one strict envelope carrying stable codes.

A machine caller branches on the answer without knowing whether the
mutation committed, so the shape is fixed in both directions: the status
says which, the result carries a commit's receipt, and the error rows
carry a refusal. What makes the error rows usable is that their codes come
from a closed list, so a client can route on them instead of matching
prose.

Two totality checks live here as well. Every code the transaction refuses
with must be a declared domain code, or a refusal would reach the wire
under a name nobody published; and every denial the transition registry
can return must map to one, or a guard failure would fall through to an
internal error.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.transitions import DenialCode
from eawf.runtime.daemon.epoch2_transaction import (
    _STRUCTURAL_DENIALS,
    MutationReceipt,
    TransactionRefusalCode,
    TransactionRefusedError,
)
from eawf.runtime.daemon.methods.domain_envelope import (
    DOMAIN_TRANSITION_METHOD,
    ENVELOPE_SCHEMA_VERSION,
    REFUSAL_ERROR_CODES,
    DomainEnvelope,
    DomainError,
    DomainErrorCode,
    DomainStatus,
    accepted_envelope,
    refused_envelope,
)

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
ENTITY = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030"

#: The stable domain errors the packet publishes. Held verbatim here so a
#: rename in the enum has to be a deliberate edit of the contract too.
PACKET_CODES = frozenset(
    {
        "schema_validation_failed",
        "workspace_not_registered",
        "workspace_ambiguous",
        "project_not_member",
        "identity_not_found",
        "identity_kind_mismatch",
        "legacy_identity_read_only",
        "revision_conflict",
        "idempotency_conflict",
        "illegal_transition",
        "transition_guard_failed",
        "child_scope_active",
        "proof_stale",
        "merge_truth_ambiguous",
        "protected_approval_required",
        "migration_not_quiescent",
        "migration_source_changed",
        "migration_count_mismatch",
        "migration_reference_unresolved",
        "migration_fabrication_detected",
        "migration_idempotence_failed",
        "legacy_operation_removed",
        "rollback_boundary_crossed",
        "native_authority_required",
    }
)


def _receipt() -> MutationReceipt:
    return MutationReceipt(
        event_name="domain.milestone.activated",
        entity_ref=ENTITY,
        revision_before=1,
        revision_after=2,
        canonical_sequence=7,
        event_id="evt-0001",
        idempotency_key="req-0001",
        occurred_at=AT,
        wal_record_id="wal-0001",
    )


def _refusal(**overrides: object) -> TransactionRefusedError:
    payload: dict[str, object] = {
        "code": TransactionRefusalCode.ILLEGAL_TRANSITION,
        "detail": "milestone PLANNED -> COMPLETED is not a registered edge",
        "entity_ref": ENTITY,
        "remediation": "Move along a registered edge.",
        "revision": 1,
    }
    payload.update(overrides)
    return TransactionRefusedError(**payload)  # type: ignore[arg-type]


def test_domain_error_codes_are_exactly_the_packet_list() -> None:
    assert {code.value for code in DomainErrorCode} == PACKET_CODES


def test_every_refusal_code_is_a_declared_domain_code() -> None:
    """A code the transaction can raise but nobody declared would reach a client."""
    assert set(REFUSAL_ERROR_CODES) == set(TransactionRefusalCode)
    assert {code.value for code in REFUSAL_ERROR_CODES.values()} <= PACKET_CODES


def test_every_registry_denial_maps_to_a_refusal_code() -> None:
    """A guard denial with no mapping would fall through as an internal error."""
    mapped = {
        code: _STRUCTURAL_DENIALS.get(code, TransactionRefusalCode.TRANSITION_GUARD_FAILED)
        for code in DenialCode
    }
    assert set(mapped) == set(DenialCode)
    assert all(code in REFUSAL_ERROR_CODES for code in mapped.values())


def test_accepted_envelope_carries_the_receipt_and_both_revisions() -> None:
    envelope = accepted_envelope(_receipt(), operation=DOMAIN_TRANSITION_METHOD)

    assert envelope.schema_version == ENVELOPE_SCHEMA_VERSION
    assert envelope.status is DomainStatus.OK
    assert envelope.operation == DOMAIN_TRANSITION_METHOD
    assert (envelope.revision_before, envelope.revision_after) == (1, 2)
    assert envelope.errors == ()
    assert envelope.result is not None
    assert envelope.result["canonical_sequence"] == 7


def test_refused_envelope_carries_one_row_and_moves_no_revision() -> None:
    envelope = refused_envelope(_refusal(), operation=DOMAIN_TRANSITION_METHOD)

    assert envelope.status is DomainStatus.ERROR
    assert envelope.result is None
    assert envelope.revision_before == envelope.revision_after == 1
    assert len(envelope.errors) == 1
    row = envelope.errors[0]
    assert row.code is DomainErrorCode.ILLEGAL_TRANSITION
    assert row.entity_ref == ENTITY
    assert row.remediation == "Move along a registered edge."


def test_refused_envelope_carries_the_guard_when_a_predicate_was_reached() -> None:
    envelope = refused_envelope(
        _refusal(
            code=TransactionRefusalCode.TRANSITION_GUARD_FAILED,
            guard="host_merge_observed",
        ),
        operation=DOMAIN_TRANSITION_METHOD,
    )

    assert envelope.errors[0].guard == "host_merge_observed"
    assert envelope.errors[0].code is DomainErrorCode.TRANSITION_GUARD_FAILED


def test_refused_envelope_omits_the_revision_when_no_record_was_read() -> None:
    """A refusal before the re-read asserts no revision rather than inventing one."""
    envelope = refused_envelope(
        _refusal(code=TransactionRefusalCode.IDENTITY_NOT_FOUND, revision=None),
        operation=DOMAIN_TRANSITION_METHOD,
    )

    assert envelope.revision_before is None
    assert envelope.revision_after is None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"unknown": "field"}, id="unknown-field"),
        pytest.param({"status": "maybe"}, id="status-outside-the-enum"),
        pytest.param({"operation": ""}, id="empty-operation"),
        pytest.param({"revision_before": 0}, id="revision-below-one"),
        pytest.param({"schema_version": 1}, id="non-string-schema-version"),
    ],
)
def test_envelope_rejects_a_drifted_shape(overrides: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "status": DomainStatus.OK.value,
        "operation": DOMAIN_TRANSITION_METHOD,
    }
    payload.update(overrides)

    with pytest.raises(ValidationError):
        DomainEnvelope.model_validate(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"code": "not_a_declared_code"}, id="undeclared-code"),
        pytest.param({"message": ""}, id="empty-message"),
        pytest.param({"entity_ref": ""}, id="empty-entity-ref"),
        pytest.param({"remediation": ""}, id="empty-remediation"),
        pytest.param({"guard": ""}, id="empty-guard"),
        pytest.param({"trace": "..."}, id="unknown-field"),
    ],
)
def test_error_row_rejects_a_drifted_shape(overrides: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "code": DomainErrorCode.REVISION_CONFLICT.value,
        "message": "the record is at revision 2 but the request expects 1",
        "entity_ref": ENTITY,
        "remediation": "Re-read the record and retry.",
    }
    payload.update(overrides)

    with pytest.raises(ValidationError):
        DomainError.model_validate(payload)


def test_error_row_requires_a_remediation() -> None:
    """A code without a way forward tells an operator only that it failed."""
    with pytest.raises(ValidationError):
        DomainError.model_validate(
            {
                "code": DomainErrorCode.REVISION_CONFLICT.value,
                "message": "stale",
                "entity_ref": ENTITY,
            }
        )


def test_refusal_message_leads_with_its_stable_code() -> None:
    refusal = _refusal()

    assert str(refusal).startswith("validation_failed: illegal_transition: ")
    assert refusal.code is TransactionRefusalCode.ILLEGAL_TRANSITION
