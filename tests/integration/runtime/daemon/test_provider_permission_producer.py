"""The provider permission's producer, its expiry sweep and its consumers, live.

A disposable epoch-2 canary holds one running Run whose vendor session is the
host session the permission hook names. The host's request is recorded through
the real ``runtime.host.permission.request`` verb, the lapse through the
daemon's own expiry sweep, and the record is read back through
``projection.attention.read`` and drawn by the console's Attention frame exactly
as a linked console draws it.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.attention import (
    AttentionBucket,
    AttentionItem,
    AttentionNeedKind,
    NotificationClass,
    attention_mine,
    build_attention_view,
)
from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.registers import ATTENTION_ROUTE, RegisterView, build_register_view
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.permission import (
    HOST_DECISION_WINDOW,
    HOST_PERMISSION_REQUEST_METHOD,
    PERMISSION_DECIDE_METHOD,
    PERMISSION_OPEN_METHOD,
)
from eawf.runtime.daemon.permission_expiry import sweep_once
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

SESSION: Final = "host-session-0001"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"
OPERATOR: Final = "OP-0001"


def _running_run(session: str | None) -> dict[str, Any]:
    """Return the seeded running Run, on the host *session* when one is named."""
    row = seed_row("run", "RUNNING")
    if session is not None:
        row["vendor_session"] = {
            "harness": "claude-code",
            "session_digest": hash_vendor_session_id(session),
        }
    return row


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one running Run on the host session."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {RUN_KEY: _running_run(SESSION)}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """One daemon context, shared by every call so the sweep sees the attached tree."""
    return method_context(tmp_path / "runtime")


def call(method: str, ctx: MethodContext, canary: CanaryProvision, **params: Any) -> Any:
    """Dispatch one daemon verb against the canary, the way a client does."""
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def request(
    ctx: MethodContext, canary: CanaryProvision, *, session: str = SESSION, **tool_input: Any
) -> dict[str, Any]:
    """Report one call the host is holding, as the permission hook does."""
    answer: dict[str, Any] = call(
        HOST_PERMISSION_REQUEST_METHOD,
        ctx,
        canary,
        harness="claude-code",
        host_session_id=session,
        tool_name="Bash",
        tool_input=tool_input or {"command": "pytest -q", "description": "Run the test suite"},
    )
    return answer


def permission_lines(canary: CanaryProvision, tmp_path: Path) -> list[dict[str, Any]]:
    """Return every permission line the run ledger holds, in file order."""
    context = root_context(canary, tmp_path / "runtime")
    with context.session([RUN_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    return [r.payload for r in records if r.payload.get("payload_kind") == "provider_permission"]


def attention(ctx: MethodContext, canary: CanaryProvision) -> RegisterView:
    """Read the Attention register through the daemon's projection verb."""
    answer = call(f"projection.{ATTENTION_ROUTE}.read", ctx, canary)
    return build_register_view(RouteProjection.model_validate(answer))


def frame(register: RegisterView) -> list[str]:
    """Render the Attention route of a linked console acting as the operator."""
    session = Session()
    session.route = ATTENTION_ROUTE
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=160,
        h=30,
        register=register,
        attention=register,
        linked=True,
        principal=OPERATOR,
    )
    return render_route(view)


# ---------------------------------------------------------------------------
# RUN-051: the producer -- the host's held call becomes a record on its Run
# ---------------------------------------------------------------------------


