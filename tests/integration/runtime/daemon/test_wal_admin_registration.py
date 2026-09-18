"""The four ``wal.*`` admin verbs are live on the daemon and find the right WAL.

Until now the admin surface shipped unregistered: the handlers existed
and only a test that imported the module by hand could reach them, so an
operator inspecting a poisoned record on a running daemon got
``method not found``. Importing them from the server is what makes the
verbs real, and the subprocess check below is what proves it -- in a
fresh interpreter nothing else can have imported the module first.

Registration alone is not enough, because the handlers took the WAL
directory as a raw path. A native root does not keep its records in the
daemon's WAL directly: it keeps them in a namespace named by a digest of
its tree, which no caller can spell. So the verbs now name a tree and the
root context answers with its directory, the daemon's own WAL stays the
default, and naming a directory and a tree at once is refused rather than
ranked.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.epoch2.authority import NATIVE_AUTHORITY_REQUIRED
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_transaction import TransitionRequest, run_transaction
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.wal import WalStatus, list_records, mark_poisoned
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR = "OP-0001"
KEY = "req-0001"

#: The whole admin surface, as the operator names it.
WAL_METHODS = ("wal.list_pending", "wal.list_poisoned", "wal.gc", "wal.inspect")


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one planned Milestone."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"milestone": {"MLS-0030": seed_row("milestone", "PLANNED")}})
    return provisioned


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context bound to that runtime directory's WAL."""
    return method_context(runtime_root)


def _committed(canary: CanaryProvision, runtime_root: Path) -> Path:
    """Commit one native mutation and return the root's WAL namespace."""
    context = root_context(canary, runtime_root)
    run_transaction(
        context=context,
        request=TransitionRequest.model_validate(
            {
                "urn": MILESTONE_URN,
                "to_status": "ACTIVE",
                "expected_revision": 1,
                "idempotency_key": KEY,
                "actor": ACTOR,
            }
        ),
        now=AT,
    )
    return context.wal_dir


def _call(method: str, ctx: MethodContext, **params: Any) -> dict[str, Any]:
    return asyncio.run(methods.dispatch(method, ctx, params))


# ---------------------------------------------------------------------------
# B70: the verbs are registered by the server, not by a test
# ---------------------------------------------------------------------------


def test_the_wal_admin_verbs_are_registered() -> None:
    import eawf.runtime.daemon.server  # noqa: F401

    assert set(WAL_METHODS).issubset(set(methods.registered_methods()))


