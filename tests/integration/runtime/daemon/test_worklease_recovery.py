"""Reconcile cleans only what it can prove is worthless, and keeps the rest.

Three claims are pinned here. Residue a git worktree reports as clean and
still sitting on the commit it was materialized at is removed, because
nobody could want it. Residue of any other shape -- uncommitted entries, a
HEAD that moved, a directory that is gone -- is quarantined under a
recovery handle and left exactly where it lies, and the tests check the
bytes afterwards rather than trusting the status field. And the pass is
root-scoped: a second canary's leases and a production tree's epoch-1
worktree ledger come out of it byte-identical.

Expiry is injected, never waited for. Every pass is driven by handing
:func:`reconcile_root` the instant it should judge deadlines at, so the
suite has no timing window at all.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest

from eawf.kernel.runtime.lease import LeaseStatus
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.workspace_lease import RECONCILE_METHOD
from eawf.runtime.daemon.native_guard import NativeAuthorityRefusedError
from eawf.runtime.workspace.lease import (
    QUARANTINE_LOCATOR,
    QuarantineRecord,
    issue_lease,
    lease_path,
    quarantine_path,
    read_lease,
    reconcile_root,
    revoke_lease,
    workspace_path,
)
from tests.integration.runtime.daemon.test_worklease import (
    RUN_URN,
    TASK_URN,
    WRITABLE,
    call,
    make_repo,
)

pytestmark = pytest.mark.integration


AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
ISSUED = AT - timedelta(hours=3)
TERM = timedelta(hours=1)
#: Judged well past the term, so the lease is unambiguously expired.
JUDGED = AT


def build_canary(root: Path, *, code: str) -> tuple[CanaryProvision, Epoch2RootContext]:
    """Return a provisioned canary over a real repository, with its context."""
    make_repo(root)
    provisioned = provision_canary(repo_root=root, ref=canary_ref(code), provisioned_at=AT)
    ctx = MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=provisioned.runtime_dir / "wal",
    )
    return provisioned, ctx.native_root_context(provisioned.root / ".ea")


def lease_the_repo(context: Epoch2RootContext, *, run_ref: str = RUN_URN) -> str:
    """Issue one lease that is already three hours old, and return its id."""
    return issue_lease(
        context,
        run_ref=run_ref,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=WRITABLE,
        now=ISSUED,
        ttl=TERM,
    ).lease_id


def tree_digest(root: Path) -> str:
    """Return a digest over every file under *root*, path and bytes alike."""
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def quarantine_records(context: Epoch2RootContext) -> list[QuarantineRecord]:
    """Return every quarantine record one root has filed."""
    directory = context.identity.tree_root / QUARANTINE_LOCATOR
    if not directory.is_dir():
        return []
    return [
        QuarantineRecord.model_validate(orjson.loads(path.read_bytes()))
        for path in sorted(directory.glob("*.json"))
    ]


@pytest.fixture
def canary(tmp_path: Path) -> tuple[CanaryProvision, Epoch2RootContext]:
    """Provision one canary with its native context."""
    return build_canary(tmp_path / "repo", code="LSA")


# ---- clean residue is released ----------------------------------------------


def test_reconcile_releases_an_expired_lease_over_a_clean_worktree(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """Nothing anybody could want, so the worktree goes and the lease ends."""
    _, context = canary
    lease_id = lease_the_repo(context)
    target = workspace_path(context, handle=read_lease(context, lease_id=lease_id).workspace_handle)
    assert target.is_dir()

    report = reconcile_root(context, now=JUDGED)

    assert (report.examined, report.expired, report.released, report.quarantined) == (1, 1, 1, 0)
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.RELEASED
    assert not target.exists()
    assert quarantine_records(context) == []


def test_reconcile_releases_a_revoked_lease_over_a_clean_worktree(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """A withdrawn lease reaches the same end without the deadline passing."""
    _, context = canary
    lease_id = issue_lease(
        context,
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=WRITABLE,
        now=JUDGED,
        ttl=timedelta(hours=6),
    ).lease_id
    revoke_lease(context, lease_id=lease_id, now=JUDGED)

    report = reconcile_root(context, now=JUDGED)

    assert (report.expired, report.released, report.quarantined) == (0, 1, 0)
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.RELEASED


def test_a_withdrawing_lease_releases_once_its_residue_is_proven(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """The one exit a soft revoke has, taken because the proof was there."""
    _, context = canary
    lease_id = issue_lease(
        context,
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=WRITABLE,
        now=JUDGED,
        ttl=timedelta(hours=6),
    ).lease_id
    revoke_lease(context, lease_id=lease_id, now=JUDGED, hard=False)
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.REVOKING

    report = reconcile_root(context, now=JUDGED)

    assert (report.released, report.quarantined) == (1, 0)
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.RELEASED


def test_a_withdrawing_lease_waits_while_its_residue_is_unproven(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """Its machine declares no quarantine edge, so it is left where it is."""
    _, context = canary
    lease = issue_lease(
        context,
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=WRITABLE,
        now=JUDGED,
        ttl=timedelta(hours=6),
    )
    target = workspace_path(context, handle=lease.workspace_handle)
    (target / "unsaved.txt").write_text("still here", encoding="utf-8")
    revoke_lease(context, lease_id=lease.lease_id, now=JUDGED, hard=False)

    report = reconcile_root(context, now=JUDGED)

    assert report.untouched == (lease.lease_id,)
    assert (report.released, report.quarantined) == (0, 0)
    assert read_lease(context, lease_id=lease.lease_id).status is LeaseStatus.REVOKING
    assert (target / "unsaved.txt").read_text(encoding="utf-8") == "still here"


# ---- uncertain residue is quarantined, never deleted ------------------------


def test_reconcile_quarantines_an_uncommitted_worktree_and_keeps_its_bytes(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """The residue is still on disk afterwards, byte for byte."""
    _, context = canary
    lease_id = lease_the_repo(context)
    handle = read_lease(context, lease_id=lease_id).workspace_handle
    target = workspace_path(context, handle=handle)
    unsaved = target / "src" / "module.py"
    unsaved.write_text("x = 2  # the work nobody has seen\n", encoding="utf-8")

    report = reconcile_root(context, now=JUDGED)

    assert (report.released, report.quarantined) == (0, 1)
    lease = read_lease(context, lease_id=lease_id)
    assert lease.status is LeaseStatus.QUARANTINED
    assert lease.recovery_handle is not None
    assert "uncommitted" in lease.quarantine_reason
    assert unsaved.read_text(encoding="utf-8") == "x = 2  # the work nobody has seen\n"


def test_quarantine_record_is_readable_under_the_recovery_handle(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """The handle on the lease finds the record that locates the residue."""
    _, context = canary
    lease_id = lease_the_repo(context)
    handle = read_lease(context, lease_id=lease_id).workspace_handle
    (workspace_path(context, handle=handle) / "stray.txt").write_text("keep", encoding="utf-8")

    reconcile_root(context, now=JUDGED)

    lease = read_lease(context, lease_id=lease_id)
    record = QuarantineRecord.model_validate(
        orjson.loads(quarantine_path(context, recovery_handle=lease.recovery_handle).read_bytes())
    )
    assert record.recovery_handle == lease.recovery_handle
    assert record.lease_id == lease_id
    assert record.workspace_handle == handle
    assert record.workspace_present
    assert record.residue


def test_quarantine_record_states_no_host_path(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """A record read by an operator carries no absolute directory."""
    provisioned, context = canary
    lease_id = lease_the_repo(context)
    handle = read_lease(context, lease_id=lease_id).workspace_handle
    (workspace_path(context, handle=handle) / "stray.txt").write_text("keep", encoding="utf-8")

    reconcile_root(context, now=JUDGED)

    [record] = quarantine_records(context)
    rendered = json.dumps(record.model_dump(mode="json"))
    assert str(provisioned.root) not in rendered
    assert str(provisioned.root.parent) not in rendered


def test_reconcile_quarantines_a_worktree_that_committed_unintegrated_work(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """A moved HEAD holds content the base does not, so it is kept."""
    _, context = canary
    lease_id = lease_the_repo(context)
    target = workspace_path(context, handle=read_lease(context, lease_id=lease_id).workspace_handle)
    (target / "src" / "module.py").write_text("x = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "worker output"], cwd=target, check=True)

    reconcile_root(context, now=JUDGED)

    lease = read_lease(context, lease_id=lease_id)
    assert lease.status is LeaseStatus.QUARANTINED
    assert "committed work" in lease.quarantine_reason
    assert (target / "src" / "module.py").read_text(encoding="utf-8") == "x = 3\n"


def test_reconcile_quarantines_a_workspace_that_is_gone(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """A workspace nobody can look at cannot be shown to be worthless."""
    _, context = canary
    lease_id = lease_the_repo(context)
    target = workspace_path(context, handle=read_lease(context, lease_id=lease_id).workspace_handle)
    shutil.rmtree(target)

    reconcile_root(context, now=JUDGED)

    lease = read_lease(context, lease_id=lease_id)
    assert lease.status is LeaseStatus.QUARANTINED
    [record] = quarantine_records(context)
    assert not record.workspace_present


# ---- what the pass deliberately leaves alone --------------------------------


def test_reconcile_leaves_an_unexpired_active_lease_alone(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """A live lease is work in flight, not residue."""
    _, context = canary
    lease_id = issue_lease(
        context,
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=WRITABLE,
        now=JUDGED,
        ttl=timedelta(hours=6),
    ).lease_id

    report = reconcile_root(context, now=JUDGED)

    assert report.untouched == (lease_id,)
    assert (report.released, report.quarantined) == (0, 0)
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.ACTIVE
    assert workspace_path(
        context, handle=read_lease(context, lease_id=lease_id).workspace_handle
    ).is_dir()


def test_a_second_pass_neither_re_releases_nor_re_quarantines(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """Terminal leases are left as they are, so the pass is idempotent."""
    _, context = canary
    clean = lease_the_repo(context)
    reconcile_root(context, now=JUDGED)
    first = lease_path(context, lease_id=clean).read_bytes()

    second = reconcile_root(context, now=JUDGED + timedelta(hours=1))

    assert second.untouched == (clean,)
    assert (second.released, second.quarantined) == (0, 0)
    assert lease_path(context, lease_id=clean).read_bytes() == first


def test_a_second_pass_files_no_second_quarantine_record(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """One quarantine, one handle, however often the sweep runs."""
    _, context = canary
    lease_id = lease_the_repo(context)
    handle = read_lease(context, lease_id=lease_id).workspace_handle
    (workspace_path(context, handle=handle) / "stray.txt").write_text("keep", encoding="utf-8")
    reconcile_root(context, now=JUDGED)

    reconcile_root(context, now=JUDGED + timedelta(hours=1))

    assert len(quarantine_records(context)) == 1


# ---- the pass is scoped to one root -----------------------------------------


def test_reconcile_of_one_root_leaves_another_roots_leases_untouched(
    tmp_path: Path,
) -> None:
    """Two canaries, one sweep: the other root comes out byte-identical."""
    _, first = build_canary(tmp_path / "first", code="LSA")
    second_provision, second = build_canary(tmp_path / "second", code="LSB")
    lease_the_repo(first)
    other_id = lease_the_repo(second)
    other_bytes = lease_path(second, lease_id=other_id).read_bytes()
    other_workspace = workspace_path(
        second, handle=read_lease(second, lease_id=other_id).workspace_handle
    )

    report = reconcile_root(first, now=JUDGED)

    assert report.root_id == first.identity.root_id
    assert report.examined == 1
    assert lease_path(second, lease_id=other_id).read_bytes() == other_bytes
    assert read_lease(second, lease_id=other_id).status is LeaseStatus.ACTIVE
    assert other_workspace.is_dir()
    assert quarantine_records(second) == []
    assert (second_provision.root / ".git").exists()


def test_reconcile_never_touches_a_production_roots_worktree_ledger(
    tmp_path: Path,
) -> None:
    """The epoch-1 ledger of a production tree is not this sweep's business."""
    _, context = build_canary(tmp_path / "canary", code="LSA")
    lease_the_repo(context)
    production = tmp_path / "production"
    make_repo(production)
    ledger_root = production / ".ea"
    ledger_root.mkdir()
    (ledger_root / "state.json").write_text(
        json.dumps(
            {"schema_version": "1.10", "worktrees": {"WT-P33-I01-W61-1": {"status": "active"}}}
        ),
        encoding="utf-8",
    )
    (ledger_root / "worktrees" / "p33-w61").mkdir(parents=True)
    (ledger_root / "worktrees" / "p33-w61" / "README.md").write_text("held", encoding="utf-8")
    before = tree_digest(ledger_root)

    reconcile_root(context, now=JUDGED)

    assert tree_digest(ledger_root) == before


def test_a_production_root_is_refused_the_reconcile_verb(tmp_path: Path) -> None:
    """Leases apply inside canaries only, so the verb never reaches epoch 1."""
    production = tmp_path / "production"
    make_repo(production)
    (production / ".ea").mkdir()
    (production / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    ctx = MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=tmp_path / "wal",
    )
    with pytest.raises(NativeAuthorityRefusedError):
        call(RECONCILE_METHOD, ctx, {"repo_root": str(production)})
    assert not (production / ".ea" / "local").exists()


def test_reconcile_over_a_root_with_no_leases_reports_nothing(
    canary: tuple[CanaryProvision, Epoch2RootContext],
) -> None:
    """The empty boundary: a root that has issued none sweeps cleanly."""
    _, context = canary
    report = reconcile_root(context, now=JUDGED)
    assert (report.examined, report.released, report.quarantined) == (0, 0, 0)
    assert report.untouched == ()
