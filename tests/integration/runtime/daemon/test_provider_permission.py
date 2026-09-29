"""The daemon owns the provider permission: its key, its lapse and its refusals.

Every case drives the real ``runtime.permission.*`` verbs against a
provisioned canary. The provider's deadline is placed relative to the
real clock, far ahead for a record that must stay open and a few
milliseconds ahead for one that must lapse, so the lapse is crossed by
waiting past a deadline the test chose rather than by patching a clock.

The hold refusal is asserted here, on the daemon, independently of the
read projection a surface binds to: passing one cannot stand in for the
other.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.permission import PROTECTED_ACTION_REQUIRED
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.permission import (
    PERMISSION_DECIDE_METHOD,
    PERMISSION_EXPIRE_METHOD,
    PERMISSION_OPEN_METHOD,
    PERMISSION_READ_METHOD,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"
OPERATOR_ONLY: Final = {"approve": ["operator"], "deny": ["operator"]}
REPOSITORY_TOO: Final = {"approve": ["operator", "repository"], "deny": ["repository"]}

#: How far ahead a deadline sits that no test outlasts.
FAR: Final = timedelta(hours=1)

#: How far ahead a deadline sits that a test waits past.
NEAR: Final = timedelta(milliseconds=50)


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one running Run."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(runtime_root)


def call(method: str, ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb the way the server does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


def open_one(
    ctx: MethodContext,
    canary: CanaryProvision,
    *,
    ahead: timedelta = FAR,
    action_class: str = "tool",
    authority: dict[str, list[str]] = OPERATOR_ONLY,
) -> dict[str, Any]:
    """Record one call the provider holds, with a deadline *ahead* of now."""
    return call(
        PERMISSION_OPEN_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        call_ref="call-00000000000000a1",
        tool_id="run_scoped_command",
        action_class=action_class,
        request_scope="run the test suite once",
        deadline_at=(datetime.now(UTC) + ahead).isoformat(),
        approval_authority=authority,
    )


def decide(
    ctx: MethodContext,
    canary: CanaryProvision,
    urn: str,
    verb: str,
    *,
    principal_class: str = "operator",
    revision: int = 1,
) -> dict[str, Any]:
    """Ask the daemon to apply one principal's verb."""
    return call(
        PERMISSION_DECIDE_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=urn,
        verb=verb,
        principal_class=principal_class,
        actor=ACTOR,
        expected_revision=revision,
    )


