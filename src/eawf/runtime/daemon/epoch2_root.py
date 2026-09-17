"""The daemon's one door into an epoch-2 tree: a context per native root.

A native read or write reaches a tree's document only through a session
this module opens, and a session is opened in one fixed order. The
authority resolver is asked first, so a tree that is not in epoch 2 is
refused before anything -- a lock file included -- is written into it.
The selection pointer must then name the same generation the epoch
marker does, because the document a session reads is that generation's,
and a tree whose two halves disagree cannot say which one it means. Only
then are the locks taken, and the authority is asked again before every
read and write, because a rollback can remove the marker while a session
is open.

Locks are taken in sorted order of the canonical entity URN. A URN is
parsed and rendered again, without any rung fragment, before it is
sorted: two spellings of one address would otherwise be two locks that
exclude nothing, or would sort apart and let two sessions take the same
pair of locks in opposite orders. The document lock comes last. It is
held for the whole session, so two sessions over disjoint entities still
cannot interleave their read and rewrite of the one document and lose an
update.

A root is identified by a digest of its resolved tree path, never by
anything spelled inside it. Two trees can hold records whose keys, and
even whose full URNs, read the same; what keeps them apart is that each
root's lock files live in its own tree, its WAL records in a directory
named by its root id, and its idempotency keys under that id in a cache
of its own. The WAL namespace is a subdirectory of the daemon's WAL, and
the epoch-1 replay and sweep list only that directory's top level, so a
native record is never replayed into an epoch-1 state file.

Every path a context writes is classified under the commit policy before
it is handed out, so a new file family has to be declared before a
session can write its first byte.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Annotated, Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.identity import QualifiedUrn, parse_qualified_urn
from eawf.kernel.migration.epoch2.errors import MigrationDualAuthorityError
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT, read_selection
from eawf.kernel.state.epoch2.authority import RootAuthority, require_native_authority
from eawf.kernel.store import paths as store_paths
from eawf.kernel.store.commit_policy import CENSUS_SURFACE_PREFIX, PathClass, classify_path
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.lock import portalock, sibling

logger = logging.getLogger(__name__)


#: The shape of a root id: a fixed prefix and a truncated path digest.
ROOT_ID_PATTERN: Final = r"^root-[0-9a-f]{16}$"

#: How many hex characters of the path digest name a root.
ROOT_ID_WIDTH: Final = 16

#: The directory under the daemon's WAL that holds one namespace per root.
NATIVE_WAL_DIRNAME: Final = "native"

#: Where a root's lock files live, relative to its tree root. Outside the
#: generation, so taking a lock never changes the bytes a rollback
#: compares the generation against.
LOCK_LOCATOR: Final = "locks/epoch2"

#: The lock that serialises every read and rewrite of one root's document.
DOCUMENT_LOCK_NAME: Final = "document"


def root_id_for(tree_root: Path) -> str:
    """Return the root id of the tree at ``tree_root``.

    Args:
        tree_root: The tree's root directory. It is resolved first, so a
            symlinked spelling of one tree yields that tree's id.

    Returns:
        ``root-`` plus the first :data:`ROOT_ID_WIDTH` hex characters of
        the resolved path's sha256 digest.
    """
    resolved = Path(tree_root).resolve().as_posix()
    return f"root-{hashlib.sha256(resolved.encode()).hexdigest()[:ROOT_ID_WIDTH]}"


class RootIdentity(BaseModel):
    """Which tree a native context belongs to.

    Attributes:
        root_id: The digest-derived id every namespace is keyed by.
        tree_root: The resolved tree root the id was derived from.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_id: Annotated[str, Field(pattern=ROOT_ID_PATTERN)]
    tree_root: Path

    @classmethod
    def of(cls, tree_root: Path) -> Self:
        """Return the identity of the tree at ``tree_root``.

        Args:
            tree_root: The tree's root directory, in any spelling.

        Returns:
            The identity, carrying the resolved root.
        """
        return cls(root_id=root_id_for(tree_root), tree_root=Path(tree_root).resolve())


def canonical_entity_urn(urn: QualifiedUrn | str) -> str:
    """Return the one spelling of the entity ``urn`` addresses.

    Args:
        urn: A parsed URN or a URN string.

    Returns:
        The canonical URN string of the addressed entity. A rung
        fragment is dropped, because a rung is part of its claim and is
        locked with it.

    Raises:
        IdentityError: The string is not a qualified URN.
    """
    parsed = parse_qualified_urn(urn) if isinstance(urn, str) else urn
    return str(dataclasses.replace(parsed, rung=None))


def entity_lock_order(urns: Iterable[QualifiedUrn | str]) -> tuple[str, ...]:
    """Return the order a session takes its entity locks in.

    Args:
        urns: The entities the session touches, in any order and spelling.

    Returns:
        The distinct canonical entity URNs, sorted.

    Raises:
        ValueError: No entity was named. A session that locks nothing
            would read and write the document unguarded by any entity.
        IdentityError: A string is not a qualified URN.
    """
    order = tuple(sorted({canonical_entity_urn(urn) for urn in urns}))
    if not order:
        raise ValueError("a native session names at least one entity to lock")
    return order


