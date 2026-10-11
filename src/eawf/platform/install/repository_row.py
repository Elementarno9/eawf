"""Admitting the repository row a freshly initialised tree belongs to.

A born tree holds no repository row, and several daemon paths resolve the
repository a change belongs to by finding exactly one: without it a
dispatched Run's workspace edits are recorded against no repository and are
dropped. ``eawf init`` therefore admits the row the way ``eawf repository
create`` does -- one create request through the native transaction, the
head read from git by the transaction itself -- keyed by the project code,
so the URN is the ``eawf://<CODE>/<CODE>/<CODE>/repository/<CODE>`` every
create document of the tree already names.

A directory that is not a git repository, or one with no commit yet, has
no head a row could record, so it gets no row and the caller is told why.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from eawf.kernel.identity import EntityKind, format_qualified_urn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.state.io import fallback_wal_dir
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon.epoch2_create import (
    CreateRequest,
    _repository_head,
    run_repository_create,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootIdentity
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY

logger = logging.getLogger(__name__)

#: The principal the row's admission is attributed to.
INIT_ACTOR: Final = "EAWF-INIT"

#: The request name the admission is sent under, so a retried init replays it.
INIT_IDEMPOTENCY_KEY: Final = "init-repository-row"


class RepositoryRowOutcome(StrEnum):
    """What init did about the tree's repository row."""

    ADMITTED = "admitted"
    ALREADY_ADMITTED = "already_admitted"
    NOT_A_GIT_REPOSITORY = "not_a_git_repository"
    NO_COMMIT = "no_commit"


@dataclass(frozen=True)
class RepositoryRow:
    """The answer of :func:`admit_tree_repository`.

    Attributes:
        outcome: Whether a row was admitted, already stood, or could not be.
        urn: The repository URN the row is, or would be, keyed by.
    """

    outcome: RepositoryRowOutcome
    urn: str


def tree_repository_urn(project_code: str) -> str:
    """Return the repository URN a tree initialised for *project_code* is keyed by.

    Raises:
        IdentityError: *project_code* is not a symbol key.
    """
    return format_qualified_urn(
        workspace_key=project_code,
        project_key=project_code,
        repository_key=project_code,
        kind=EntityKind.REPOSITORY,
        entity_key=project_code,
    )


def _inside_git_repository(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            **no_window_kwargs(),
        )
    except OSError:
        return False
    return result.returncode == 0


def admit_tree_repository(
    tree_root: Path, *, project_code: str, submit: Callable[[CreateRequest], None]
) -> RepositoryRow:
    """Admit the repository row of the epoch-2 tree at *tree_root*, once.

    Args:
        tree_root: The tree's ``.ea`` directory; its parent is the repository.
        project_code: The code the tree was initialised for, which keys the row.
        submit: Sends the create request to the canonical mutator: the
            daemon, or :func:`admit_in_process` when no daemon serves the tree.

    Returns:
        What happened. A row the tree already holds is left as it stands,
        and nothing is sent for a directory with no git head.

    Raises:
        NativeAuthorityRequiredError: The tree is not an epoch-2 tree.
        IdentityError: *project_code* is not a symbol key.

    *submit* raises its own refusal for a create the mutator refused, and
    that refusal propagates unchanged.
    """
    urn = tree_repository_urn(project_code)
    repo_root = tree_root.parent
    if not _inside_git_repository(repo_root):
        return RepositoryRow(outcome=RepositoryRowOutcome.NOT_A_GIT_REPOSITORY, urn=urn)
    if _repository_head(repo_root) is None:
        return RepositoryRow(outcome=RepositoryRowOutcome.NO_COMMIT, urn=urn)
    authority = require_native_authority(tree_root)
    assert authority.target is not None and authority.generation_id is not None
    document = read_document(
        authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    )
    if project_code in document_rows(document, Epoch2Collection.REPOSITORY):
        return RepositoryRow(outcome=RepositoryRowOutcome.ALREADY_ADMITTED, urn=urn)
    request = CreateRequest.model_validate(
        {
            "urn": urn,
            "expected_revision": document.get(CANONICAL_SEQUENCE_KEY, 0),
            "idempotency_key": INIT_IDEMPOTENCY_KEY,
            "actor": INIT_ACTOR,
            "spec": {"key": project_code},
        }
    )
    submit(request)
    logger.info(f"admit_tree_repository admitted code={project_code}")
    return RepositoryRow(outcome=RepositoryRowOutcome.ADMITTED, urn=urn)


def admit_in_process(tree_root: Path, request: CreateRequest, *, now: datetime) -> None:
    """Commit *request* through the native transaction in this process.

    The daemonless carve-out: the transaction takes the same root locks the
    daemon takes and logs to the tree's repo-local fallback WAL.

    Args:
        tree_root: The tree's ``.ea`` directory.
        request: The repository create request.
        now: When the row is admitted.

    Raises:
        TransactionRefusedError: The transaction refused the create.
    """
    context = Epoch2RootContext(
        identity=RootIdentity.of(tree_root),
        daemon_wal_dir=fallback_wal_dir(tree_root / "state.json"),
    )
    run_repository_create(context=context, request=request, now=now)


__all__ = [
    "INIT_ACTOR",
    "INIT_IDEMPOTENCY_KEY",
    "RepositoryRow",
    "RepositoryRowOutcome",
    "admit_in_process",
    "admit_tree_repository",
    "tree_repository_urn",
]