def test_importing_only_the_server_registers_the_wal_admin_verbs() -> None:
    """A fresh interpreter proves the server import is what registers them."""
    script = (
        "import eawf.runtime.daemon.server\n"
        "from eawf.runtime.daemon.methods import registered_methods\n"
        "print(','.join(sorted(m for m in registered_methods() if m.startswith('wal.'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )

    assert sorted(result.stdout.strip().split(",")) == sorted(WAL_METHODS)


# ---------------------------------------------------------------------------
# The WAL directory resolved through the root context
# ---------------------------------------------------------------------------


def test_list_pending_resolves_a_named_tree_to_its_native_namespace(
    canary: CanaryProvision, runtime_root: Path, ctx: MethodContext
) -> None:
    wal_dir = _committed(canary, runtime_root)
    record = list_records(wal_dir)[0]
    os.replace(record, wal_dir / record.name.replace(".fsynced.", ".pending."))

    answer = _call("wal.list_pending", ctx, repo_root=str(canary.root))

    assert answer["count"] == 1
    assert Path(answer["paths"][0]).parent == wal_dir


def test_list_pending_on_a_named_tree_does_not_see_the_daemon_wal(
    canary: CanaryProvision, runtime_root: Path, ctx: MethodContext
) -> None:
    """The native namespace is a subdirectory, so the two never cross."""
    _committed(canary, runtime_root)
    daemon_wal = runtime_root / "wal"
    (daemon_wal / "epoch1.pending.json").write_bytes(b"{}")

    answer = _call("wal.list_pending", ctx, repo_root=str(canary.root))

    assert answer["count"] == 0


def test_inspect_resolves_a_named_tree(
    canary: CanaryProvision, runtime_root: Path, ctx: MethodContext
) -> None:
    wal_dir = _committed(canary, runtime_root)
    record_id = list_records(wal_dir)[0].name.split(".")[0]

    answer = _call("wal.inspect", ctx, repo_root=str(canary.root), record_id=record_id)

    assert answer["status"] == WalStatus.FSYNCED.value
    assert answer["record"]["record_id"] == record_id
    assert answer["record"]["idempotency_key"] == KEY


def test_list_poisoned_resolves_a_named_tree(
    canary: CanaryProvision, runtime_root: Path, ctx: MethodContext
) -> None:
    wal_dir = _committed(canary, runtime_root)
    record_id = list_records(wal_dir)[0].name.split(".")[0]
    mark_poisoned(wal_dir, record_id, reason="operator_review")

    answer = _call("wal.list_poisoned", ctx, repo_root=str(canary.root))

    assert answer["count"] == 1
    assert Path(answer["paths"][0]).parent == wal_dir / "poisoned"


def test_gc_collects_aged_records_of_a_named_tree(
    canary: CanaryProvision, runtime_root: Path, ctx: MethodContext
) -> None:
    wal_dir = _committed(canary, runtime_root)
    durable = list_records(wal_dir)[0]
    past = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(durable, (past, past))

    answer = _call("wal.gc", ctx, repo_root=str(canary.root), max_age_seconds=3600)

    assert answer["removed_count"] == 1
    assert list_records(wal_dir) == []


def test_gc_keeps_a_record_inside_the_retention_window(
    canary: CanaryProvision, runtime_root: Path, ctx: MethodContext
) -> None:
    wal_dir = _committed(canary, runtime_root)

    answer = _call("wal.gc", ctx, repo_root=str(canary.root), max_age_seconds=3600)

    assert answer["removed_count"] == 0
    assert len(list_records(wal_dir)) == 1


# ---------------------------------------------------------------------------
# The other two rungs of the resolution ladder
# ---------------------------------------------------------------------------


def test_an_unnamed_wal_falls_back_to_the_daemon_directory(
    runtime_root: Path, ctx: MethodContext
) -> None:
    daemon_wal = runtime_root / "wal"
    daemon_wal.mkdir(parents=True)
    (daemon_wal / "epoch1.pending.json").write_bytes(b"{}")

    answer = _call("wal.list_pending", ctx)

    assert answer["count"] == 1
    assert Path(answer["paths"][0]).parent == daemon_wal


def test_an_explicit_directory_is_still_honoured(tmp_path: Path, ctx: MethodContext) -> None:
    """The operator CLI targets a local WAL with no daemon and no tree."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "rec.pending.json").write_bytes(b"{}")

    answer = _call("wal.list_pending", ctx, wal_dir=str(elsewhere))

    assert answer["count"] == 1
    assert Path(answer["paths"][0]).parent == elsewhere


def test_an_empty_directory_lists_nothing(tmp_path: Path, ctx: MethodContext) -> None:
    answer = _call("wal.list_pending", ctx, wal_dir=str(tmp_path / "never-made"))

    assert answer == {"count": 0, "paths": []}


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", WAL_METHODS)
def test_naming_both_a_directory_and_a_tree_is_refused(
    method: str, canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    extra: dict[str, Any] = {"record_id": "unused"} if method == "wal.inspect" else {}

    with pytest.raises(DaemonValidationError, match="exactly one of wal_dir / repo_root"):
        _call(method, ctx, wal_dir=str(tmp_path), repo_root=str(canary.root), **extra)


def test_naming_neither_without_a_bound_wal_is_refused() -> None:
    bare = MethodContext(started_at=AT.isoformat(), pid=1, protocol_version="1", version="test")

    with pytest.raises(DaemonValidationError, match="bound to no WAL directory"):
        _call("wal.list_pending", bare)


def test_a_tree_that_is_not_epoch_two_has_no_native_namespace(
    tmp_path: Path, ctx: MethodContext
) -> None:
    """The operator's mistake answers as a validation failure, not a fault."""
    plain = tmp_path / "plainrepo"
    (plain / ".ea").mkdir(parents=True)

    with pytest.raises(DaemonValidationError, match=NATIVE_AUTHORITY_REQUIRED):
        _call("wal.list_pending", ctx, repo_root=str(plain))


def test_inspect_raises_for_a_record_id_no_status_holds(
    canary: CanaryProvision, runtime_root: Path, ctx: MethodContext
) -> None:
    _committed(canary, runtime_root)

    with pytest.raises(FileNotFoundError, match="wal record not found"):
        _call("wal.inspect", ctx, repo_root=str(canary.root), record_id="no-such-record")


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"wal_dir": ""}, id="empty-directory"),
        pytest.param({"repo_root": ""}, id="empty-tree"),
        pytest.param({"unknown": "field"}, id="unknown-field"),
    ],
)
def test_params_are_strict(params: dict[str, Any], ctx: MethodContext) -> None:
    with pytest.raises(ValueError):
        _call("wal.list_pending", ctx, **params)


@pytest.mark.parametrize(
    "max_age_seconds",
    [pytest.param(-1, id="below-zero"), pytest.param(30 * 24 * 3600 + 1, id="above-the-ceiling")],
)
def test_gc_bounds_its_retention_window(
    max_age_seconds: int, tmp_path: Path, ctx: MethodContext
) -> None:
    with pytest.raises(ValueError):
        _call("wal.gc", ctx, wal_dir=str(tmp_path), max_age_seconds=max_age_seconds)


@pytest.mark.parametrize(
    ("age_seconds", "removed"),
    [
        pytest.param(3600, 1, id="exactly-at-the-window"),
        pytest.param(3540, 0, id="one-minute-inside-it"),
    ],
)
def test_gc_sweeps_at_the_window_edge(
    age_seconds: int,
    removed: int,
    canary: CanaryProvision,
    runtime_root: Path,
    ctx: MethodContext,
) -> None:
    """The cutoff is inclusive: a record exactly at the window goes."""
    wal_dir = _committed(canary, runtime_root)
    durable = list_records(wal_dir)[0]
    aged = (datetime.now(UTC) - timedelta(seconds=age_seconds)).timestamp()
    os.utime(durable, (aged, aged))

    answer = _call("wal.gc", ctx, repo_root=str(canary.root), max_age_seconds=3600)

    assert answer["removed_count"] == removed
    assert len(list_records(wal_dir)) == 1 - removed