class Epoch2RootContext:
    """The native read and write surface of one epoch-2 tree.

    Attributes:
        identity: The tree this context belongs to.
        wal_dir: The WAL namespace of this root.
        idempotency_cache: Replay results of this root alone, keyed by
            :meth:`idempotency_key`.
    """

    def __init__(self, *, identity: RootIdentity, daemon_wal_dir: Path) -> None:
        """Build the context of ``identity`` without touching the disk.

        Args:
            identity: The tree the context belongs to.
            daemon_wal_dir: The daemon's WAL directory; this root's
                namespace is a subdirectory of it.
        """
        self.identity = identity
        self.wal_dir = daemon_wal_dir / NATIVE_WAL_DIRNAME / identity.root_id
        self.idempotency_cache: dict[str, Any] = {}

    def idempotency_key(self, client_key: str) -> str:
        """Return ``client_key`` inside this root's idempotency namespace.

        Args:
            client_key: The key the client sent.

        Returns:
            ``<root_id>:<client_key>``.

        Raises:
            ValueError: The key is empty, so it names no request.
        """
        if not client_key:
            raise ValueError("an idempotency key is a non-empty string")
        return f"{self.identity.root_id}:{client_key}"

    def classify(self, path: Path) -> PathClass:
        """Return the commit-policy row that governs a path this root writes.

        Args:
            path: A path under this root's tree.

        Returns:
            The governing row.

        Raises:
            ValueError: The path is not under this root's tree.
            UndeclaredPathError: No row declares the path.
        """
        relative = path.relative_to(self.identity.tree_root).as_posix()
        return classify_path(f"{CENSUS_SURFACE_PREFIX}{relative}")

    def declared_path(self, path: Path) -> Path:
        """Return ``path`` once the commit policy declares it.

        Args:
            path: A path under this root's tree that is about to be written.

        Returns:
            The path unchanged.

        Raises:
            ValueError: The path is not under this root's tree.
            UndeclaredPathError: No row declares the path, so nothing
                says whether a clone should carry it.
        """
        self.classify(path)
        return path

    def lock_path(self, name: str) -> Path:
        """Return the lock file one lock name is held through.

        Args:
            name: A canonical entity URN, or :data:`DOCUMENT_LOCK_NAME`.

        Returns:
            The declared lock file under this root's lock directory.

        Raises:
            UndeclaredPathError: The lock directory is not declared.
        """
        return self.declared_path(sibling.lock_path(self._lock_target(name)))

    def _lock_target(self, name: str) -> Path:
        """Return the path whose sibling lock file holds ``name``.

        An entity lock is named by its URN's digest, since a URN holds
        characters a file name cannot, and no digest can spell the
        document lock's name.
        """
        stem = name if name == DOCUMENT_LOCK_NAME else hashlib.sha256(name.encode()).hexdigest()
        return self.identity.tree_root / LOCK_LOCATOR / stem

    def require_selected_generation(self) -> RootAuthority:
        """Return this tree's epoch-2 answer once its select is whole.

        Returns:
            The epoch-2 answer, whose generation the selection pointer
            also names.

        Raises:
            NativeAuthorityRequiredError: The tree resolves to epoch 1.
            MigrationDualAuthorityError: The selection pointer is absent,
                unreadable, or names another generation than the marker.
        """
        authority = require_native_authority(self.identity.tree_root)
        assert authority.target is not None, "an epoch-2 answer always carries its target"
        try:
            selection = read_selection(authority.target)
        except ValueError as error:
            raise MigrationDualAuthorityError(
                f"{self.identity.tree_root.name} carries an unreadable selection pointer, "
                "so no generation can be shown to be the selected one"
            ) from error
        selected = None if selection is None else selection.generation_id
        if selected != authority.generation_id:
            raise MigrationDualAuthorityError(
                f"{self.identity.tree_root.name} is marked for {authority.generation_id} but "
                f"selects {selected}, so a native session cannot say which document it reads"
            )
        return authority

    @contextmanager
    def session(self, urns: Iterable[QualifiedUrn | str]) -> Iterator[RootSession]:
        """Open a session over the selected document with its locks held.

        The call blocks while another session holds a lock it needs, so an
        asyncio caller runs it off the event loop.

        Args:
            urns: The entities the session touches.

        Yields:
            The open session. Its locks are released, last taken first,
            when the block exits.

        Raises:
            NativeAuthorityRequiredError: The tree resolves to epoch 1;
                nothing was written.
            MigrationDualAuthorityError: The select is not whole.
            ValueError: No entity was named.
            IdentityError: An entity URN does not parse.
            LockTimeout: A lock stayed held past the lock timeout.
        """
        authority = self.require_selected_generation()
        order = entity_lock_order(urns)
        with ExitStack() as held:
            for name in (*order, DOCUMENT_LOCK_NAME):
                target = self._lock_target(name)
                self.declared_path(sibling.lock_path(target))
                held.enter_context(portalock.acquire(target))
            session = RootSession(context=self, authority=authority, locked_urns=order)
            logger.debug(
                f"session root={self.identity.root_id} generation={authority.generation_id} "
                f"locks={len(order)}"
            )
            try:
                yield session
            finally:
                session.closed = True


