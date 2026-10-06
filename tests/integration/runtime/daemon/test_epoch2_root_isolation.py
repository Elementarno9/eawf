"""Two registered roots whose records are spelled alike share no namespace.

Both canaries are provisioned under one canary reference, so every entity
URN either tree can hold -- its workspace, project and repository keys
included -- reads the same in both. They are registered side by side under
two registry codes and reached through one daemon ``MethodContext``, the
way one machine's daemon serves them. Nothing spelled inside a tree can
keep them apart; only the root id each context derives from its own
resolved tree can.

Each namespace is checked twice: by path, and by behaviour with a negative
control. Both roots hold their document lock at once, where a second
session on the same root cannot; both write a WAL record under the same id,
where a second write into one namespace is refused; and one root's cached
replay is invisible to the other.

A daemon boot sweeps the per-entity lock files earlier releases leaked into
every tree it can reach, and leaves each tree's document lock in place.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.kernel.migration.epoch2.generation import tree_digests
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.platform.install.canary import (
    CanaryProvision,
    canary_ref,
    provision_canary,
    register_canary,
)
from eawf.platform.registry.models import Registry, RegistryRepoEntry
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon.epoch2_root import (
    DOCUMENT_LOCK_NAME,
    LOCK_LOCATOR,
    NATIVE_WAL_DIRNAME,
    canonical_entity_urn,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.state_context import resolve_mutator_paths
from eawf.runtime.daemon.wal import WalRecord, list_records, write_pending
from eawf.runtime.lock.portalock import LockTimeout

PROVISIONED_AT = datetime(2026, 1, 1, tzinfo=UTC)
CODE = "TWIN"
SECOND_CODE = "TWINB"
MILESTONE = f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/milestone/MLS-0001"
RETRIED_REQUEST = "retry-0001"


@dataclass(frozen=True)
class Twins:
    """Two canaries born under one reference and registered apart."""

    first: CanaryProvision
    second: CanaryProvision
    registry: Registry

    @property
    def first_tree(self) -> Path:
        return self.first.root / ".ea"

    @property
    def second_tree(self) -> Path:
        return self.second.root / ".ea"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def lock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a contended lock give up quickly instead of after five seconds."""
    monkeypatch.setenv("EA_LOCK_TIMEOUT", "0.2")


@pytest.fixture
def twins(tmp_path: Path) -> Twins:
    """Provision two canaries under one reference and register both."""
    ref = canary_ref(CODE)
    first = provision_canary(repo_root=tmp_path / "first", ref=ref, provisioned_at=PROVISIONED_AT)
    second = provision_canary(repo_root=tmp_path / "second", ref=ref, provisioned_at=PROVISIONED_AT)
    registry = register_canary(Registry(), first)
    second_row = RegistryRepoEntry(code=SECOND_CODE, path=str(second.root))
    registry = registry.model_copy(update={"repos": {**registry.repos, SECOND_CODE: second_row}})
    return Twins(first=first, second=second, registry=registry)


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """Return one daemon context with a WAL directory of its own."""
    return MethodContext(
        started_at="2026-01-01T00:00:00+00:00",
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=tmp_path / "runtime" / "wal",
    )


def _record(record_id: str) -> WalRecord:
    return WalRecord(
        record_id=record_id,
        envelope=Envelope(
            id=f"env-{record_id}",
            kind=StoreKind.EVENT,
            scope_id=None,
            created_at=PROVISIONED_AT,
            summary="native intent",
            payload={"entity": MILESTONE},
        ),
        idempotency_key=RETRIED_REQUEST,
        written_at=PROVISIONED_AT,
        before_state_version="sha:before",
        after_state_version="sha:after",
    )


def test_twin_roots_are_registered_apart_and_spelled_alike(twins: Twins) -> None:
    assert twins.first.ref == twins.second.ref
    assert twins.first.generation_id == twins.second.generation_id
    assert twins.first.root != twins.second.root
    assert {code: row.path for code, row in twins.registry.repos.items()} == {
        CODE: str(twins.first.root),
        SECOND_CODE: str(twins.second.root),
    }


def test_twin_roots_resolve_to_two_contexts(ctx: MethodContext, twins: Twins) -> None:
    first = ctx.native_root_context(twins.first_tree)
    second = ctx.native_root_context(twins.second_tree)
    assert first is not second
    assert first.identity.root_id != second.identity.root_id
    assert set(ctx.native_roots) == {first.identity.root_id, second.identity.root_id}