def test_run_051_the_host_request_is_recorded_on_the_run_of_its_session(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    before = datetime.now(UTC)
    permission = request(ctx, canary)["permission"]

    assert (permission["key"], permission["run_ref"]) == ("PERM-0001", RUN_URN)
    assert permission["tool_id"] == "bash"
    assert permission["action_class"] == "tool"
    assert permission["request_scope"] == "Bash: Run the test suite"
    assert permission["deadline_owner"] == "provider"
    assert permission["approval_authority"] == {"approve": ["operator"], "deny": ["operator"]}
    deadline = datetime.fromisoformat(permission["deadline_at"])
    assert before + HOST_DECISION_WINDOW <= deadline <= datetime.now(UTC) + HOST_DECISION_WINDOW


def test_run_051_a_retried_host_request_is_one_record(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    first = request(ctx, canary)["permission"]
    again = request(ctx, canary)["permission"]
    other = request(ctx, canary, command="ls", description="List the files")["permission"]

    assert again["key"] == first["key"] == "PERM-0001"
    assert other["key"] == "PERM-0002"
    assert [line["key"] for line in permission_lines(canary, tmp_path)] == [
        "PERM-0001",
        "PERM-0002",
    ]


def test_run_051_a_file_write_is_a_filesystem_permission(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    permission = call(
        HOST_PERMISSION_REQUEST_METHOD,
        ctx,
        canary,
        harness="claude-code",
        host_session_id=SESSION,
        tool_name="Write",
        tool_input={"content": "x"},
    )["permission"]

    assert (permission["action_class"], permission["request_scope"]) == ("filesystem", "Write call")


def test_run_051_words_carrying_a_home_path_are_withheld(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    leaky = "cat /home/someone/notes"  # pragma: allowlist secret
    permission = request(ctx, canary, command=leaky)["permission"]

    assert permission["request_scope"].startswith("Bash call; its input carries a path")
    assert "someone" not in permission["request_scope"]


def test_run_051_a_session_on_no_live_run_is_refused_and_writes_nothing(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    with pytest.raises(DaemonValidationError, match="identity_not_found: 0 live Runs"):
        request(ctx, canary, session="another-session")

    assert permission_lines(canary, tmp_path) == []


def test_run_051_a_session_two_live_runs_share_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    twin = _running_run(SESSION)
    twin["key"] = "RUN-00000011"
    twin["urn"] = RUN_URN.replace(RUN_KEY, "RUN-00000011")
    seed(canary, {"run": {"RUN-00000011": twin}})

    with pytest.raises(DaemonValidationError, match="identity_not_found: 2 live Runs"):
        request(ctx, canary)


def test_run_051_an_unknown_harness_is_refused_at_the_boundary(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed: check harness"):
        call(
            HOST_PERMISSION_REQUEST_METHOD,
            ctx,
            canary,
            harness="opencode",
            host_session_id=SESSION,
            tool_name="Bash",
        )


# ---------------------------------------------------------------------------
# RUN-051: the expiry sweep -- a lapse nobody answered still ends in a record
# ---------------------------------------------------------------------------


def test_run_051_the_sweep_records_a_lapse_as_the_provider_s_expiry(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    near = timedelta(milliseconds=50)
    call(
        PERMISSION_OPEN_METHOD,
        ctx,
        canary,
        urn=RUN_URN,
        call_ref="call-00000000000000a1",
        tool_id="bash",
        action_class="tool",
        request_scope="run the test suite once",
        deadline_at=(datetime.now(UTC) + near).isoformat(),
        approval_authority={"approve": ["operator"], "deny": ["operator"]},
    )
    request(ctx, canary)
    time.sleep(near.total_seconds() * 2)

    expired = sweep_once(ctx, now=datetime.now(UTC))

    assert expired == ("PERM-0001",)
    latest = permission_lines(canary, tmp_path)[-1]
    assert latest["resolution"]["decision"] == "expired"
    assert latest["resolution"]["decided_by"] == "provider"
    assert sweep_once(ctx, now=datetime.now(UTC)) == ()


def test_run_051_the_sweep_leaves_an_open_deadline_alone(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    request(ctx, canary)

    assert sweep_once(ctx, now=datetime.now(UTC)) == ()
    assert [line["revision"] for line in permission_lines(canary, tmp_path)] == [1]


def test_run_051_the_sweep_of_a_daemon_with_no_tree_attached_is_empty(tmp_path: Path) -> None:
    assert sweep_once(method_context(tmp_path / "runtime"), now=datetime.now(UTC)) == ()


# ---------------------------------------------------------------------------
# RUN-051 / RUN-052: the consumers -- attention lists it, the console draws it
# ---------------------------------------------------------------------------


def _needing(register: RegisterView) -> tuple[AttentionItem, ...]:
    """Return the items a principal answers; the running Runs are listed beside them."""
    return tuple(
        i for i in build_attention_view(register).items if i.bucket is not AttentionBucket.ACTIVE
    )


def test_run_051_the_attention_register_lists_the_open_permission(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    request(ctx, canary)

    register = attention(ctx, canary)
    (item,) = _needing(register)

    assert (item.key, item.bucket, item.need) == (
        "PERM-0001",
        AttentionBucket.NEEDS_OPERATOR,
        AttentionNeedKind.PERMISSION,
    )
    assert item.notification_class is NotificationClass.NEEDS_PERMISSION
    assert attention_mine(register, principal=OPERATOR).value == "1"


def test_run_051_a_decided_permission_leaves_the_register(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    urn = request(ctx, canary)["permission"]["urn"]
    call(
        PERMISSION_DECIDE_METHOD,
        ctx,
        canary,
        urn=urn,
        verb="deny",
        principal_class="operator",
        actor=OPERATOR,
        expected_revision=1,
    )

    assert _needing(attention(ctx, canary)) == ()


def test_run_052_the_console_draws_the_permission_without_a_hold(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    request(ctx, canary)

    text = "\n".join(frame(attention(ctx, canary)))

    assert "PERM-0001" in text
    assert "Bash: Run the test suite" in text
    assert "repository approve disabled · approve is operator only" in text
    assert "no hold: the provider owns the deadline" in text
    assert "hold " not in text.replace("no hold:", "")
