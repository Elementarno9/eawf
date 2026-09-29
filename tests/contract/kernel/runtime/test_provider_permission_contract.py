"""The provider permission's contract: what the record refuses to hold.

These are the negative fixtures a surface binds against. A record that
could carry a hold, an eawf-owned deadline or a repository authority over
an account verb would let a surface offer the affordance; the record
refuses each shape instead, so the surface has nothing to render it from.
The daemon's own refusal of the hold verb is asserted separately in
``tests/integration/runtime/daemon/test_provider_permission.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import pytest
from pydantic import ValidationError

from eawf.kernel.identity import EntityKind
from eawf.kernel.runtime.permission import (
    PROTECTED_ACTION_REQUIRED,
    REPOSITORY_BARRED_CLASSES,
    PermissionActionClass,
    PermissionRefusalCode,
    PermissionRefusalError,
    PermissionVerb,
    PrincipalClass,
    ProviderPermission,
    decide_permission,
    expire_permission,
)
from eawf.kernel.state.epoch2.values import Hold

AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
DEADLINE: Final = AT + timedelta(seconds=45)
RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
PERM_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/permission/PERM-0001"
OPERATOR_ONLY: Final = {"approve": ["operator"], "deny": ["operator"]}


def permission_fields(**overrides: Any) -> dict[str, Any]:
    """Return one open permission's fields, overriding whichever is under test."""
    fields: dict[str, Any] = {
        "uid": UUID(int=1),
        "key": "PERM-0001",
        "urn": PERM_URN,
        "run_ref": RUN_URN,
        "call_ref": "call-00000000000000a1",
        "tool_id": "run_scoped_command",
        "action_class": "tool",
        "request_scope": "run the test suite once",
        "deadline_at": DEADLINE,
        "approval_authority": OPERATOR_ONLY,
        "opened_at": AT,
    }
    fields.update(overrides)
    return fields


def permission(**overrides: Any) -> ProviderPermission:
    """Return one validated open permission."""
    return ProviderPermission.model_validate(permission_fields(**overrides))


def decide(
    record: ProviderPermission, verb: PermissionVerb, **overrides: Any
) -> ProviderPermission:
    """Apply *verb* as an operator a second before the deadline."""
    arguments: dict[str, Any] = {
        "verb": verb,
        "principal_class": PrincipalClass.OPERATOR,
        "principal_ref": "OP-0001",
        "expected_revision": 1,
        "now": DEADLINE - timedelta(seconds=1),
    }
    arguments.update(overrides)
    return decide_permission(record, **arguments)


# ---------------------------------------------------------------------------
# RUN-051: the record's own family, deadline and authority
# ---------------------------------------------------------------------------


def test_run_051_the_deadline_is_the_provider_s_and_no_hold_is_supported() -> None:
    record = permission()

    assert (record.deadline_owner, record.hold_supported) == ("provider", False)
    assert record.expiry_outcome == "provider_denied"