@pytest.mark.usefixtures("lock_timeout")
def test_twin_roots_hold_their_document_locks_at_once(ctx: MethodContext, twins: Twins) -> None:
    first = ctx.native_root_context(twins.first_tree)
    second = ctx.native_root_context(twins.second_tree)
    entity = canonical_entity_urn(MILESTONE)
    first_lock = first.lock_path(DOCUMENT_LOCK_NAME)
    second_lock = second.lock_path(DOCUMENT_LOCK_NAME)
    assert first_lock != second_lock
    assert first_lock.is_relative_to(twins.first_tree)
    assert second_lock.is_relative_to(twins.second_tree)
    with first.session([MILESTONE]), second.session([MILESTONE]) as held:
        assert held.locked_urns == (entity,)
        assert first_lock.exists()
        assert second_lock.exists()
    assert not first.lock_path(entity).exists()
    assert not second.lock_path(entity).exists()


@pytest.mark.usefixtures("lock_timeout")
def test_one_root_does_not_hold_the_same_entity_lock_twice(
    ctx: MethodContext, twins: Twins
) -> None:
    first = ctx.native_root_context(twins.first_tree)
    with (
        first.session([MILESTONE]),
        pytest.raises(LockTimeout),
        first.session([MILESTONE]),
    ):
        pytest.fail("a root's own lock must exclude a second session")


def test_twin_roots_write_wal_records_into_disjoint_directories(
    ctx: MethodContext, twins: Twins
) -> None:
    first = ctx.native_root_context(twins.first_tree)
    second = ctx.native_root_context(twins.second_tree)
    assert first.wal_dir != second.wal_dir
    assert first.wal_dir.parent == second.wal_dir.parent == ctx.wal_dir / NATIVE_WAL_DIRNAME
    written_first = write_pending(first.wal_dir, _record("rec-0001"))
    written_second = write_pending(second.wal_dir, _record("rec-0001"))
    assert written_first != written_second
    assert list_records(first.wal_dir) == [written_first]
    assert list_records(second.wal_dir) == [written_second]


def test_one_root_refuses_a_second_wal_record_under_one_id(
    ctx: MethodContext, twins: Twins
) -> None:
    first = ctx.native_root_context(twins.first_tree)
    write_pending(first.wal_dir, _record("rec-0001"))
    with pytest.raises(FileExistsError):
        write_pending(first.wal_dir, _record("rec-0001"))


def test_native_wal_records_are_invisible_to_epoch1_replay(
    ctx: MethodContext, twins: Twins
) -> None:
    for tree in (twins.first_tree, twins.second_tree):
        write_pending(ctx.native_root_context(tree).wal_dir, _record("rec-0001"))
    assert list_records(ctx.wal_dir) == []


def test_twin_roots_keep_disjoint_idempotency_namespaces(ctx: MethodContext, twins: Twins) -> None:
    first = ctx.native_root_context(twins.first_tree)
    second = ctx.native_root_context(twins.second_tree)
    first_key = first.idempotency_key(RETRIED_REQUEST)
    second_key = second.idempotency_key(RETRIED_REQUEST)
    assert first_key != second_key
    assert first_key.endswith(RETRIED_REQUEST)
    assert second_key.endswith(RETRIED_REQUEST)
    first.idempotency_cache[first_key] = {"receipt": "first"}
    assert first.idempotency_cache is not second.idempotency_cache
    assert second.idempotency_cache == {}
    assert ctx.idempotency_cache is None


def test_twin_roots_write_only_their_own_document(ctx: MethodContext, twins: Twins) -> None:
    first = ctx.native_root_context(twins.first_tree)
    second = ctx.native_root_context(twins.second_tree)
    untouched = tree_digests(twins.second.root)
    with first.session([MILESTONE]) as session:
        document = session.read_document()
        document["milestone"] = {"MLS-0001": {"status": "planned"}}
        session.write_document(document)
    assert tree_digests(twins.second.root) == untouched
    with second.session([MILESTONE]) as session:
        assert "milestone" not in session.read_document()


def test_one_root_through_two_spellings_shares_one_context(
    ctx: MethodContext, twins: Twins, tmp_path: Path
) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(twins.first.root, target_is_directory=True)
    direct = ctx.native_root_context(twins.first_tree)
    aliased = ctx.native_root_context(alias / ".ea")
    assert aliased is direct
    assert len(ctx.native_roots) == 1


def test_native_roots_leave_epoch1_resolution_unchanged(tmp_path: Path, twins: Twins) -> None:
    production = tmp_path / "production"
    (production / ".ea").mkdir(parents=True)
    state_path = production / ".ea" / "state.json"
    state_path.write_text('{"schema_version": "1.20"}\n', encoding="utf-8")
    wal_dir = tmp_path / "runtime" / "wal"
    ctx = MethodContext(
        started_at="2026-01-01T00:00:00+00:00",
        pid=1,
        protocol_version="1",
        version="test",
        state_path=state_path,
        wal_dir=wal_dir,
    )
    bound_before = resolve_mutator_paths(repo_root=None, ctx=ctx)
    routed_before = resolve_mutator_paths(repo_root=str(production), ctx=ctx)
    ctx.native_root_context(twins.first_tree)
    ctx.native_root_context(twins.second_tree)
    assert resolve_mutator_paths(repo_root=None, ctx=ctx) == bound_before
    assert resolve_mutator_paths(repo_root=str(production), ctx=ctx) == routed_before
    assert bound_before == (state_path, state_path.parent / "store" / "event.jsonl", wal_dir)
    assert (ctx.state_path, ctx.wal_dir, ctx.idempotency_cache) == (state_path, wal_dir, None)


