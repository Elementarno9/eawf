"""Lifecycle hooks: session start and end, and subagent adoption.

A host session's lifecycle events reach eawf only through its hooks. The start and exit
hooks index the session's transcript file in the tree's machine-local transcript index
and, when the session runs on a Task worktree branch, bind it to that Task, and a
harness-spawned subagent is adopted as a Run and its own transcript indexed. A
Run's runtime counters are not forwarded from here: the measurement producer reads them
through the Run's vendor session. No hook blocks the host: every failure degrades to a
non-blocking :class:`~eawf.runtime.hooks.runner.HookResult` that names why.

The index is written by the hook process itself, under the index file's lock, rather
than through the daemon: it is a machine-local pointer file, not project state, and a
host session that runs with no daemon still has a transcript worth finding.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from eawf.runtime.daemon.native_guard import EA_DIRNAME
from eawf.runtime.hooks import runner
from eawf.runtime.hooks.event import HOST_HARNESSES, HookEvent, HookEventType
from eawf.runtime.hooks.runner import DaemonClientFactory, HookResult
from eawf.runtime.session.task_binding import bind_session_to_branch_task
from eawf.runtime.session.transcript_index import record_transcript
from eawf.runtime.vcs.branch_name import branch_task_key, checked_out_branch, main_checkout
from eawf.surfaces.cli.errors import StateConflict

#: The name the session-start hook reports under.
SESSION_INDEX_HOOK: Final = "session.transcript_index"

#: The name the session-exit hook reports under.
SESSION_END_HOOK: Final = "session.end_stamp"

#: What writing an index or binding row can fail with: a held lock, an unwritable
#: file, or a corrupt line already in the file.
_ROW_FAILURES: Final = (StateConflict, OSError, ValidationError)


def _nonempty(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _reason(error: Exception) -> str:
    """Return *error* as the one line a hook answer names it by."""
    lines = str(error).splitlines()
    return f"{type(error).__name__}: {lines[0] if lines else ''}"


def _here(repo_root: Path | None) -> Path:
    return repo_root if repo_root is not None else Path.cwd()


def _tree_root(repo_root: Path | None) -> Path:
    """Return the main checkout's tree, where the index and bindings are kept.

    A session started in a linked worktree finds that worktree's own ``.ea``,
    which the main tree never reads and which is deleted with the worktree.
    """
    return main_checkout(_here(repo_root)) / EA_DIRNAME


def index_host_session(
    event: HookEvent, *, name: str = SESSION_INDEX_HOOK, repo_root: Path | None = None
) -> HookResult:
    """Index the host session's transcript file that a lifecycle event names.

    A session not yet bound to a Task is bound to the one its checked-out
    branch names, when that is a canonical Task worktree branch.

    Args:
        event: The SESSION_START, SESSION_END or AGENT_END event.
        name: The hook name the result reports under.
        repo_root: The repository the harness runs in; the process working
            directory when ``None``.

    Returns:
        A non-blocking :class:`HookResult` saying whether a row was written,
        or why none was, and naming the Task a new binding names or why the
        binding was not written.
    """
    harness = HOST_HARNESSES.get(event.runtime)
    if harness is None:
        return HookResult(name=name, output=f"{name} skipped: {event.runtime} names no transcript")
    payload = runner._session_end_payload(event)
    session_id = _nonempty(payload, "session_id")
    transcript = _nonempty(payload, "transcript_path")
    if session_id is None or transcript is None:
        return HookResult(name=name, output=f"{name} skipped: no session transcript named")
    tree_root = _tree_root(repo_root)
    if not tree_root.is_dir():
        return HookResult(name=name, output=f"{name} skipped: not an eawf tree")
    try:
        row = record_transcript(
            tree_root,
            runtime=harness,
            session_id=session_id,
            transcript_path=transcript,
            now=event.occurred_at,
        )
    except _ROW_FAILURES as error:
        return HookResult(name=name, output=f"{name} skipped: {_reason(error)}")
    output = f"{name} {'ok' if row else 'already indexed'}"
    # The branch is read where the session runs, which the payload names; the
    # tree root is the main checkout's, whose branch is not the session's.
    cwd = _nonempty(payload, "cwd")
    branch = checked_out_branch(Path(cwd) if cwd is not None else _here(repo_root))
    task_key = None if branch is None else branch_task_key(branch)
    if task_key is None:
        return HookResult(name=name, output=output)
    try:
        bound = bind_session_to_branch_task(
            tree_root, session_id=session_id, task_key=task_key, now=event.occurred_at
        )
    except _ROW_FAILURES as error:
        return HookResult(name=name, output=f"{output}; binding skipped: {_reason(error)}")
    return HookResult(name=name, output=f"{output} task={task_key}" if bound else output)


def answer_session_end(event: HookEvent, *, repo_root: Path | None = None) -> HookResult:
    """Answer a SESSION_END or AGENT_END event by indexing the session's transcript.

    Args:
        event: The SESSION_END or AGENT_END hook event.
        repo_root: The repository the harness runs in; the process working
            directory when ``None``.

    Returns:
        A non-blocking :class:`HookResult` saying whether a row was written.
    """
    return index_host_session(event, name=SESSION_END_HOOK, repo_root=repo_root)


#: The adoption verb each subagent event calls.
HOST_SUBAGENT_METHODS: dict[HookEventType, str] = {
    HookEventType.SUBAGENT_START: "runtime.host.subagent.start",
    HookEventType.SUBAGENT_STOP: "runtime.host.subagent.stop",
}


def adopt_host_subagent(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> HookResult:
    """Adopt a harness-spawned subagent as a Run, or name why it was not.

    The start event admits and starts the Run; the stop event bridges the
    subagent's own transcript into it, completes it, and indexes that
    transcript under the spawning session. The daemon writes the
    Run, so every outcome is either that durable row or the daemon's typed
    refusal -- a tree still in epoch 1 answers ``native_authority_required``
    -- and the hook never blocks the host over it.

    Args:
        event: The SUBAGENT_START or SUBAGENT_STOP event.
        daemon_client_factory: Opens the daemon client; the default one when
            ``None``.
        repo_root: The repository the harness runs in; the process working
            directory when ``None``.

    Returns:
        A non-blocking :class:`HookResult` naming the adopted Run, or the
        reason no Run was written, and why the transcript was not indexed
        when it was not.
    """
    name = "runtime.host_subagent"
    harness = HOST_HARNESSES.get(event.runtime)
    if harness is None:
        return HookResult(name=name, output=f"{name} skipped: {event.runtime} spawns no subagent")
    payload = runner._session_end_payload(event)
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        return HookResult(name=name, output=f"{name} skipped: missing agent_id")
    params: dict[str, Any] = {
        "harness": harness,
        "agent_id": agent_id,
        "repo_root": str(repo_root if repo_root is not None else Path.cwd()),
    }
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        params["host_session_id"] = session_id
    transcript = payload.get("agent_transcript_path")
    if (
        event.event_type is HookEventType.SUBAGENT_STOP
        and isinstance(transcript, str)
        and transcript
    ):
        params["transcript_path"] = transcript
    factory = daemon_client_factory or runner._default_daemon_client_factory
    run_ref: str | None = None
    try:
        with factory() as client:
            answer = client.call(HOST_SUBAGENT_METHODS[event.event_type], params)
    except Exception as exc:
        output = repr(exc)
    else:
        run_ref = _nonempty(answer, "run_ref")
        output = f"{name} ok run={run_ref}"
    # A subagent the daemon refused still wrote a transcript, so it is indexed
    # either way, with its Run only when one was adopted.
    tree_root = _tree_root(repo_root)
    if "transcript_path" in params and tree_root.is_dir():
        try:
            record_transcript(
                tree_root,
                runtime=harness,
                session_id=agent_id,
                transcript_path=params["transcript_path"],
                now=event.occurred_at,
                run_ref=run_ref,
                parent_session_id=params.get("host_session_id"),
            )
        except _ROW_FAILURES as error:
            output = f"{output}; index skipped: {_reason(error)}"
    return HookResult(name=name, output=output)


__all__ = [
    "HOST_SUBAGENT_METHODS",
    "SESSION_END_HOOK",
    "SESSION_INDEX_HOOK",
    "adopt_host_subagent",
    "answer_session_end",
    "index_host_session",
]
