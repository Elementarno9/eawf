"""A native session touches the selected document only under epoch 2 and sorted locks.

Every canary is provisioned through the production provisioning path under
``tmp_path``, so the authority resolver reads the same declaration, pointer
and marker a real canary carries. A refused call is checked against the
whole tree afterwards, lock directory included, because "refused" means
nothing was written -- not merely that the handler raised.

The lock-order cases record the lock targets the session hands to the lock
primitive, so the order is asserted as taken rather than as computed.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.identity import IdentityError, QualifiedUrn, parse_qualified_urn
from eawf.kernel.migration.epoch2.canary import CANARY_DECLARATION_FILENAME, DisposableTarget
from eawf.kernel.migration.epoch2.cutover import DOCUMENT_SCHEMA_KEY
from eawf.kernel.migration.epoch2.errors import MigrationDualAuthorityError
from eawf.kernel.migration.epoch2.generation import (
    GENERATION_DOCUMENT,
    atomic_write_json,
    read_marker,
    read_selection,
    tree_digests,
)
from eawf.kernel.migration.epoch2.manifest import MANIFEST_SCHEMA_VERSION
from eawf.kernel.state.epoch2.authority import NativeAuthorityRequiredError
from eawf.platform.install.canary import canary_ref, provision_canary
from eawf.runtime.daemon.epoch2_root import (
    DOCUMENT_LOCK_NAME,
    Epoch2RootContext,
    attach_root_context,
    canonical_entity_urn,
    entity_lock_order,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.lock import portalock, sibling

PROVISIONED_AT = datetime(2026, 1, 1, tzinfo=UTC)
CODE = "ROOTCTX"
MILESTONE = f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/milestone/MLS-0001"
BATCH = f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/batch/BAT-0002"
CLAIM = f"eawf://WSP-{CODE}/PRJ-{CODE}/_/claim/CLM-0001"
OTHER_GENERATION = "gen-0000000000000000"


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


@pytest.fixture
def lock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a contended lock give up quickly instead of after five seconds."""
    monkeypatch.setenv("EA_LOCK_TIMEOUT", "0.2")


def _ctx(wal_dir: Path | None) -> MethodContext:
    return MethodContext(
        started_at="2026-01-01T00:00:00+00:00",
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=wal_dir,
    )


def _production_tree(repo: Path) -> Path:
    tree = repo / ".ea"
    tree.mkdir(parents=True)
    (tree / "state.json").write_text('{"schema_version": "1.20"}\n', encoding="utf-8")
    return tree


def _declared_only(repo: Path) -> Path:
    tree = _production_tree(repo)
    declaration = {"declared_by": "root-test", "disposable": True, "purpose": "throwaway"}
    (tree / CANARY_DECLARATION_FILENAME).write_text(json.dumps(declaration), encoding="utf-8")
    return tree


def _marker_removed(repo: Path) -> Path:
    provision_canary(repo_root=repo, ref=canary_ref(CODE), provisioned_at=PROVISIONED_AT)
    tree = repo / ".ea"
    DisposableTarget.require(tree).marker_path.unlink()
    return tree


def _marker_unreadable(repo: Path) -> Path:
    provision_canary(repo_root=repo, ref=canary_ref(CODE), provisioned_at=PROVISIONED_AT)
    tree = repo / ".ea"
    DisposableTarget.require(tree).marker_path.write_text("{", encoding="utf-8")
    return tree


EPOCH1_TREES = [
    pytest.param(_production_tree, "undeclared", id="production-tree"),
    pytest.param(_declared_only, "marker_absent", id="declared-not-activated"),
    pytest.param(_marker_removed, "marker_absent", id="marker-removed"),
    pytest.param(_marker_unreadable, "marker_unreadable", id="marker-unreadable"),
]


def _select_other(target: DisposableTarget) -> None:
    selection = read_selection(target)
    assert selection is not None
    atomic_write_json(
        target.selection_path, selection.model_copy(update={"generation_id": OTHER_GENERATION})
    )


def _unselect(target: DisposableTarget) -> None:
    target.selection_path.unlink()


def _garble_selection(target: DisposableTarget) -> None:
    target.selection_path.write_text("{", encoding="utf-8")


def _move_generation(target: DisposableTarget) -> None:
    """Point both halves of the select at another generation, as a re-cutover would."""
    marker = read_marker(target)
    assert marker is not None
    _select_other(target)
    atomic_write_json(
        target.marker_path, marker.model_copy(update={"generation_id": OTHER_GENERATION})
    )


