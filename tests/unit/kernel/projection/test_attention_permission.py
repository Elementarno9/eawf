"""The attention reducer lists an open provider permission for the principals who may decide it.

RUN-051: a provider permission is consumed by the attention projection. An open one lands
under ``needs operator`` in the ``permission`` need and is announced as
``needs_permission``; a resolved one needs nobody; and it is addressed to a console's
principal only when the operator class may approve or deny it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import pytest

from eawf.kernel.projection.attention import (
    AttentionBucket,
    AttentionNeedKind,
    NotificationClass,
    attention_all,
    attention_mine,
    build_attention_view,
)
from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.registers import ATTENTION_ROUTE, RegisterView, build_register_view
from eawf.kernel.runtime.permission import ProviderPermission
from eawf.kernel.store.tiers import Epoch2Collection

pytestmark = pytest.mark.unit

AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
OPERATOR: Final = "OP-0001"


def permission_row(key: str = "PERM-0001", **overrides: Any) -> dict[str, Any]:
    """Return one permission as the daemon supplies it to the Attention register."""
    fields: dict[str, Any] = {
        "uid": UUID(int=int(key[-4:])),
        "key": key,
        "urn": f"eawf://WSP-MAIN/PRJ-EAWF/_/permission/{key}",
        "run_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010",
        "call_ref": "call-00000000000000a1",
        "tool_id": "bash",
        "action_class": "tool",
        "request_scope": "Bash: Run the test suite",
        "deadline_at": AT + timedelta(seconds=60),
        "approval_authority": {"approve": ["operator"], "deny": ["operator"]},
        "opened_at": AT,
    }
    fields.update(overrides)
    record = ProviderPermission.model_validate(fields)
    return {
        **record.model_dump(mode="json"),
        "status": "open" if record.resolution is None else record.resolution.decision,
        "repository_may_approve": record.repository_may_approve,
    }


def register(*rows: dict[str, Any]) -> RegisterView:
    """Return the Attention register holding *rows* as its permissions."""
    projection = build_route_projection(
        route=ATTENTION_ROUTE,
        document={},
        cursor=3,
        scope_id="root-0001",
        generated_at=AT,
        ledger_rows={Epoch2Collection.PERMISSION: rows},
    )
    return build_register_view(projection)


def test_run_051_an_open_permission_needs_the_operator() -> None:
    (item,) = build_attention_view(register(permission_row())).items

    assert (item.key, item.revision) == ("PERM-0001", 1)
    assert (item.bucket, item.need) == (
        AttentionBucket.NEEDS_OPERATOR,
        AttentionNeedKind.PERMISSION,
    )
    assert item.notification_class is NotificationClass.NEEDS_PERMISSION
    assert item.source_ref.endswith("/permission/PERM-0001")


def test_run_051_the_register_row_states_the_deadline_and_the_authorities() -> None:
    (row,) = register(permission_row()).rows

    assert row.title == "Bash: Run the test suite"
    assert row.facts["kind"] == "provider_permission"
    assert row.facts["subject"] == "RUN-00000010"
    assert (row.facts["approve"], row.facts["deny"]) == ("operator", "operator")
    assert row.facts["repository_may_approve"] == "no"
    assert row.facts["deadline_owner"] == "provider"
    assert row.facts["deadline_at"].startswith("2026-09-29T12:01:00")


def test_run_051_a_resolved_permission_needs_nobody() -> None:
    resolution = {
        "decision": "denied",
        "decided_by": "principal",
        "principal_class": "operator",
        "principal_ref": OPERATOR,
        "decided_at": AT,
    }

    held = register(permission_row(resolution=resolution, revision=2))

    assert build_attention_view(held).items == ()
    assert attention_all(held).value == "0"


def test_run_051_it_is_mine_when_the_operator_may_decide_it() -> None:
    held = register(permission_row())

    assert attention_mine(held, principal=OPERATOR).value == "1"
    assert attention_mine(held, principal="OP-0002").value == "1"


def test_run_051_it_is_nobody_s_on_a_console_when_only_the_repository_may_decide_it() -> None:
    repository_only = {"approve": ["repository"], "deny": ["repository"]}
    held = register(permission_row(approval_authority=repository_only))

    assert attention_mine(held, principal=OPERATOR).value == "0"
    assert attention_all(held).value == "1"


def test_run_051_an_operator_who_may_only_deny_is_still_addressed() -> None:
    split = {"approve": ["repository"], "deny": ["operator"]}

    held = register(permission_row(approval_authority=split))

    assert attention_mine(held, principal=OPERATOR).value == "1"


def test_run_051_permissions_list_in_key_order_under_their_bucket() -> None:
    items = build_attention_view(
        register(permission_row("PERM-0002"), permission_row("PERM-0001"))
    ).items

    assert [item.key for item in items] == ["PERM-0001", "PERM-0002"]


def test_run_051_an_empty_register_lists_no_permission() -> None:
    assert build_attention_view(register()).items == ()
