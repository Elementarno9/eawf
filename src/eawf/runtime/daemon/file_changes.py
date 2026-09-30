"""Recording a Run's file edits on its stream, bound to the trees on either side.

Whoever watched the edit -- the dispatch path around a worker's turn, or the host's
tool hooks around one edit call -- hands this module the working tree as it stood
before and after. What changed is read off those two trees rather than off what the
editor claimed to touch, so the record is the tree's own account. The diff is filed in
the Run's bounded, scrubbed content store and the event names it by reference; a diff
the store would cut short is filed as its per-file summary instead and recorded as
``diff_summarized``, so the transcript never shows a cut-off hunk as if it were the
change.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from eawf.kernel.identity import QualifiedUrn, parse_qualified_urn
from eawf.kernel.runtime.content import bound_content
from eawf.kernel.runtime.events import FileChangePayload, RunEventKind
from eawf.kernel.state.epoch2.run import RepositoryScope, Run
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.content_store import file_content
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.run import RunEventAnswer, append_run_event
from eawf.runtime.daemon.native_dispatch import run_ledger, stored_run
from eawf.runtime.daemon.run_events import RunEventAppend
from eawf.runtime.worktree.tree_change import TreeSnapshot, changed_paths, tree_diff

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FileEdit:
    """One observed edit, as the two trees around it.

    Attributes:
        workspace: The working tree the edit happened in.
        before: The tree before the edit.
        after: The tree after it.
        paths: The repository-relative paths the observer saw edited; every
            path of the tree when empty.
    """

    workspace: Path
    before: TreeSnapshot
    after: TreeSnapshot
    paths: Sequence[str] = ()


def _repository_of(session: RootSession, run: Run) -> QualifiedUrn:
    """Return the repository whose files *run* edits.

    Raises:
        DaemonValidationError: The Run's scope names no repository and the tree
            admits no one repository, so the files belong to no one row.
    """
    if isinstance(run.scope, RepositoryScope):
        return run.scope.repository_ref
    rows = document_rows(session.read_document(), Epoch2Collection.REPOSITORY)
    if len(rows) != 1:
        raise DaemonValidationError(
            f"validation_failed: identity_not_found: the tree admits {len(rows)} "
            "repositories, so the edited files belong to no one repository"
        )
    (row,) = rows.values()
    return parse_qualified_urn(row["urn"])


def record_file_edit(
    context: Epoch2RootContext,
    edit: FileEdit,
    *,
    run_ref: QualifiedUrn,
    event_ref: str,
    actor: str,
    now: datetime,
) -> RunEventAnswer | None:
    """Append the edit to the Run's stream, with its diff stored beside it.

    The line takes the stream's tail, because the Run's stream has other writers,
    and a re-delivered observation repeats *event_ref*, which the append answers
    with the line already standing.

    Args:
        context: The tree the Run lives in.
        edit: The trees around the edit.
        run_ref: The Run that made the edit.
        event_ref: The event's identity, derived from the observation.
        actor: The principal the observation is attributed to.
        now: The daemon's recording clock.

    Returns:
        The append's answer; ``None`` when the trees agree on the edited paths,
        so there is nothing to record.

    Raises:
        DaemonValidationError: The edited files belong to no one repository,
            or the append was refused.
    """
    changed = changed_paths(edit.workspace, edit.before, edit.after, paths=edit.paths)
    if not changed:
        return None
    diff = tree_diff(edit.workspace, edit.before, edit.after, paths=changed)
    bounded = bound_content(diff)
    summarized = len(bounded.lines) < bounded.total_lines
    if summarized:
        diff = tree_diff(edit.workspace, edit.before, edit.after, paths=changed, stat_only=True)
    with context.session([run_ref]) as session:
        run = stored_run(session, read_ledger_records(run_ledger(session)), run_ref)
        repository = _repository_of(session, run)
        diff_ref = file_content(session, run_ref=run_ref, text=diff, now=now)
    answer = append_run_event(
        context,
        RunEventAppend(
            urn=run_ref,
            event_ref=event_ref,
            run_sequence=1,
            event_kind=RunEventKind.DIFF_SUMMARIZED if summarized else RunEventKind.FILE_CHANGED,
            provenance="daemon_observed",
            payload=FileChangePayload(
                repository_ref=repository,
                changed_paths=changed,
                before_tree_digest=edit.before.digest,
                after_tree_digest=edit.after.digest,
                diff_ref=diff_ref,
                summary_only=summarized,
            ),
            actor=actor,
        ),
        now=now,
        at_tail=True,
    )
    logger.info(
        f"record_file_edit run={run_ref.entity_key} paths={len(changed)} "
        f"summarized={summarized} "
        f"disposition={answer.disposition}"
    )
    return answer


__all__ = ["FileEdit", "record_file_edit"]
