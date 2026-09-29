"""The lease scheduler reads the Task graph: narrowing, ownership, exclusivity.

Each case provisions a canary over a real git repository, seeds native
Task records carrying their graph fields, and issues leases through the
library the dispatcher calls. The refusals asserted here are the whole
enforcement: a Task declared to run alone is never leased beside another
Task, and a lease never widens the write claims its Task declared.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.workspace.lease import (
    LeaseRefusalCode,
    LeaseRefusedError,
    issue_lease,
    read_lease,
    revoke_lease,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import rekeyed, seed, seed_row

pytestmark = pytest.mark.integration

AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
PREFIX: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
TASK_A: Final = "EAWF-0101"
TASK_B: Final = "EAWF-0102"


def _repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "ci"], cwd=root, check=True)
    for directory in ("src", "docs"):
        (root / directory).mkdir(exist_ok=True)
        (root / directory / "keep.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)


def _task(key: str, **graph: Any) -> dict[str, Any]:
    row = rekeyed(seed_row("task", "PLANNED"), key=key)
    row.update(graph)
    return row


def _context(tmp_path: Path, tasks: dict[str, dict[str, Any]]) -> Epoch2RootContext:
    _repo(tmp_path / "repo")
    provisioned: CanaryProvision = provision_canary(
        repo_root=tmp_path / "repo", ref=canary_ref("GRF"), provisioned_at=AT
    )
    seed(provisioned, {"task": tasks})
    ctx = MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=provisioned.runtime_dir / "wal",
    )
    return ctx.native_root_context(provisioned.root / ".ea")


def _issue(
    context: Epoch2RootContext, *, task: str, run: str, roots: tuple[str, ...] = ("src",)
) -> Any:
    return issue_lease(
        context,
        run_ref=f"{PREFIX}/run/{run}",
        task_ref=f"{PREFIX}/task/{task}",
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=roots,
        now=datetime.now(UTC),
        ttl=timedelta(hours=1),
    )


# ---- SURF-093: the narrowing is declared once, on the Task -------------------


def test_surf_093_lease_inside_the_task_write_claims_is_granted(tmp_path: Path) -> None:
    context = _context(tmp_path, {TASK_A: _task(TASK_A, write_claims=["src"])})
    lease = _issue(context, task=TASK_A, run="RUN-00000020", roots=("src/eawf",))
    assert lease.writable_roots == ("src/eawf",)


def test_surf_093_lease_widening_the_task_write_claims_is_refused(tmp_path: Path) -> None:
    context = _context(tmp_path, {TASK_A: _task(TASK_A, write_claims=["src/eawf"])})
    with pytest.raises(LeaseRefusedError) as refused:
        _issue(context, task=TASK_A, run="RUN-00000020", roots=("src/eawf", "docs"))
    assert refused.value.code is LeaseRefusalCode.SCOPE_WIDENED
    assert "docs" in refused.value.detail


def test_surf_093_sibling_prefix_is_not_inside_a_claim(tmp_path: Path) -> None:
    context = _context(tmp_path, {TASK_A: _task(TASK_A, write_claims=["src/app"])})
    with pytest.raises(LeaseRefusedError) as refused:
        _issue(context, task=TASK_A, run="RUN-00000020", roots=("src/application",))
    assert refused.value.code is LeaseRefusalCode.SCOPE_WIDENED


def test_surf_093_task_with_no_claims_declares_no_narrowing(tmp_path: Path) -> None:
    context = _context(tmp_path, {TASK_A: _task(TASK_A)})
    assert _issue(context, task=TASK_A, run="RUN-00000020", roots=("docs",)).lease_id


# ---- SURF-096: exclusivity is enforced by lease conflict ---------------------


def test_surf_096_exclusive_task_is_not_leased_beside_another_task(tmp_path: Path) -> None:
    context = _context(tmp_path, {TASK_A: _task(TASK_A), TASK_B: _task(TASK_B, exclusive=True)})
    _issue(context, task=TASK_A, run="RUN-00000020", roots=("src",))
    with pytest.raises(LeaseRefusedError) as refused:
        _issue(context, task=TASK_B, run="RUN-00000021", roots=("docs",))
    assert refused.value.code is LeaseRefusalCode.EXCLUSIVE_CONFLICT


def test_surf_096_no_task_is_leased_beside_an_exclusive_one(tmp_path: Path) -> None:
    context = _context(tmp_path, {TASK_A: _task(TASK_A, exclusive=True), TASK_B: _task(TASK_B)})
    held = _issue(context, task=TASK_A, run="RUN-00000020", roots=("src",))
    assert held.exclusive is True
    with pytest.raises(LeaseRefusedError) as refused:
        _issue(context, task=TASK_B, run="RUN-00000021", roots=("docs",))
    assert refused.value.code is LeaseRefusalCode.EXCLUSIVE_CONFLICT
    assert TASK_A in refused.value.detail


def test_surf_096_released_exclusive_lease_frees_the_next_task(tmp_path: Path) -> None:
    context = _context(tmp_path, {TASK_A: _task(TASK_A, exclusive=True), TASK_B: _task(TASK_B)})
    held = _issue(context, task=TASK_A, run="RUN-00000020", roots=("src",))
    revoke_lease(context, lease_id=held.lease_id, now=datetime.now(UTC))
    assert _issue(context, task=TASK_B, run="RUN-00000021", roots=("docs",)).lease_id


def test_surf_096_exclusive_flag_is_stored_on_the_lease(tmp_path: Path) -> None:
    context = _context(tmp_path, {TASK_A: _task(TASK_A, exclusive=True)})
    held = _issue(context, task=TASK_A, run="RUN-00000020")
    stored = read_lease(context, lease_id=held.lease_id)
    assert stored is not None
    assert stored.exclusive is True


# ---- SURF-095: ownership claims order two Tasks ------------------------------


def test_surf_095_overlapping_roots_of_two_tasks_conflict(tmp_path: Path) -> None:
    context = _context(tmp_path, {TASK_A: _task(TASK_A), TASK_B: _task(TASK_B)})
    _issue(context, task=TASK_A, run="RUN-00000020", roots=("src",))
    with pytest.raises(LeaseRefusedError) as refused:
        _issue(context, task=TASK_B, run="RUN-00000021", roots=("src/eawf",))
    assert refused.value.code is LeaseRefusalCode.OWNERSHIP_CONFLICT


def test_surf_095_disjoint_roots_of_two_tasks_fan_out(tmp_path: Path) -> None:
    context = _context(tmp_path, {TASK_A: _task(TASK_A), TASK_B: _task(TASK_B)})
    _issue(context, task=TASK_A, run="RUN-00000020", roots=("src",))
    assert _issue(context, task=TASK_B, run="RUN-00000021", roots=("docs",)).lease_id


def test_surf_095_a_retry_of_the_same_task_is_not_an_ownership_conflict(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, {TASK_A: _task(TASK_A, exclusive=True)})
    _issue(context, task=TASK_A, run="RUN-00000020", roots=("src",))
    assert _issue(context, task=TASK_A, run="RUN-00000021", roots=("src",)).lease_id
