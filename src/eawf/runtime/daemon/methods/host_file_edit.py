"""The ``runtime.host.file_edit.*`` verbs: a host harness's file edit, on its Run's stream.

The host edits files through its own tools, which Eawf neither runs nor sees, so the
tool hooks bracket each edit. ``before`` is called as the host is about to run an edit
tool: it binds the call to the one live Run on the host's session, takes the tree the
repository's working files commit to, and keeps it under the tool call's id. ``after``
is called once the tool has run: it takes the tree again and records what changed
between the two on the Run's stream, limited to the path the tool named, because the
daemon and other sessions write to the same working tree while the tool runs. The tree
taken is the work tree holding the file, so an edit in a managed worktree is read off
that worktree rather than off the main checkout, which ignores it.

The pending snapshot is kept in the tree's machine-local directory rather than in
memory, so a daemon restart between the two hooks does not lose the edit. An
``after`` whose ``before`` never arrived records nothing: without the tree before the
edit there is no honest before digest to state. A ``before`` whose ``after`` never
arrives, because the host refused the call after its pre-tool hook, is released once
it is older than :data:`PENDING_LIFETIME_SECONDS`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.file_changes import FileEdit, record_file_edit
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.host_subagent import HARNESS_ACTORS
from eawf.runtime.daemon.methods.permission import host_run
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.runtimes.host_transcript import HostHarness
from eawf.runtime.worktree.tree_change import TreeSnapshot, snapshot_tree

logger = logging.getLogger(__name__)

#: Take the tree before a host edit tool runs.
HOST_FILE_EDIT_BEFORE_METHOD: Final = "runtime.host.file_edit.before"

#: Record what a host edit tool changed.
HOST_FILE_EDIT_AFTER_METHOD: Final = "runtime.host.file_edit.after"

#: Where a tree keeps the snapshots taken before an edit, until its after arrives.
PENDING_LOCATOR: Final = "local/epoch2/file-edits"

#: How long a kept snapshot waits for its after. A host refuses some calls after the
#: pre-tool hook ran, and the after of such a call never arrives.
PENDING_LIFETIME_SECONDS: Final = 86_400

_HostId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


class HostFileEdit(BaseModel):
    """The strict parameters of one host edit call, as either hook reports it.

    Attributes:
        harness: The harness that runs the edit tool.
        host_session_id: The session, or the subagent, the call was made in.
        tool_use_id: The host's own id of the tool call, shared by both hooks.
        tool_name: The edit tool.
        file_path: The file the tool edits, as the host named it.
    """

    model_config = ConfigDict(extra="forbid")

    harness: HostHarness
    host_session_id: _HostId
    tool_use_id: _HostId
    tool_name: Literal["Edit", "Write", "MultiEdit"]
    file_path: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4096)]


class HostFileEditAnswer(BaseModel):
    """What either verb answers with.

    Attributes:
        run_ref: The Run the edit belongs to.
        event_ref: The recorded line, when ``after`` recorded one.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str
    event_ref: str | None = None
    reason: str