@pytest.mark.parametrize(("build", "gap"), EPOCH1_TREES)
def test_attach_root_context_epoch1_tree_refused_with_zero_writes(
    tmp_path: Path, build: Callable[[Path], Path], gap: str
) -> None:
    tree = build(tmp_path / "repo")
    before = tree_digests(tree)
    contexts: dict[str, Epoch2RootContext] = {}
    with pytest.raises(NativeAuthorityRequiredError) as caught:
        attach_root_context(contexts, tree_root=tree, daemon_wal_dir=tmp_path / "wal")
    assert caught.value.gap.value == gap
    assert contexts == {}
    assert tree_digests(tree) == before
    assert not (tree / "locks").exists()
    assert not (tmp_path / "wal").exists()


def test_session_refused_once_the_marker_is_gone_with_zero_writes(
    context: Epoch2RootContext, canary: Path
) -> None:
    DisposableTarget.require(canary).marker_path.unlink()
    before = tree_digests(canary)
    with pytest.raises(NativeAuthorityRequiredError), context.session([MILESTONE]):
        pytest.fail("a session on an epoch-1 tree must not open")
    assert tree_digests(canary) == before
    assert not (canary / "locks").exists()


@pytest.mark.parametrize(
    "break_select",
    [
        pytest.param(_select_other, id="selects-another-generation"),
        pytest.param(_unselect, id="selection-absent"),
        pytest.param(_garble_selection, id="selection-unreadable"),
    ],
)
def test_attach_root_context_incomplete_select_refused(
    canary: Path, tmp_path: Path, break_select: Callable[[DisposableTarget], None]
) -> None:
    break_select(DisposableTarget.require(canary))
    before = tree_digests(canary)
    contexts: dict[str, Epoch2RootContext] = {}
    with pytest.raises(MigrationDualAuthorityError):
        attach_root_context(contexts, tree_root=canary, daemon_wal_dir=tmp_path / "wal")
    assert contexts == {}
    assert tree_digests(canary) == before


def test_attach_root_context_without_wal_dir_rejected(canary: Path) -> None:
    contexts: dict[str, Epoch2RootContext] = {}
    with pytest.raises(RuntimeError, match="wal_dir not configured"):
        attach_root_context(contexts, tree_root=canary, daemon_wal_dir=None)
    assert contexts == {}


def test_session_reads_the_selected_generation_document(
    context: Epoch2RootContext, canary: Path
) -> None:
    target = DisposableTarget.require(canary)
    marker = read_marker(target)
    assert marker is not None
    with context.session([MILESTONE]) as session:
        expected = target.generation_path(marker.generation_id) / GENERATION_DOCUMENT
        assert session.document_path == expected
        assert session.read_document() == {DOCUMENT_SCHEMA_KEY: MANIFEST_SCHEMA_VERSION}


def test_session_write_lands_in_the_selected_generation(context: Epoch2RootContext) -> None:
    with context.session([MILESTONE]) as session:
        document = session.read_document()
        document["milestone"] = {"MLS-0001": {"status": "planned"}}
        session.write_document(document)
    with context.session([MILESTONE]) as session:
        assert session.read_document() == document
        assert json.loads(session.document_path.read_text("utf-8")) == document


def test_write_document_refused_when_the_marker_goes_mid_session(
    context: Epoch2RootContext, canary: Path
) -> None:
    with context.session([MILESTONE]) as session:
        before = session.document_path.read_bytes()
        DisposableTarget.require(canary).marker_path.unlink()
        with pytest.raises(NativeAuthorityRequiredError):
            session.write_document({"rewritten": True})
        with pytest.raises(NativeAuthorityRequiredError):
            session.read_document()
        assert session.document_path.read_bytes() == before


def test_write_document_refused_when_the_generation_moves_mid_session(
    context: Epoch2RootContext, canary: Path
) -> None:
    with context.session([MILESTONE]) as session:
        before = session.document_path.read_bytes()
        _move_generation(DisposableTarget.require(canary))
        with pytest.raises(MigrationDualAuthorityError, match="stale generation"):
            session.write_document({"rewritten": True})
        assert session.document_path.read_bytes() == before


