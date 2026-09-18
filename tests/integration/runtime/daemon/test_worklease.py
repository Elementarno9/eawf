"""RUN-007 at the daemon door: no lease, no write; no agent, no extension.

The suite drives the registered ``workspace.lease.*`` verbs through the
daemon dispatcher against a provisioned canary whose repository is a real
git tree, so what it exercises is what a client on the socket reaches.

Nothing here waits. Expiry is driven by issuing a lease through the
library at an instant two hours in the past with a one-hour term, so the
deadline is already behind whatever the process clock reads when the verb
runs; the margin is an hour of elapsed time rather than a race, and the
assertion cannot flake on a slow machine. Contention is driven by a real
``threading.Barrier`` and the root's real advisory lock: two threads ask
for a lease over one Run at once and exactly one of them may have it.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.runtime.lease import LeaseStatus
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, dispatch
from eawf.runtime.daemon.methods.workspace_lease import (
    HEARTBEAT_METHOD,
    ISSUE_METHOD,
    MAX_LEASE_SECONDS,
    OPEN_METHOD,
    REVOKE_METHOD,
    SHOW_METHOD,
)
from eawf.runtime.daemon.native_guard import NativeAuthorityRefusedError
from eawf.runtime.workspace.lease import (
    LeaseRefusalCode,
    LeaseRefusedError,
    issue_lease,
    workspace_path,
)

pytestmark = pytest.mark.integration


AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
OTHER_RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"
TASK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
WRITABLE = ("src",)


def make_repo(root: Path) -> str:
    """Initialise a one-commit git repository at *root* and return its HEAD."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "ci"], cwd=root, check=True)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "module.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def make_canary(root: Path, *, code: str = "LSE") -> CanaryProvision:
    """Return a provisioned epoch-2 canary over a real git repository."""
    make_repo(root)
    return provision_canary(repo_root=root, ref=canary_ref(code), provisioned_at=AT)


def make_context(provisioned: CanaryProvision) -> tuple[MethodContext, Epoch2RootContext]:
    """Return the daemon context and the native root context of a canary."""
    ctx = MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=provisioned.runtime_dir / "wal",
    )
    return ctx, ctx.native_root_context(provisioned.root / ".ea")