class _Pending(BaseModel):
    """The snapshot ``before`` keeps for ``after``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str
    workspace: str
    path: str
    git_tree: str
    digest: str


def _call_digest(args: HostFileEdit) -> str:
    return hashlib.sha256(f"{args.harness}:{args.tool_use_id}".encode()).hexdigest()[:32]


def _pending_path(context: Epoch2RootContext, args: HostFileEdit) -> Path:
    return context.declared_path(
        context.identity.tree_root / PENDING_LOCATOR / f"{_call_digest(args)}.json"
    )


def _edited_file(context: Epoch2RootContext, file_path: str) -> tuple[Path, str]:
    """Return the work tree holding *file_path*, and the path relative to that tree.

    The tree is the innermost one inside the repository's main checkout, so an edit
    in a managed worktree belongs to that worktree.

    Raises:
        DaemonValidationError: The path lies outside the main checkout, so the
            edit is not the repository's to record.
    """
    root = context.identity.tree_root.parent.resolve()
    target = Path(file_path)
    target = (target if target.is_absolute() else root / target).resolve()
    if not target.is_relative_to(root) or target == root:
        raise DaemonValidationError(
            "validation_failed: path_escape: the edited file lies outside the repository"
        )
    workspace = next(
        (tree for tree in target.parents if tree.is_relative_to(root) and (tree / ".git").exists()),
        root,
    )
    return workspace, target.relative_to(workspace).as_posix()


def _release_stale(directory: Path) -> None:
    """Remove the kept snapshots whose after has not come within their lifetime."""
    cutoff = time.time() - PENDING_LIFETIME_SECONDS
    for kept in directory.glob("*.json"):
        try:
            if kept.stat().st_mtime < cutoff:
                kept.unlink()
        except FileNotFoundError:
            # Its after arrived and released it while the sweep ran.
            continue


def _before(
    context: Epoch2RootContext, authority: RootAuthority, args: HostFileEdit
) -> HostFileEditAnswer:
    run = host_run(authority, args.host_session_id)
    workspace, path = _edited_file(context, args.file_path)
    snapshot = snapshot_tree(workspace)
    pending = _Pending(
        run_ref=str(run),
        workspace=str(workspace),
        path=path,
        git_tree=snapshot.git_tree,
        digest=snapshot.digest,
    )
    target = _pending_path(context, args)
    target.parent.mkdir(parents=True, exist_ok=True)
    _release_stale(target.parent)
    target.write_text(pending.model_dump_json())
    return HostFileEditAnswer(run_ref=str(run), reason=f"the tree before {path} is kept")


def _after(
    context: Epoch2RootContext, authority: RootAuthority, args: HostFileEdit
) -> HostFileEditAnswer:
    source = _pending_path(context, args)
    if not source.is_file():
        run = host_run(authority, args.host_session_id)
        return HostFileEditAnswer(
            run_ref=str(run), reason="no tree was taken before the edit, so none is recorded"
        )
    pending = _Pending.model_validate(json.loads(source.read_text()))
    run = parse_qualified_urn(pending.run_ref)
    workspace = Path(pending.workspace)
    answer = record_file_edit(
        context,
        FileEdit(
            workspace=workspace,
            before=TreeSnapshot(git_tree=pending.git_tree, digest=pending.digest),
            after=snapshot_tree(workspace),
            paths=(pending.path,),
        ),
        run_ref=run,
        event_ref=f"EVT-{_call_digest(args)}",
        actor=HARNESS_ACTORS[args.harness],
        now=datetime.now(UTC),
    )
    source.unlink(missing_ok=True)
    if answer is None:
        return HostFileEditAnswer(run_ref=str(run), reason=f"{pending.path} did not change")
    return HostFileEditAnswer(
        run_ref=str(run),
        event_ref=str(answer.event["event_ref"]),
        reason=f"{pending.path} changed and is recorded",
    )


@native_mutator(HOST_FILE_EDIT_BEFORE_METHOD)
async def _before_host_file_edit(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Keep the tree as it stands before a host edit tool runs."""
    args = native_params(HostFileEdit, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_before, context, authority, args)
    return answer.model_dump(mode="json")


@native_mutator(HOST_FILE_EDIT_AFTER_METHOD)
async def _after_host_file_edit(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record what a host edit tool changed on its Run's stream."""
    args = native_params(HostFileEdit, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_after, context, authority, args)
    return answer.model_dump(mode="json")


__all__ = [
    "HOST_FILE_EDIT_AFTER_METHOD",
    "HOST_FILE_EDIT_BEFORE_METHOD",
    "PENDING_LIFETIME_SECONDS",
    "PENDING_LOCATOR",
    "HostFileEdit",
    "HostFileEditAnswer",
]