def test_session_takes_entity_locks_in_sorted_urn_order(
    context: Epoch2RootContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    urns = [CLAIM, MILESTONE, BATCH]
    by_urn = sorted(urns)
    # Lock files are named by digest; a session that sorted by file name
    # instead of by URN must fail here, so the two orders have to differ.
    assert sorted(urns, key=lambda urn: hashlib.sha256(urn.encode()).hexdigest()) != by_urn
    assert urns != by_urn
    taken: list[Path] = []
    real_acquire = portalock.acquire

    def recording(target: Path, **kwargs: Any) -> AbstractContextManager[portalock.LockHandle]:
        taken.append(target)
        return real_acquire(target, **kwargs)

    monkeypatch.setattr(portalock, "acquire", recording)
    with context.session(urns) as session:
        assert session.locked_urns == tuple(by_urn)
    expected = [context.lock_path(name) for name in (*by_urn, DOCUMENT_LOCK_NAME)]
    assert [sibling.lock_path(target) for target in taken] == expected


def test_session_takes_one_lock_for_two_spellings_of_one_entity(
    context: Epoch2RootContext,
) -> None:
    with context.session([f"{CLAIM}#rung-01", CLAIM, f"{CLAIM}#rung-1"]) as session:
        assert session.locked_urns == (CLAIM,)


def test_entity_lock_order_is_independent_of_input_order() -> None:
    orders = {
        entity_lock_order(permutation)
        for permutation in itertools.permutations([CLAIM, MILESTONE, BATCH])
    }
    assert orders == {tuple(sorted([CLAIM, MILESTONE, BATCH]))}


def test_entity_lock_order_folds_spellings_to_one_entity() -> None:
    spellings: list[QualifiedUrn | str] = [
        f"{CLAIM}#rung-01",
        f"{CLAIM}#rung-1",
        CLAIM,
        parse_qualified_urn(f"{CLAIM}#rung-4"),
    ]
    assert entity_lock_order(spellings) == (CLAIM,)


def test_canonical_entity_urn_single_spelling_is_unchanged() -> None:
    assert canonical_entity_urn(MILESTONE) == MILESTONE
    assert canonical_entity_urn(parse_qualified_urn(MILESTONE)) == MILESTONE


def test_canonical_entity_urn_drops_a_padded_rung() -> None:
    assert canonical_entity_urn(f"{CLAIM}#rung-004") == CLAIM


def test_entity_lock_order_empty_rejected() -> None:
    with pytest.raises(ValueError, match="at least one entity"):
        entity_lock_order([])


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("urn:eawf:milestone:MLS-0001", id="epoch1-form"),
        pytest.param(f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/milestone/MLS-1", id="short-key"),
        pytest.param(f"eawf://WSP-{CODE}/PRJ-{CODE}/_/milestone/MLS-0001", id="reserved-slot"),
    ],
)
def test_entity_lock_order_malformed_urn_rejected(raw: str) -> None:
    with pytest.raises(IdentityError):
        entity_lock_order([MILESTONE, raw])


def test_session_without_entities_refused_with_zero_writes(
    context: Epoch2RootContext, canary: Path
) -> None:
    before = tree_digests(canary)
    with pytest.raises(ValueError, match="at least one entity"), context.session([]):
        pytest.fail("a session that locks nothing must not open")
    assert tree_digests(canary) == before
    assert not (canary / "locks").exists()


@pytest.mark.usefixtures("lock_timeout")
def test_session_holds_its_entity_lock_until_it_exits(context: Epoch2RootContext) -> None:
    lock_file = context.lock_path(MILESTONE)
    held_target = lock_file.with_name(lock_file.stem)
    with (
        context.session([MILESTONE]),
        pytest.raises(portalock.LockTimeout),
        portalock.acquire(held_target),
    ):
        pytest.fail("the entity lock must be held while the session is open")
    with portalock.acquire(held_target):
        pass


@pytest.mark.usefixtures("lock_timeout")
def test_sessions_over_disjoint_entities_still_take_turns(context: Epoch2RootContext) -> None:
    with (
        context.session([MILESTONE]),
        pytest.raises(portalock.LockTimeout),
        context.session([BATCH]),
    ):
        pytest.fail("a second session must wait for the document lock")
    with context.session([BATCH]) as session:
        assert session.locked_urns == (BATCH,)


def test_session_used_after_exit_refused(context: Epoch2RootContext) -> None:
    with context.session([MILESTONE]) as session:
        document = session.read_document()
    assert session.closed
    with pytest.raises(RuntimeError, match="closed"):
        session.read_document()
    with pytest.raises(RuntimeError, match="closed"):
        session.write_document(document)


def test_idempotency_key_empty_rejected(context: Epoch2RootContext) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        context.idempotency_key("")


def test_method_context_native_root_context_keeps_one_context(canary: Path, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path / "wal")
    context = ctx.native_root_context(canary)
    assert ctx.native_root_context(canary) is context
    assert ctx.native_roots == {context.identity.root_id: context}
    assert context.identity.tree_root == canary
    assert context.wal_dir == tmp_path / "wal" / "native" / context.identity.root_id


def test_method_context_native_root_context_epoch1_refused(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path / "wal")
    tree = _production_tree(tmp_path / "repo")
    with pytest.raises(NativeAuthorityRequiredError):
        ctx.native_root_context(tree)
    assert ctx.native_roots == {}