class RootSession:
    """One locked pass over a root's selected generation.

    Attributes:
        authority: The epoch-2 answer the session was opened under.
        locked_urns: The canonical entity URNs held, in acquisition order.
        closed: Whether the session's locks have been released.
    """

    def __init__(
        self,
        *,
        context: Epoch2RootContext,
        authority: RootAuthority,
        locked_urns: tuple[str, ...],
    ) -> None:
        """Bind a session to the locks its context has just taken.

        Args:
            context: The root context that opened the session.
            authority: The epoch-2 answer the session was opened under.
            locked_urns: The canonical entity URNs held.
        """
        self._context = context
        self.authority = authority
        self.locked_urns = locked_urns
        self.closed = False

    @property
    def document_path(self) -> Path:
        """Return the declared path of the selected generation's document."""
        target, generation_id = self.authority.target, self.authority.generation_id
        assert target is not None, "an epoch-2 answer always carries its target"
        assert generation_id is not None, "an epoch-2 answer always names a generation"
        path = target.generation_path(generation_id) / GENERATION_DOCUMENT
        return self._context.declared_path(path)

    def ledger_path(self, collection: Epoch2Collection) -> Path:
        """Return the declared ledger of ``collection`` in the generation.

        Args:
            collection: A collection declared at the ledger tier.

        Returns:
            ``<generation>/ledger/<collection>.jsonl``.

        Raises:
            ValueError: The collection has no ledger.
        """
        return self._context.declared_path(store_paths.ledger_path(self.document_path, collection))

    def read_document(self) -> dict[str, Any]:
        """Return the selected generation's document.

        Returns:
            The decoded document.

        Raises:
            RuntimeError: The session is closed.
            NativeAuthorityRequiredError: The tree left epoch 2.
            MigrationDualAuthorityError: The tree now selects another
                generation.
            FileNotFoundError: The generation carries no document.
        """
        self._require_current()
        return read_document(self.document_path)

    def write_document(self, document: dict[str, Any]) -> None:
        """Replace the selected generation's document.

        Args:
            document: The whole document to leave behind.

        Raises:
            RuntimeError: The session is closed.
            NativeAuthorityRequiredError: The tree left epoch 2; the
                document is untouched.
            MigrationDualAuthorityError: The tree now selects another
                generation; the document is untouched.
            TypeError: The document holds a value JSON cannot encode.
        """
        self._require_current()
        write_document(self.document_path, document)
        logger.info(
            f"write_document root={self._context.identity.root_id} "
            f"generation={self.authority.generation_id}"
        )

    def _require_current(self) -> None:
        """Refuse a closed session, or one whose tree moved under it."""
        if self.closed:
            raise RuntimeError("the native session is closed and its locks are released")
        current = self._context.require_selected_generation()
        if current.generation_id != self.authority.generation_id:
            raise MigrationDualAuthorityError(
                f"the session was opened on {self.authority.generation_id} but the tree now "
                f"selects {current.generation_id}, so its write would land in a stale generation"
            )


def attach_root_context(
    contexts: dict[str, Epoch2RootContext], *, tree_root: Path, daemon_wal_dir: Path | None
) -> Epoch2RootContext:
    """Return the context of the epoch-2 tree at ``tree_root``.

    Args:
        contexts: The daemon's per-root contexts, keyed by root id. A
            context is kept only once its tree is shown to be epoch 2.
        tree_root: The tree's root directory.
        daemon_wal_dir: The daemon's WAL directory.

    Returns:
        The one context of that tree, shared by every spelling of it.

    Raises:
        NativeAuthorityRequiredError: The tree resolves to epoch 1;
            nothing was written and no context was kept.
        MigrationDualAuthorityError: The tree's select is not whole.
        RuntimeError: The daemon has no WAL directory to namespace.
    """
    identity = RootIdentity.of(tree_root)
    context = contexts.get(identity.root_id)
    if context is None:
        if not isinstance(daemon_wal_dir, Path):
            raise RuntimeError("wal_dir not configured on daemon context")
        context = Epoch2RootContext(identity=identity, daemon_wal_dir=daemon_wal_dir)
    context.require_selected_generation()
    contexts[identity.root_id] = context
    logger.debug(f"attach_root_context root={identity.root_id} tree={identity.tree_root.name}")
    return context


__all__ = [
    "DOCUMENT_LOCK_NAME",
    "LOCK_LOCATOR",
    "NATIVE_WAL_DIRNAME",
    "ROOT_ID_PATTERN",
    "ROOT_ID_WIDTH",
    "Epoch2RootContext",
    "RootIdentity",
    "RootSession",
    "attach_root_context",
    "canonical_entity_urn",
    "entity_lock_order",
    "root_id_for",
]