def call(method: str, ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one registered verb the way the socket listener does."""
    return asyncio.run(dispatch(method, ctx, params))


@pytest.fixture
def canary(tmp_path: Path) -> tuple[CanaryProvision, MethodContext, Epoch2RootContext]:
    """Provision a canary with its daemon and native contexts."""
    provisioned = make_canary(tmp_path / "repo")
    ctx, context = make_context(provisioned)
    return provisioned, ctx, context


def issue(ctx: MethodContext, provisioned: CanaryProvision, **overrides: Any) -> dict[str, Any]:
    """Issue one lease through the registered verb."""
    params: dict[str, Any] = {
        "repo_root": str(provisioned.root),
        "run_ref": RUN_URN,
        "task_ref": TASK_URN,
        "purpose": RunPurpose.IMPLEMENT.value,
        "base": "main",
        "writable_roots": list(WRITABLE),
        "ttl_seconds": 3600,
    }
    params.update(overrides)
    return call(ISSUE_METHOD, ctx, params)


# ---- RUN-007: a mutating purpose writes only under an active lease ----------


def test_mutating_purpose_without_a_lease_is_refused(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """The contract itself: no lease named, so no path is produced."""
    provisioned, ctx, _ = canary
    with pytest.raises(DaemonValidationError, match=LeaseRefusalCode.LEASE_REQUIRED.value):
        call(
            OPEN_METHOD,
            ctx,
            {
                "repo_root": str(provisioned.root),
                "purpose": RunPurpose.IMPLEMENT.value,
                "relative": "src/module.py",
            },
        )


def test_mutating_purpose_with_an_unissued_lease_is_refused(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """A lease id the root never issued grants nothing."""
    provisioned, ctx, _ = canary
    with pytest.raises(DaemonValidationError, match=LeaseRefusalCode.LEASE_NOT_FOUND.value):
        call(
            OPEN_METHOD,
            ctx,
            {
                "repo_root": str(provisioned.root),
                "lease_id": "LSE-" + "0" * 32,
                "purpose": RunPurpose.IMPLEMENT.value,
                "relative": "src/module.py",
            },
        )


def test_mutating_purpose_with_an_expired_lease_is_refused(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """The deadline is already an hour behind the process clock."""
    provisioned, ctx, context = canary
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    lease = issue_lease(
        context,
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=WRITABLE,
        now=long_ago,
        ttl=timedelta(hours=1),
    )
    assert lease.status is LeaseStatus.ACTIVE
    with pytest.raises(DaemonValidationError, match=LeaseRefusalCode.LEASE_EXPIRED.value):
        call(
            OPEN_METHOD,
            ctx,
            {
                "repo_root": str(provisioned.root),
                "lease_id": lease.lease_id,
                "purpose": RunPurpose.IMPLEMENT.value,
                "relative": "src/module.py",
            },
        )


def test_mutating_purpose_with_a_revoked_lease_is_refused(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """A withdrawn lease stops granting the instant it is withdrawn."""
    provisioned, ctx, _ = canary
    lease = issue(ctx, provisioned)
    revoked = call(
        REVOKE_METHOD, ctx, {"repo_root": str(provisioned.root), "lease_id": lease["lease_id"]}
    )
    assert revoked["status"] == LeaseStatus.REVOKED.value
    with pytest.raises(DaemonValidationError, match=LeaseRefusalCode.LEASE_NOT_ACTIVE.value):
        call(
            OPEN_METHOD,
            ctx,
            {
                "repo_root": str(provisioned.root),
                "lease_id": lease["lease_id"],
                "purpose": RunPurpose.IMPLEMENT.value,
                "relative": "src/module.py",
            },
        )


@pytest.mark.parametrize(
    "purpose", [RunPurpose.REVIEW, RunPurpose.AUDIT, RunPurpose.RESEARCH, RunPurpose.OBSERVE]
)
def test_read_only_purpose_is_issued_no_workspace(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext], purpose: RunPurpose
) -> None:
    """A run with nothing to write is refused a place to write it."""
    provisioned, ctx, _ = canary
    with pytest.raises(DaemonValidationError, match=LeaseRefusalCode.PURPOSE_NOT_MUTATING.value):
        issue(ctx, provisioned, purpose=purpose.value)


def test_active_lease_opens_a_path_inside_its_own_workspace(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """The one call that yields a path, and only because a lease proved out."""
    provisioned, ctx, context = canary
    lease = issue(ctx, provisioned)
    opened = call(
        OPEN_METHOD,
        ctx,
        {
            "repo_root": str(provisioned.root),
            "lease_id": lease["lease_id"],
            "purpose": RunPurpose.IMPLEMENT.value,
            "relative": "src/module.py",
        },
    )
    root = workspace_path(context, handle=lease["workspace_handle"]).resolve()
    assert Path(opened["path"]).is_relative_to(root)
    assert Path(opened["path"]).read_text(encoding="utf-8") == "x = 1\n"


@pytest.mark.parametrize(
    "relative", ["tests/unit/x.py", "../escape.py", "/etc/passwd", "src/../../escape.py"]
)
def test_open_refuses_a_path_outside_the_writable_roots(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext], relative: str
) -> None:
    """A lease pins a working root the holder cannot reach outside of."""
    provisioned, ctx, _ = canary
    lease = issue(ctx, provisioned)
    with pytest.raises(DaemonValidationError, match=LeaseRefusalCode.SCOPE_ESCAPE.value):
        call(
            OPEN_METHOD,
            ctx,
            {
                "repo_root": str(provisioned.root),
                "lease_id": lease["lease_id"],
                "purpose": RunPurpose.IMPLEMENT.value,
                "relative": relative,
            },
        )


# ---- the workspace the lease hands out --------------------------------------


def test_issue_answers_with_an_opaque_handle_and_no_path(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """Raw canonical paths are withheld: the holder gets a handle."""
    provisioned, ctx, _ = canary
    lease = issue(ctx, provisioned)
    assert lease["workspace_handle"].startswith("wsh-")
    assert "/" not in lease["workspace_handle"]
    flattened = " ".join(str(value) for value in lease.values())
    assert str(provisioned.root) not in flattened


def test_issue_materializes_a_real_isolated_worktree(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """The handle resolves, daemon-side, to a checked-out git worktree."""
    provisioned, ctx, context = canary
    lease = issue(ctx, provisioned)
    target = workspace_path(context, handle=lease["workspace_handle"])
    assert (target / "src" / "module.py").is_file()
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=provisioned.root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(target.resolve()) in listed


def test_second_materialization_of_one_task_takes_the_next_generation(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """Generations rise across rematerialization; they never repeat."""
    provisioned, ctx, _ = canary
    first = issue(ctx, provisioned)
    call(REVOKE_METHOD, ctx, {"repo_root": str(provisioned.root), "lease_id": first["lease_id"]})
    second = issue(ctx, provisioned, run_ref=OTHER_RUN_URN)
    assert first["workspace_generation"] == 1
    assert second["workspace_generation"] == 2
    assert second["workspace_handle"] != first["workspace_handle"]


# ---- the deadline the agent cannot move -------------------------------------


def test_agent_heartbeat_cannot_extend_expires_at(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """The contract: an agent asking for longer is refused and moves nothing."""
    provisioned, ctx, _ = canary
    lease = issue(ctx, provisioned)
    with pytest.raises(DaemonValidationError, match="cannot extend"):
        call(
            HEARTBEAT_METHOD,
            ctx,
            {
                "repo_root": str(provisioned.root),
                "lease_id": lease["lease_id"],
                "origin": "agent",
                "extend_seconds": MAX_LEASE_SECONDS,
            },
        )
    after = call(
        SHOW_METHOD, ctx, {"repo_root": str(provisioned.root), "lease_id": lease["lease_id"]}
    )
    assert after["expires_at"] == lease["expires_at"]


def test_agent_heartbeat_moves_liveness_and_leaves_the_deadline(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """A plain beat is accepted as evidence of running, and nothing else."""
    provisioned, ctx, _ = canary
    lease = issue(ctx, provisioned)
    beaten = call(
        HEARTBEAT_METHOD,
        ctx,
        {"repo_root": str(provisioned.root), "lease_id": lease["lease_id"], "origin": "agent"},
    )
    assert beaten["expires_at"] == lease["expires_at"]
    assert beaten["heartbeat_at"] >= lease["heartbeat_at"]


def test_daemon_heartbeat_is_the_one_origin_that_extends(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """The deadline moves, and only on the daemon's own beat."""
    provisioned, ctx, _ = canary
    lease = issue(ctx, provisioned)
    renewed = call(
        HEARTBEAT_METHOD,
        ctx,
        {
            "repo_root": str(provisioned.root),
            "lease_id": lease["lease_id"],
            "origin": "daemon",
            "extend_seconds": 7200,
        },
    )
    assert renewed["expires_at"] > lease["expires_at"]


def test_heartbeat_of_an_expired_lease_is_refused(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """A beat does not reopen a deadline that has already passed."""
    provisioned, ctx, context = canary
    lease = issue_lease(
        context,
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=WRITABLE,
        now=datetime.now(UTC) - timedelta(hours=2),
        ttl=timedelta(hours=1),
    )
    with pytest.raises(DaemonValidationError, match=LeaseRefusalCode.LEASE_EXPIRED.value):
        call(
            HEARTBEAT_METHOD,
            ctx,
            {"repo_root": str(provisioned.root), "lease_id": lease.lease_id, "origin": "daemon"},
        )


# ---- strict parameters and bounded terms ------------------------------------


def test_issue_refuses_an_unknown_parameter(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """Every lease verb forbids extras, like every other strict surface."""
    provisioned, ctx, _ = canary
    with pytest.raises(DaemonValidationError, match="not a valid IssueParams"):
        issue(ctx, provisioned, forever=True)


@pytest.mark.parametrize("ttl_seconds", [0, -1, MAX_LEASE_SECONDS + 1, "3600", 3600.0])
def test_issue_refuses_a_term_outside_the_ceiling(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext], ttl_seconds: object
) -> None:
    """A lease that never lapses is a directory nobody reclaims."""
    provisioned, ctx, _ = canary
    with pytest.raises(DaemonValidationError, match="ttl_seconds"):
        issue(ctx, provisioned, ttl_seconds=ttl_seconds)


def test_issue_refuses_an_empty_writable_root_set(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """A lease granting no write grants nothing."""
    provisioned, ctx, _ = canary
    with pytest.raises(DaemonValidationError, match="writable_roots"):
        issue(ctx, provisioned, writable_roots=[])


def test_heartbeat_refuses_an_unknown_origin(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
) -> None:
    """The origin is a closed pair; a third one is not a beat at all."""
    provisioned, ctx, _ = canary
    lease = issue(ctx, provisioned)
    with pytest.raises(DaemonValidationError, match="origin"):
        call(
            HEARTBEAT_METHOD,
            ctx,
            {
                "repo_root": str(provisioned.root),
                "lease_id": lease["lease_id"],
                "origin": "operator",
            },
        )


# ---- contention and the epoch-2 fence ---------------------------------------


def test_two_concurrent_issues_of_one_run_leave_exactly_one_active_lease(
    canary: tuple[CanaryProvision, MethodContext, Epoch2RootContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two threads, one barrier, one real lock: one lease, one refusal."""
    provisioned, _, context = canary
    monkeypatch.setenv("EA_LOCK_TIMEOUT", "60")
    barrier = threading.Barrier(2)

    def attempt() -> object:
        barrier.wait(timeout=30)
        try:
            return issue_lease(
                context,
                run_ref=RUN_URN,
                task_ref=TASK_URN,
                purpose=RunPurpose.IMPLEMENT,
                base="main",
                writable_roots=WRITABLE,
                now=datetime.now(UTC),
                ttl=timedelta(hours=1),
            )
        except LeaseRefusedError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result(timeout=120) for future in [pool.submit(attempt) for _ in range(2)]
        ]

    granted = [item for item in outcomes if not isinstance(item, LeaseRefusedError)]
    refused = [item for item in outcomes if isinstance(item, LeaseRefusedError)]
    assert len(granted) == 1
    assert len(refused) == 1
    assert refused[0].code is LeaseRefusalCode.LEASE_ALREADY_ACTIVE
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=provisioned.root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert listed.count("worktree ") == 2


def test_a_production_root_is_refused_every_lease_verb(tmp_path: Path) -> None:
    """Leases apply inside canaries only; a production tree reaches none."""
    repo = tmp_path / "production"
    make_repo(repo)
    (repo / ".ea").mkdir()
    (repo / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    ledger = repo / ".ea" / "worktrees"
    ledger.mkdir()
    ctx = MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=tmp_path / "wal",
    )
    for method, params in (
        (ISSUE_METHOD, {"run_ref": RUN_URN}),
        (OPEN_METHOD, {"relative": "src/module.py"}),
        (HEARTBEAT_METHOD, {"lease_id": "LSE-" + "0" * 32, "origin": "agent"}),
        (SHOW_METHOD, {"lease_id": "LSE-" + "0" * 32}),
    ):
        with pytest.raises(NativeAuthorityRefusedError):
            call(method, ctx, {"repo_root": str(repo), **params})
    assert list(ledger.iterdir()) == []
    assert not (repo / ".ea" / "local").exists()