def _leak_entity_locks(tree: Path, count: int) -> list[Path]:
    """Write the per-entity lock files an earlier release left behind."""
    lock_dir = tree / LOCK_LOCATOR
    lock_dir.mkdir(parents=True, exist_ok=True)
    leaked = []
    for index in range(count):
        digest = hashlib.sha256(f"{MILESTONE}-{index}".encode()).hexdigest()
        path = lock_dir / f"{digest}.lock"
        path.write_bytes(b"")
        leaked.append(path)
    return leaked


def _boot(tmp_path: Path, registry: Registry, mp: pytest.MonkeyPatch) -> None:
    """Boot the daemon through ``run`` with only its listener stubbed out.

    The daemon is bound to a placeholder project and reaches the twins only
    through the registry, so the sweep is shown to cover registered trees.
    """
    runtime_root = tmp_path / "boot-runtime"
    runtime_root.mkdir(exist_ok=True)
    project = tmp_path / "project" / ".ea"
    project.mkdir(parents=True, exist_ok=True)
    (project / "state.json").write_bytes(orjson.dumps({"placeholder": True}))
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(registry.model_dump_json(), encoding="utf-8")
    mp.setenv("EA_STATE", str(project / "state.json"))
    mp.setenv("EAWF_REGISTRY_PATH", str(registry_path))
    mp.setattr(daemon_main, "ensure_runtime_dir", lambda: runtime_root)
    mp.setattr(daemon_main, "pid_path", lambda: runtime_root / "eawfd.pid")
    mp.setattr(daemon_main, "log_path", lambda: runtime_root / "eawfd.log")
    mp.setattr(daemon_main, "socket_path", lambda: runtime_root / "eawfd.sock")
    mp.setattr(daemon_main.asyncio, "run", lambda coro: coro.close())
    assert daemon_main.run(foreground=True) == 0


def test_daemon_boot_removes_leaked_entity_locks_and_keeps_the_document_lock(
    ctx: MethodContext, twins: Twins, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for tree in (twins.first_tree, twins.second_tree):
        with ctx.native_root_context(tree).session([MILESTONE]):
            pass
    first_leaked = _leak_entity_locks(twins.first_tree, 499)
    second_leaked = _leak_entity_locks(twins.second_tree, 1)
    assert all(path.exists() for path in (*first_leaked, *second_leaked))
    _boot(tmp_path, twins.registry, monkeypatch)
    for tree in (twins.first_tree, twins.second_tree):
        names = sorted(entry.name for entry in (tree / LOCK_LOCATOR).iterdir())
        assert names == ["document.lock"]


def test_daemon_boot_leaves_files_that_are_not_entity_locks(
    twins: Twins, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = twins.first_tree / LOCK_LOCATOR
    lock_dir.mkdir(parents=True)
    kept = [
        lock_dir / "document.lock",
        lock_dir / f"{'a' * 63}.lock",
        lock_dir / f"{'a' * 65}.lock",
        lock_dir / f"{'A' * 64}.lock",
        lock_dir / f"{'a' * 64}.lock.tmp",
        lock_dir / f"{'a' * 64}",
    ]
    for path in kept:
        path.write_bytes(b"")
    nested = lock_dir / f"{'b' * 64}.lock"
    nested.mkdir()
    _leak_entity_locks(twins.first_tree, 1)
    _boot(tmp_path, twins.registry, monkeypatch)
    assert sorted(lock_dir.iterdir()) == sorted([*kept, nested])


def test_daemon_boot_with_no_lock_directory_creates_none(
    twins: Twins, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot(tmp_path, twins.registry, monkeypatch)
    assert not (twins.first_tree / LOCK_LOCATOR).exists()
    assert not (twins.second_tree / LOCK_LOCATOR).exists()


def test_daemon_boot_sweeps_the_other_trees_when_one_cannot_be_swept(
    twins: Twins, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaked = _leak_entity_locks(twins.second_tree, 2)
    real = daemon_main.remove_entity_lock_files

    def failing(tree_root: Path) -> int:
        if tree_root == twins.first_tree:
            raise PermissionError("lock directory unreadable")
        return real(tree_root)

    monkeypatch.setattr(daemon_main, "remove_entity_lock_files", failing)
    _boot(tmp_path, twins.registry, monkeypatch)
    assert not any(path.exists() for path in leaked)
