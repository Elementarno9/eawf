"""Every path a native session writes has a declared commit policy.

The session writes three file families: the selected generation's document,
that generation's ledgers, and the root's lock files. Each is classified
against the production commit-policy table, and a real write is checked by
diffing the tree before and after, so a file the session creates without
declaring it is caught even if no accessor names it.

The guard is also shown to fire: with the lock rows removed from the table,
a session is refused before its first lock file exists, and a generation
file no row declares is refused outright.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.generation import tree_digests
from eawf.kernel.store.commit_policy import (
    CENSUS_SURFACE_PREFIX,
    EA_PATH_CLASSES,
    CommitPolicy,
    PathClass,
    UndeclaredPathError,
    classify_path,
)
from eawf.kernel.store.tiers import LEDGER_COLLECTIONS, Epoch2Collection, StorageTier
from eawf.platform.install.canary import canary_ref, provision_canary
from eawf.runtime.daemon import epoch2_root
from eawf.runtime.daemon.epoch2_root import (
    DOCUMENT_LOCK_NAME,
    Epoch2RootContext,
    attach_root_context,
)

PROVISIONED_AT = datetime(2026, 1, 1, tzinfo=UTC)
CODE = "POLICY"
MILESTONE = f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/milestone/MLS-0001"
BATCH = f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/batch/BAT-0001"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> Path:
    """Return the tree root of a freshly provisioned epoch-2 canary."""
    repo = tmp_path / "canary"
    provision_canary(repo_root=repo, ref=canary_ref(CODE), provisioned_at=PROVISIONED_AT)
    return (repo / ".ea").resolve()


@pytest.fixture
def context(canary: Path, tmp_path: Path) -> Epoch2RootContext:
    """Return the canary's native context."""
    return attach_root_context({}, tree_root=canary, daemon_wal_dir=tmp_path / "wal")


def _without(*patterns: str) -> tuple[PathClass, ...]:
    return tuple(row for row in EA_PATH_CLASSES if row.pattern not in patterns)


def _classify_with(classes: tuple[PathClass, ...]) -> Callable[[str], PathClass]:
    def classify(path: str) -> PathClass:
        return classify_path(path, classes=classes)

    return classify


def test_session_document_path_classifies_as_committed_document(
    context: Epoch2RootContext,
) -> None:
    with context.session([MILESTONE]) as session:
        row = context.classify(session.document_path)
    assert (row.pattern, row.policy, row.tier) == (
        ".ea/generations/gen-*/state.json",
        CommitPolicy.COMMITTED,
        StorageTier.DOCUMENT,
    )


@pytest.mark.parametrize("collection", LEDGER_COLLECTIONS, ids=str)
def test_session_ledger_path_classifies_as_committed_ledger(
    context: Epoch2RootContext, collection: Epoch2Collection
) -> None:
    with context.session([MILESTONE]) as session:
        path = session.ledger_path(collection)
        generation_dir = session.document_path.parent
    row = context.classify(path)
    assert path.parent.parent == generation_dir
    assert (row.pattern, row.policy, row.tier) == (
        ".ea/generations/gen-*/ledger/*.jsonl",
        CommitPolicy.COMMITTED,
        StorageTier.LEDGER,
    )


@pytest.mark.parametrize("name", [MILESTONE, DOCUMENT_LOCK_NAME])
def test_lock_path_classifies_as_not_committed(context: Epoch2RootContext, name: str) -> None:
    assert context.classify(context.lock_path(name)).policy is CommitPolicy.NOT_COMMITTED


def test_session_write_leaves_only_declared_files_behind(
    context: Epoch2RootContext, canary: Path
) -> None:
    before = tree_digests(canary)
    with context.session([BATCH, MILESTONE]) as session:
        document = session.read_document()
        document["milestone"] = {"MLS-0001": {"status": "planned"}}
        session.write_document(document)
        document_path = session.document_path
    after = tree_digests(canary)
    written = sorted(locator for locator, digest in after.items() if before.get(locator) != digest)
    expected = sorted(
        path.relative_to(canary).as_posix()
        for path in (
            document_path,
            context.lock_path(MILESTONE),
            context.lock_path(BATCH),
            context.lock_path(DOCUMENT_LOCK_NAME),
        )
    )
    assert written == expected
    assert set(before) <= set(after)
    for locator in written:
        classify_path(f"{CENSUS_SURFACE_PREFIX}{locator}")


def test_declared_path_undeclared_generation_file_rejected(context: Epoch2RootContext) -> None:
    with context.session([MILESTONE]) as session:
        firehose = session.document_path.parent / "store" / "event.jsonl"
        with pytest.raises(UndeclaredPathError):
            context.declared_path(firehose)
    assert not firehose.exists()


def test_declared_path_outside_the_tree_rejected(
    context: Epoch2RootContext, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="is not in the subpath"):
        context.declared_path(tmp_path / "elsewhere" / "state.json")


def test_ledger_path_collection_without_a_ledger_rejected(context: Epoch2RootContext) -> None:
    with context.session([MILESTONE]) as session, pytest.raises(ValueError, match="not ledger"):
        session.ledger_path(Epoch2Collection.WORKSPACE)


def test_session_undeclared_lock_refused_before_it_exists(
    context: Epoch2RootContext, canary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _without(".ea/locks/**", ".ea/**/*.lock")
    monkeypatch.setattr(epoch2_root, "classify_path", _classify_with(table))
    before = tree_digests(canary)
    with pytest.raises(UndeclaredPathError), context.session([MILESTONE]):
        pytest.fail("a session whose lock files are undeclared must not open")
    assert tree_digests(canary) == before
    assert not (canary / "locks").exists()


def test_write_document_undeclared_document_refused_before_writing(
    context: Epoch2RootContext, canary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with context.session([MILESTONE]) as session:
        document_path = session.document_path
        before = document_path.read_bytes()
        table = _without(".ea/generations/gen-*/state.json")
        monkeypatch.setattr(epoch2_root, "classify_path", _classify_with(table))
        with pytest.raises(UndeclaredPathError):
            session.write_document({"rewritten": True})
    assert document_path.read_bytes() == before