def read(ctx: MethodContext, canary: CanaryProvision) -> list[dict[str, Any]]:
    """Return the Run's permissions as a surface reads them."""
    answer = call(PERMISSION_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    return list(answer["permissions"])


def permission_lines(canary: CanaryProvision, runtime_root: Path) -> list[dict[str, Any]]:
    """Return every permission line the run ledger holds, in file order."""
    context = root_context(canary, runtime_root)
    with context.session([RUN_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    return [
        item.payload
        for item in records
        if item.payload.get("payload_kind") == "provider_permission"
    ]


def wait_past(deadline: timedelta) -> None:
    """Sleep until a deadline placed *deadline* ahead has certainly passed."""
    time.sleep(deadline.total_seconds() * 2)


# ---------------------------------------------------------------------------
# RUN-051: a typed daemon-owned record with its own URN family and deadline
# ---------------------------------------------------------------------------


def test_run_051_a_held_call_is_recorded_under_its_own_urn_family(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    permission = open_one(ctx, canary)["permission"]

    assert permission["key"] == "PERM-0001"
    assert permission["urn"] == "eawf://WSP-MAIN/PRJ-EAWF/_/permission/PERM-0001"
    assert permission["run_ref"] == RUN_URN
    assert permission["deadline_owner"] == "provider"
    assert permission["resolution"] is None


def test_run_051_keys_are_allocated_monotonically(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    first = open_one(ctx, canary)["permission"]["key"]
    decide(ctx, canary, open_one(ctx, canary)["permission"]["urn"], "deny")
    third = open_one(ctx, canary)["permission"]["key"]

    assert (first, third) == ("PERM-0001", "PERM-0003")


def test_run_051_an_approval_is_recorded_as_a_new_revision(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    urn = open_one(ctx, canary)["permission"]["urn"]

    decided = decide(ctx, canary, urn, "approve")["permission"]

    assert decided["resolution"]["decision"] == "approved"
    assert decided["resolution"]["principal_ref"] == ACTOR
    assert decided["revision"] == 2
    assert [line["revision"] for line in permission_lines(canary, runtime_root)] == [1, 2]


def test_run_051_resolving_a_permission_writes_nothing_but_the_permission(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    urn = open_one(ctx, canary)["permission"]["urn"]
    context = root_context(canary, runtime_root)
    with context.session([RUN_URN]) as session:
        before = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
        document = session.read_document()

    decide(ctx, canary, urn, "approve")

    with context.session([RUN_URN]) as session:
        after = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
        assert session.read_document().get("pending_action") == document.get("pending_action")
    added = after[len(before) :]
    assert [line.payload["payload_kind"] for line in added] == ["provider_permission"]


def test_run_051_a_lapse_is_recorded_as_expired_by_the_provider(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    open_one(ctx, canary, ahead=NEAR)
    wait_past(NEAR)

    answer = call(PERMISSION_EXPIRE_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)

    assert answer["expired"] == ["PERM-0001"]
    resolution = answer["permissions"][0]["permission"]["resolution"]
    assert (resolution["decision"], resolution["decided_by"]) == ("expired", "provider")
    assert resolution["principal_ref"] is None


def test_run_051_an_expiry_is_recorded_once(canary: CanaryProvision, ctx: MethodContext) -> None:
    open_one(ctx, canary, ahead=NEAR)
    wait_past(NEAR)
    call(PERMISSION_EXPIRE_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)

    again = call(PERMISSION_EXPIRE_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)

    assert again["expired"] == []


def test_run_051_an_open_permission_is_not_expired_early(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    open_one(ctx, canary)

    answer = call(PERMISSION_EXPIRE_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)

    assert answer["expired"] == []
    assert answer["permissions"][0]["permission"]["resolution"] is None


def test_run_051_a_late_answer_records_the_expiry_and_is_refused(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    urn = open_one(ctx, canary, ahead=NEAR)["permission"]["urn"]
    wait_past(NEAR)

    with pytest.raises(DaemonValidationError, match="permission_lapsed"):
        decide(ctx, canary, urn, "approve")

    lines = permission_lines(canary, runtime_root)
    assert lines[-1]["resolution"]["decision"] == "expired"


def test_run_051_a_deadline_already_passed_is_not_recorded(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    with pytest.raises(DaemonValidationError, match="permission_lapsed"):
        open_one(ctx, canary, ahead=-FAR)

    assert permission_lines(canary, runtime_root) == []


def test_run_051_a_stopped_run_holds_no_call(tmp_path: Path, ctx: MethodContext) -> None:
    provisioned = provision(tmp_path / "stopped")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "COMPLETED")}})

    with pytest.raises(DaemonValidationError, match="illegal_transition"):
        open_one(ctx, provisioned)


def test_run_051_a_decision_against_a_stale_revision_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    urn = open_one(ctx, canary)["permission"]["urn"]

    with pytest.raises(DaemonValidationError, match="stale_revision"):
        decide(ctx, canary, urn, "approve", revision=2)


def test_run_051_a_second_decision_is_refused(canary: CanaryProvision, ctx: MethodContext) -> None:
    urn = open_one(ctx, canary)["permission"]["urn"]
    decide(ctx, canary, urn, "deny")

    with pytest.raises(DaemonValidationError, match="permission_already_resolved"):
        decide(ctx, canary, urn, "approve", revision=2)


def test_run_051_an_unknown_permission_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        decide(ctx, canary, "eawf://WSP-MAIN/PRJ-EAWF/_/permission/PERM-0009", "approve")


def test_run_051_a_pending_action_urn_is_not_a_permission(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="urn"):
        decide(ctx, canary, "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001", "approve")


# ---------------------------------------------------------------------------
# RUN-052: the daemon refuses the hold on the target kind
# ---------------------------------------------------------------------------


def test_run_052_the_daemon_refuses_a_hold_on_a_provider_permission(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    urn = open_one(ctx, canary)["permission"]["urn"]

    with pytest.raises(DaemonValidationError) as refused:
        decide(ctx, canary, urn, "hold")

    assert PROTECTED_ACTION_REQUIRED in str(refused.value)
    assert "deadline is owned by the provider and expires" in str(refused.value)
    assert [line["revision"] for line in permission_lines(canary, runtime_root)] == [1]


def test_run_052_a_hold_is_refused_even_on_a_lapsed_permission(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    urn = open_one(ctx, canary, ahead=NEAR)["permission"]["urn"]
    wait_past(NEAR)

    with pytest.raises(DaemonValidationError, match=PROTECTED_ACTION_REQUIRED):
        decide(ctx, canary, urn, "hold")


def test_run_052_the_repository_approves_only_where_the_authority_lists_it(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    urn = open_one(ctx, canary, authority=REPOSITORY_TOO)["permission"]["urn"]

    decided = decide(ctx, canary, urn, "approve", principal_class="repository")

    assert decided["permission"]["resolution"]["principal_class"] == "repository"


def test_run_052_a_class_the_verb_does_not_list_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    urn = open_one(ctx, canary)["permission"]["urn"]

    with pytest.raises(DaemonValidationError, match="authority_denied"):
        decide(ctx, canary, urn, "approve", principal_class="repository")


@pytest.mark.parametrize("action_class", ["auth", "credential", "cost_basis"])
def test_run_052_a_repository_authority_on_an_account_verb_is_refused_at_open(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path, action_class: str
) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        open_one(ctx, canary, action_class=action_class, authority=REPOSITORY_TOO)

    assert permission_lines(canary, runtime_root) == []


@pytest.mark.parametrize("action_class", ["auth", "credential", "cost_basis"])
def test_run_052_the_read_names_the_authority_behind_a_disabled_repository_approve(
    canary: CanaryProvision, ctx: MethodContext, action_class: str
) -> None:
    open_one(ctx, canary, action_class=action_class)

    (row,) = read(ctx, canary)

    assert row["repository_may_approve"] is False
    assert row["approve_authority"] == ["operator"]
    assert row["hold_supported"] is False


def test_run_052_the_read_offers_repository_approval_where_the_authority_lists_it(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    open_one(ctx, canary, authority=REPOSITORY_TOO)

    (row,) = read(ctx, canary)

    assert row["repository_may_approve"] is True
    assert row["hold_supported"] is False


def test_run_052_a_run_with_no_permission_reads_empty(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    assert read(ctx, canary) == []