@pytest.mark.parametrize(
    ("field", "value"),
    [("hold_supported", True), ("deadline_owner", "eawf"), ("expiry_outcome", "approved")],
)
def test_run_051_a_record_that_could_be_held_does_not_validate(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        ProviderPermission.model_validate(permission_fields(**{field: value}))


def test_run_051_a_pending_action_urn_is_not_a_permission_urn() -> None:
    with pytest.raises(ValidationError, match="urn"):
        permission(urn="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001")


@pytest.mark.parametrize("key", ["TOOL-0401", "PERM-1", "ACT-0001", ""])
def test_run_051_only_a_canonical_perm_key_is_admitted(key: str) -> None:
    with pytest.raises(ValidationError, match="key"):
        permission(key=key)


def test_run_051_the_urn_must_address_the_record_s_own_key() -> None:
    with pytest.raises(ValidationError, match="urn addresses PERM-0001, not PERM-0002"):
        permission(key="PERM-0002")


def test_run_051_an_empty_authority_for_a_verb_is_refused() -> None:
    with pytest.raises(ValidationError, match="approve"):
        permission(approval_authority={"approve": [], "deny": ["operator"]})


def test_run_051_a_class_named_twice_for_a_verb_is_refused() -> None:
    with pytest.raises(ValidationError, match="approve"):
        permission(approval_authority={"approve": ["operator", "operator"], "deny": ["operator"]})


def test_run_051_approve_and_deny_carry_separate_authorities() -> None:
    record = permission(approval_authority={"approve": ["operator"], "deny": ["repository"]})

    denied = decide(record, PermissionVerb.DENY, principal_class=PrincipalClass.REPOSITORY)

    assert denied.resolution is not None
    assert denied.resolution.decision == "denied"
    with pytest.raises(PermissionRefusalError, match="authority_denied"):
        decide(record, PermissionVerb.APPROVE, principal_class=PrincipalClass.REPOSITORY)


def test_run_051_an_answer_one_instant_before_the_deadline_stands() -> None:
    approved = decide(permission(), PermissionVerb.APPROVE)

    assert approved.resolution is not None
    assert (approved.resolution.decision, approved.revision) == ("approved", 2)


def test_run_051_an_answer_at_the_deadline_is_refused_as_lapsed() -> None:
    with pytest.raises(PermissionRefusalError) as refused:
        decide(permission(), PermissionVerb.APPROVE, now=DEADLINE)

    assert refused.value.code is PermissionRefusalCode.LAPSED


def test_run_051_a_lapse_outranks_a_stale_revision() -> None:
    with pytest.raises(PermissionRefusalError) as refused:
        decide(permission(), PermissionVerb.APPROVE, now=DEADLINE, expected_revision=7)

    assert refused.value.code is PermissionRefusalCode.LAPSED


def test_run_051_the_lapse_is_recorded_at_the_deadline_by_the_provider() -> None:
    expired = expire_permission(permission(), now=DEADLINE)

    assert expired is not None
    assert expired.resolution is not None
    assert (expired.resolution.decision, expired.resolution.decided_by) == ("expired", "provider")
    assert expired.revision == 2


def test_run_051_nothing_lapses_one_instant_before_the_deadline() -> None:
    assert expire_permission(permission(), now=DEADLINE - timedelta(microseconds=1)) is None


def test_run_051_an_answered_permission_does_not_lapse_afterwards() -> None:
    approved = decide(permission(), PermissionVerb.APPROVE)

    assert expire_permission(approved, now=DEADLINE + timedelta(hours=1)) is None


def test_run_051_an_expiry_dated_before_the_deadline_does_not_validate() -> None:
    with pytest.raises(ValidationError, match="disagrees with the provider deadline"):
        permission(
            resolution={"decision": "expired", "decided_by": "provider", "decided_at": AT},
            revision=2,
        )


def test_run_051_an_answer_dated_after_the_deadline_does_not_validate() -> None:
    with pytest.raises(ValidationError, match="disagrees with the provider deadline"):
        permission(
            resolution={
                "decision": "approved",
                "decided_by": "principal",
                "principal_class": "operator",
                "principal_ref": "OP-0001",
                "decided_at": DEADLINE,
            },
            revision=2,
        )


def test_run_051_an_answer_that_names_no_principal_does_not_validate() -> None:
    with pytest.raises(ValidationError, match="names the principal and its class"):
        permission(
            resolution={"decision": "approved", "decided_by": "principal", "decided_at": AT},
            revision=2,
        )


def test_run_051_a_resolved_permission_takes_no_second_decision() -> None:
    denied = decide(permission(), PermissionVerb.DENY)

    with pytest.raises(PermissionRefusalError) as refused:
        decide(denied, PermissionVerb.APPROVE, expected_revision=2)

    assert refused.value.code is PermissionRefusalCode.ALREADY_RESOLVED


def test_run_051_a_decision_against_another_revision_is_refused() -> None:
    with pytest.raises(PermissionRefusalError) as refused:
        decide(permission(), PermissionVerb.APPROVE, expected_revision=2)

    assert refused.value.code is PermissionRefusalCode.STALE_REVISION


# ---------------------------------------------------------------------------
# RUN-052: no hold anywhere, and no repository approval on an account verb
# ---------------------------------------------------------------------------


def test_run_052_the_hold_verb_is_refused_on_the_kind() -> None:
    with pytest.raises(PermissionRefusalError) as refused:
        decide(permission(), PermissionVerb.HOLD)

    assert refused.value.code is PermissionRefusalCode.PROTECTED_ACTION_REQUIRED
    assert refused.value.code.value == PROTECTED_ACTION_REQUIRED
    assert "deadline is owned by the provider and expires" in refused.value.reason


def test_run_052_the_hold_verb_is_refused_before_any_state_is_read() -> None:
    lapsed_and_stale = permission()

    with pytest.raises(PermissionRefusalError) as refused:
        decide(lapsed_and_stale, PermissionVerb.HOLD, now=DEADLINE, expected_revision=9)

    assert refused.value.code is PermissionRefusalCode.PROTECTED_ACTION_REQUIRED


def test_run_052_no_hold_record_can_name_a_provider_permission() -> None:
    with pytest.raises(ValidationError, match=PROTECTED_ACTION_REQUIRED):
        Hold.model_validate(
            {
                "hold_id": UUID(int=7),
                "scope": {"kind": EntityKind.PERMISSION.value, "urn": PERM_URN},
                "reason": {"code": "operator-pause", "message": "pause it"},
                "created_by": {"principal_kind": "operator", "principal_id": "OP-0001"},
                "created_at": AT,
            }
        )


def test_run_052_a_hold_on_a_run_is_still_a_hold() -> None:
    hold = Hold.model_validate(
        {
            "hold_id": UUID(int=7),
            "scope": {"kind": EntityKind.RUN.value, "urn": RUN_URN},
            "reason": {"code": "operator-pause", "message": "pause it"},
            "created_by": {"principal_kind": "operator", "principal_id": "OP-0001"},
            "created_at": AT,
        }
    )

    assert hold.scope.kind is EntityKind.RUN


@pytest.mark.parametrize("action_class", sorted(REPOSITORY_BARRED_CLASSES))
@pytest.mark.parametrize("verb", ["approve", "deny"])
def test_run_052_no_account_verb_admits_a_repository_principal(
    action_class: PermissionActionClass, verb: str
) -> None:
    authority = {"approve": ["operator"], "deny": ["operator"]}
    authority[verb] = ["operator", "repository"]

    with pytest.raises(ValidationError, match="admits no repository principal"):
        permission(action_class=action_class.value, approval_authority=authority)


@pytest.mark.parametrize("action_class", sorted(REPOSITORY_BARRED_CLASSES))
def test_run_052_repository_may_approve_is_false_for_every_account_verb(
    action_class: PermissionActionClass,
) -> None:
    record = permission(action_class=action_class.value)

    assert record.repository_may_approve is False
    assert record.approval_authority.approve == (PrincipalClass.OPERATOR,)


def test_run_052_repository_may_approve_follows_the_approve_authority() -> None:
    listed = permission(approval_authority={"approve": ["repository"], "deny": ["operator"]})
    unlisted = permission(approval_authority={"approve": ["operator"], "deny": ["repository"]})

    assert (listed.repository_may_approve, unlisted.repository_may_approve) == (True, False)


def test_run_052_the_barred_classes_are_exactly_auth_credential_and_cost_basis() -> None:
    assert {item.value for item in REPOSITORY_BARRED_CLASSES} == {
        "auth",
        "credential",
        "cost_basis",
    }
